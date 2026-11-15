"""P2: where a pull_request_target check run attaches, and whether it blocks.

ADR-0024 §5 puts `class-scope` on `pull_request_target`, which runs the
workflow from the default branch. Read from the docs, not tried: which commit
its check run attaches to, whether a red one blocks a merge, whether a push
or a label re-runs it, whether a later green run on the same head replaces a
red one, and which runs exist for two pull requests with one head. This
script installs the stub, a ruleset requiring `class-scope` from GitHub
Actions, and walks those cases. Nothing is merged.

    python3 p2.py stub      --dry-run   # the stub workflow on main
    python3 p2.py ruleset   --dry-run   # class-scope required at integration 15368
    python3 p2.py pr        --dry-run   # PR 1 into main: expect a red run on its head
    python3 p2.py read      --dry-run   # check runs on the head, mergeStateStatus
    python3 p2.py push      --dry-run   # a second commit: a new run on the new head?
    python3 p2.py green     --dry-run   # label probe-green: a re-run, green on the same head?
    python3 p2.py two-prs   --dry-run   # PR 2, same head, into another branch: which runs?
    python3 p2.py cleanup   --dry-run
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
import probe  # noqa: E402

BRANCH, OTHER = "p2/a", "p2-other"


def ruleset_body() -> dict:
    return {"name": "p2-required-check", "target": "branch", "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}}, "bypass_actors": [],
            "rules": [{"type": "required_status_checks", "parameters": {
                "strict_required_status_checks_policy": True, "do_not_enforce_on_create": False,
                "required_status_checks": [{"context": "class-scope", "integration_id": probe.ACTIONS_APP_ID}]}}]}


def head_of(api, st) -> str:
    res = api.call("GET", f"repos/{probe.REPO}/pulls/{st.get('pr1')}", note="PR 1's head")
    return (res or {}).get("head", {}).get("sha", "<head>") if isinstance(res, dict) else "<head>"


def read(api, st, label: str) -> None:
    sha = head_of(api, st)
    api.call("GET", f"repos/{probe.REPO}/commits/{sha}/check-runs", params={"check_name": "class-scope", "filter": "all"},
             note=f"{label}: class-scope runs on the head (expect app.id 15368 and head_sha equal to the head)")
    api.call("GET", f"repos/{probe.REPO}/actions/runs", params={"event": "pull_request_target", "per_page": 10},
             note="the pull_request_target runs, with their head_sha and pull_requests")
    api.pr_status(st.get("pr1"), note=f"{label}: PR 1 reviewDecision and mergeStateStatus")
    if st.get("pr2"):
        api.pr_status(st.get("pr2"), note=f"{label}: PR 2 (into {OTHER})")


def put_file(api, branch: str, path: str, text: str, msg: str) -> None:
    api.call("PUT", f"repos/{probe.REPO}/contents/{path}", {"message": msg, "branch": branch,
             "content": probe.b64(text)}, note=f"{msg} on {branch}")


def main(argv=None) -> int:
    ap = probe.parser("P2: class-scope's attachment", ["stub", "ruleset", "pr", "read", "push", "green", "two-prs",
                                                       "cleanup"])
    a = ap.parse_args(argv)
    api = probe.Api(a.execute)
    st = probe.load_state(HERE)
    if a.step == "stub":
        put_file(api, "main", ".github/workflows/class-scope.yml", (HERE / "class-scope-stub.yml").read_text(),
                 "p2: the class-scope stub (pull_request_target)")
    elif a.step == "ruleset":
        res = api.call("POST", f"repos/{probe.REPO}/rulesets", ruleset_body(), note="class-scope required, strict")
        if isinstance(res, dict):
            st["ruleset"] = res.get("id")
    elif a.step == "pr":
        base = probe.main_sha(api)
        api.call("POST", f"repos/{probe.REPO}/git/refs", {"ref": f"refs/heads/{BRANCH}", "sha": base}, note="PR 1's branch")
        put_file(api, BRANCH, "p2-a.txt", "p2: first commit\n", "p2: first commit")
        res = api.call("POST", f"repos/{probe.REPO}/pulls", {"title": "p2: PR 1 into main", "head": BRANCH, "base": "main",
                       "body": "P2 probe. Not to be merged."}, note="PR 1: expect one red class-scope run on its head")
        st["pr1"] = probe.number(res, st.get("pr1"))
    elif a.step == "read":
        read(api, st, "read")
    elif a.step == "push":
        meta = api.call("GET", f"repos/{probe.REPO}/contents/p2-a.txt", params={"ref": BRANCH}, note="blob sha")
        api.call("PUT", f"repos/{probe.REPO}/contents/p2-a.txt",
                 {"message": "p2: second commit", "branch": BRANCH, "content": probe.b64("p2: second commit\n"),
                  "sha": (meta or {}).get("sha", "<blob sha>") if isinstance(meta, dict) else "<blob sha>"},
                 note="a push: expect a new red run on the new head (synchronize)")
        read(api, st, "after the push")
    elif a.step == "green":
        api.call("POST", f"repos/{probe.REPO}/issues/{st.get('pr1')}/labels", {"labels": ["probe-green"]},
                 note="a label: expect a re-run on the same head, green, and mergeStateStatus no longer blocked by it")
        read(api, st, "after the label")
    elif a.step == "two-prs":
        base = probe.main_sha(api)
        api.call("POST", f"repos/{probe.REPO}/git/refs", {"ref": f"refs/heads/{OTHER}", "sha": base}, note="another base")
        res = api.call("POST", f"repos/{probe.REPO}/pulls", {"title": f"p2: PR 2, same head, into {OTHER}", "head": BRANCH,
                       "base": OTHER, "body": "P2 probe. Not to be merged."},
                       note="PR 2: expect no class-scope run for it (branches: [main])")
        st["pr2"] = probe.number(res, st.get("pr2"))
        read(api, st, "two pull requests, one head")
    elif a.step == "cleanup":
        if st.get("ruleset"):
            api.call("DELETE", f"repos/{probe.REPO}/rulesets/{st['ruleset']}", note="owner: a loosening")
        for n in (st.get("pr1"), st.get("pr2")):
            if n:
                api.call("PATCH", f"repos/{probe.REPO}/pulls/{n}", {"state": "closed"}, note=f"close #{n}")
        for b in (BRANCH, OTHER):
            api.call("DELETE", f"repos/{probe.REPO}/git/refs/heads/{b}", note=f"delete {b}")
    probe.save_state(HERE, st, api)
    return 0


if __name__ == "__main__":
    sys.exit(main())
