"""In-memory stand-ins for GitHub and the records branch.

The JSON shapes follow the REST description (pull requests, issue events,
workflow runs and jobs, check runs, rulesets); only the fields the code reads
are filled. Every test builds its world by hand, so each rule can be read off
the test.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path

from factory import classfile
from factory.gh import ApiError
from factory.records import RecordExists
from factory.records import dumps as records_dumps

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(__file__).resolve().parent / "data"
SVC = "platform-factory/svc-hello"
CONTROLS = "platform-factory/factory-controls"
HUMAN = "listed-human"
AGENT_BOT = "platform-factory-agent[bot]"
# A well-formed seed key whose hash World puts in the class file, so jobs
# that check the key against key_sha256 accept it (ADV-17).
SEED_KEY = "0123456789abcdef" * 4
# The id World gives the wall ruleset (class-wall). The real one exists once
# class-wall is created on svc-hello, disabled, before the class file's first
# commit; until then the class file holds WALL-RULESET-ID.
WALL_ID = 21000001
RUNG_ID = 20204893


# The class file once its first commit has landed, the template before; the
# tests pass in both states of the repository.
CLASS_SOURCE = classfile.committed_path(ROOT)
PLACEHOLDER_VALUES = {"human": "LISTED-HUMAN-LOGIN", "key_sha256": "SEED-KEY-SHA256",
                      "skill_tag": "SKILL-TAG", "skill_commit": "SKILL-COMMIT", "wall_id": "WALL-RULESET-ID"}


def class_file_text(*, human: str | None = HUMAN, key_sha256: str | None = None, skill_tag: str | None = None,
                    skill_commit: str | None = None, wall_id: int | str | None = None,
                    source: Path | None = None) -> str:
    """The committed class file's text (or the template's) with the given
    values put in place of whatever stands there, placeholder or filled value:
    the listed login, and when given the seed key's hash, the skill pin and
    the wall ruleset's id (in both places it appears). None leaves a value as
    it is. Strings are written double-quoted, as the first-commit pull request
    writes them (an all-digit hash would otherwise read as a number); the id
    bare. Lines are edited inside their top-level block only, so `key_sha256`
    under `seeds.rule` and other `commit:` keys are left alone."""
    q = json.dumps
    out, block = [], None
    for line in (source or CLASS_SOURCE).read_text().splitlines(keepends=True):
        top = re.match(r"^([a-z_]+):", line)
        if top:
            block = top.group(1)
        if block == "human_logins" and human is not None:
            if top:
                out.append(f"human_logins: [{q(human)}]\n")
            elif not line.strip() or line.lstrip().startswith("#"):
                out.append(line)
            continue                                   # a block list's items: replaced above
        if block == "seeds" and key_sha256 is not None and line.startswith("  key_sha256: "):
            line = f"  key_sha256: {q(key_sha256)}\n"
        elif block == "skill" and skill_tag is not None and line.startswith("  tag: "):
            line = f"  tag: {q(skill_tag)}\n"
        elif block == "skill" and skill_commit is not None and line.startswith("  commit: "):
            line = f"  commit: {q(skill_commit)}\n"
        elif block == "rulesets" and wall_id is not None and line.startswith("  wall_ruleset_id: "):
            line = f"  wall_ruleset_id: {wall_id}\n"
        elif block == "rulesets" and wall_id is not None and "name: class-wall" in line and "{id: " in line:
            line = re.sub(r"\{id: [^,]+,", f"{{id: {wall_id},", line)
        out.append(line)
    return "".join(out)


def placeholder_class_file_text() -> str:
    """The class file with its five placeholders, in either state of the repository."""
    return class_file_text(**PLACEHOLDER_VALUES)


def load_cf() -> dict:
    """The committed class file (or the template) with the listed login set to
    HUMAN and the wall ruleset's id to WALL_ID, for tests."""
    return classfile.load(class_file_text(wall_id=WALL_ID))


def load_json(name: str):
    return json.loads((DATA / name).read_text()) if (DATA / name).exists() else json.loads(
        (ROOT / name).read_text())


def l1() -> dict:
    return json.loads((ROOT / "rulesets/svc-hello/l1.json").read_text())


def l2() -> dict:
    return json.loads((ROOT / "rulesets/svc-hello/l2.json").read_text())


def wall() -> dict:
    return json.loads((ROOT / "rulesets/svc-hello/wall.json").read_text())


def as_get(body: dict, updated_at: str = "2026-11-12T10:00:00.000-08:00", rid: int = RUNG_ID) -> dict:
    """A PUT body as GET returns it: GitHub's own fields added, rules reordered."""
    out = copy.deepcopy(body)
    out.update({"id": rid, "source_type": "Repository", "source": SVC,
                "node_id": "RRS_x", "created_at": "2026-08-01T14:58:42.851-07:00",
                "updated_at": updated_at, "current_user_can_bypass": "never",
                "_links": {"self": {"href": "x"}}})
    out["rules"] = list(reversed(out["rules"]))
    return out


def _graphql_query(call: dict) -> bool:
    return call["path"] == "graphql" and str((call.get("body") or {}).get("query", "")).lstrip().startswith("query")


class FakeGitHub:
    """Routes (method, regex) to handlers; records every call."""

    def __init__(self):
        self.routes: list[tuple[str, re.Pattern, object]] = []
        self.calls: list[dict] = []

    def route(self, method: str, pattern: str, handler):
        self.routes.insert(0, (method, re.compile("^" + pattern + "$"), handler))

    def _dispatch(self, method, path, params=None, body=None):
        path = path.lstrip("/")
        self.calls.append({"method": method, "path": path, "params": params or {}, "body": body})
        for m, rx, h in self.routes:
            if m == method:
                mt = rx.match(path)
                if mt:
                    res = h(mt, params or {}, body) if callable(h) else copy.deepcopy(h)
                    if isinstance(res, ApiError):
                        raise res
                    return res
        raise ApiError(404, {"message": f"no route {method} {path}"}, path)

    def writes(self, method=None, prefix=""):
        """Every call that is not a read: not a GET, and not a GraphQL query."""
        return [c for c in self.calls if c["method"] != "GET" and not _graphql_query(c)
                and (method is None or c["method"] == method) and c["path"].startswith(prefix)]

    def get(self, path, /, **params):
        return self._dispatch("GET", path, params)

    def paginate(self, path, /, **params):
        res = self._dispatch("GET", path, params)
        if isinstance(res, dict):
            lists = [v for v in res.values() if isinstance(v, list)]
            return lists[0]
        return res

    def post(self, path, body=None):
        return self._dispatch("POST", path, None, body)

    def put(self, path, body=None):
        return self._dispatch("PUT", path, None, body)

    def patch(self, path, body=None):
        return self._dispatch("PATCH", path, None, body)

    def delete(self, path, body=None):
        return self._dispatch("DELETE", path, None, body)

    def get_raw(self, path, /, **params):
        return self._dispatch("GET", path, params)

    def graphql(self, query, variables, *, accept=None):
        """Like GitHub.graphql: POST graphql, return `data`, raise on `errors`."""
        res = self._dispatch("POST", "graphql", None, {"query": query, "variables": variables})
        if isinstance(res, dict) and res.get("errors"):
            raise ApiError(200, res, "graphql")
        return (res or {}).get("data", {})


class MemoryRecords:
    """The Records interface over a dict."""

    def __init__(self, gh, repo=CONTROLS):
        self.gh = gh
        self.repo = repo
        self.files: dict[str, dict] = {}

    def read(self, path):
        return copy.deepcopy(self.files.get(path))

    def list(self, prefix):
        return sorted(p for p in self.files if p.startswith(prefix))

    def write(self, path, obj, message):
        if path in self.files:
            raise RecordExists(path)
        self.files[path] = json.loads(json.dumps(obj, default=str))

    def write_replace(self, path, obj, message):
        self.files[path] = json.loads(json.dumps(obj, default=str))

    def replace_text(self, path, text, message):
        try:
            self.files[path] = json.loads(text)
        except ValueError:
            self.files[path] = text

    def refresh(self):
        """The dict is always current."""


def ev(kind, at, actor=HUMAN, label=None, app=None, eid=None):
    e = {"id": eid or abs(hash((kind, at, actor, label))) % 10**9, "event": kind, "created_at": at,
         "actor": {"login": actor}, "performed_via_github_app": ({"slug": app} if app else None)}
    if label:
        e["label"] = {"name": label}
    return e


GO_MOD_BASE = """module github.com/platform-factory/svc-hello

go 1.26.0

require github.com/jackc/pgx/v5 v5.11.0

require (
\tgithub.com/jackc/pgpassfile v1.0.0 // indirect
\tgithub.com/jackc/puddle/v2 v2.2.2 // indirect
\tgolang.org/x/sync v0.17.0 // indirect
\tgolang.org/x/text v0.29.0 // indirect
)
"""


def go_mod_with(**versions) -> str:
    text = GO_MOD_BASE
    names = {"sync": "golang.org/x/sync", "text": "golang.org/x/text", "pgx": "github.com/jackc/pgx/v5"}
    for short, v in versions.items():
        mod = names[short]
        text = re.sub(rf"({re.escape(mod)}) v\S+", rf"\1 {v}", text)
    return text


class World:
    """svc-hello and the controls repo, as the routes of one FakeGitHub."""

    def __init__(self, cf=None):
        from factory import seeds as sd
        self.cf = cf or load_cf()
        self.cf["seeds"]["key_sha256"] = sd.key_sha256(SEED_KEY)
        self.gh = FakeGitHub()
        self.records = MemoryRecords(self.gh)
        self.prs: dict[int, dict] = {}
        self.events: dict[int, list] = {}
        self.main_sha = "main0"
        self.files: dict[tuple[str, str], str] = {("main0", "go.mod"): GO_MOD_BASE, ("main0", "go.sum"): "sum0\n"}
        self.parents: dict[str, str] = {}
        self.ruleset = as_get(l1(), "2026-11-12T09:00:00.000-08:00")
        # The wall ruleset, active, as after the L1 switch-on. Nothing writes
        # it: PUT to its path has no route, so a write would show as a 404 call.
        self.wall = as_get(wall(), "2026-11-12T09:00:00.000-08:00", WALL_ID)
        self.extra_rulesets: list[dict] = []
        self.runs: list[dict] = []
        self.jobs: dict[tuple[int, int], list] = {}
        self.check_runs: dict[str, list] = {}
        self.jobs_by_id: dict[int, dict] = {}
        self.behind: dict[str, int] = {}
        self.class_records: list[dict] = []   # {"name","kind","merged_at","pr"}
        self.issues: list[dict] = []
        self.merge_handler = None
        self.reviews: dict[int, list] = {}
        self.pr_commits: dict[int, list] = {}
        self._version = 0
        self._wire()

    # -- builders -----------------------------------------------------------

    def queue_pr(self, n, position, module, version, created_at, *, state="open", merged_at=None,
                 merge_sha=None, head_sha=None, closed_at=None, author=AGENT_BOT, run_id=None):
        self.prs[n] = {"number": n, "state": state, "created_at": created_at, "merged_at": merged_at,
                       "closed_at": closed_at or merged_at, "merge_commit_sha": merge_sha,
                       "head": {"sha": head_sha or f"head{n}", "ref": f"agent/go-module-bump/x-{version}"},
                       "base": {"ref": "main"}, "user": {"login": author}, "labels": []}
        self.records.files[f"runs/{run_id or 1000 + n}-1.json"] = {
            "schema": 1, "class": "go-module-bump", "queue_position": position, "module": module,
            "version": version, "pr_number": n, "head_sha": head_sha or f"head{n}",
            "opened_at": created_at, "controls_run_id": run_id or 1000 + n, "controls_run_attempt": 1,
            "job": "open", "repo": SVC, "branch": f"agent/go-module-bump/x-{version}"}
        evs = self.events.setdefault(n, [])
        if merged_at:
            evs += [ev("merged", merged_at, AGENT_BOT), ev("closed", merged_at, AGENT_BOT)]
        elif state == "closed":
            evs.append(ev("closed", closed_at, HUMAN))
        return self.prs[n]

    def human_pr(self, n, created_at, *, merged_at=None, merge_sha=None, state=None):
        self.prs[n] = {"number": n, "state": state or ("closed" if merged_at else "open"),
                       "created_at": created_at, "merged_at": merged_at, "closed_at": merged_at,
                       "merge_commit_sha": merge_sha, "head": {"sha": f"head{n}", "ref": f"h{n}"},
                       "base": {"ref": "main"}, "user": {"login": "maintainer"}, "labels": []}
        self.events.setdefault(n, [])
        return self.prs[n]

    def push_run(self, run_id, head_sha, attempts):
        """attempts: list of (status, conclusion, completed_at) for the go-test job, attempt 1 first."""
        self.runs.append({"id": run_id, "event": "push", "head_sha": head_sha, "head_branch": "main",
                          "status": attempts[-1][0], "created_at": attempts[0][2] or "2026-11-12T00:00:00Z",
                          "run_attempt": len(attempts), "updated_at": attempts[-1][2]})
        for i, (status, concl, done) in enumerate(attempts, start=1):
            self.jobs[(run_id, i)] = [{"id": run_id * 10 + i, "name": "go-test", "status": status,
                                       "conclusion": concl, "completed_at": done, "run_attempt": i}]

    def set_main(self, sha, go_mod, go_sum="sum\n", parent=None):
        self.main_sha = sha
        self.files[(sha, "go.mod")] = go_mod
        self.files[(sha, "go.sum")] = go_sum
        if parent:
            self.parents[sha] = parent

    def class_scope_job(self, head_sha, job_id, conclusion, steps, status="completed", check_id=None,
                        started="2026-11-12T10:00:00Z"):
        cid = check_id or job_id
        self.check_runs.setdefault(head_sha, []).append(
            {"id": cid, "name": "class-scope", "app": {"id": 15368}, "head_sha": head_sha,
             "status": status, "conclusion": conclusion if status == "completed" else None,
             "started_at": started,
             "details_url": f"https://github.com/{SVC}/actions/runs/{job_id // 10}/job/{job_id}"})
        self.jobs_by_id[job_id] = {"id": job_id, "run_id": job_id // 10, "name": "class-scope",
                                   "status": status, "conclusion": conclusion if status == "completed" else None,
                                   "steps": [{"name": n, "conclusion": c, "status": "completed"} for n, c in steps]}

    def go_test_check(self, head_sha, conclusion, status="completed", cid=None):
        self.check_runs.setdefault(head_sha, []).append(
            {"id": cid or 90000 + len(self.check_runs.get(head_sha, [])), "name": "go-test",
             "app": {"id": 15368}, "head_sha": head_sha, "status": status,
             "conclusion": conclusion if status == "completed" else None, "started_at": "2026-11-12T10:00:00Z"})

    def review(self, n, commit_id, submitted_at="2026-11-12T10:30:00Z", *, actor=HUMAN, state="APPROVED",
               body="minutes: 3; sections: verdict; decision: approve; reason: agrees", rid=None):
        self.reviews.setdefault(n, []).append(
            {"id": rid or 5000 + len(self.reviews.get(n, [])), "user": {"login": actor}, "state": state,
             "commit_id": commit_id, "submitted_at": submitted_at, "body": body})

    def heartbeat(self, completed_at):
        self.records.files["detector/heartbeat.json"] = {"schema": 1, "completed_at": completed_at}

    # -- the records branch over the API --------------------------------------

    def serve_records_branch(self, branch: str = "records"):
        """Serve `self.records.files` as the controls repo's records branch
        through the REST endpoints the real Records client calls, so a test
        can drive a CLI end to end with `factory.records.Records`. The dict
        stays the one store: a value that is not JSON (the scorecard's
        Markdown) is kept as text."""
        g, c = self.gh, re.escape(CONTROLS)
        files = self.records.files

        def text(v) -> str:
            return v if isinstance(v, str) else records_dumps(v)

        def blob_sha(v) -> str:
            data = text(v).encode()
            return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()

        def commit() -> str:
            # A new commit whenever any file changes, as on GitHub.
            return hashlib.sha1(json.dumps({k: text(v) for k, v in files.items()}, sort_keys=True)
                                .encode()).hexdigest()

        def ref(m, p, b):
            return {"ref": f"refs/heads/{branch}", "object": {"type": "commit", "sha": commit()}}

        def tarball(m, p, b):
            """The branch as GitHub's tarball of a commit: one top directory,
            directory entries, a pax header naming the commit."""
            sha = m.group(1)
            if sha != commit():
                return ApiError(404, {"message": "Not Found"}, m.group(0))
            top = f"platform-factory-factory-controls-{sha[:7]}"
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT,
                              pax_headers={"comment": sha}) as tf:
                dirs = {top} | {f"{top}/{d}" for k in files for d in
                                ("/".join(k.split("/")[:i]) for i in range(1, k.count("/") + 1))}
                for d in sorted(dirs):
                    info = tarfile.TarInfo(d)
                    info.type = tarfile.DIRTYPE
                    tf.addfile(info)
                for k in sorted(files):
                    data = text(files[k]).encode()
                    info = tarfile.TarInfo(f"{top}/{k}")
                    info.size = len(data)
                    tf.addfile(info, io.BytesIO(data))
            self.tarballs_served += 1
            return buf.getvalue()

        def get_content(m, p, b):
            path = m.group(1)
            if p.get("ref") != branch or path not in files:
                return ApiError(404, {"message": "Not Found"}, m.group(0))
            return {"path": path, "sha": blob_sha(files[path]),
                    "content": base64.b64encode(text(files[path]).encode()).decode()}

        def put_content(m, p, b):
            path = m.group(1)
            if b.get("branch") != branch:
                if hasattr(self, "branch_files"):
                    self.branch_files[(b["branch"], path)] = base64.b64decode(b["content"]).decode()
                    return {"content": {"path": path}}
                return ApiError(404, {"message": "Branch not found"}, m.group(0))
            if path in files and b.get("sha") is None:
                return ApiError(422, {"message": "Invalid request.\n\n\"sha\" wasn't supplied."}, m.group(0))
            if path in files and b.get("sha") != blob_sha(files[path]):
                return ApiError(409, {"message": f"{path} does not match {b.get('sha')}"}, m.group(0))
            raw = base64.b64decode(b["content"]).decode()
            try:
                files[path] = json.loads(raw)
            except ValueError:
                files[path] = raw
            return {"content": {"path": path, "sha": blob_sha(files[path])}}

        self.tarballs_served = 0
        g.route("GET", rf"repos/{c}/git/ref/heads/{re.escape(branch)}", ref)
        g.route("GET", rf"repos/{c}/tarball/([0-9a-f]+)", tarball)
        # Paths under classes/ are the class records on main, served above.
        g.route("GET", rf"repos/{c}/contents/(?!classes/)(.+)", get_content)
        g.route("PUT", rf"repos/{c}/contents/(?!classes/)(.+)", put_content)
        return self

    # -- routes -------------------------------------------------------------

    def _wire(self):
        g, s, c = self.gh, re.escape(SVC), re.escape(CONTROLS)

        def pulls(m, p, b):
            st = p.get("state", "open")
            return [v for _, v in sorted(self.prs.items()) if st == "all" or v["state"] == st]

        g.route("GET", rf"repos/{s}/pulls", pulls)
        g.route("GET", rf"repos/{s}/pulls/(\d+)", lambda m, p, b: {
            **self.prs[int(m.group(1))], "merged": bool(self.prs[int(m.group(1))].get("merged_at")),
            "merged_by": {"login": AGENT_BOT} if self.prs[int(m.group(1))].get("merged_at") else None})
        g.route("GET", rf"repos/{s}/issues/(\d+)/events", lambda m, p, b: self.events.get(int(m.group(1)), []))
        g.route("GET", rf"repos/{s}/pulls/(\d+)/reviews", lambda m, p, b: self.reviews.get(int(m.group(1)), []))
        g.route("GET", rf"repos/{s}/pulls/(\d+)/commits", lambda m, p, b: self.pr_commits.get(int(m.group(1)), []))
        g.route("GET", rf"repos/{s}/branches/main", lambda m, p, b: {"commit": {"sha": self.main_sha}})

        def contents(m, p, b):
            key = (p.get("ref"), m.group(1))
            if key not in self.files:
                return ApiError(404, {"message": "Not Found"}, m.group(0))
            return {"content": base64.b64encode(self.files[key].encode()).decode()}

        g.route("GET", rf"repos/{s}/contents/(.+)", contents)

        def get_ruleset(m, p, b):
            if self.ruleset is None:
                return ApiError(404, {"message": "Not Found"}, m.group(0))
            return self.ruleset

        def put_ruleset(m, p, b):
            self._version += 1
            self.ruleset = as_get(b, f"2026-11-12T11:{self._version:02d}:00.000-08:00")
            return self.ruleset

        g.route("GET", rf"repos/{s}/rulesets/20204893", get_ruleset)
        g.route("PUT", rf"repos/{s}/rulesets/20204893", put_ruleset)
        def get_wall(m, p, b):
            if self.wall is None:
                return ApiError(404, {"message": "Not Found"}, m.group(0))
            return self.wall

        g.route("GET", rf"repos/{s}/rulesets/{WALL_ID}", get_wall)
        summary = ("id", "name", "target", "source_type", "source", "enforcement", "updated_at")
        g.route("GET", rf"repos/{s}/rulesets", lambda m, p, b: [
            {k: r[k] for k in summary} for r in (self.ruleset, self.wall) if r] + self.extra_rulesets)

        def runs(m, p, b):
            out = [r for r in self.runs if (not p.get("event") or r["event"] == p["event"])
                   and (not p.get("head_sha") or r["head_sha"] == p["head_sha"])]
            return {"total_count": len(out), "workflow_runs": out}

        g.route("GET", rf"repos/{s}/actions/workflows/validate\.yml/runs", runs)
        g.route("GET", rf"repos/{s}/actions/runs/(\d+)/attempts/(\d+)/jobs", lambda m, p, b: {
            "total_count": 1, "jobs": self.jobs.get((int(m.group(1)), int(m.group(2))), [])})

        def latest_jobs(m, p, b):
            rid = int(m.group(1))
            atts = [a for (r, a) in self.jobs if r == rid]
            return {"jobs": self.jobs.get((rid, max(atts)), []) if atts else []}

        g.route("GET", rf"repos/{s}/actions/runs/(\d+)/jobs", latest_jobs)
        g.route("GET", rf"repos/{s}/actions/runs/(\d+)/artifacts", {"total_count": 0, "artifacts": []})
        g.route("GET", rf"repos/{s}/actions/jobs/(\d+)", lambda m, p, b: self.jobs_by_id[int(m.group(1))])
        g.route("GET", rf"repos/{s}/commits/([^/]+)/check-runs", lambda m, p, b: {"check_runs": [
            c for c in self.check_runs.get(m.group(1), []) if c["name"] == p.get("check_name", c["name"])]})
        g.route("GET", rf"repos/{s}/commits/([^/]+)", lambda m, p, b: {
            "sha": m.group(1), "parents": [{"sha": self.parents.get(m.group(1), "unknown-parent")}]})
        g.route("GET", rf"repos/{s}/compare/([^.]+)\.\.\.(.+)", lambda m, p, b: {
            "behind_by": self.behind.get(m.group(2), 0), "ahead_by": 1})
        g.route("POST", rf"repos/{s}/issues/(\d+)/labels", lambda m, p, b: [{"name": x} for x in b["labels"]])
        g.route("POST", rf"repos/{s}/issues/(\d+)/comments", lambda m, p, b: {"id": 1})

        def close(m, p, b):
            pr = self.prs[int(m.group(1))]
            pr["state"] = b.get("state", pr["state"])
            return pr

        g.route("PATCH", rf"repos/{s}/pulls/(\d+)", close)
        g.route("PUT", rf"repos/{s}/pulls/(\d+)/update-branch", {"message": "Updating pull request branch."})

        def merge(m, p, b):
            if self.merge_handler:
                return self.merge_handler(int(m.group(1)), b)
            return {"merged": True, "sha": "merged-sha"}

        g.route("PUT", rf"repos/{s}/pulls/(\d+)/merge", merge)

        def graphql(m, p, b):
            # The merge phase's status read; tests that make other GraphQL
            # calls route their own handler in front of this one.
            if "reviewDecision" not in (b or {}).get("query", ""):
                return ApiError(404, {"message": "no route POST graphql"}, "graphql")
            n = b["variables"]["n"]
            pr = self.prs.get(n) or {}
            head = (pr.get("head") or {}).get("sha")
            approved = any(r["state"] == "APPROVED" and r["commit_id"] == head and r["user"]["login"] == HUMAN
                           for r in self.reviews.get(n, []))
            return {"data": {"repository": {"pullRequest": {
                "headRefOid": head, "reviewDecision": "APPROVED" if approved else "REVIEW_REQUIRED",
                "mergeStateStatus": "CLEAN" if approved or self.rung() == 0 else "BLOCKED"}}}}

        g.route("POST", "graphql", graphql)

        # controls repo: class records on main, and issues
        def rec_listing(m, p, b):
            if not self.class_records:
                return ApiError(404, {"message": "Not Found"}, m.group(0))
            return [{"name": r["name"], "path": f"classes/records/{r['name']}", "type": "file"}
                    for r in self.class_records]

        g.route("GET", rf"repos/{c}/contents/classes/records", rec_listing)

        def rec_content(m, p, b):
            r = next(x for x in self.class_records if x["name"] == m.group(1))
            text = f"schema: 1\nkind: {r['kind']}\nclass: go-module-bump\n"
            return {"content": base64.b64encode(text.encode()).decode()}

        g.route("GET", rf"repos/{c}/contents/classes/records/(.+)", rec_content)
        g.route("GET", rf"repos/{c}/commits", lambda m, p, b: [{"sha": "c-" + p["path"].split("/")[-1]}])

        def rec_pulls(m, p, b):
            name = m.group(1)[2:]
            r = next(x for x in self.class_records if x["name"] == name)
            return [{"number": r.get("pr", 1), "merged_at": r["merged_at"]}] if r.get("merged_at") else []

        g.route("GET", rf"repos/{c}/commits/([^/]+)/pulls", rec_pulls)
        g.route("GET", rf"repos/{c}/issues", lambda m, p, b: [i for i in self.issues if i["state"] == "open"])

        def new_issue(m, p, b):
            i = {"number": len(self.issues) + 1, "state": "open", "title": b["title"], "body": b["body"],
                 "html_url": f"https://github.com/{CONTROLS}/issues/{len(self.issues) + 1}"}
            self.issues.append(i)
            return i

        g.route("POST", rf"repos/{c}/issues", new_issue)

    def rung(self) -> int:
        for r in self.ruleset["rules"]:
            if r["type"] == "pull_request":
                for e in r["parameters"]["required_reviewers"]:
                    if e["file_patterns"] == ["/go.mod", "/go.sum"]:
                        return e["minimum_approvals"]
        return -1

    def set_rung(self, value: int):
        for r in self.ruleset["rules"]:
            if r["type"] == "pull_request":
                for e in r["parameters"]["required_reviewers"]:
                    if e["file_patterns"] == ["/go.mod", "/go.sum"]:
                        e["minimum_approvals"] = value
        self._version += 1
        self.ruleset["updated_at"] = f"2026-11-12T08:{self._version:02d}:00.000-08:00"
