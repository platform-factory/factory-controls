"""The escape detector: escape types (a) and (b), and the ruleset comparison.

Why it exists: ADR-0024 §9 returns the class to L1 on any escape, before
anyone says why it happened. This module finds the trusted events that count
as escapes and has the ratchet raise the rung entry to 1. It runs every ten
minutes from detector.yml, and the merge step runs the same checks before
every merge, because scheduled runs can be delayed or dropped.

Escape (a), failed verification after merge: a merged queue-item pull request
whose merge commit has no `push` run of go-test whose FIRST attempt succeeded
within escape_a_minutes of the merge. Failed, cancelled and missing all
count, read per attempt; a re-run (attempt 2 and later) never clears one. A
missing run covers `[skip ci]`. The event time is the first attempt's
completion when it failed, or the deadline when nothing succeeded in time.

Escape (b), revert for cause: a merged pull request whose go.mod and go.sum
equal those at the parent of the latest class merge before it, carrying
`escape:revert` in force from the listed human. The event time is its merge.

It acts once per event (the ledger file `ledger/<key>.json` is the record
that it acted), and only on events after the latest merged promotion or
fixture record. On each: the ratchet raises the rung entry to 1, every open
agent pull request is labelled `needs-human`, and the ledger entry is
written last, so a run that dies half way is finished by the next run (each
step is idempotent). When the ratchet's write failed, each later run (and
each pre-merge run) tries the raise again until the rung entry is 1
(`raise_to_retry`).

The ruleset comparison covers two rulesets, because GitHub refuses two
required-reviewers entries for one team in one ruleset (probe P1) and the
readiness note's fallback is two rulesets: the rung ruleset holds the rung
entry, the wall ruleset the wall entry. The live rung ruleset is compared
with the committed L1 JSON (enforcement, conditions, every rule,
bypass_actors, name and target; never timestamps or ids GitHub assigns); L2
is accepted only from a merged promotion or fixture record until the
ledger's next demotion. The live wall ruleset must equal wall.json exactly,
active, on every rung. The list of rulesets on the repository must be exactly
the class file's two. Any other difference (the wall missing, disabled or
changed included) is an event, keyed by the rulesets' version, and is acted
on once: rung raised to 1, issue opened. The merge step refuses while it
stands. Nothing here or in the ratchet ever writes the wall ruleset.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import classfile
from . import ratchet
from . import ruleset as rs
from . import state as st
from .gh import ApiError, DryRunGitHub, GitHub
from .records import RecordExists, Records, branch_from_env

CONTROLS_REPO = "platform-factory/factory-controls"


# -- pure decisions ----------------------------------------------------------

def escape_a_status(merged_at: str, merge_sha: str | None, runs: list[dict], now: datetime,
                    minutes: int, job_name: str) -> dict:
    """{"status": "clear"|"pending"|"escape", "event_time", "reason", "attempts"}.

    `runs` is [{"run": <workflow run>, "jobs": <jobs of attempt 1>}] for the
    push runs on the merge commit. Only attempt-1 jobs are passed, so a
    re-run cannot clear anything.
    """
    t0 = st.parse_time(merged_at)
    deadline = t0 + timedelta(minutes=minutes)
    attempts: list[dict] = []
    for r in runs:
        run = r.get("run", {})
        if run.get("event") != "push" or (merge_sha and run.get("head_sha") != merge_sha):
            continue
        jobs = [j for j in r.get("jobs", []) if j.get("name") == job_name
                and j.get("run_attempt", 1) == 1]
        if not jobs:
            attempts.append({"run_id": run.get("id"), "job_id": None,
                             "done": run.get("status") == "completed", "conclusion": "missing-job",
                             "completed_at": run.get("updated_at"), "cleared": False})
            continue
        for j in jobs:
            done = j.get("status") == "completed"
            completed = st.parse_time(j.get("completed_at"))
            cleared = bool(done and j.get("conclusion") == "success" and completed and completed <= deadline)
            attempts.append({"run_id": run.get("id"), "job_id": j.get("id"), "done": done,
                             "conclusion": j.get("conclusion"), "completed_at": j.get("completed_at"),
                             "cleared": cleared})
    base = {"merged_at": merged_at, "deadline": st.iso(deadline), "attempts": attempts}
    if any(a["cleared"] for a in attempts):
        return {**base, "status": "clear", "event_time": None, "reason": None}
    failures = [a for a in attempts if a["done"] and not a["cleared"]]
    fail_times = [st.parse_time(a["completed_at"]) for a in failures if a["completed_at"]]
    fail_times = [t for t in fail_times if t and t <= deadline]
    if now >= deadline or (attempts and all(a["done"] for a in attempts)):
        if not attempts:
            reason = "missing"
        elif failures and all(a["conclusion"] == "success" for a in failures):
            reason = "late"
        else:
            reason = ",".join(sorted({str(a["conclusion"]) for a in failures})) or "pending-at-deadline"
        event_time = min(fail_times) if fail_times else deadline
        return {**base, "status": "escape", "event_time": st.iso(event_time), "reason": reason}
    return {**base, "status": "pending", "event_time": None, "reason": None}


def escape_b_status(pr: dict, labels: dict, prior_class_merge: dict | None,
                    files_at_merge: dict | None, files_at_parent: dict | None) -> dict | None:
    """None when the pull request carries no escape:revert in force; otherwise
    {"status": "escape"|"unknown", ...}."""
    lab = labels.get("escape:revert")
    if not lab:
        return None
    base = {"pr": pr["number"], "merged_at": pr.get("merged_at"), "label": lab,
            "merge_commit_sha": pr.get("merge_commit_sha")}
    if prior_class_merge is None:
        return {**base, "status": "unknown", "reason": "no class merge before this pull request"}
    ok = (files_at_merge is not None and files_at_parent is not None
          and all(files_at_merge.get(f) is not None for f in ("go.mod", "go.sum"))
          and files_at_merge == files_at_parent)
    if not ok:
        return {**base, "status": "unknown", "reverted_pr": prior_class_merge["pr"],
                "reason": "escape:revert in force but go.mod and go.sum do not equal those at "
                          "the parent of the latest class merge"}
    return {**base, "status": "escape", "event_time": pr.get("merged_at"),
            "reverted_pr": prior_class_merge["pr"], "reason": "revert for cause"}


def cutoff(class_records: list[dict]) -> datetime | None:
    rec = st.latest_record(class_records)
    return st.parse_time(rec["merged_at"]) if rec else None


def after_cutoff(event_time: str | None, cut: datetime | None) -> bool:
    return cut is None or (event_time is not None and st.parse_time(event_time) > cut)


def handled_keys(ledger: list[dict]) -> set[str]:
    return {e.get("key") for e in ledger if e.get("key")}


def ruleset_signature(seen: dict) -> str:
    """One string per version of the rulesets: the rung and wall rulesets'
    updated_at, and the ids and updated_at of every ruleset on the
    repository. `seen` is what _read_rulesets returns."""
    items = sorted([[r.get("id"), r.get("updated_at")] for r in (seen.get("listing") or [])],
                   key=lambda x: x[0] or 0)
    raw = json.dumps([(seen.get("rung") or {}).get("updated_at"), (seen.get("wall") or {}).get("updated_at"), items],
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def seen_signatures(ledger: list[dict]) -> set[str]:
    out: set[str] = set()
    for e in ledger:
        out.update(e.get("ruleset_versions_seen", []))
    return out


def list_signature(listing: list[dict]) -> list[dict]:
    keys = ("id", "name", "source_type", "source", "target")
    return sorted([{k: r.get(k) for k in keys} for r in listing], key=lambda x: x["id"] or 0)


def ruleset_check(seen: dict, l1: dict, l2: dict, wall: dict, l2_ok: bool, expected_list: list[dict]) -> dict:
    """{"ok", "matches": "l1"|"l2"|None, "diff_vs_l1", "wall_diff", "list_diff", "l2_accepted"}. Pure.

    `seen` holds the live rung ruleset, the live wall ruleset and the
    repository's listing (_read_rulesets). ok iff the listing is exactly the
    expected two, the wall equals wall.json, and the rung ruleset equals L1,
    or L2 while L2 is accepted."""
    live, wall_live = seen.get("rung"), seen.get("wall")
    list_diff = rs.diff(list_signature(expected_list), list_signature(seen.get("listing") or []))
    missing = [{"path": [], "expected": "ruleset", "actual": "not found"}]
    wall_diff = missing if wall_live is None else rs.diff(rs.normalize(wall), rs.normalize(wall_live))
    if live is None:
        return {"ok": False, "matches": None, "diff_vs_l1": missing, "wall_diff": wall_diff,
                "list_diff": list_diff, "l2_accepted": l2_ok}
    n_live = rs.normalize(live)
    d1 = rs.diff(rs.normalize(l1), n_live)
    d2 = rs.diff(rs.normalize(l2), n_live)
    matches = "l1" if not d1 else ("l2" if not d2 else None)
    ok = not list_diff and not wall_diff and (matches == "l1" or (matches == "l2" and l2_ok))
    return {"ok": ok, "matches": matches, "diff_vs_l1": d1, "wall_diff": wall_diff, "list_diff": list_diff,
            "l2_accepted": l2_ok}


# -- context and I/O ---------------------------------------------------------

@dataclass
class Context:
    cf: dict
    l1: dict
    l2: dict
    gh_svc: Any            # svc-hello: the ratchet App's token (reads and the ruleset write)
    records: Records       # the controls repo's records branch; records.gh also opens issues
    now: datetime = field(default_factory=st.utcnow)
    run_id: str | None = None
    run_attempt: str | None = None
    phase: str = "detector"
    wall: dict | None = None   # the committed wall.json; read from the class file's path when not given


def _wall(ctx: Context) -> dict:
    if ctx.wall is None:
        ctx.wall = st.committed_bodies(ctx.cf)["wall"]
    return ctx.wall


def _files_at(gh, repo: str, sha: str) -> dict:
    return {f: st.read_file_at(gh, repo, f, sha) for f in ("go.mod", "go.sum")}


def find_escape_a(ctx: Context, merged: list[dict], handled: set[str], cut: datetime | None) -> tuple[list, list, list]:
    """(events, pending, ignored_before_cutoff)."""
    cf, svc = ctx.cf, ctx.cf["repo"]
    minutes = cf["escape_a_minutes"]
    wf, job = cf["checks"]["go_test"]["workflow"], cf["checks"]["go_test"]["job"]
    events, pending, ignored = [], [], []
    for m in merged:
        key = f"escape-a-pr{m['pr']}"
        if key in handled or not m.get("merge_commit_sha"):
            continue
        deadline = st.parse_time(m["merged_at"]) + timedelta(minutes=minutes)
        if cut is not None and deadline <= cut:
            continue  # every event time it could have is at or before the cutoff
        runs = []
        for run in ctx.gh_svc.paginate(f"repos/{svc}/actions/workflows/{wf}/runs",
                                       event="push", head_sha=m["merge_commit_sha"]):
            jobs = ctx.gh_svc.paginate(f"repos/{svc}/actions/runs/{run['id']}/attempts/1/jobs")
            runs.append({"run": run, "jobs": jobs})
        res = escape_a_status(m["merged_at"], m["merge_commit_sha"], runs, ctx.now, minutes, job)
        ev = {"key": key, "type": "a", "pr": m["pr"], "merge_commit_sha": m["merge_commit_sha"], **res}
        if res["status"] == "escape":
            (events if after_cutoff(res["event_time"], cut) else ignored).append(ev)
        elif res["status"] == "pending":
            pending.append({"key": key, "pr": m["pr"], "deadline": res["deadline"]})
    return events, pending, ignored


def find_escape_b(ctx: Context, snap: dict, state: st.ClassState, handled: set[str],
                  cut: datetime | None) -> tuple[list, list, list]:
    """(events, unknowns, ignored_before_cutoff)."""
    svc = ctx.cf["repo"]
    merged = state.merged
    if not merged:
        return [], [], []
    first_class = st.parse_time(merged[0]["merged_at"])
    qnums = set(state.queue_item_prs) | set(state.fixture_prs)
    events, unknowns, ignored = [], [], []
    prs = {int(k): v for k, v in snap["prs"].items()}
    for n in sorted(prs):
        pr = prs[n]
        t = st.parse_time(pr.get("merged_at"))
        if not t or n in qnums or t <= first_class:
            continue
        key = f"escape-b-pr{n}"
        if key in handled:
            continue
        labels = st.labels_in_force(ctx.gh_svc, svc, n, ctx.cf)
        for u in labels.get("unknown_label_events", []):
            unknowns.append({"type": "label-event-not-counted", "pr": n, **u})
        if not labels.get("escape:revert"):
            continue
        prior = [m for m in merged if st.parse_time(m["merged_at"]) < t]
        prior_m = prior[-1] if prior else None
        files_merge = files_parent = None
        if prior_m:
            files_merge = _files_at(ctx.gh_svc, svc, pr["merge_commit_sha"])
            commit = ctx.gh_svc.get(f"repos/{svc}/commits/{prior_m['merge_commit_sha']}")
            parent = commit["parents"][0]["sha"]
            files_parent = _files_at(ctx.gh_svc, svc, parent)
        res = escape_b_status(pr, labels, prior_m, files_merge, files_parent)
        if res is None:
            continue
        if res["status"] == "unknown":
            unknowns.append({"type": "escape-revert-unmatched", "key": key, **res})
            continue
        ev = {"key": key, "type": "b", **res}
        (events if after_cutoff(res["event_time"], cut) else ignored).append(ev)
    return events, unknowns, ignored


def _read_one(ctx: Context, rid) -> dict | None:
    try:
        return ctx.gh_svc.get(f"repos/{ctx.cf['repo']}/rulesets/{rid}")
    except ApiError as e:
        if e.status != 404:
            raise
        return None


def _read_rulesets(ctx: Context) -> dict:
    """{"rung", "wall", "listing"}: the two rulesets as GET returns them (None
    when not found) and the repository's ruleset listing. Reads only."""
    rulesets = ctx.cf["rulesets"]
    return {"rung": _read_one(ctx, rulesets["ruleset_id"]), "wall": _read_one(ctx, rulesets["wall_ruleset_id"]),
            "listing": ctx.gh_svc.paginate(f"repos/{ctx.cf['repo']}/rulesets")}


def _open_agent_prs(ctx: Context, state: st.ClassState) -> list[int]:
    svc = ctx.cf["repo"]
    bot = classfile.agent_bot_login(ctx.cf)
    known = set(state.queue_item_prs) | set(state.fixture_prs)
    out = []
    for p in ctx.gh_svc.paginate(f"repos/{svc}/pulls", state="open", base="main"):
        if p["number"] in known or (p.get("user") or {}).get("login") == bot:
            out.append(p["number"])
    return sorted(out)


def act(ctx: Context, event: dict, state: st.ClassState) -> dict:
    """Freeze for one event: ratchet, labels, then the ledger entry (last)."""
    key = event["key"]
    seen_before = _read_rulesets(ctx)
    reason = {"event_key": key, "type": event["type"], "event_time": event.get("event_time"),
              "pr": event.get("pr"), "reason": event.get("reason")}
    res = ratchet.raise_rung(ctx.gh_svc, ctx.records, ctx.cf, ctx.l1, ctx.l2, key, reason)
    issue = res.get("issue")
    issue_errors = list(res.get("issue_errors", []))
    if event["type"] == "ruleset" and not issue:
        # A refused issue (the controls token's limit) does not stop the
        # labels, which use the ratchet App's token.
        issue = ratchet.try_open_issue(
            ctx.records, f"ratchet: {key}: ruleset difference",
            "The detector found svc-hello's rulesets differing from the committed JSON: the rung "
            "ruleset from L1 (or from L2 while L2 is not accepted), the wall ruleset from wall.json, "
            "or the repository's list of rulesets from the class file's. It raised the rung entry to 1 "
            "where it could; nothing writes the wall ruleset. The merge step refuses while the "
            "difference stands.\n\n```json\n"
            + json.dumps({"diff_vs_l1": event.get("diff_vs_l1"), "wall_diff": event.get("wall_diff"),
                          "list_diff": event.get("list_diff"),
                          "matches": event.get("matches"), "l2_accepted": event.get("l2_accepted")},
                         indent=2, sort_keys=True) + "\n```", issue_errors)
    labelled, label_errors = [], []
    for n in _open_agent_prs(ctx, state):
        try:
            ctx.gh_svc.post(f"repos/{ctx.cf['repo']}/issues/{n}/labels", {"labels": ["needs-human"]})
            labelled.append(n)
        except ApiError as e:
            label_errors.append({"pr": n, "status": e.status, "message": e.message})
    if label_errors:
        # Leave no ledger entry, so the next run repeats the (idempotent) freeze.
        raise RuntimeError(f"labelling failed for {label_errors}")
    seen_after = _read_rulesets(ctx)
    entry = {
        "schema": 1, "key": key, "type": event["type"], "event": event,
        "acted_at": st.iso(st.utcnow()), "phase": ctx.phase,
        "detector_run": {"run_id": ctx.run_id, "run_attempt": ctx.run_attempt},
        "ratchet": res, "issue": issue, "issue_errors": issue_errors, "labelled_needs_human": labelled,
        "ruleset_versions_seen": sorted({ruleset_signature(seen_before), ruleset_signature(seen_after)}),
    }
    try:
        ctx.records.write(f"ledger/{key}.json", entry, f"ledger: {key}")
    except RecordExists:
        entry["note"] = "ledger entry already written by a concurrent run"
    return entry


def raise_to_retry(ledger: list[dict], class_records: list[dict], live_rung: int, cf: dict,
                   acted_now: set[str]) -> dict | None:
    """The demotion whose raise is tried again, or None. Pure.

    A ruleset PUT that failed leaves the rung entry at 0 after a demotion,
    and the ledger entry records that ruleset version as seen, so the
    ruleset comparison does not act on it again (B5). So while the latest
    demotion's ratchet result is `write-failed`, the live rung entry still
    reads L2's value, and no promotion or fixture record has merged since
    (after one, L2 is a person's act again), each detector run and each
    pre-merge run tries the raise again. A demotion acted on in this same run
    is left to the next run."""
    if live_rung != cf["rungs"]["l2"] or st.l2_accepted(class_records, ledger):
        return None
    demotions = st.ledger_demotions(ledger)
    if not demotions:
        return None
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    latest = max(demotions, key=lambda e: (st.parse_time(e.get("acted_at")) or oldest, e.get("key") or ""))
    if latest.get("key") in acted_now or (latest.get("ratchet") or {}).get("result") != "write-failed":
        return None
    return latest


def retry_raise(ctx: Context, demotion: dict) -> dict:
    """One more raise for a demotion whose write failed, recorded as its own
    ledger entry (type `ratchet-retry`, not a demotion) with the ratchet's
    before and after records under its own key. Its issue is the demotion's,
    so a write that keeps failing keeps one issue open."""
    key = f"{demotion['key']}-retry-{ctx.phase}-{ctx.run_id or 'local'}-{ctx.run_attempt or '0'}"
    ev = demotion.get("event") or {}
    reason = {"event_key": demotion["key"], "type": "ratchet-retry", "retry_of": demotion["key"],
              "event_time": ev.get("event_time"), "pr": ev.get("pr"),
              "reason": "the ratchet's write for this demotion failed and the rung entry is still 0"}
    res = ratchet.raise_rung(ctx.gh_svc, ctx.records, ctx.cf, ctx.l1, ctx.l2, key, reason,
                             issue_key=demotion["key"])
    entry = {"schema": 1, "key": key, "type": "ratchet-retry", "retry_of": demotion["key"],
             "acted_at": st.iso(st.utcnow()), "phase": ctx.phase,
             "detector_run": {"run_id": ctx.run_id, "run_attempt": ctx.run_attempt}, "ratchet": res}
    try:
        ctx.records.write(f"ledger/{key}.json", entry, f"ledger: {key}")
    except RecordExists:
        entry["note"] = "ledger entry already written by a concurrent run"
    return entry


def run(ctx: Context, write_heartbeat: bool = True) -> dict:
    """One detector pass. Returns the JSON the CLI prints."""
    snap = st.gather(ctx.gh_svc, ctx.records, ctx.cf)
    state = st.derive_state(snap, ctx.cf, ctx.l1, ctx.l2, None)
    ledger = list(snap["ledger"])
    handled = handled_keys(ledger)
    cut = cutoff(snap["class_records"])
    ev_a, pending, ign_a = find_escape_a(ctx, state.merged, handled, cut)
    ev_b, unknowns_b, ign_b = find_escape_b(ctx, snap, state, handled, cut)
    acted = []
    for ev in sorted(ev_a + ev_b, key=lambda e: (st.parse_time(e["event_time"]), e["key"])):
        entry = act(ctx, ev, state)
        ledger.append(entry)
        acted.append(entry["key"])
    seen = _read_rulesets(ctx)
    retried = []
    demotion = raise_to_retry(ledger, snap["class_records"], st.live_rung(seen["rung"], ctx.l1, ctx.l2), ctx.cf,
                              set(acted))
    if demotion is not None:
        entry = retry_raise(ctx, demotion)
        ledger.append(entry)
        retried.append(entry["key"])
        seen = _read_rulesets(ctx)
    accepted = st.l2_accepted(snap["class_records"], ledger)
    expected_list = ctx.cf["rulesets"]["repository_rulesets"]
    chk = ruleset_check(seen, ctx.l1, ctx.l2, _wall(ctx), accepted, expected_list)
    if not chk["ok"]:
        sig = ruleset_signature(seen)
        if sig not in seen_signatures(ledger):
            ev = {"key": f"ruleset-{sig}", "type": "ruleset", "event_time": st.iso(ctx.now),
                  "reason": "ruleset difference", **chk}
            entry = act(ctx, ev, state)
            ledger.append(entry)
            acted.append(entry["key"])
            seen = _read_rulesets(ctx)
            accepted = st.l2_accepted(snap["class_records"], ledger)
            chk = ruleset_check(seen, ctx.l1, ctx.l2, _wall(ctx), accepted, expected_list)
    unknowns = list(snap.get("unknowns", [])) + unknowns_b
    out = {
        "phase": ctx.phase, "now": st.iso(ctx.now), "acted": acted, "retried": retried, "pending": pending,
        "ignored_before_latest_record": [e["key"] for e in ign_a + ign_b],
        "latest_record": st.latest_record(snap["class_records"]),
        "ruleset": chk, "rung": st.live_rung(seen["rung"], ctx.l1, ctx.l2), "unknowns": unknowns,
        "main_sha": snap.get("main_sha"),
    }
    if write_heartbeat:
        hb = {"schema": 1, "completed_at": st.iso(st.utcnow()), "run_id": ctx.run_id,
              "run_attempt": ctx.run_attempt, "acted": acted, "retried": retried, "ruleset_ok": chk["ok"],
              "rung": out["rung"], "pending": pending, "unknowns": unknowns}
        ctx.records.write_replace("detector/heartbeat.json", hb, "detector: heartbeat")
        out["heartbeat"] = hb
    return out


# -- CLI ---------------------------------------------------------------------

def context_from_env(phase: str, dry_run: bool = False, allow_placeholders: bool = False,
                     env: dict | None = None) -> Context:
    """Raises classfile.NotReady while there is no filled class file."""
    env = dict(os.environ if env is None else env)
    # The wall ruleset is read by its id, so a placeholder there stops the run too.
    cf = classfile.require(env, {"human_logins[0]", "rulesets"}, allow_placeholders=allow_placeholders)
    bodies = st.committed_bodies(cf)
    l1, l2 = bodies["l1"], bodies["l2"]
    svc_token, controls_token = env.get("SVC_TOKEN"), env.get("CONTROLS_TOKEN")
    if not svc_token or not controls_token:
        raise SystemExit(json.dumps({"error": "SVC_TOKEN and CONTROLS_TOKEN are required"}))
    gh_svc: Any = GitHub(svc_token)
    gh_controls: Any = GitHub(controls_token)
    if dry_run:
        gh_svc, gh_controls = DryRunGitHub(gh_svc), DryRunGitHub(gh_controls)
    records = Records(gh_controls, env.get("CONTROLS_REPO", CONTROLS_REPO), branch_from_env(env))
    return Context(cf=cf, l1=l1, l2=l2, gh_svc=gh_svc, records=records,
                   run_id=env.get("GITHUB_RUN_ID"), run_attempt=env.get("GITHUB_RUN_ATTEMPT"), phase=phase,
                   wall=bodies["wall"])


def dry_run_writes(ctx: Context) -> list:
    out = []
    for g in (ctx.gh_svc, ctx.records.gh):
        if isinstance(g, DryRunGitHub):
            out.extend(g.writes)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m factory.detector", description="One detector run.")
    ap.add_argument("--dry-run", action="store_true", help="read live state, send no write")
    ap.add_argument("--allow-placeholders", action="store_true",
                    help="run with the class file's placeholders (dry runs only)")
    args = ap.parse_args(argv)
    if args.allow_placeholders and not args.dry_run:
        print(json.dumps({"error": "--allow-placeholders needs --dry-run"}))
        return 2
    try:
        ctx = context_from_env("detector", args.dry_run, args.allow_placeholders)
    except classfile.NotReady as e:
        return classfile.report_not_ready(e.reason, {"phase": "detector"})
    out = run(ctx)
    if args.dry_run:
        out["dry_run_writes"] = dry_run_writes(ctx)
    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
