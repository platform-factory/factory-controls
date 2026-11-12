"""The prepare job: decide whether the model runs, and build its workspace.

Why a separate job: the model's job holds one secret and no GitHub
credential (ADR-0024 §7), so everything it needs is fetched here, with no
secret at all, and handed over as an artifact: svc-hello's go.mod, go.sum
and every .go file at main's tip, the queue item, the pinned skill, and the
upstream data the model may quote.

It runs only when the merge job reports that no agent pull request is open.
The model runs only when the next queue item exists and its release is at
least `min_release_age_days` old by the module proxy's `.info` time; a
younger item waits (the readiness note's "The agent's workflow waits rather
than open a younger item").

The skill is fetched at the commit the class file pins, after checking that
the pinned tag points at that commit; a tag whose commit differs is refused
(ADR-0024 §13). svc-hello and the skill are fetched with git over HTTPS with
no credential: both repositories are public.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from . import classfile
from . import state as st
from .gh import GitHub
from .records import Records, branch_from_env

CONTROLS_REPO = "platform-factory/factory-controls"
PROXY = "https://proxy.golang.org"
UPSTREAM_LIMIT = 16 * 1024


class PrepareRefused(Exception):
    pass


# -- the module proxy ------------------------------------------------------------

def escape_module_path(path: str) -> str:
    """The proxy's case encoding: each upper-case letter becomes '!' + lower case."""
    return re.sub(r"[A-Z]", lambda m: "!" + m.group(0).lower(), path)


def http_get(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "factory-controls"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def proxy_info(module: str, version: str, fetch: Callable[[str], bytes] = http_get) -> dict:
    return json.loads(fetch(f"{PROXY}/{escape_module_path(module)}/@v/{version}.info"))


# -- the decision ------------------------------------------------------------------

def decide(state: st.ClassState, cf: dict, now: datetime, info: dict | None) -> dict:
    """{"ready": bool, "reason": str, "item": dict|None, "release_time", "age_days"}. Pure."""
    if state.open_agent_prs:
        return {"ready": False, "reason": "an agent pull request is open", "item": None}
    item = state.next_item
    if item is None:
        return {"ready": False, "reason": "no next queue item", "item": None}
    t = st.parse_time((info or {}).get("Time"))
    if t is None:
        return {"ready": False, "reason": "the release time could not be read from the proxy", "item": item}
    age = now - t
    out = {"item": item, "release_time": st.iso(t), "age_days": round(age.total_seconds() / 86400, 2)}
    if age < timedelta(days=cf["min_release_age_days"]):
        return {**out, "ready": False,
                "reason": f"{item['module']} {item['version']} is younger than {cf['min_release_age_days']} days; waiting"}
    return {**out, "ready": True, "reason": "next item is old enough"}


def gather_light(gh, records, cf: dict) -> dict:
    """The reads derive_state needs for the next item and the open pull
    requests, without the ruleset (this job's token has no use for it)."""
    svc = cf["repo"]
    run_records = st.own_run_records([r for r in (records.read(p) for p in records.list("runs/")) if r], cf)
    fixture_prs = [f["pr_number"] for f in (records.read(p) or {} for p in records.list("fixtures/"))
                   if isinstance(f.get("pr_number"), int)]
    seed_record_prs = [int(m.group(1)) for m in (re.match(r"^seeds/(\d+)\.json$", p)
                                                 for p in records.list("seeds/")) if m]
    prs = {p["number"]: p for p in gh.paginate(f"repos/{svc}/pulls", state="all", base="main")}
    prs = st.read_queue_prs(gh, svc, run_records, fixture_prs, prs)
    qnums = {r.get("pr_number") for r in run_records} - set(fixture_prs)
    close_events = {n: [e for e in gh.paginate(f"repos/{svc}/issues/{n}/events")
                        if e.get("event") in st.CLOSE_EVENTS]
                    for n in sorted(x for x in qnums if isinstance(x, int) and x in prs)}
    main_sha = gh.get(f"repos/{svc}/branches/main")["commit"]["sha"]
    return {"run_records": run_records, "fixture_prs": fixture_prs, "seed_record_prs": seed_record_prs,
            "prs": prs, "close_events": close_events, "main_sha": main_sha,
            "main_go_mod": st.read_file_at(gh, svc, "go.mod", main_sha)}


# -- git, without credentials --------------------------------------------------------

def _git(args: list[str], cwd: str | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "HOME": tempfile.gettempdir()})
    # No credential helper, so an HTTPS fetch is anonymous; a file:// source
    # (local dry runs) may hand out any commit by id.
    base = ["git", "-c", "credential.helper=", "-c", "protocol.file.allow=always",
            "-c", "uploadpack.allowAnySHA1InWant=true"]
    p = subprocess.run(base + args, cwd=cwd, env=env, capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        raise PrepareRefused(f"git {' '.join(args[:2])} failed: {p.stderr.strip()[-400:]}")
    return p.stdout


def fetch_commit(url: str, commit: str, dest: str) -> None:
    _git(["init", "-q", dest])
    _git(["fetch", "-q", "--depth", "1", "--no-tags", url, commit], cwd=dest)
    got = _git(["rev-parse", "FETCH_HEAD^{commit}"], cwd=dest).strip()
    if got != commit:
        raise PrepareRefused(f"fetched {got}, expected {commit}")


def tag_commit(url: str, tag: str) -> str | None:
    """The commit a tag names on the remote (peeled when annotated), or None."""
    out = _git(["ls-remote", url, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"])
    refs = dict(reversed(line.split("\t", 1)) for line in out.splitlines() if "\t" in line)
    return refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")


def module_files(repo_dir: str, commit: str) -> dict[str, bytes]:
    """go.mod, go.sum, every .go file and every nested go.mod at a commit: the
    same set class-scope recomputes from, because `go mod tidy` counts test
    imports and stops at nested module boundaries."""
    listing = _git(["ls-tree", "-r", "-z", "--full-tree", commit], cwd=repo_dir)
    out: dict[str, bytes] = {}
    for entry in listing.split("\0"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        mode, kind, sha = meta.split()
        if kind != "blob":
            continue
        if path in ("go.mod", "go.sum") or path.endswith(".go") or path.endswith("/go.mod"):
            if mode == "120000":
                raise PrepareRefused(f"{path} is a symbolic link")
            out[path] = subprocess.run(["git", "cat-file", "blob", sha], cwd=repo_dir,
                                       capture_output=True, check=True).stdout
    if "go.mod" not in out:
        raise PrepareRefused(f"no go.mod at {commit}")
    return out


def fetch_skill(url: str, tag: str, commit: str, path: str, work: str) -> tuple[bytes, dict]:
    on_remote = tag_commit(url, tag)
    if on_remote is None:
        raise PrepareRefused(f"skill tag {tag} does not exist")
    if on_remote != commit:
        raise PrepareRefused(f"skill tag {tag} names {on_remote}, not the pinned {commit}: refused")
    d = os.path.join(work, "skill-repo")
    fetch_commit(url, commit, d)
    data = subprocess.run(["git", "show", f"{commit}:{path}"], cwd=d, capture_output=True, check=True).stdout
    return data, {"tag": tag, "commit": commit, "content_sha256": hashlib.sha256(data).hexdigest()}


# -- upstream data, quoted -------------------------------------------------------------

def upstream_data(module: str, version: str, fetch: Callable[[str], bytes] = http_get) -> list[dict]:
    """What the model may read about the release, each piece with its source.
    The prompt quotes it as data; nothing here is an instruction."""
    out = []
    base = f"{PROXY}/{escape_module_path(module)}/@v/{version}"
    for suffix, name in ((".info", "proxy .info"), (".mod", "the release's go.mod")):
        try:
            out.append({"source": base + suffix, "name": name,
                        "text": fetch(base + suffix)[:UPSTREAM_LIMIT].decode("utf-8", "replace")})
        except (urllib.error.URLError, OSError) as e:
            out.append({"source": base + suffix, "name": name, "text": f"(not available: {e})"})
    m = re.match(r"^github\.com/([^/]+)/([^/]+)", module)
    if m:
        url = f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{version}/CHANGELOG.md"
        try:
            out.append({"source": url, "name": "CHANGELOG.md at the tag",
                        "text": fetch(url)[:UPSTREAM_LIMIT].decode("utf-8", "replace")})
        except (urllib.error.URLError, OSError):
            pass
    return out


def _quote(text: str) -> str:
    # The closing marker cannot appear inside the quoted text.
    return text.replace("</upstream-data", "<​/upstream-data")


def build_prompt(item: dict, upstream: list[dict], template: str) -> str:
    blocks = "\n\n".join(f'<upstream-data source="{u["source"]}" name="{u["name"]}">\n{_quote(u["text"])}\n'
                         f"</upstream-data>" for u in upstream)
    return (template.replace("{{POSITION}}", str(item["position"]))
            .replace("{{MODULE}}", item["module"]).replace("{{VERSION}}", item["version"])
            .replace("{{UPSTREAM}}", blocks))


# -- the workspace ------------------------------------------------------------------------

def build_workspace(out: str, cf: dict, item: dict, main_sha: str, svc_url: str, skill_url: str,
                    fetch: Callable[[str], bytes] = http_get, root: Path = st.ROOT) -> dict:
    outp = Path(out)
    if outp.exists():
        shutil.rmtree(outp)
    (outp / "svc").mkdir(parents=True)
    (outp / "skill").mkdir()
    with tempfile.TemporaryDirectory(prefix="prepare-") as work:
        svc_dir = os.path.join(work, "svc-repo")
        fetch_commit(svc_url, main_sha, svc_dir)
        for path, data in module_files(svc_dir, main_sha).items():
            dest = outp / "svc" / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        sk = cf["skill"]
        skill_bytes, skill = fetch_skill(skill_url, sk["tag"], sk["commit"], sk["path"], work)
    (outp / "skill" / "SKILL.md").write_bytes(skill_bytes)
    upstream = upstream_data(item["module"], item["version"], fetch)
    template = (root / "agent" / "prompt.md").read_text()
    (outp / "prompt.md").write_text(build_prompt(item, upstream, template))
    (outp / "item.json").write_text(json.dumps(item, sort_keys=True) + "\n")
    (outp / "model.txt").write_text(cf["agent"]["model"] + "\n")
    shutil.copytree(root / "agent", outp / "agent")
    manifest = {}
    for p in sorted(outp.rglob("*")):
        if p.is_file():
            manifest[str(p.relative_to(outp))] = hashlib.sha256(p.read_bytes()).hexdigest()
    (outp / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    # The same list in `sha256sum -c` form, for the model's job, whose image
    # has no JSON reader.
    (outp / "manifest.sha256").write_text("".join(f"{h}  {p}\n" for p, h in manifest.items()))
    return {"skill": skill, "files": len(manifest), "svc_files": sorted(
        str(p.relative_to(outp / "svc")) for p in (outp / "svc").rglob("*") if p.is_file())}


# -- CLI --------------------------------------------------------------------------------------

def _output(pairs: dict, env: dict) -> None:
    path = env.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            for k, v in pairs.items():
                f.write(f"{k}={v}\n")


def main(argv: list[str] | None = None, env: dict | None = None,
         fetch: Callable[[str], bytes] = http_get) -> int:
    env = dict(os.environ if env is None else env)
    ap = argparse.ArgumentParser(prog="python3 -m factory.prepare")
    ap.add_argument("--out", required=True, help="the workspace directory to build")
    ap.add_argument("--svc-git-url", default="https://github.com/platform-factory/svc-hello")
    ap.add_argument("--skill-git-url", default=None)
    ap.add_argument("--now", default=None, help="ISO time to judge release age at (local dry runs)")
    ap.add_argument("--allow-placeholders", action="store_true",
                    help="local dry runs: accept the skill pin from SKILL_TAG/SKILL_COMMIT")
    args = ap.parse_args(argv)
    try:
        # Only an absent class file stops here; the skill pin is checked below.
        cf = classfile.require(env, set(), allow_placeholders=args.allow_placeholders)
    except classfile.NotReady as e:
        return classfile.report_not_ready(e.reason, {"phase": "prepare"}, {"ready": "false"}, env)
    if args.allow_placeholders:
        cf["skill"]["tag"] = env.get("SKILL_TAG", cf["skill"]["tag"])
        cf["skill"]["commit"] = env.get("SKILL_COMMIT", cf["skill"]["commit"])
    out: dict[str, Any] = {"phase": "prepare"}
    unfilled = [p for p in classfile.unfilled(cf) if p.startswith("skill.")]
    if unfilled:
        return classfile.report_not_ready(f"the class file still holds placeholders at {', '.join(unfilled)}",
                                          out, {"ready": "false"}, env)
    token = env.get("CONTROLS_TOKEN")
    gh = GitHub(token)
    records = Records(gh, env.get("CONTROLS_REPO", CONTROLS_REPO), branch_from_env(env))
    snap = gather_light(gh, records, cf)
    state = _derive(snap, cf)
    item = state.next_item
    info = None
    if item and not state.open_agent_prs:
        try:
            info = proxy_info(item["module"], item["version"], fetch)
        except (urllib.error.URLError, OSError, ValueError) as e:
            out["proxy_error"] = str(e)
    now = st.parse_time(args.now) if args.now else st.utcnow()
    d = decide(state, cf, now, info)
    out.update(d, main_sha=snap["main_sha"])
    pairs: dict[str, str] = {"ready": "false"}
    if d["ready"]:
        skill_url = args.skill_git_url or f"https://github.com/{cf['skill']['repo']}"
        try:
            ws = build_workspace(args.out, cf, item, snap["main_sha"], args.svc_git_url, skill_url, fetch)
        except PrepareRefused as e:
            out.update(ready=False, reason=str(e))
            _output(pairs, env)
            print(f"::warning::{e}", file=sys.stderr)
            print(json.dumps(out, indent=2, sort_keys=True, default=str))
            return 0
        out["workspace"] = ws
        prepared = {"item": item, "main_sha": snap["main_sha"], "skill": ws["skill"]}
        pairs = {"ready": "true", "prepared": json.dumps(prepared, sort_keys=True, separators=(",", ":")),
                 "builder_image": cf["agent"]["builder_image"], "model": cf["agent"]["model"],
                 "harness_version": cf["agent"]["harness"]["version"],
                 "harness_platform": cf["agent"]["harness"]["platform"],
                 "harness_sha256": cf["agent"]["harness"]["sha256"],
                 "skill_sha256": ws["skill"]["content_sha256"]}
    _output(pairs, env)
    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    return 0


def _derive(snap: dict, cf: dict) -> st.ClassState:
    # No ruleset is read here, so the rung is not derived (it reads -1); the
    # next item and the open pull requests do not depend on it.
    return st.derive_state(snap, cf, {}, {}, None)


if __name__ == "__main__":
    sys.exit(main())
