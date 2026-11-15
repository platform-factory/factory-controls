"""P3: approvals, stale dismissal and labels, with the agent App.

ADR-0024 rests on four behaviours read from the docs and not tried: (a) the
listed human's approval on an entry is dismissed when the App pushes a
content change, an empty commit, or updates the branch; (b) with the rung
entry at 0 an unapproved App pull request on go.mod merges; (c) an App's
approval satisfies neither entry; (d) an App's label events name it, in
`performed_via_github_app`. A fifth (e) is added here: which reads the Apps'
installation tokens may make on a public repository without `actions`,
`checks` or `contents` read (apps/README.md in factory-controls).

Steps marked [App] run with the agent App's installation token for m4-probe,
from the workflow `p3-app-acts.yml` in factory-controls (GH_TOKEN is that
token); the others run with the delegate's credential, except the approvals,
which the listed human makes in the browser (human step 6).

No step loosens or deletes a ruleset, and none touches `main`. Each case has
a base branch of its own, as P1's had: `p3-base/l1` under two rulesets with
the rung entry at 1 (cases a, c, d, e), `p3-base/l2` under two rulesets made
with the rung entry at 0 (case b, which is what L2 is), and `p3-base/f` under
none (case f). Two rulesets per base because GitHub refuses two entries for
one team in one ruleset (P1).

    python3 p3.py bases         --dry-run          # the three base branches, from main's tip
                                                   # (every step takes --set NAME: a second set of bases,
                                                   #  rulesets and branches, for a case that has to be run again)
    python3 p3.py ruleset       --dry-run          # l1: rung 1 + wall; l2: rung 0 + wall; stale dismissal on
    python3 p3.py human-prs     --dry-run          # README.md and go.mod pull requests by the delegate
    python3 p3.py a-open        --dry-run          # [App] go.mod pull request, one commit behind its base
    python3 p3.py a-read        --dry-run --pr N   # reads only (the approved pull request is mergeable)
    python3 p3.py a-push-content|a-push-empty|a-update-branch --dry-run --pr N   # [App], then settled reads
    python3 p3.py a-merge       --dry-run --pr N   # [App] a merge attempt on the settled head, no push before it
    python3 p3.py c-approve     --dry-run --pr N --pr2 M   # [App] approves the delegate's two pull requests
    python3 p3.py d-labels      --dry-run --pr N   # [App] applies and removes a label; then read the events
    python3 p3.py e-reads       --dry-run --pr N   # [App] the reads the detector and merge step make
    python3 p3.py b-open-merge  --dry-run          # [App] an unapproved go.mod pull request at rung 0, and its merge
    python3 p3.py f-open        --dry-run          # [App] a go.mod pull request at p3-base/f's tip
    python3 p3.py f-workflow-base --dry-run        # a workflow-file change committed to p3-base/f, so the App's branch is behind
    python3 p3.py f-update-branch --dry-run --pr N # [App] update-branch now that the base holds a workflow change
    python3 p3.py f-remake      --dry-run --pr N   # [App] the merge step's other path: the change made again on the base

The f- steps (review finding M5) ask whether the agent App, which has no
`workflows` permission, may update a branch when its base gained a workflow
file after the branch was made, and whether the merge step's fallback (a
fresh commit of the same go.mod and go.sum on the base's tip, then the branch
moved to it) is accepted.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
import probe  # noqa: E402

COMMIT = """mutation($input: CreateCommitOnBranchInput!) { createCommitOnBranch(input: $input) { commit { oid } } }"""
RUNG = ["/go.mod", "/go.sum"]
WALL = ["*", ".github/**", "!/go.mod", "!/go.sum"]   # P1: these patterns behave as expected
BASE_L1, BASE_L2, BASE_F = "p3-base/l1", "p3-base/l2", "p3-base/f"
# name, base branch, the one entry's patterns, its minimum approvals
RULESETS = (("p3-l1-rung", BASE_L1, RUNG, 1), ("p3-l1-wall", BASE_L1, WALL, 1),
            ("p3-l2-rung", BASE_L2, RUNG, 0), ("p3-l2-wall", BASE_L2, WALL, 1))


def ruleset_body(name: str, base: str, patterns: list[str], minimum: int) -> dict:
    """One required-reviewers entry, with the pull_request parameters of
    factory-controls' rulesets/svc-hello JSON (no required checks: the probe
    repository has neither go-test nor the real class-scope)."""
    team = {"id": probe.TEAM_ID, "type": "Team"}
    return {"name": name, "target": "branch", "enforcement": "active",
            "conditions": {"ref_name": {"include": [f"refs/heads/{base}"], "exclude": []}}, "bypass_actors": [],
            "rules": [{"type": "pull_request", "parameters": {
                "required_approving_review_count": 0, "dismiss_stale_reviews_on_push": True,
                "require_code_owner_review": False, "require_last_push_approval": False,
                "required_review_thread_resolution": False, "allowed_merge_methods": ["squash"],
                "required_reviewers": [{"minimum_approvals": minimum, "file_patterns": patterns,
                                        "reviewer": team}]}}]}


def tip(api, branch: str) -> str:
    res = api.call("GET", f"repos/{probe.REPO}/branches/{branch}", note=f"{branch}'s tip")
    return (res or {}).get("commit", {}).get("sha", f"<{branch}-sha>") if isinstance(res, dict) else f"<{branch}-sha>"


def commit(api, branch: str, head: str, files: dict, msg: str):
    adds = [{"path": p, "contents": probe.b64(t)} for p, t in files.items()]
    return api.graphql(COMMIT, {"input": {"branch": {"repositoryNameWithOwner": probe.REPO, "branchName": branch},
                                          "expectedHeadOid": head, "message": {"headline": msg},
                                          "fileChanges": {"additions": adds}}}, note=f"[App] {msg}")


def pr_head(api, n) -> tuple[str, str]:
    res = api.call("GET", f"repos/{probe.REPO}/pulls/{n}", note=f"#{n}'s head")
    if isinstance(res, dict):
        return res["head"]["sha"], res["head"]["ref"]
    return "<head>", "<branch>"


def settle(api, n, old_head: str | None = None, want_head: str | None = None, tries: int = 20, wait: float = 3.0):
    """Wait until GitHub has taken a push into the pull request: its head is
    the new commit (or no longer the old one) and its mergeability is
    computed. Why: on the first run (2026-11-15) the reads made straight
    after createCommitOnBranch still saw the old head and the old approval,
    and the merge attempt, sent with the old head, got a transient 405
    ("Base branch was modified"), which says nothing about the approval.
    Sends nothing in --dry-run."""
    if not api.execute:
        return
    for _ in range(tries):
        res = api.call("GET", f"repos/{probe.REPO}/pulls/{n}", note="settling: head and mergeable_state")
        if isinstance(res, dict):
            head = res["head"]["sha"]
            moved = (want_head is None or head == want_head) and (old_head is None or head != old_head)
            if moved and res.get("mergeable_state") not in (None, "unknown"):
                return
        time.sleep(wait)


def new_oid(res) -> str | None:
    return ((((res or {}).get("data") or {}).get("createCommitOnBranch") or {}).get("commit") or {}).get("oid") \
        if isinstance(res, dict) else None


def reads(api, n, label: str, merge: bool = True) -> None:
    api.pr_status(n, note=f"{label}: reviewDecision, mergeStateStatus, reviews (is the approval DISMISSED?)")
    api.call("GET", f"repos/{probe.REPO}/pulls/{n}/reviews", note=f"{label}: review states and commit ids")
    if merge:
        head, _ = pr_head(api, n)
        api.call("PUT", f"repos/{probe.REPO}/pulls/{n}/merge", {"sha": head, "merge_method": "squash"},
                 note=f"[App] {label}: the merge attempt (expect 405 unless the listed human's approval is on this head)")


def go_mod_line(text: str | None, marker: str) -> str:
    return (text or "<go.mod at the branch>").rstrip("\n") + f"\n// p3 {marker}\n"


def current_go_mod(api, ref: str) -> str | None:
    meta = api.call("GET", f"repos/{probe.REPO}/contents/go.mod", params={"ref": ref}, note="go.mod at the branch")
    if isinstance(meta, dict):
        import base64
        return base64.b64decode(meta.get("content", "")).decode()
    return None


def main(argv=None) -> int:
    steps = ["bases", "ruleset", "human-prs", "a-open", "a-read", "a-merge", "a-push-content", "a-push-empty",
             "a-update-branch", "c-approve", "d-labels", "e-reads", "b-open-merge",
             "f-open", "f-workflow-base", "f-update-branch", "f-remake"]
    ap = probe.parser("P3: approvals and labels", steps)
    ap.add_argument("--pr", type=int)
    ap.add_argument("--pr2", type=int)
    ap.add_argument("--set", default="", help="a second set of bases, rulesets and branches, named with this suffix")
    a = ap.parse_args(argv)
    # A case that ended (the pull request merged) cannot be run again on the
    # same base: its branch exists and its base has moved. `--set b` runs the
    # same steps on bases, rulesets and branches of their own, all new.
    sfx = f"-{a.set}" if a.set else ""
    base_l1, base_l2, base_f = BASE_L1 + sfx, BASE_L2 + sfx, BASE_F + sfx
    api = probe.Api(a.execute, token=os.environ.get("APP_TOKEN"))
    st = probe.load_state(HERE)
    n = a.pr
    if a.step == "bases":
        base = probe.main_sha(api)
        for b in (base_l1, base_l2, base_f):
            api.call("POST", f"repos/{probe.REPO}/git/refs", {"ref": f"refs/heads/{b}", "sha": base}, note=f"base {b}")
        # Before the rulesets exist: one commit on p3-base/l1 that is not a
        # workflow file, so the App's branch (made from this commit's parent)
        # is one commit behind and update-branch has work to do that needs
        # no `workflows` permission.
        api.call("PUT", f"repos/{probe.REPO}/contents/p3-note.txt", {
            "message": "p3: one commit on the base", "branch": base_l1,
            "content": probe.b64("p3: this commit puts the agent App's branch one commit behind\n")},
            note=f"{base_l1} moves one commit past main's tip")
    elif a.step == "ruleset":
        ids = st.setdefault("rulesets", {})
        for name, base, patterns, minimum in RULESETS:
            name, base = name + sfx, base + sfx
            res = api.call("POST", f"repos/{probe.REPO}/rulesets", ruleset_body(name, base, patterns, minimum),
                           note=f"{name}: one entry at {minimum} on {base}, stale dismissal on (a creation, never an edit)")
            if isinstance(res, dict) and res.get("id"):
                ids[name] = res["id"]
    elif a.step == "human-prs":
        base = tip(api, base_l1)
        for name, path in (("c-readme", "README.md"), ("c-gomod", "go.mod")):
            b = f"p3/{name}{sfx}"
            api.call("POST", f"repos/{probe.REPO}/git/refs", {"ref": f"refs/heads/{b}", "sha": base}, note=b)
            meta = api.call("GET", f"repos/{probe.REPO}/contents/{path}", params={"ref": b}, note="blob")
            import base64
            text = base64.b64decode(meta["content"]).decode() if isinstance(meta, dict) else f"<{path}>"
            api.call("PUT", f"repos/{probe.REPO}/contents/{path}", {
                "message": f"p3: {name}", "branch": b, "content": probe.b64(text.rstrip("\n") + "\n// p3 person\n"),
                "sha": meta.get("sha") if isinstance(meta, dict) else "<blob>"}, note=f"the delegate's change to {path}")
            res = api.call("POST", f"repos/{probe.REPO}/pulls", {"title": f"p3: {name}", "head": b, "base": base_l1,
                           "body": "P3 probe. Not to be merged."}, note=f"a person's pull request on {path}")
            st[name + sfx] = probe.number(res, st.get(name + sfx))
    elif a.step == "a-open":
        res = api.call("GET", f"repos/{probe.REPO}/commits/{base_l1}", note=f"{base_l1} and its parent")
        parent = res["parents"][0]["sha"] if isinstance(res, dict) else "<the base's parent>"
        b = "p3/agent-gomod" + sfx
        api.call("POST", f"repos/{probe.REPO}/git/refs", {"ref": f"refs/heads/{b}", "sha": parent},
                 note="[App] a branch one commit behind its base, so update-branch has work to do later")
        commit(api, b, parent, {"go.mod": go_mod_line(current_go_mod(api, b), "agent 1")}, "p3: agent change to go.mod")
        res = api.call("POST", f"repos/{probe.REPO}/pulls", {"title": "p3: agent pull request on go.mod", "head": b,
                       "base": base_l1, "body": "P3 probe."}, note="[App] the pull request the listed human approves")
        st["agent_pr" + sfx] = probe.number(res, st.get("agent_pr" + sfx))
    elif a.step == "a-read":
        # No merge attempt here: with the approval on the head the pull request is mergeable,
        # and merging it would end case (a) before the pushes.
        reads(api, n, "after the listed human's approval", merge=False)
    elif a.step == "a-merge":
        # A merge attempt on a settled head, with no push before it: the plain
        # answer to "is the merge refused?" for the state the pull request is in.
        settle(api, n)
        reads(api, n, "merge attempt on the settled head")
    elif a.step in ("a-push-content", "a-push-empty"):
        head, b = pr_head(api, n)
        files = {"go.mod": go_mod_line(current_go_mod(api, b), "agent 2")} if a.step == "a-push-content" else {}
        res = commit(api, b, head, files, f"p3: {'a content change' if files else 'an empty commit'}")
        settle(api, n, old_head=head, want_head=new_oid(res))
        reads(api, n, f"after {a.step}")
    elif a.step == "a-update-branch":
        head, _ = pr_head(api, n)
        api.call("PUT", f"repos/{probe.REPO}/pulls/{n}/update-branch", {"expected_head_sha": head},
                 note="[App] update the branch with its base")
        settle(api, n, old_head=head)
        reads(api, n, "after update-branch")
    elif a.step == "c-approve":
        for x in (n, a.pr2):
            head, _ = pr_head(api, x)
            api.call("POST", f"repos/{probe.REPO}/pulls/{x}/reviews", {"event": "APPROVE", "commit_id": head},
                     note=f"[App] approve the delegate's #{x}")
            reads(api, x, f"#{x} after the App's approval", merge=False)
    elif a.step == "d-labels":
        api.call("POST", f"repos/{probe.REPO}/issues/{n}/labels", {"labels": ["probe-label"]}, note="[App] apply")
        api.call("DELETE", f"repos/{probe.REPO}/issues/{n}/labels/probe-label", note="[App] remove")
        api.call("GET", f"repos/{probe.REPO}/issues/{n}/events",
                 note="(any token) actor.login and performed_via_github_app on both events")
    elif a.step == "e-reads":
        head, b = pr_head(api, n)
        for path, params, why in (
                (f"repos/{probe.REPO}/actions/runs", {"per_page": 5}, "actions: read (detector, merge step)"),
                (f"repos/{probe.REPO}/commits/{head}/check-runs", {"filter": "all"}, "checks: read (merge step)"),
                (f"repos/{probe.REPO}/contents/go.mod", {"ref": head}, "contents: read (detector, escape b)"),
                (f"repos/{probe.REPO}/compare/main...{head}", None, "contents: read (merge step)"),
                (f"repos/{probe.REPO}/pulls/{n}/reviews", None, "pull_requests: read"),
                (f"repos/{probe.REPO}/issues/{n}/events", None, "pull_requests: read (labels in force)"),
                (f"repos/{probe.REPO}/rulesets", None, "metadata: read"),
                (f"repos/{probe.REPO}/actions/artifacts", {"per_page": 5}, "actions: read (verdict artifact)")):
            api.call("GET", path, params=params, note=f"[App] {why}: 200 or 403?")
    elif a.step == "b-open-merge":
        base = tip(api, base_l2)
        b = "p3/agent-rung0" + sfx
        api.call("POST", f"repos/{probe.REPO}/git/refs", {"ref": f"refs/heads/{b}", "sha": base}, note="[App] branch")
        res = commit(api, b, base, {"go.mod": go_mod_line(current_go_mod(api, b), "rung 0")}, "p3: unapproved go.mod")
        res = api.call("POST", f"repos/{probe.REPO}/pulls", {"title": "p3: unapproved at rung 0", "head": b,
                       "base": base_l2, "body": "P3 probe."}, note="[App] no approval will be given")
        m = probe.number(res, st.get("rung0_pr"))
        st["rung0_pr"] = m
        settle(api, m)
        reads(api, m, "rung 0, no approval (expect merged)")
    elif a.step == "f-open":
        base = tip(api, base_f)
        b = "p3/agent-behind-workflow" + sfx
        api.call("POST", f"repos/{probe.REPO}/git/refs", {"ref": f"refs/heads/{b}", "sha": base}, note="[App] branch")
        commit(api, b, base, {"go.mod": go_mod_line(current_go_mod(api, b), "behind a workflow")},
               "p3: go.mod change made before the base gains a workflow file")
        res = api.call("POST", f"repos/{probe.REPO}/pulls", {"title": "p3: behind a workflow change", "head": b,
                       "base": base_f, "body": "P3 probe (M5). Not to be merged."}, note="[App] the pull request")
        st["behind_pr"] = probe.number(res, st.get("behind_pr"))
    elif a.step == "f-workflow-base":
        base = tip(api, base_f)
        api.call("PUT", f"repos/{probe.REPO}/contents/.github/workflows/p3-noop.yml", {
            "message": "p3: a workflow file on the base (M5)", "branch": base_f,
            "content": probe.b64("name: p3-noop\non: workflow_dispatch\npermissions: {}\njobs:\n  noop:\n"
                                 "    runs-on: ubuntu-24.04\n    steps:\n      - run: \"true\"\n")},
                 note=f"the delegate's credential (workflow scope); no ruleset holds {base_f}: it now "
                      f"holds a workflow file the App's branch from {base[:12]} lacks")
    elif a.step == "f-update-branch":
        head, _ = pr_head(api, n)
        api.call("PUT", f"repos/{probe.REPO}/pulls/{n}/update-branch", {"expected_head_sha": head},
                 note="[App] update-branch: is it refused for want of `workflows`? Record status and message")
        api.call("GET", f"repos/{probe.REPO}/pulls/{n}", note="the head after the attempt")
    elif a.step == "f-remake":
        head, ref = pr_head(api, n)
        base = tip(api, base_f)
        files = {}
        for f in ("go.mod", "go.sum"):
            meta = api.call("GET", f"repos/{probe.REPO}/contents/{f}", params={"ref": head}, note=f"{f} at the head")
            import base64
            files[f] = base64.b64decode(meta["content"]).decode() if isinstance(meta, dict) else f"<{f} at head>"
        tmp = f"{ref}-remake"
        api.call("POST", f"repos/{probe.REPO}/git/refs", {"ref": f"refs/heads/{tmp}", "sha": base},
                 note="[App] a temporary branch at the base's tip")
        res = commit(api, tmp, base, files, "p3: the change made again on the base (the merge step's remake)")
        oid = (((res or {}).get("data") or {}).get("createCommitOnBranch") or {}).get("commit", {}).get("oid") \
            if isinstance(res, dict) else None
        api.call("PATCH", f"repos/{probe.REPO}/git/refs/heads/{ref}", {"sha": oid or "<new commit>", "force": True},
                 note="[App] move the pull request's branch to the new commit: accepted, or refused for workflows?")
        api.call("DELETE", f"repos/{probe.REPO}/git/refs/heads/{tmp}", note="[App] delete the temporary branch")
        api.call("GET", f"repos/{probe.REPO}/pulls/{n}", note="the pull request's head, still open")
    probe.save_state(HERE, st, api)
    return 0


if __name__ == "__main__":
    sys.exit(main())
