"""P1: which required-reviewers entry GitHub applies to which file.

The L1 ruleset rests on two entries for team `platform` in one ruleset: the
rung entry on `/go.mod`, `/go.sum` and the wall entry on `*`, `.github/**`,
`!/go.mod`, `!/go.sum` (ADR-0024 §4). The docs give the pattern syntax two
ways (gitignore on the rules page, fnmatch in the REST description), say
nothing of two entries for one team, and nothing of `*` on dotfiles or nested
paths. This probe reads, for six files, which entry blocks.

To tell the two entries apart, each variant runs in two configurations: W
(wall at 1, rung at 0) blocks a file only if the wall matches it; R (wall at
0, rung at 1) only if the rung matches it. A pull request with no required
approval reads mergeStateStatus CLEAN; one that needs `platform` reads
BLOCKED. Nothing is merged: the reads are reviewDecision, mergeStateStatus,
the review requests, and rule insights.

    python3 p1.py create  --dry-run      # the throwaway repository, platform write, seeded from svc-hello
    python3 p1.py prs     --dry-run --variant anchored --config R   # a base branch, and a pull request per file into it
    python3 p1.py ruleset --dry-run --variant anchored --mode evaluate --config R   # on that base branch only
    python3 p1.py read    --dry-run --variant anchored --config R
    python3 p1.py ruleset --dry-run --variant anchored --mode active --config R     # evaluate -> active, a tightening
    python3 p1.py l1-roundtrip --dry-run       # factory-controls' l1.json, created on its own branch, read back and diffed
    python3 p1.py ratchet-rehearsal --dry-run  # a ruleset created at rung 0; the ratchet's own plan and PUT body
    python3 p1.py cleanup --dry-run      # pull requests closed; the rulesets stay with the repository

Each (variant, config) has its own base branch and ruleset, so no step lowers
or removes anything and the delegate runs them all.

The last two read the controls repo's code and committed rulesets from a
checkout beside this kit (FACTORY_CONTROLS_DIR, default ../factory-controls).
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
import probe  # noqa: E402

CONTROLS_DIR = Path(os.environ.get("FACTORY_CONTROLS_DIR") or HERE.parent.parent / "factory-controls")

FILES = ["go.mod", "x/go.mod", "README.md", ".github/workflows/a.yml", ".gitignore", "sub/dir/f.txt"]
VARIANTS = {
    # ADR-0024 §4 as registered.
    "anchored": {"rung": ["/go.mod", "/go.sum"], "wall": ["*", ".github/**", "!/go.mod", "!/go.sum"]},
    # The gitignore reading of a bare name: matches at any depth, so x/go.mod would leave the wall.
    "unanchored": {"rung": ["go.mod", "go.sum"], "wall": ["*", ".github/**", "!go.mod", "!go.sum"]},
    # The note's fallback if `*` misses nested paths or dotfiles.
    "dotfiles": {"rung": ["/go.mod", "/go.sum"],
                 "wall": ["*", "**/*", ".*", "**/.*", ".github/**", "!/go.mod", "!/go.sum"]},
}
CONFIGS = {"W": {"wall": 1, "rung": 0}, "R": {"wall": 0, "rung": 1}}
# Expected (the readiness note, P1): root go.mod the rung entry only; every other file the wall entry.
EXPECTED = {f: ("rung" if f == "go.mod" else "wall") for f in FILES}
HARMLESS_WORKFLOW = "name: p1-probe\non: workflow_dispatch\npermissions: {}\njobs:\n  noop:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: \"true\"\n"


def slug(path: str) -> str:
    return path.replace("/", "-").replace(".", "dot")


def base_branch(variant: str, config: str) -> str:
    """Each (variant, config) has its own base branch and its own ruleset on
    it, so no ruleset is ever edited from one configuration to another: every
    step is a creation or a tightening (evaluate to active), and the delegate
    can run all of them. Pull requests go into the base, never into main."""
    return f"p1-base/{variant}-{config}"


ENTRIES = ("rung", "wall")


def ruleset_body(variant: str, mode: str, config: str, entry: str) -> dict:
    """One ruleset per entry. GitHub refuses two required-reviewers entries
    for one team in one ruleset (422, "Required reviewers must be unique",
    read on m4-probe 2026-11-12), so the note's fallback applies: two
    rulesets on the same branch, the rung entry in one and the wall entry in
    the other."""
    v, c = VARIANTS[variant], CONFIGS[config]
    team = {"id": probe.TEAM_ID, "type": "Team"}
    return {
        "name": f"p1-{variant}-{config}-{entry}", "target": "branch", "enforcement": mode,
        "conditions": {"ref_name": {"include": [f"refs/heads/{base_branch(variant, config)}"], "exclude": []}},
        "bypass_actors": [],
        "rules": [{"type": "pull_request", "parameters": {
            "required_approving_review_count": 0, "dismiss_stale_reviews_on_push": True,
            "require_code_owner_review": False, "require_last_push_approval": False,
            "required_review_thread_resolution": False, "allowed_merge_methods": ["squash"],
            "required_reviewers": [
                {"minimum_approvals": c[entry], "file_patterns": v[entry], "reviewer": team},
            ]}}],
    }


def one_ruleset_body(variant: str, mode: str, config: str) -> dict:
    """ADR-0024 §4 as registered: both entries in one ruleset. Refused by
    GitHub; kept so the refusal can be shown again."""
    body = ruleset_body(variant, mode, config, "rung")
    body["name"] = f"p1-{variant}-{config}"
    body["rules"][0]["parameters"]["required_reviewers"].append(
        ruleset_body(variant, mode, config, "wall")["rules"][0]["parameters"]["required_reviewers"][0])
    return body


def change_for(path: str, current: str | None) -> str:
    """The new content: a harmless workflow, one appended line, or a new file."""
    if path == ".github/workflows/a.yml":
        return HARMLESS_WORKFLOW
    if current is not None:
        marker = "// p1 probe: a comment line" if path.endswith("go.mod") else "<!-- p1 probe -->"
        return current.rstrip("\n") + "\n" + marker + "\n"
    return f"p1 probe: {path}\n"


def step_create(api: probe.Api, st: dict) -> None:
    api.call("POST", f"orgs/{probe.ORG}/repos", {"name": "m4-probe", "visibility": "public", "auto_init": False,
             "description": "M4 throwaway probe repository (P1-P4). Not a service."}, note="the throwaway repository")
    api.call("PUT", f"orgs/{probe.ORG}/teams/platform/repos/{probe.REPO}", {"permission": "push"},
             note="team platform gets write, so its approvals count")
    api.call("GET", f"orgs/{probe.ORG}/installations", note="confirm the claude App is not installed here")
    print("# then seed it with svc-hello's tree at 312b18d (shell, not the API):")
    print("#   git clone https://github.com/platform-factory/svc-hello.git && cd svc-hello && git checkout 312b18d")
    print("#   git push https://github.com/platform-factory/m4-probe.git 312b18d:refs/heads/main")


def step_prs(api: probe.Api, st: dict, variant: str, config: str) -> None:
    base_sha = probe.main_sha(api)
    base = base_branch(variant, config)
    key = f"{variant}-{config}"
    api.call("POST", f"repos/{probe.REPO}/git/refs", {"ref": f"refs/heads/{base}", "sha": base_sha},
             note=f"the base branch for {key}, at main's tip")
    prs = st.setdefault("prs", {}).setdefault(key, {})
    for f in FILES:
        b = f"p1/{key}/{slug(f)}"
        api.call("POST", f"repos/{probe.REPO}/git/refs", {"ref": f"refs/heads/{b}", "sha": base_sha}, note=f"branch for {f}")
        current, blob = None, None
        if f in ("go.mod", "README.md"):
            meta = api.call("GET", f"repos/{probe.REPO}/contents/{f}", params={"ref": b}, note="its content and blob sha")
            if isinstance(meta, dict):
                import base64
                current, blob = base64.b64decode(meta.get("content", "")).decode(), meta.get("sha")
            else:
                current, blob = f"<{f} at main>", "<blob sha>"
        body = {"message": f"p1: change {f}", "branch": b, "content": probe.b64(change_for(f, current))}
        if blob:
            body["sha"] = blob
        api.call("PUT", f"repos/{probe.REPO}/contents/{f}", body,
                 note=f"one commit changing only {f} (the delegate's token needs the workflow scope for a.yml)")
        res = api.call("POST", f"repos/{probe.REPO}/pulls", {"title": f"p1 {key}: {f}", "head": b, "base": base,
                       "body": "P1 probe. Not to be merged."}, note=f"the pull request for {f} into {base}")
        prs[f] = probe.number(res, prs.get(f))


def step_ruleset(api: probe.Api, st: dict, variant: str, mode: str, config: str) -> None:
    key = f"{variant}-{config}"
    ids = (st.get("rulesets") or {}).get(key) or {}
    was = (st.get("modes") or {}).get(key)
    if ids and not (was == "evaluate" and mode == "active"):
        raise SystemExit(f"{key}: {was} -> {mode} is not a tightening; the delegate does not send it")
    for entry in ENTRIES:
        body = ruleset_body(variant, mode, config, entry)
        if ids.get(entry):
            api.call("PUT", f"repos/{probe.REPO}/rulesets/{ids[entry]}", body,
                     note=f"{key} {entry}: evaluate -> active, the same entry (a tightening)")
        else:
            res = api.call("POST", f"repos/{probe.REPO}/rulesets", body, note=f"{key} {entry}: created in {mode}")
            if isinstance(res, dict) and res.get("id"):
                st.setdefault("rulesets", {}).setdefault(key, {})[entry] = res["id"]
    if not api.execute or len((st.get("rulesets") or {}).get(key) or {}) == len(ENTRIES):
        st.setdefault("modes", {})[key] = mode
    api.call("GET", f"repos/{probe.REPO}/rulesets", note="the rulesets now on the repository")


def step_one_ruleset(api: probe.Api, st: dict, variant: str, mode: str, config: str) -> None:
    api.call("POST", f"repos/{probe.REPO}/rulesets", one_ruleset_body(variant, mode, config),
             note="both entries for one team in one ruleset, as registered: expected 422")


def step_read(api: probe.Api, st: dict, variant: str, config: str) -> None:
    key = f"{variant}-{config}"
    for f in FILES:
        n = ((st.get("prs") or {}).get(key) or {}).get(f)
        api.pr_status(n, note=f"{f} (#{n}) under {key}, {(st.get('modes') or {}).get(key)}")
    api.call("GET", f"repos/{probe.REPO}/rulesets/rule-suites", params={"time_period": "day"},
             note="rule insights (logged only on a merge attempt; in evaluate mode expect few or none)")


def step_cleanup(api: probe.Api, st: dict) -> None:
    """Close P1's pull requests. Its rulesets stay: each is on its own base
    branch, none touches main, and deleting a ruleset is a loosening the
    delegate does not make. They go with the repository at close-out."""
    for key, prs in (st.get("prs") or {"<variant>-<config>": {f: f"<pr for {f}>" for f in FILES}}).items():
        for f, n in prs.items():
            api.call("PATCH", f"repos/{probe.REPO}/pulls/{n}", {"state": "closed"}, note=f"close #{n} ({key}: {f})")


# -- the committed L1 body, and the ratchet's write, tried on m4-probe ----------
#
# Why (review findings M7 and ADV-08): the detector compares every field GitHub
# returns with l1.json, and l1.json carries fields no other probe body uses
# (required status checks, squash only, the dismissal restriction GitHub adds,
# two entries for one team beside status checks). A field GitHub adds,
# reorders or reshapes would be a standing difference that refuses every
# merge, and a correction after the class file's first commit is a
# deviation. And the ratchet's first real PUT is C-18's injection (a): at L1
# it has nothing to raise. Both are tried here, on m4-probe, before the class
# file's first commit.

def controls():
    sys.path.insert(0, str(CONTROLS_DIR))
    from factory import ratchet, ruleset  # noqa: E402  (the controls repo's code, as committed)
    return ratchet, ruleset


ROUNDTRIP_BRANCHES = {"l1": "p1-base/l1-roundtrip", "ratchet": "p1-base/ratchet"}


def adapted(which: str, purpose: str) -> dict:
    """factory-controls' l1.json or l2.json for m4-probe: the same body with
    its name and its target branch changed. The target is a branch of its
    own, not `~DEFAULT_BRANCH`, so the active ruleset never holds m4-probe's
    main (P2 to P4 work there) and never has to be removed. `~DEFAULT_BRANCH`
    itself is already known to round-trip: svc-hello's live ruleset carries
    it. Team id, check contexts and integration 15368 are the same on both
    repositories."""
    body = json.loads((CONTROLS_DIR / "rulesets" / "svc-hello" / f"{which}.json").read_text())
    body = copy.deepcopy(body)
    body["name"] = f"p1-{purpose}"
    body["conditions"] = {"ref_name": {"include": [f"refs/heads/{ROUNDTRIP_BRANCHES[purpose]}"], "exclude": []}}
    return body


def step_l1_roundtrip(api: probe.Api, st: dict) -> None:
    """l1.json (the rung ruleset) and wall.json (the wall ruleset), both
    created active on one branch of their own, each read back and diffed."""
    _, rs = controls()
    for which in ("l1", "wall"):
        body = adapted(which, "l1")
        body["name"] = f"p1-l1-{which}"
        res = api.call("POST", f"repos/{probe.REPO}/rulesets", body,
                       note=f"{which}.json adapted to m4-probe (name and target branch), created active")
        rid = res.get("id") if isinstance(res, dict) else "<new ruleset id>"
        st.setdefault("l1_roundtrip", {})[which] = rid
        got = api.call("GET", f"repos/{probe.REPO}/rulesets/{rid}", note="read back with the owner's token")
        if isinstance(got, dict):
            d = rs.diff(rs.normalize(body), rs.normalize(got))
            print(f"# {which} round trip: " + json.dumps({"diff": d, "empty": not d}, sort_keys=True))
        else:
            print(f"# executed: factory.ruleset.diff(normalize({which} adapted), normalize(GET)) must print []; "
                  "otherwise correct the JSON and its hash before the class file's first commit")


def step_ratchet_rehearsal(api: probe.Api, st: dict) -> None:
    """A new ruleset equal to l2.json (rung entry 0) on its own branch, then
    the ratchet's own plan and PUT body on it. Nothing is lowered: the
    ruleset is created at rung 0 and only raised."""
    ratchet, rs = controls()
    l1, l2 = adapted("l1", "ratchet"), adapted("l2", "ratchet")
    rid = st.get("ratchet_rehearsal")
    if not rid:
        res = api.call("POST", f"repos/{probe.REPO}/rulesets", l2,
                       note="l2.json adapted to m4-probe (name and target branch), created active at rung 0")
        rid = res.get("id") if isinstance(res, dict) else "<new ruleset id>"
        st["ratchet_rehearsal"] = rid
    live = api.call("GET", f"repos/{probe.REPO}/rulesets/{rid}", note="the live body the ratchet plans from")
    if not isinstance(live, dict):
        live = {**copy.deepcopy(l2), "bypass_actors": []}   # dry run: plan from the committed body
    plan = ratchet.plan_raise(live, l1, l2)
    print("# ratchet plan: " + json.dumps({k: plan[k] for k in ("action", "reason", "diff")}))
    if plan["action"] != "write":
        print("# the ratchet would not write: record its reason; this is a finding before the first commit")
        return
    api.call("PUT", f"repos/{probe.REPO}/rulesets/{rid}", plan["body"],
             note="the ratchet's own PUT body (factory.ratchet.plan_raise), sent with the delegate's token: a "
                  "tightening. The ratchet App's own permission is first exercised at C-18")
    after = api.call("GET", f"repos/{probe.REPO}/rulesets/{rid}", note="read back")
    if isinstance(after, dict):
        d = rs.diff(rs.normalize(l1), rs.normalize(after))
        print("# after the PUT, diff against l1: " + json.dumps({"diff": d, "empty": not d}, sort_keys=True))


def main(argv=None) -> int:
    ap = probe.parser("P1: required-reviewers patterns",
                      ["create", "prs", "one-ruleset", "ruleset", "read", "l1-roundtrip", "ratchet-rehearsal", "cleanup"])
    ap.add_argument("--variant", choices=sorted(VARIANTS), default="anchored")
    ap.add_argument("--mode", choices=["evaluate", "active"], default="evaluate")
    ap.add_argument("--config", choices=sorted(CONFIGS), default="R")
    a = ap.parse_args(argv)
    api = probe.Api(a.execute)
    st = probe.load_state(HERE)
    {"create": lambda: step_create(api, st), "prs": lambda: step_prs(api, st, a.variant, a.config),
     "ruleset": lambda: step_ruleset(api, st, a.variant, a.mode, a.config),
     "one-ruleset": lambda: step_one_ruleset(api, st, a.variant, a.mode, a.config),
     "read": lambda: step_read(api, st, a.variant, a.config), "cleanup": lambda: step_cleanup(api, st),
     "l1-roundtrip": lambda: step_l1_roundtrip(api, st),
     "ratchet-rehearsal": lambda: step_ratchet_rehearsal(api, st)}[a.step]()
    probe.save_state(HERE, st, api)
    return 0


if __name__ == "__main__":
    sys.exit(main())
