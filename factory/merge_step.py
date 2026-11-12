"""The merge step: the first two jobs of every agent-workflow run.

Why two phases in two jobs: the checks before a merge need the ratchet App's
token (to demote first if an escape is unhandled) and the merge needs the
agent App's token. Each token sits in its own environment, so each phase is
its own job (ADR-0024 §7).

`--phase pre-merge` (environment `ratchet`) runs the detector's checks
without writing the heartbeat, demotes first on any unhandled event, and
then refuses (ok=false) while main's latest push run of go-test has not
succeeded, while a ruleset difference stands, or while the last complete
detector run is older than detector_stale_minutes. A demotion alone does not
refuse: at L1 the merge API itself refuses an unapproved pull request, and
C-18 records that refusal.

`--phase merge` (environment `agent-app`) acts on the one open agent pull
request. It never merges or closes a seed, so it refuses to act without
SEED_KEY. A queue-item pull request left behind main is updated. One whose
class-scope or go-test has concluded fail is closed (a near-miss); fail is
told from inconclusive by the class-scope job's step names, never by a label.
Otherwise, when both checks pass on the head, it calls the merge API with the
head commit class-scope verified, squash, and a fixed title and message, and
records GitHub's answer: a 405 is a recorded refusal, not an error. The
ruleset decides.

Seeds must look like any other item until the reviewer has decided
(ADR-0024 §11: "its logs print nothing that differs"), and this phase's
output is printed to a public log and written to the records branch. So a
seed takes the same path as a real item (it is read, compared with main and
updated the same way) up to the point where the two would differ, and that
point is put after the reviewer's decision: while a seed can be open (the
rung is not L2 and no more than the last seed position's count of items has
merged), no pull request is sent to the merge API without the listed human's
approval on its head; it waits, seed or not. Once the listed human has
decided on a seed, or a check failed on it, the phase reports `wait-seed`
and the settlement step (factory/open_pr.py) labels and closes it.

Both phases print one JSON object, write it to the records branch, and exit
non-zero only on their own failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from typing import Any

from . import classfile
from . import detector
from . import open_pr
from . import seeds as sd
from . import state as st
from .gh import ApiError
from .records import RecordExists

MERGE_REFUSALS = (405, 409, 422)

TITLE = "go-module-bump: {module} {version} (queue item {position})"
MESSAGE = (
    "Merged by the factory-controls merge step.\n"
    "\n"
    "Pull request: #{pr}\n"
    "Head commit verified by class-scope: {sha}\n"
    "class-scope check run: {check_run_id}\n"
    "Controls run: {run_id}, attempt {run_attempt}\n"
)


# -- pure decisions ----------------------------------------------------------

def main_go_test_status(latest_run: dict | None, jobs: list[dict], main_sha: str | None,
                        job_name: str) -> dict:
    """ok iff main's latest push run on its tip has a successful go-test job
    in its latest attempt (a re-run that passes unblocks merges; it never
    clears an escape, which the detector reads from attempt 1)."""
    if latest_run is None:
        return {"ok": False, "reason": "no push run of go-test for main's tip", "main_sha": main_sha}
    jobs = [j for j in jobs if j.get("name") == job_name]
    if not jobs:
        return {"ok": False, "reason": "main's latest push run has no go-test job",
                "run_id": latest_run.get("id")}
    j = max(jobs, key=lambda x: (x.get("run_attempt", 1), x.get("id", 0)))
    ok = j.get("status") == "completed" and j.get("conclusion") == "success"
    return {"ok": ok, "reason": None if ok else f"go-test {j.get('status')}/{j.get('conclusion')}",
            "run_id": latest_run.get("id"), "job_id": j.get("id"),
            "run_attempt": j.get("run_attempt")}


def pre_merge_decision(det: dict, go_test: dict, heartbeat: dict | None, now, stale_minutes: int) -> dict:
    reasons = []
    if not det["ruleset"]["ok"]:
        reasons.append("a ruleset difference stands")
    if not go_test["ok"]:
        reasons.append(f"main's latest push run of go-test has not succeeded ({go_test.get('reason')})")
    hb_time = st.parse_time((heartbeat or {}).get("completed_at"))
    if hb_time is None:
        reasons.append("no complete detector run recorded")
    elif now - hb_time > timedelta(minutes=stale_minutes):
        reasons.append(f"the last complete detector run ({st.iso(hb_time)}) is older than {stale_minutes} minutes")
    return {"ok": not reasons, "reasons": reasons}


SEED_SETTLE = "the reviewer's decision is recorded or a check failed: the settlement step closes it"


def decide_merge(open_agent_prs: list[dict], seeds: list[int], seed_key_present: bool,
                 behind_by: int | None, cs: dict | None, gt: dict | None, *,
                 review_hold: bool = False, approved: bool = False, seed_decided: bool = False) -> dict:
    """What the merge phase does with the open agent pull request. Pure.

    `review_hold`: a seed can be open (see the module docstring), so nothing
    goes to the merge API without `approved` (the listed human's approval on
    the head). `seed_decided`: the open pull request is a seed and the listed
    human has decided on it.
    """
    if not open_agent_prs:
        return {"action": "none", "reason": "no open agent pull request"}
    if len(open_agent_prs) > 1:
        return {"action": "refuse", "reason": f"{len(open_agent_prs)} open agent pull requests; concurrency is 1"}
    pr = open_agent_prs[0]
    if not pr.get("queue_item"):
        return {"action": "skip", "reason": "the open agent pull request is not a queue item"}
    if not seed_key_present:
        return {"action": "refuse", "reason": "SEED_KEY absent: a seed cannot be told apart"}
    is_seed = pr["number"] in seeds
    if is_seed and seed_decided:
        return {"action": "wait-seed", "reason": SEED_SETTLE}
    if behind_by is None:
        return {"action": "wait", "reason": "could not compare the head with main"}
    if behind_by > 0:
        return {"action": "update-branch", "reason": f"head is {behind_by} commit(s) behind main"}
    cs_v, gt_v = (cs or {}).get("verdict"), (gt or {}).get("verdict")
    if cs_v == "fail" or gt_v == "fail":
        if is_seed:
            # Never closed here: a failed check voids the seed, and the
            # settlement step records that before it closes the pull request.
            return {"action": "wait-seed", "reason": SEED_SETTLE}
        which = [n for n, v in (("class-scope", cs_v), ("go-test", gt_v)) if v == "fail"]
        return {"action": "close", "reason": f"{' and '.join(which)} concluded fail", "failed": which}
    if cs_v == "pass" and gt_v == "pass":
        if (cs or {}).get("head_sha") != pr.get("head_sha"):
            return {"action": "wait", "reason": "class-scope's verdict is not on the current head"}
        if review_hold and not approved:
            return {"action": "wait", "reason": "awaiting the listed human's approval on the head"}
        if is_seed:
            return {"action": "wait-seed", "reason": "a seed is never merged"}
        return {"action": "merge", "reason": "class-scope and go-test passed on the head"}
    return {"action": "wait", "reason": f"class-scope {cs_v}, go-test {gt_v}"}


# -- I/O ---------------------------------------------------------------------

def _record(ctx: detector.Context, phase: str, out: dict) -> None:
    path = f"merge-step/{ctx.run_id or 'local'}-{ctx.run_attempt or '0'}-{phase}.json"
    try:
        ctx.records.write(path, out, f"merge step: {phase}")
    except RecordExists:
        pass


def _github_output(pairs: dict, env: dict) -> None:
    path = env.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a") as f:
        for k, v in pairs.items():
            f.write(f"{k}={v}\n")


def pre_merge(ctx: detector.Context) -> dict:
    det = detector.run(ctx, write_heartbeat=False)
    cf, svc = ctx.cf, ctx.cf["repo"]
    wf, job = cf["checks"]["go_test"]["workflow"], cf["checks"]["go_test"]["job"]
    main_sha = det.get("main_sha")
    runs = ctx.gh_svc.paginate(f"repos/{svc}/actions/workflows/{wf}/runs", event="push",
                               branch="main", head_sha=main_sha) if main_sha else []
    runs = [r for r in runs if r.get("head_sha") == main_sha and r.get("event") == "push"]
    latest = max(runs, key=lambda r: (r.get("created_at") or "", r.get("id") or 0)) if runs else None
    jobs = ctx.gh_svc.paginate(f"repos/{svc}/actions/runs/{latest['id']}/jobs") if latest else []
    gt = main_go_test_status(latest, jobs, main_sha, job)
    hb = ctx.records.read("detector/heartbeat.json")
    decision = pre_merge_decision(det, gt, hb, ctx.now, cf["detector_stale_minutes"])
    return {"phase": "pre-merge", "ok": decision["ok"], "reasons": decision["reasons"],
            "detector": det, "main_go_test": gt, "heartbeat": hb}


def review_hold(state: st.ClassState, cf: dict) -> bool:
    """True while a seed can be open: the rung is not L2 and no more queue
    items have merged than the highest seed position. Both facts are public,
    so the hold says nothing about the schedule."""
    return state.rung != cf["rungs"]["l2"] and len(state.merged) <= cf["seeds"]["positions"][1]


REMAKE_MUTATION = open_pr.COMMIT_MUTATION

STATUS_QUERY = """query($owner: String!, $name: String!, $n: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $n) {
    headRefOid reviewDecision mergeStateStatus } } }"""
# GitHub has listed mergeStateStatus under this GraphQL preview; the media
# type is sent in case it still is (not verified; probes P1 to P3 read the
# field through `gh`).
MERGE_INFO_PREVIEW = "application/vnd.github.merge-info-preview+json"


def pr_status(gh, repo: str, number: int) -> dict:
    """GitHub's own view of the pull request at decision time: what it says
    is needed (`reviewDecision`) and whether it would merge
    (`mergeStateStatus`). C-15's L1 refusal is graded on these beside the
    merge API's answer. One GraphQL read, made for every queue item, seed or
    not. Recorded only: nothing here decides, and a failed read is recorded
    rather than stopping the phase."""
    owner, name = repo.split("/", 1)
    out = {"reviewDecision": None, "mergeStateStatus": None, "headRefOid": None, "error": None}
    try:
        data = gh.graphql(STATUS_QUERY, {"owner": owner, "name": name, "n": number}, accept=MERGE_INFO_PREVIEW)
    except ApiError as e:
        out["error"] = {"status": e.status, "message": e.message}
        return out
    pr = ((data or {}).get("repository") or {}).get("pullRequest") or {}
    out.update({k: pr.get(k) for k in ("reviewDecision", "mergeStateStatus", "headRefOid")})
    return out


def update_refused_for_workflows(err: ApiError) -> bool:
    """update-branch refused because main gained a workflow file the agent
    App may not write (it has no `workflows` permission). The exact answer is
    not verified (probe P3 tries it); any refusal that names workflows counts."""
    return err.status in (403, 422) and "workflow" in (err.message or json.dumps(err.body, default=str)).lower()


def remake_branch(ctx: detector.Context, pr: dict, main_sha: str) -> dict:
    """Bring a queue-item pull request up to main without update-branch.

    A new commit is made on main's tip through createCommitOnBranch, on a
    temporary branch, with the head's own go.mod and go.sum, so its diff
    against its parent touches only the two class files; then the pull
    request's branch is moved to it (force) and the temporary branch is
    deleted. The pull request is never closed, because a close is a near-miss.
    Whether GitHub accepts the force-move from the agent App when main holds
    workflow changes the old head lacks is not verified (probe P3)."""
    cf, svc, gh = ctx.cf, ctx.cf["repo"], ctx.gh_svc
    head = pr["head_sha"]
    files = {f: st.read_file_at(gh, svc, f, head) for f in ("go.mod", "go.sum")}
    if any(v is None for v in files.values()):
        return {"result": "refused", "reason": "the head lacks go.mod or go.sum"}
    tmp = f"{pr['head_ref']}-remake-r{ctx.run_id}-{ctx.run_attempt}"
    steps = []
    gh.post(f"repos/{svc}/git/refs", {"ref": f"refs/heads/{tmp}", "sha": main_sha})
    steps.append({"created": tmp, "at": main_sha})
    import base64
    res = gh.graphql(REMAKE_MUTATION, {"input": {
        "branch": {"repositoryNameWithOwner": svc, "branchName": tmp},
        "message": {"headline": f"go-module-bump: {pr.get('module')} {pr.get('version')} "
                                f"(queue item {pr.get('queue_position')})",
                    "body": f"The change of {head} made again on main {main_sha} by the merge step "
                            f"(run {ctx.run_id}, attempt {ctx.run_attempt}), because update-branch was refused."},
        "expectedHeadOid": main_sha,
        "fileChanges": {"additions": [{"path": f, "contents": base64.b64encode(v.encode()).decode()}
                                      for f, v in files.items()]},
    }})
    oid = ((res.get("createCommitOnBranch") or {}).get("commit") or {}).get("oid")
    steps.append({"commit": oid})
    out = {"result": "remade", "new_head": oid, "old_head": head, "steps": steps}
    try:
        gh.patch(f"repos/{svc}/git/refs/heads/{pr['head_ref']}", {"sha": oid, "force": True})
        steps.append({"moved": pr["head_ref"], "to": oid})
    except ApiError as e:
        out.update(result="refused", status=e.status, body=e.body)
    try:
        gh.delete(f"repos/{svc}/git/refs/heads/{tmp}")
        steps.append({"deleted": tmp})
    except ApiError as e:
        steps.append({"delete_failed": tmp, "status": e.status})
    return out


def merge(ctx: detector.Context, seed_key: str | None, pre_merge_ok: bool,
          allow_placeholders: bool = False) -> dict:
    cf, svc, gh = ctx.cf, ctx.cf["repo"], ctx.gh_svc
    out: dict[str, Any] = {"phase": "merge"}
    # The key is checked against the class file before any schedule is drawn
    # from it: a wrong key would not recognise a seed (ADV-17).
    if seed_key:
        try:
            seed_key = sd.verified_key(seed_key, cf, allow_placeholders)
        except sd.SeedKeyError as e:
            out.update(action="refuse", decision={"action": "refuse", "reason": str(e)}, agent_pr_open=True)
            return out
    snap = st.gather(gh, ctx.records, cf)
    positions = st.seed_positions_from_key(seed_key, cf) if seed_key else None
    state = st.derive_state(snap, cf, ctx.l1, ctx.l2, positions)
    prs = state.open_agent_prs
    if not pre_merge_ok:
        out.update(action="refuse", decision={"action": "refuse", "reason": "pre-merge did not report ok"},
                   agent_pr_open=bool(prs))
        return out
    pr = prs[0] if len(prs) == 1 else None
    cs = gt = status = None
    behind = None
    approved = seed_decided = False
    # The same reads for a seed as for any other item (see the module docstring).
    if pr and pr.get("queue_item") and seed_key:
        cmp_ = gh.get(f"repos/{svc}/compare/{state.main_sha}...{pr['head_sha']}")
        behind = cmp_.get("behind_by")
        cs = st.class_scope_verdict(gh, svc, pr["head_sha"], name=cf["checks"]["class_scope"]["name"],
                                    integration_id=cf["checks"]["integration_id"])
        gt = st.check_run_verdict(gh, svc, pr["head_sha"], cf["checks"]["go_test"]["name"],
                                  cf["checks"]["integration_id"])
        reviews = gh.paginate(f"repos/{svc}/pulls/{pr['number']}/reviews")
        approved = open_pr.approved_on_head(reviews, cf["human_logins"], pr["head_sha"])
        status = pr_status(gh, svc, pr["number"])
        # Read for every queue item, used only for a seed (ADV-11): a read made
        # only for a seed would show in the log's timing and in a traceback.
        events = gh.paginate(f"repos/{svc}/issues/{pr['number']}/events")
        seed_record = ctx.records.read(f"seeds/{pr['number']}.json")
        if pr["number"] in state.seeds:
            seed_decided = (open_pr.reviewer_decision(reviews, events, cf["human_logins"]) is not None
                            or seed_record is not None)
    hold = review_hold(state, cf)
    d = decide_merge(prs, state.seeds, bool(seed_key), behind, cs, gt,
                     review_hold=hold, approved=approved, seed_decided=seed_decided)
    out.update(decision=d, pr=pr, class_scope=cs, go_test=gt, behind_by=behind, main_sha=state.main_sha,
               approved_on_head=approved, review_hold=hold, pr_status=status)
    action = d["action"]
    out["action"] = action
    if action == "update-branch":
        try:
            out["response"] = {"status": 202, "body": gh.put(f"repos/{svc}/pulls/{pr['number']}/update-branch",
                                                             {"expected_head_sha": pr["head_sha"]})}
        except ApiError as e:
            if update_refused_for_workflows(e):
                # Main gained a workflow change the agent App may not bring
                # into its branch. Make the change again on main's tip (M5).
                out["response"] = {"status": e.status, "body": e.body}
                out["remake"] = remake_branch(ctx, pr, state.main_sha)
            elif e.status in (409, 422):
                out["response"] = {"status": e.status, "body": e.body}
            else:
                raise
    elif action == "close":
        body = (f"Closed by the merge step: {d['reason']} on head {pr['head_sha']} "
                f"(class-scope check run {(cs or {}).get('check_run_id')}, go-test check run "
                f"{(gt or {}).get('check_run_id')}). Under the class file this is a near-miss: it "
                f"resets the streak and closes queue item {pr.get('queue_position')}.")
        gh.post(f"repos/{svc}/issues/{pr['number']}/comments", {"body": body})
        out["response"] = {"body": gh.patch(f"repos/{svc}/pulls/{pr['number']}", {"state": "closed"})}
    elif action == "merge":
        req = {"sha": cs["head_sha"], "merge_method": "squash",
               "commit_title": TITLE.format(module=pr.get("module"), version=pr.get("version"),
                                            position=pr.get("queue_position")),
               "commit_message": MESSAGE.format(pr=pr["number"], sha=cs["head_sha"],
                                                check_run_id=cs.get("check_run_id"),
                                                run_id=ctx.run_id, run_attempt=ctx.run_attempt)}
        out["request"] = req
        try:
            out["response"] = {"status": 200, "body": gh.put(f"repos/{svc}/pulls/{pr['number']}/merge", req)}
            out["result"] = "merged"
        except ApiError as e:
            out["response"] = {"status": e.status, "body": e.body}
            out["result"] = "refused" if e.status in MERGE_REFUSALS else "error"
        after = gh.get(f"repos/{svc}/pulls/{pr['number']}")
        out["pr_after"] = {"merged": after.get("merged"), "merged_at": after.get("merged_at"),
                           "merge_commit_sha": after.get("merge_commit_sha"),
                           "merged_by": (after.get("merged_by") or {}).get("login"),
                           "mergeable_state": after.get("mergeable_state")}
        if out["result"] == "error" and after.get("merged"):
            out["result"] = "merged"  # a retried PUT after a lost answer
    merged_now = out.get("result") == "merged" or (out.get("pr_after") or {}).get("merged")
    out["agent_pr_open"] = bool(prs) and not (action == "close" or merged_now)
    out["last_scorecard_read_at"] = last_scorecard_read(ctx.records, cf)
    out["scorecard_due"] = bool(merged_now) or scorecard_due(state, cf, ctx.now, out["last_scorecard_read_at"])
    return out


def last_scorecard_read(records, cf: dict) -> str | None:
    """When the latest scorecard made its reads: `as_of` in the inputs file
    it publishes on the records branch. Read from the snapshot this process
    already holds, so it costs no request."""
    try:
        doc = records.read(f"scorecard/{cf['class']}.inputs.json")
    except ValueError:
        return None
    return doc.get("as_of") if isinstance(doc, dict) else None


def scorecard_due(state: st.ClassState, cf: dict, now, last_read_at: str | None) -> bool:
    """Whether this run should write the scorecard when it merged nothing.

    The scorecard runs on each merge (the caller adds that), and once more
    after the latest merge's escape (a) window closes: it counts no merge
    whose window is open (ADV-04), so the run after the 20th merge's window
    closes is the one that writes the promotion record, without waiting for
    the daily run or a person. It is due from the window's close until a
    scorecard has read GitHub after that close, which its published inputs
    file says (`last_read_at`). So it is true on the first agent run after
    the close and, once that scorecard has run, on no later one; a failed
    scorecard run leaves it due. Both facts are public (B4)."""
    if not state.merged:
        return False
    closes = st.parse_time(state.merged[-1]["merged_at"]) + timedelta(minutes=cf["escape_a_minutes"])
    if now < closes:
        return False
    last = st.parse_time(last_read_at)
    return last is None or last < closes


def main(argv: list[str] | None = None, env: dict | None = None) -> int:
    env = dict(os.environ if env is None else env)
    ap = argparse.ArgumentParser(prog="python3 -m factory.merge_step")
    ap.add_argument("--phase", required=True, choices=["pre-merge", "merge"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-placeholders", action="store_true")
    args = ap.parse_args(argv)
    if args.allow_placeholders and not args.dry_run:
        print(json.dumps({"error": "--allow-placeholders needs --dry-run"}))
        return 2
    try:
        ctx = detector.context_from_env(args.phase, args.dry_run, args.allow_placeholders, env)
    except classfile.NotReady as e:
        # The same outputs as a refusal: nothing merges, and an agent pull
        # request reads as open, so prepare does not run.
        outputs = ({"ok": "false"} if args.phase == "pre-merge"
                   else {"action": "none", "agent_pr_open": "true", "scorecard_due": "false"})
        return classfile.report_not_ready(e.reason, {"phase": args.phase}, outputs, env)
    if args.phase == "pre-merge":
        out = pre_merge(ctx)
        _github_output({"ok": "true" if out["ok"] else "false"}, env)
    else:
        try:
            out = merge(ctx, env.get("SEED_KEY") or None, env.get("PRE_MERGE_OK") == "true",
                        args.allow_placeholders)
        except ApiError:
            # One message whatever the open pull request is: a traceback's
            # line numbers would show which branch ran (ADV-11). The job
            # fails, nothing is merged, and the next run starts again.
            print("::error::merge: a GitHub read or write failed; nothing more was done this run", file=sys.stderr)
            _github_output({"action": "error", "agent_pr_open": "true", "scorecard_due": "false"}, env)
            return 1
        _github_output({"action": out["action"],
                        "agent_pr_open": "true" if out.get("agent_pr_open") else "false",
                        "scorecard_due": "true" if out.get("scorecard_due") else "false"}, env)
    if args.dry_run:
        out["dry_run_writes"] = detector.dry_run_writes(ctx)
    else:
        _record(ctx, args.phase, out)
    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    return 1 if out.get("result") == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
