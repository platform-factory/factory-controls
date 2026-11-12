"""The scorecard: one JSON and one Markdown file for the class, from the record.

Why it exists: C-17 asks whether a class's track record can be kept by the
machinery, with every field traced to an API read or to one of six fields a
person enters by design. C-16 asks for review minutes and caught seeds, and
C-18 and C-19 for the streak, the escapes and their freeze times. This module
computes all of them from GitHub's record (pull requests, their issue events,
reviews, check runs, workflow runs and their attempts), the records branch
(run records, the ledger, seed and fixture records, the detector's
heartbeat) and the class file. Nothing in the output is typed by hand.

How it is built, and why that way:

- **Read once, then compute.** `gather` makes every read and returns one
  JSON-able snapshot. `compute` is a pure function of the snapshot, the class
  file and the committed rulesets: it reads no clock and no network. So two
  runs over the same inputs give byte-identical JSON (C-17's pass line), and
  anyone can recompute the scorecard from the published inputs file.
- **Withheld until closed.** Per-pull-request rows exist only for pull
  requests that are closed or merged (ADR-0024 §11). `gather` does not read
  the events, reviews or checks of an open pull request at all, and
  `compute` drops them again if a snapshot carries them, so the published
  inputs file holds nothing about an open pull request beyond its number.
- **The same rules as the machinery.** The streak, the near-misses, the seeds
  and the labels in force come from `factory.state`, the code the merge step
  and the detector use, so the scorecard cannot count differently from them.
- **The six human-entered fields are declared.** The `reviewing` event, the
  entered review minutes and the packet sections used are measurements C-16
  asks for; the listed human's close of a queue-item pull request,
  `escape:revert` and `human-cleared` are human classifications the design
  intends. The review form's `decision` repeats the review's own state and is
  checked against it; its `reason` is copied and never read.

At the 20th clean merge the scorecard also writes a promotion record to a
branch of the controls repo and opens an issue; the delegate opens the pull
request and the listed human approves it (ADR-0024 §8).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import classfile
from . import open_pr
from . import ruleset as rs
from . import state as st
from .gh import ApiError, DryRunGitHub, GitHub
from .records import Records, branch_from_env, dumps

SCHEMA = 1
CONTROLS_REPO = "platform-factory/factory-controls"
ORG_GITHUB_REPO = "platform-factory/.github"
INJECTION_STEP = "injected failure (M4, C-18)"
# The switch-on is recorded by hand in platform-factory/.github, in M3's form
# (`<UTC time>-<step>-<before|after|history>-<change>.json`); docs/RUNBOOK.md
# fixes the change name. Its time starts "changes after the switch-on".
SWITCH_ON_RE = re.compile(r"^(\d{8}T\d{6}Z)-\d+-after-l1-switch-on\.json$")
RULESET_RECORD_RE = re.compile(r"^(\d{8}T\d{6}Z)-(\d+)-(before|after|history)-(.+)\.json$")
# Steps of a go-test job that are GitHub's own set-up and tear-down, not the
# verification: a failure there is not evidence that the change was bad.
INFRA_STEP_RE = re.compile(r"^(Set up job|Initialize containers|Stop containers|Complete job|Post .*|"
                           r"Run actions/checkout.*)$")
# The fixed review form (readiness note, C-16).
REVIEW_FORM_RE = re.compile(
    r"^\s*minutes:\s*(?P<minutes>\d+(?:\.\d+)?)\s*;\s*sections:\s*(?P<sections>[^;]*?)\s*;"
    r"\s*decision:\s*(?P<decision>approve|close)\s*;\s*reason:\s*(?P<reason>.*?)\s*$", re.S)

HUMAN_ENTERED_FIELDS = [
    {"field": "reviewing", "source": "the listed human's `reviewing` labeled event",
     "kind": "measurement", "why": "C-16's review minutes start at it"},
    {"field": "entered_minutes", "source": "`minutes:` in the review form",
     "kind": "measurement", "why": "C-16 asks for the reviewer's own minutes beside the measured ones"},
    {"field": "sections", "source": "`sections:` in the review form",
     "kind": "measurement", "why": "C-16 asks which packet sections were used"},
    {"field": "close", "source": "the listed human's close of a queue-item pull request",
     "kind": "human classification the design intends", "why": "the review rule's close is a near-miss"},
    {"field": "escape:revert", "source": "the listed human's `escape:revert` labeled event",
     "kind": "human classification the design intends", "why": "escape type (b) rests on a person's label"},
    {"field": "human-cleared", "source": "the listed human's `human-cleared` labeled event",
     "kind": "human classification the design intends", "why": "a clearable inconclusive is cleared by a person"},
]
REVIEW_FORM_NOTES = ("`decision` repeats the review's own state and is checked against it; "
                     "`reason` is free text the scorecard copies and does not read.")


# -- small pure helpers -----------------------------------------------------

def _t(s: str | None) -> datetime | None:
    return st.parse_time(s)


def _minutes(a: str | None, b: str | None) -> float | None:
    ta, tb = _t(a), _t(b)
    if ta is None or tb is None:
        return None
    return round((tb - ta).total_seconds() / 60.0, 2)


def _median(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 2) if xs else None


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def inputs_sha256(snap: dict) -> str:
    """Hash of the inputs the output depends on (the snapshot without `meta`)."""
    return hashlib.sha256(canonical({k: v for k, v in snap.items() if k != "meta"}).encode()).hexdigest()


def level(min_approvals: int) -> str:
    return {1: "L1", 0: "L2"}.get(min_approvals, "unknown")


def parse_review_form(body: str | None) -> dict:
    """The fixed form `minutes: <n>; sections: <list>; decision: <approve|close>; reason: <text>`.

    A body that does not match is reported as not valid, never repaired: a
    repair would be a hand edit of the output.
    """
    m = REVIEW_FORM_RE.match(body or "")
    if not m:
        return {"form_valid": False, "entered_minutes": None, "sections": [], "decision": None,
                "reason": None}
    sections = [s.strip() for s in m.group("sections").split(",") if s.strip()]
    mins = float(m.group("minutes"))
    return {"form_valid": True, "entered_minutes": int(mins) if mins.is_integer() else mins,
            "sections": sections, "decision": m.group("decision"), "reason": m.group("reason")}


def human_labeled_times(events: list[dict], logins: list[str], name: str) -> list[str]:
    """Times of the listed human's own `labeled` events for one label (no App)."""
    humans = {h.lower() for h in logins}
    out = []
    for e in events:
        if (e.get("event") == "labeled" and (e.get("label") or {}).get("name") == name
                and (e.get("actor") or {}).get("login", "").lower() in humans
                and not e.get("performed_via_github_app")):
            out.append(e.get("created_at"))
    return sorted(t for t in out if t)


def rung_by_records(t: str | None, class_records: list[dict], ledger: list[dict]) -> str:
    """The rung the records allow at time t: L2 from a merged promotion or fixture
    record until the ledger's next demotion, L1 otherwise. A close record
    grants nothing (state.latest_record reads the same two kinds). The
    ruleset's own history needs `administration`, which this job does not
    hold; the live rung is reported beside this."""
    tt = _t(t)
    if tt is None:
        return "L1"
    recs = [r for r in class_records if r.get("merged_at") and r.get("kind") in ("promotion", "fixture")
            and _t(r["merged_at"]) <= tt]
    if not recs:
        return "L1"
    latest = max(_t(r["merged_at"]) for r in recs)
    for d in st.ledger_demotions(ledger):
        a = _t(d.get("acted_at"))
        if a is not None and latest < a <= tt:
            return "L1"
    return "L2"


def check_runs_on(snap: dict, sha: str | None, name: str | None = None, integration: int = 15368) -> list[dict]:
    runs = (snap.get("check_runs") or {}).get(sha or "", [])
    return [c for c in runs if (name is None or c.get("name") == name) and (c.get("app_id") == integration)]


def cs_verdict(snap: dict, cr: dict | None) -> dict:
    """class-scope's verdict for one check run, from its job's step names
    (`state.verdict_from_job`), with the reason codes, pass kind and verifier
    version from the check run's class-scope-verdict annotation, which is
    public. (The verdict artifact needs `actions: read` on svc-hello, which
    this job's token does not have; an old snapshot's artifact entry is still
    read.)"""
    if cr is None:
        return {"verdict": "none", "reason_codes": [], "check_run_id": None}
    job = (snap.get("cs_jobs") or {}).get(str(cr.get("job_id")))
    if job is None:
        v = {"verdict": "inconclusive" if cr.get("status") == "completed" else "pending",
             "reason_class": "rerun", "note": "job not readable"}
    else:
        v = st.verdict_from_job(job)
    art = (snap.get("cs_verdicts") or {}).get(str(cr.get("id")))
    if not isinstance(art, dict):
        art = (snap.get("cs_artifacts") or {}).get(str(cr.get("run_id")))
    codes = list(art.get("reason_codes") or []) if isinstance(art, dict) else []
    out = {"verdict": v["verdict"], "reason_class": v.get("reason_class") or (art or {}).get("reason_class"),
           "reason_codes": codes, "check_run_id": cr.get("id"), "run_id": cr.get("run_id"),
           "completed_at": cr.get("completed_at"),
           "verifier_version": (art or {}).get("verifier_version") if isinstance(art, dict) else None,
           "artifact_read": isinstance(art, dict)}
    if v["verdict"] == "inconclusive" and not codes:
        # No artifact: the step names still tell a verdict-inconclusive
        # (reason unreadable here) from a run that reached no verdict.
        out["reason_codes"] = ["reason-unreadable" if job is not None and v.get("reason_class") is None
                               else "no-verdict"]
    return out


def latest(runs: list[dict]) -> dict | None:
    return max(runs, key=lambda c: (c.get("started_at") or "", c.get("id") or 0)) if runs else None


def gotest_verdict(cr: dict | None) -> str:
    return st.check_conclusion(cr)["verdict"] if cr else "none"


def shadow_of(cs: dict, gt: str) -> str:
    """What "merge when the required checks are green" would have done."""
    if cs["verdict"] == "fail" or gt == "fail":
        return "close"
    if cs["verdict"] == "pass" and gt == "pass":
        # A pass that a person's clearance made is not a checks-green pass.
        return "wait" if "cleared" in cs.get("reason_codes", []) else "merge"
    return "wait"


def classify_attempt1(jobs: list[dict], job_name: str) -> dict:
    """Why the first attempt of a push run's go-test did not clear: the failing
    step, and whether it was the injection step."""
    js = [j for j in jobs if j.get("name") == job_name and j.get("run_attempt", 1) == 1]
    if not js:
        return {"cause": "missing-job", "failed_step": None}
    j = js[0]
    failed = [s.get("name") for s in j.get("steps", []) if s.get("conclusion") in ("failure", "timed_out")]
    if INJECTION_STEP in failed:
        return {"cause": "injected", "failed_step": INJECTION_STEP}
    real = [s for s in failed if not INFRA_STEP_RE.match(s or "")]
    if real:
        return {"cause": "verification-failed", "failed_step": real[0]}
    if failed:
        return {"cause": "infrastructure", "failed_step": failed[0]}
    return {"cause": j.get("conclusion") or j.get("status"), "failed_step": None}


# -- the pure computation ----------------------------------------------------

def _sort(xs: list, key) -> list:
    return sorted(xs, key=lambda x: (key(x), canonical(x)))


def normalize_snapshot(snap: dict) -> dict:
    """A copy with every list whose order carries no meaning put in one order,
    so that the same reads in another order are the same inputs."""
    out = json.loads(canonical(snap))
    S = out.get("state", {})
    S["run_records"] = _sort(S.get("run_records", []), lambda r: (r.get("opened_at") or "", r.get("controls_run_id") or 0))
    S["ledger"] = _sort(S.get("ledger", []), lambda e: e.get("key") or "")
    S["class_records"] = _sort(S.get("class_records", []), lambda r: r.get("path") or "")
    S["unknowns"] = _sort(S.get("unknowns", []), lambda u: "")
    for k in ("fixture_prs", "seed_record_prs"):
        S[k] = sorted(S.get(k, []))
    for k, v in list(S.get("close_events", {}).items()):
        S["close_events"][k] = _sort(v, lambda e: (e.get("created_at") or "", e.get("id") or 0))
    for k, v in list((out.get("events") or {}).items()):
        out["events"][k] = _sort(v, lambda e: (e.get("created_at") or "", e.get("id") or 0))
    for k, v in list((out.get("reviews") or {}).items()):
        out["reviews"][k] = _sort(v, lambda r: (r.get("submitted_at") or "", r.get("id") or 0))
    for k, v in list((out.get("pr_commits") or {}).items()):
        out["pr_commits"][k] = sorted(v)
    for k, v in list((out.get("check_runs") or {}).items()):
        out["check_runs"][k] = _sort(v, lambda c: c.get("id") or 0)
    for k, v in list((out.get("push_runs") or {}).items()):
        out["push_runs"][k] = _sort(v, lambda r: (r.get("run") or {}).get("id") or 0)
    for k in ("fixtures", "svc_commits", "controls_commits"):
        out[k] = _sort(out.get(k) or [], lambda x: x.get("id") or x.get("sha") or "")
    out["ruleset_records"] = sorted(out.get("ruleset_records") or [])
    return out


def withhold(snap: dict) -> dict:
    """Drop everything per-pull-request about pull requests that are still open.

    Their close events stay (closed, reopened and merged only, read by the
    state code for every queue item), because a pull request reopened after a
    close is still a near-miss and resets the streak."""
    out = normalize_snapshot(snap)
    prs = out.get("state", {}).get("prs", {})
    open_ = {k for k, p in prs.items() if p.get("state") == "open"}
    for key in ("events", "reviews", "pr_commits"):
        out[key] = {k: v for k, v in (out.get(key) or {}).items() if k not in open_}
    keep_shas = {s for v in out["pr_commits"].values() for s in v}
    keep_shas |= {p.get("head", {}).get("sha") for k, p in prs.items() if k not in open_}
    keep_shas |= set((out.get("push_runs") or {}).keys())
    out["check_runs"] = {k: v for k, v in (out.get("check_runs") or {}).items() if k in keep_shas}
    return out


def seed_positions(snap: dict, cf: dict) -> tuple[list[int] | None, dict]:
    """Positions from the published seed key, once the reveal job has run."""
    sk = snap.get("seed_key")
    info: dict[str, Any] = {"key_published": bool(sk), "key_matches_hash": None, "schedule": None}
    if not sk:
        return None, info
    key = sk.get("key_hex") or sk.get("seed_key") or sk.get("key")   # reveal.py writes key_hex
    try:
        info["key_matches_hash"] = hashlib.sha256(bytes.fromhex(key)).hexdigest() == cf["seeds"]["key_sha256"]
    except (TypeError, ValueError):
        info["key_matches_hash"] = False
        return None, info
    try:
        from . import seeds  # built separately; present once the opening job is
        sched = seeds.schedule(key, cf)
    except Exception as e:  # noqa: BLE001 - reported, never fatal
        info["schedule_error"] = f"{type(e).__name__}: {e}"
        return None, info
    if isinstance(sched, dict):
        sched = sched.get("positions", sched.get("schedule", []))
    info["schedule"] = [s if isinstance(s, dict) else {"position": int(s)} for s in sched]
    return sorted(int(s["position"]) if isinstance(s, dict) else int(s) for s in sched), info


def compute(snap_in: dict, cf: dict, l1: dict, l2: dict) -> dict:
    """The scorecard as one JSON-able dict. Pure: same inputs, same output."""
    snap = withhold(snap_in)
    S = snap["state"]
    logins = cf["human_logins"]
    bot = classfile.agent_bot_login(cf)
    integ = cf["checks"]["integration_id"]
    cs_name, gt_name = cf["checks"]["class_scope"]["name"], cf["checks"]["go_test"]["name"]
    positions, seed_info = seed_positions(snap, cf)
    # The scorecard's snapshot also carries the push runs on each merge commit
    # and the time of the read, so the state counts type (a) escapes from the
    # runs themselves and offers promotion only once each counted merge's
    # escape window is clear (ADV-04); and the commits after the switch-on
    # that change what go-test or class-scope decides reset the streak
    # (the note's "Changes after the L1 switch-on"; ADV-05).
    S = dict(S)
    S["push_runs"] = snap.get("push_runs") or {}
    S["as_of"] = snap.get("as_of")
    S["verifier_changes"] = st.verifier_changes(snap.get("svc_commits") or [], _switch_on_time(snap))
    state = st.derive_state(S, cf, l1, l2, positions)
    prs = {int(k): v for k, v in S.get("prs", {}).items()}
    ledger = S.get("ledger", [])
    class_records = S.get("class_records", [])
    seeds = set(state.seeds)
    qprs = state.queue_item_prs
    events = {int(k): v for k, v in (snap.get("events") or {}).items()}
    reviews = {int(k): v for k, v in (snap.get("reviews") or {}).items()}
    near = {n["pr"]: n for n in state.near_misses}
    merged_nums = {m["pr"] for m in state.merged}
    seed_records = {int(k): v for k, v in (snap.get("seed_records") or {}).items()}

    # The point after which C-16's median stops: the first merged promotion or
    # fixture record, or the merge that left three queue items.
    cut_times = [r["merged_at"] for r in class_records if r.get("merged_at")]
    three_left = [m["merged_at"] for m in state.merged if (m.get("position") or 0) >= len(cf["queue"]) - 3]
    median_cutoff = min(cut_times + three_left, key=lambda s: _t(s)) if (cut_times or three_left) else None

    rows, withheld = [], []
    for n in sorted(qprs):
        rec, pr = qprs[n], prs.get(n) or {}
        if pr.get("state") == "open" or not pr:
            withheld.append(n)
            continue
        rows.append(_row(snap, cf, n, rec, pr, events.get(n, []), reviews.get(n, []), seeds, near,
                         merged_nums, seed_records, class_records, ledger, logins, bot, integ,
                         cs_name, gt_name, median_cutoff))

    # Inconclusives by reason, across every class-scope run on every commit of
    # every closed or merged queue-item pull request.
    inc_runs: dict[str, int] = {}
    inc_prs: dict[str, set] = {}
    for r in rows:
        for code in r["inconclusive_reasons"]:
            inc_runs[code] = inc_runs.get(code, 0) + r["inconclusive_runs_by_reason"].get(code, 0)
            inc_prs.setdefault(code, set()).add(r["pr"])

    escapes = _escapes(snap, cf, state, ledger, prs, events, logins)
    reviewed = [r for r in rows if r["kind"] == "item" and r["review"] and r["review"]["in_median"]]
    measured = [r["review"]["measured_minutes"] for r in reviewed]
    entered = [r["review"]["entered_minutes"] for r in reviewed]
    upper = [r["review"]["upper_bound_minutes"] for r in reviewed]
    med = _median(measured)
    seeds_out = _seeds(rows, positions, seed_info, state)
    changes = _changes_after_switch_on(snap, ledger)
    unknowns = _unknowns(snap, rows, events, prs, logins, state)
    streak = _streak(state, cf)
    first_agent = min((r["opened_at"] for r in qprs.values() if r.get("opened_at")), key=_t, default=None)
    window_end = st.iso(_t(first_agent) + timedelta(weeks=cf["window_weeks"])) if first_agent else None

    out = {
        "schema": SCHEMA,
        "class": cf["class"],
        "repo": cf["repo"],
        "inputs_sha256": inputs_sha256(snap),
        "class_file_commit": snap.get("class_file_commit"),
        "as_of": {"main_sha": state.main_sha, "latest_event_at": _latest_event(snap)},
        "n": cf["n"],
        "rung": {"live_minimum_approvals": state.rung, "live_level": level(state.rung),
                 "by_records_now": rung_by_records(_latest_event(snap), class_records, ledger),
                 "l2_accepted": state.l2_accepted, "latest_record": state.latest_record and {
                     k: state.latest_record.get(k) for k in ("path", "kind", "merged_at", "pr")}},
        "streak": streak,
        "promotion": {"due": bool(state.promotable), "n": cf["n"], "streak": state.streak,
                      "blockers": list(state.promotion_blockers),
                      "streak_prs": [m["pr"] for m in state.merged
                                     if state.streak_start and _t(m["merged_at"]) >= _t(state.streak_start)][:cf["n"]]},
        "changes": {"queue_item_prs": len(qprs), "merged": len(state.merged), "near_misses": len(state.near_misses),
                    "seeds": len(seeds), "open": withheld, "fixture_prs": state.fixture_prs,
                    "first_agent_pr_at": first_agent, "window_closes_at": window_end},
        "escapes": escapes["by_type"],
        "near_misses": [{**{k: n.get(k) for k in ("pr", "position", "module", "version", "closed_at", "closed_by")},
                         "closed_by_kind": _closer_kind(n.get("closed_by"), logins, bot)} for n in state.near_misses],
        "seeds": seeds_out,
        "shadow_verdicts": _shadow_summary(rows),
        "inconclusives": {"by_reason_runs": dict(sorted(inc_runs.items())),
                          "by_reason_prs": {k: sorted(v) for k, v in sorted(inc_prs.items())},
                          "natural_prs": sorted({r["pr"] for r in rows if any(c != "forced" for c in r["inconclusive_reasons"])}),
                          "forced_prs": sorted({r["pr"] for r in rows if "forced" in r["inconclusive_reasons"]}),
                          "cleared_prs": sorted(r["pr"] for r in rows if r["cleared_by"])},
        "reviews": {"count_in_median": len(reviewed), "median_measured_minutes": med,
                    "median_entered_minutes": _median(entered), "median_upper_bound_minutes": _median(upper),
                    "median_cutoff": median_cutoff,
                    "pass_line_minutes": cf["review_median_pass_minutes"],
                    "falsifier_minutes": cf["review_median_falsifier_minutes"],
                    "within_pass_line": None if med is None else med <= cf["review_median_pass_minutes"],
                    "beyond_falsifier": None if med is None else med > cf["review_median_falsifier_minutes"],
                    "invalid_forms": sorted(r["pr"] for r in rows if r["review"] and not r["review"]["form_valid"]),
                    "decision_mismatches": sorted(r["pr"] for r in rows if r["review"]
                                                  and r["review"]["decision_matches_state"] is False)},
        "attribution": escapes["attribution"],
        "model": sorted({r["model"] for r in rows if r.get("model")}),
        "harness": sorted({canonical(r["harness"]) for r in rows if r.get("harness")}),
        "skill_releases": sorted({canonical(r["skill"]) for r in rows if r.get("skill")}),
        "verifier_versions": sorted({v for r in rows for v in r["verifier_versions"]}),
        "pins": {"model": cf["agent"]["model"], "harness": cf["agent"]["harness"],
                 "skill": {"tag": cf["skill"]["tag"], "commit": cf["skill"]["commit"]},
                 "builder_image": cf["agent"]["builder_image"]},
        "changes_after_switch_on": changes,
        "usage_totals": {"input_tokens": sum(r["usage"]["input_tokens"] or 0 for r in rows),
                         "output_tokens": sum(r["usage"]["output_tokens"] or 0 for r in rows),
                         "agent_minutes": round(sum(r["usage"]["agent_minutes"] or 0 for r in rows), 2),
                         "ci_minutes": round(sum(r["usage"]["ci_minutes"] or 0 for r in rows), 2)},
        "unknowns": unknowns,
        "fixtures": _fixtures(snap),
        "human_entered_fields": HUMAN_ENTERED_FIELDS,
        "review_form": REVIEW_FORM_NOTES,
        "not_run": {"escape_c": "spine-attributed incident: not run in M4 (ADR-0024 §2)",
                    "c25": "not run (ADR-0017)"},
        "prs": rows,
        "withheld_prs": withheld,
    }
    return out


def _closer_kind(login: str | None, logins: list[str], bot: str) -> str:
    if not login:
        return "unknown"
    if login == bot:
        return "merge-step"
    if login.lower() in {h.lower() for h in logins}:
        return "listed-human"
    return "other"


def _latest_event(snap: dict) -> str | None:
    times = []
    for p in snap["state"].get("prs", {}).values():
        if p.get("state") != "open":
            times += [p.get("merged_at"), p.get("closed_at")]
        times.append(p.get("created_at"))
    for e in snap["state"].get("ledger", []):
        times.append(e.get("acted_at"))
    for evs in (snap.get("events") or {}).values():
        times += [e.get("created_at") for e in evs]
    for r in snap["state"].get("class_records", []):
        times.append(r.get("merged_at"))
    times = [t for t in times if t]
    return max(times, key=_t) if times else None


def _row(snap, cf, n, rec, pr, events, reviews, seeds, near, merged_nums, seed_records, class_records,
         ledger, logins, bot, integ, cs_name, gt_name, median_cutoff) -> dict:
    kind = "seed" if n in seeds else "item"
    head = (pr.get("head") or {}).get("sha")
    commits = (snap.get("pr_commits") or {}).get(str(n)) or ([head] if head else [])
    # Every class-scope run on every commit of the pull request.
    inc: dict[str, int] = {}
    vv: set[str] = set()
    ci_seconds = 0.0
    for sha in commits:
        for c in check_runs_on(snap, sha, None, integ):
            if c.get("started_at") and c.get("completed_at"):
                ci_seconds += max(0.0, (_t(c["completed_at"]) - _t(c["started_at"])).total_seconds())
        for c in check_runs_on(snap, sha, cs_name, integ):
            v = cs_verdict(snap, c)
            if v.get("verifier_version"):
                vv.add(v["verifier_version"])
            if v["verdict"] == "inconclusive":
                for code in v["reason_codes"]:
                    if code != "cleared":
                        inc[code] = inc.get(code, 0) + 1
    cs_final = cs_verdict(snap, latest(check_runs_on(snap, head, cs_name, integ)))
    gt_final = gotest_verdict(latest(check_runs_on(snap, head, gt_name, integ)))
    labels = st.labels_in_force_from_events(events, logins)
    cleared = labels.get("human-cleared") if "cleared" in cs_final.get("reason_codes", []) else None

    if n in merged_nums:
        outcome = "merged"
    elif kind == "seed":
        outcome = "seed-closed"
    elif n in near:
        outcome = "near-miss"
    else:
        outcome = "closed"
    closed_by = (near.get(n) or {}).get("closed_by")
    if closed_by is None and pr.get("state") == "closed" and not pr.get("merged_at"):
        closes = [e for e in events if e.get("event") == "closed"]
        closed_by = (closes[-1].get("actor") or {}).get("login") if closes else None

    review = _review(snap, pr, events, reviews, logins, cs_name, gt_name, integ, median_cutoff, kind,
                     class_records, ledger)
    decision = review["effective_decision"] if review else None
    shadow = shadow_of(cs_final, gt_final)
    rung_open = rung_by_records(pr.get("created_at"), class_records, ledger)
    rung_review = (review or {}).get("rung") or rung_open
    usage = rec.get("usage") or {}
    cj = (snap.get("controls_jobs") or {}).get(f"{rec.get('controls_run_id')}-{rec.get('controls_run_attempt')}") or []
    ctl_seconds = sum(max(0.0, (_t(j["completed_at"]) - _t(j["started_at"])).total_seconds())
                      for j in cj if j.get("started_at") and j.get("completed_at"))
    srec = seed_records.get(n) or {}
    return {
        "pr": n, "kind": kind, "queue_position": rec.get("queue_position"), "module": rec.get("module"),
        "version": rec.get("version"), "opened_at": pr.get("created_at"), "closed_at": pr.get("closed_at"),
        "merged_at": pr.get("merged_at"), "outcome": outcome, "closed_by": closed_by,
        "closed_by_kind": _closer_kind(closed_by, logins, bot) if closed_by else None,
        "rung_at_open": rung_open, "head_sha": head,
        "class_scope_final": {k: cs_final.get(k) for k in ("verdict", "reason_class", "reason_codes", "check_run_id")},
        "go_test_final": gt_final,
        "inconclusive_reasons": sorted(inc), "inconclusive_runs_by_reason": dict(sorted(inc.items())),
        "cleared_by": cleared,
        "labels_unknown_events": len(labels.get("unknown_label_events", [])),
        "review": review,
        # The shadow verdict sits beside the person's decision when that
        # decision was made at L1 (the rung at the review, not at opening:
        # an item opened at L2 and reviewed after a freeze is an L1 review).
        "shadow": {"verdict": shadow, "decision": decision if rung_review == "L1" else None,
                   "at_l1": rung_review == "L1",
                   "agree": (None if rung_review != "L1" or decision is None else
                             (shadow == "merge") == (decision == "approve"))},
        "seed": (_seed_cell(srec, decision, review) if kind == "seed" else None),
        "usage": {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                  "agent_minutes": round(usage["agent_seconds"] / 60.0, 2) if isinstance(usage.get("agent_seconds"), (int, float)) else None,
                  "ci_minutes": round(ci_seconds / 60.0, 2),
                  "controls_run_minutes": round(ctl_seconds / 60.0, 2) if cj else None},
        "controls_run": {"id": rec.get("controls_run_id"), "attempt": rec.get("controls_run_attempt"),
                         "job": rec.get("job")},
        "model": rec.get("model"), "harness": rec.get("harness"), "skill": rec.get("skill"),
        "verifier_versions": sorted(vv),
    }


def _seed_cell(srec: dict, decision: str | None, review: dict | None) -> dict:
    """A seed's result. The opening job's seed record (`status`, `caught`) is
    used when present; otherwise the reviewer's decision on the pull request."""
    status = srec.get("status")
    void = bool(status and str(status).startswith("void")) or bool(srec.get("void") or srec.get("tampered"))
    approved = bool((review or {}).get("approved"))
    caught = srec.get("caught") if isinstance(srec.get("caught"), bool) else (decision == "close" and not approved)
    return {"type": srec.get("type"), "position": srec.get("position"), "status": status, "void": void,
            "caught": bool(caught) and not void, "approved": approved and not caught}


def _review(snap, pr, events, reviews, logins, cs_name, gt_name, integ, median_cutoff, kind,
            class_records, ledger) -> dict | None:
    """The listed human's decision on one pull request, and its minutes.

    The decision is the first listed-human review whose body is in the fixed
    form; without one, the first approving or commenting listed-human review,
    reported as an invalid form. Measured minutes run from the latest
    `reviewing` labeled event at or before the review to the review; the
    upper bound runs from when the pull request was ready (opened, and its
    required checks on the reviewed commit complete) to the review.
    """
    humans = {h.lower() for h in logins}
    mine = [r for r in sorted(reviews, key=lambda r: (r.get("submitted_at") or "", r.get("id") or 0))
            if (r.get("user") or {}).get("login", "").lower() in humans
            and r.get("state") in ("APPROVED", "COMMENTED", "CHANGES_REQUESTED", "DISMISSED")]
    if not mine:
        return None
    formed = [r for r in mine if parse_review_form(r.get("body")).get("form_valid")]
    r = formed[0] if formed else mine[0]
    form = parse_review_form(r.get("body"))
    at = r.get("submitted_at")
    lab = [t for t in human_labeled_times(events, logins, "reviewing") if _t(t) <= _t(at)]
    reviewing_at = lab[-1] if lab else None
    ready = [pr.get("created_at")]
    for name in (cs_name, gt_name):
        done = [c.get("completed_at") for c in check_runs_on(snap, r.get("commit_id"), name, integ)
                if c.get("completed_at") and _t(c["completed_at"]) <= _t(at)]
        if done:
            ready.append(max(done, key=_t))
    ready_at = max([x for x in ready if x], key=_t) if any(ready) else None
    state_decision = {"APPROVED": "approve", "COMMENTED": "close"}.get(r.get("state"))
    rung = rung_by_records(at, class_records, ledger)
    in_median = bool(kind == "item" and rung == "L1" and reviewing_at
                     and (median_cutoff is None or _t(at) <= _t(median_cutoff)))
    # The listed human's latest decision on the head, by the merge step's own
    # rule (ADV-16): a later changes-requested or `decision: close` undoes an
    # earlier approval.
    approved = open_pr.approved_on_head(reviews, logins, (pr.get("head") or {}).get("sha"))
    return {"by": (r.get("user") or {}).get("login"), "review_id": r.get("id"), "submitted_at": at,
            "state": r.get("state"), "commit_id": r.get("commit_id"), **form,
            "decision_matches_state": (None if not form["form_valid"] else form["decision"] == state_decision),
            # Without a valid form the review's own state stands in: an
            # approval is an approve; anything else is no decision.
            "effective_decision": form["decision"] if form["form_valid"] else (
                "approve" if r.get("state") == "APPROVED" else None),
            "reviewing_at": reviewing_at, "measured_minutes": _minutes(reviewing_at, at),
            "ready_at": ready_at, "upper_bound_minutes": _minutes(ready_at, at),
            "rung": rung, "in_median": in_median, "approved": approved,
            "listed_human_reviews": len(mine)}


def _streak(state: st.ClassState, cf: dict) -> dict:
    """The streak and its history: each merge and each reset in time order."""
    evs = [(st.parse_time(m["merged_at"]), 0, {"event": "merge", "pr": m["pr"], "at": m["merged_at"],
                                               "position": m.get("position")}) for m in state.merged]
    evs += [(st.parse_time(n["closed_at"]), 1, {"event": "reset", "cause": "near-miss", "pr": n["pr"],
                                                "at": n["closed_at"]}) for n in state.near_misses if n.get("closed_at")]
    evs += [(st.parse_time(e["event_time"]), 1, {"event": "reset", "cause": f"escape-{e['type']}", "pr": e.get("pr"),
                                                 "at": e["event_time"]}) for e in state.escapes if e.get("event_time")]
    ledger_a = {e.get("pr") for e in state.escapes if e.get("type") == "a"}
    evs += [(st.parse_time(e["event_time"]), 1, {"event": "reset", "cause": "escape-a", "pr": e["pr"],
                                                 "at": e["event_time"], "source": "push runs, not in the ledger"})
            for e in st.push_run_escapes(state.merged, state.escape_a) if e["pr"] not in ledger_a]
    evs += [(st.parse_time(c["date"]), 1, {"event": "reset", "cause": "verifier-change", "pr": None, "at": c["date"],
                                           "sha": c["sha"], "files": c["files"]})
            for c in state.verifier_changes]
    evs.sort(key=lambda x: (x[0], x[1], canonical(x[2])))
    history, s, best = [], 0, 0
    for _, _, e in evs:
        s = s + 1 if e["event"] == "merge" else 0
        best = max(best, s)
        history.append({**e, "streak": s})
    return {"current": state.streak, "start": state.streak_start, "max": best,
            "resets": [h for h in history if h["event"] == "reset"], "history": history,
            "promotable": bool(state.promotable)}


def _seeds(rows: list[dict], positions: list[int] | None, info: dict, state: st.ClassState) -> dict:
    seed_rows = [r for r in rows if r["kind"] == "seed"]
    caught = sorted(r["pr"] for r in seed_rows if r["seed"]["caught"] and not r["seed"]["void"])
    missed = sorted(r["pr"] for r in seed_rows if r["seed"]["approved"] and not r["seed"]["void"])
    void = sorted(r["pr"] for r in seed_rows if r["seed"]["void"])
    undecided = sorted(r["pr"] for r in seed_rows if r["pr"] not in caught + missed + void)
    not_run = skipped = None
    if positions is not None:
        # Not reached yet, or reached at L2, where no seed opens.
        skipped = list(state.seeds_skipped_at_l2)
        not_run = sorted({k for k in positions if k > len(state.merged)} | set(skipped))
    return {"caught": caught, "missed": missed, "void": void, "undecided": undecided,
            "not_run_positions": not_run, "skipped_at_l2_positions": skipped,
            "identified_by": "seed key" if positions is not None else "seed records",
            **info}


def _shadow_summary(rows: list[dict]) -> dict:
    l1 = [r for r in rows if r["shadow"]["at_l1"]]
    return {"l1_prs": len(l1),
            "agree": sorted(r["pr"] for r in l1 if r["shadow"]["agree"] is True),
            "disagree": sorted(r["pr"] for r in l1 if r["shadow"]["agree"] is False),
            "no_decision": sorted(r["pr"] for r in l1 if r["shadow"]["agree"] is None),
            "by_verdict": {v: sorted(r["pr"] for r in l1 if r["shadow"]["verdict"] == v)
                           for v in ("merge", "close", "wait")}}


def _escapes(snap, cf, state, ledger, prs, events, logins) -> dict:
    """Escapes by type, and C-17's attribution figures.

    Precision: detected escapes (ledger entries of type a or b) that were real
    or injected. A type (a) event is injected when attempt 1 of go-test failed
    at the injection step, and real when the scorecard's own reading of the
    push runs confirms it (failed verification, missing, cancelled or late);
    a type (b) event is confirmed when the listed human's escape:revert is in
    force on the reverting pull request by its events. Recall: injected
    escapes detected; the injected set is read from the record, not from the
    ledger. Delay: minutes from the trusted event to the ruleset's new version
    (the ratchet's read-back), or to the ledger entry when the rung was
    already at 1. When the ratchet's write failed, the first later retry that
    raised the rung (a `ratchet-retry` ledger entry) gives the new version.
    """
    job = cf["checks"]["go_test"]["job"]
    minutes = cf["escape_a_minutes"]
    by_type: dict[str, list] = {"a": [], "b": []}
    detected_a, detected_b = {}, {}
    raised = ("raised", "raised-unverified")
    retries: dict[str, list] = {}
    for x in ledger:
        if x.get("type") == "ratchet-retry":
            retries.setdefault(x.get("retry_of"), []).append(x)
    for e in sorted((x for x in ledger if x.get("type") in ("a", "b")), key=lambda x: x.get("key") or ""):
        ev = e.get("event") or {}
        rat = e.get("ratchet") or {}
        freeze_at = rat.get("after_updated_at") if rat.get("result") in raised else None
        delay_to = "ruleset version" if freeze_at else "ledger entry"
        tried = sorted(retries.get(e.get("key"), []), key=lambda x: (_t(x.get("acted_at")) or _t(e.get("acted_at")),
                                                                      x.get("key") or ""))
        not_raised = False
        if freeze_at is None and rat.get("result") == "write-failed":
            ok = [x for x in tried if (x.get("ratchet") or {}).get("result") in raised]
            if ok:
                freeze_at = ok[0]["ratchet"].get("after_updated_at")
                delay_to = "ruleset version, after a retry"
            else:
                # The rung was never raised: there is no freeze to time.
                not_raised, delay_to = True, "not raised: the write failed and no retry raised it"
        item = {"key": e.get("key"), "pr": ev.get("pr"), "event_time": ev.get("event_time"),
                "acted_at": e.get("acted_at"), "phase": e.get("phase"), "ratchet_result": rat.get("result"),
                "ratchet_retries": len(tried), "freeze_at": freeze_at,
                "delay_minutes": None if not_raised else _minutes(ev.get("event_time"), freeze_at or e.get("acted_at")),
                "delay_to": delay_to, "not_raised": not_raised,
                "labelled_needs_human": e.get("labelled_needs_human", [])}
        if e["type"] == "a":
            runs = (snap.get("push_runs") or {}).get(ev.get("merge_commit_sha") or "", [])
            cls = {"cause": "missing-run", "failed_step": None}
            for r in runs:
                c = classify_attempt1(r.get("jobs", []), job)
                if c["cause"] == "injected" or cls["cause"] == "missing-run":
                    cls = c
            merged_at = ev.get("merged_at") or (prs.get(ev.get("pr")) or {}).get("merged_at")
            confirm = None
            if merged_at:
                runs_for = [{"run": r["run"], "jobs": r.get("jobs", [])} for r in runs]
                # The scorecard re-reads the rule at the deadline: no clock.
                deadline = st.parse_time(merged_at) + timedelta(minutes=minutes)
                confirm = _escape_a_recheck(merged_at, ev.get("merge_commit_sha"), runs_for, deadline, minutes, job)
            item.update(cause=cls["cause"], failed_step=cls["failed_step"],
                        injected=cls["cause"] == "injected",
                        confirmed=confirm == "escape",
                        true_positive=cls["cause"] == "injected" or confirm == "escape")
            detected_a[ev.get("pr")] = item
        else:
            lab = st.labels_in_force_from_events(events.get(ev.get("pr"), []), logins).get("escape:revert")
            item.update(reverted_pr=ev.get("reverted_pr"), label=ev.get("label"),
                        confirmed=bool(lab), injected=None, true_positive=bool(lab))
            detected_b[ev.get("pr")] = item
        by_type[e["type"]].append(item)

    # Injected (a): merged queue items whose push run's attempt 1 failed at the
    # injection step. Injected (b): merged reverts carrying the listed human's
    # escape:revert (a person stages every one in M4).
    inj_a = []
    for m in state.merged:
        for r in (snap.get("push_runs") or {}).get(m.get("merge_commit_sha") or "", []):
            if classify_attempt1(r.get("jobs", []), job)["cause"] == "injected":
                inj_a.append(m["pr"])
                break
    inj_b = []
    qnums = set(state.queue_item_prs) | set(state.fixture_prs)
    first = st.parse_time(state.merged[0]["merged_at"]) if state.merged else None
    for n, pr in sorted(prs.items()):
        if n in qnums or not pr.get("merged_at") or first is None or st.parse_time(pr["merged_at"]) <= first:
            continue
        if st.labels_in_force_from_events(events.get(n, []), logins).get("escape:revert"):
            inj_b.append(n)
    detected = by_type["a"] + by_type["b"]
    tp = [d for d in detected if d["true_positive"]]
    found_a = [n for n in inj_a if n in detected_a]
    found_b = [n for n in inj_b if n in detected_b]
    delays = [d["delay_minutes"] for d in detected if d["delay_minutes"] is not None]
    unraised = [d["key"] for d in detected if d.get("not_raised")]
    attribution = {
        "detected": len(detected), "true_positives": len(tp),
        "precision": round(len(tp) / len(detected), 4) if detected else None,
        "injected": {"a": inj_a, "b": inj_b},
        "detected_injected": {"a": found_a, "b": found_b},
        "recall": round((len(found_a) + len(found_b)) / (len(inj_a) + len(inj_b)), 4) if (inj_a or inj_b) else None,
        "recall_by_type": {"a": round(len(found_a) / len(inj_a), 4) if inj_a else None,
                           "b": round(len(found_b) / len(inj_b), 4) if inj_b else None},
        "delay_minutes": {"each": {d["key"]: d["delay_minutes"] for d in detected},
                          "max": max(delays) if delays else None, "median": _median(delays),
                          "pass_line": cf["freeze_pass_minutes"], "not_raised": unraised,
                          "within_pass_line": (not unraised and all(x <= cf["freeze_pass_minutes"] for x in delays))
                          if (delays or unraised) else None},
    }
    return {"by_type": {"a": by_type["a"], "b": by_type["b"], "c": "not run in M4 (ADR-0024 §2)",
                        "counts": {"a": len(by_type["a"]), "b": len(by_type["b"])}},
            "attribution": attribution}


def _escape_a_recheck(merged_at, sha, runs, deadline, minutes, job) -> str:
    from . import detector  # the same pure rule the detector applies
    return detector.escape_a_status(merged_at, sha, runs, deadline, minutes, job)["status"]


def _machinery_label_unknowns(events: list[dict], seed_record: dict | None = None) -> list[dict]:
    """An App applying or removing `seeded`, or removing `needs-human` (C-21's
    listed attempts): neither label is read by anything, but each such act is
    recorded as an unknown. Two acts are the machinery's own and are not
    listed: class-scope's needs-human writes (the workflow's token,
    github-actions), and the settlement step's `seeded`, applied with the
    agent App's token after it wrote the pull request's seed record (so at or
    after the record's settled_at)."""
    settled = st.parse_time((seed_record or {}).get("settled_at"))
    out = []
    for e in sorted(events, key=lambda e: (e.get("created_at") or "", e.get("id") or 0)):
        name = (e.get("label") or {}).get("name")
        app = (e.get("performed_via_github_app") or {}).get("slug")
        actor = (e.get("actor") or {}).get("login", "")
        if e.get("event") not in ("labeled", "unlabeled") or name not in st.MACHINERY_LABELS:
            continue
        if not app or app == "github-actions" or actor == "github-actions[bot]":
            continue
        t = st.parse_time(e.get("created_at"))
        if name == "seeded" and e.get("event") == "labeled" and settled and t and t >= settled:
            continue
        if (name, e.get("event")) in (("seeded", "labeled"), ("needs-human", "unlabeled"), ("seeded", "unlabeled")):
            out.append({"event": e.get("event"), "label": name, "actor": actor, "app": app,
                        "at": e.get("created_at"), "id": e.get("id")})
    return out


def _unknowns(snap, rows, events, prs, logins, state) -> dict:
    """Events the detector could not classify: label events on closed or merged
    pull requests that do not count (not the listed human, or made through an
    App), an App's acts on the machinery labels `seeded` and `needs-human`,
    the detector's own latest unknowns, unusable class records, and tampers
    the state found (a queue-item pull request retargeted away from main)."""
    label = []
    for n in sorted(events):
        if (prs.get(n) or {}).get("state") == "open":
            continue
        for u in st.labels_in_force_from_events(events[n], logins).get("unknown_label_events", []):
            label.append({"pr": n, **u})
        for u in _machinery_label_unknowns(events[n], (snap.get("seed_records") or {}).get(str(n))):
            label.append({"pr": n, **u})
    hb = snap.get("heartbeat") or {}
    det = [u for u in hb.get("unknowns", []) if u.get("type") != "label-event-not-counted"]
    return {"label_events": label, "detector": det, "class_records": list(state.unknowns),
            "count": len(label) + len(det) + len(state.unknowns)}


def _switch_on_time(snap: dict) -> str | None:
    on = sorted(SWITCH_ON_RE.match(n).group(1) for n in snap.get("ruleset_records") or [] if SWITCH_ON_RE.match(n))
    if not on:
        return None
    t0 = on[0]
    return f"{t0[0:4]}-{t0[4:6]}-{t0[6:8]}T{t0[9:11]}:{t0[11:13]}:{t0[13:15]}Z"


def _changes_after_switch_on(snap: dict, ledger: list[dict]) -> dict:
    names = sorted(snap.get("ruleset_records") or [])
    on = [SWITCH_ON_RE.match(n).group(1) for n in names if SWITCH_ON_RE.match(n)]
    if not on:
        return {"switch_on_at": None, "note": "no switch-on record found in platform-factory/.github "
                "rulesets/svc-hello/ (file name ending -after-l1-switch-on.json)",
                "ruleset_records": [], "ruleset_events": [], "svc_commits": [], "controls_commits": []}
    t0 = on[0]
    switch_on = f"{t0[0:4]}-{t0[4:6]}-{t0[6:8]}T{t0[9:11]}:{t0[11:13]}:{t0[13:15]}Z"
    recs = []
    for n in names:
        m = RULESET_RECORD_RE.match(n)
        if m and m.group(1) > t0:
            recs.append({"file": n, "step": int(m.group(2)), "kind": m.group(3), "change": m.group(4)})
    rule_events = [{"key": e.get("key"), "acted_at": e.get("acted_at"),
                    "ratchet_result": (e.get("ratchet") or {}).get("result"), "issue": e.get("issue")}
                   for e in ledger if e.get("type") == "ruleset"]

    def commits(key, interesting):
        out = []
        for c in snap.get(key) or []:
            if _t(c.get("date")) and _t(c["date"]) > _t(switch_on):
                files = [f for f in c.get("files", []) if interesting(f)]
                if files:
                    out.append({"sha": c["sha"], "date": c["date"], "title": c.get("title"), "files": files})
        return sorted(out, key=lambda x: (x["date"], x["sha"]))

    return {"switch_on_at": switch_on, "ruleset_records": recs,
            "ruleset_events": sorted(rule_events, key=lambda x: x["key"] or ""),
            "svc_commits": commits("svc_commits", lambda f: f not in ("go.mod", "go.sum")),
            "verifier_changes_resetting_the_streak": st.verifier_changes(snap.get("svc_commits") or [], switch_on),
            "controls_commits": commits("controls_commits",
                                        lambda f: f.startswith(("factory/", ".github/", "classes/")))}


def _fixtures(snap: dict) -> list[dict]:
    return sorted(({k: f.get(k) for k in ("id", "case", "claim", "outcome", "pr_number", "finished_at")}
                   for f in snap.get("fixtures") or []), key=lambda f: f.get("id") or "")


# -- Markdown ------------------------------------------------------------------

def render_markdown(sc: dict) -> str:
    """A reader's view of the JSON. Every number here is in the JSON."""
    L = [f"# Scorecard: {sc['class']} on {sc['repo']}", ""]
    L.append(f"Inputs SHA-256 `{sc['inputs_sha256']}`; class file commit `{sc['class_file_commit']}`; "
             f"main `{sc['as_of']['main_sha']}`; latest event {sc['as_of']['latest_event_at']}.")
    L.append("")
    L.append("Computed from GitHub's record, the records branch and the class file. Rows for pull requests "
             "still open are withheld until they close or merge.")
    L += ["", "## Rung and streak", ""]
    r, s = sc["rung"], sc["streak"]
    L.append(f"- Rung: live rung entry at {r['live_minimum_approvals']} ({r['live_level']}); "
             f"by the records: {r['by_records_now']}; L2 accepted: {r['l2_accepted']}.")
    L.append(f"- N = {sc['n']}. Streak {s['current']} (since {s['start']}), longest {s['max']}, "
             f"resets {len(s['resets'])}. Promotion due: {sc['promotion']['due']}.")
    for x in s["resets"]:
        L.append(f"  - reset at {x['at']}: {x['cause']} (#{x['pr']})")
    c = sc["changes"]
    L += ["", "## Changes", "",
          f"- Queue-item pull requests {c['queue_item_prs']}, merged {c['merged']}, near-misses "
          f"{c['near_misses']}, seeds {c['seeds']}, open (withheld) {len(c['open'])}.",
          f"- First agent pull request {c['first_agent_pr_at']}; window closes {c['window_closes_at']}."]
    e, a = sc["escapes"], sc["attribution"]
    L += ["", "## Escapes and attribution", "",
          f"- Type (a): {e['counts']['a']}; type (b): {e['counts']['b']}; type (c): {e['c']}.",
          f"- Precision {a['precision']} ({a['true_positives']} of {a['detected']}); recall {a['recall']}; "
          f"delay max {a['delay_minutes']['max']} min (pass line {a['delay_minutes']['pass_line']}); "
          f"unknowns {sc['unknowns']['count']}."]
    for t in ("a", "b"):
        for x in e[t]:
            L.append(f"  - {x['key']}: event {x['event_time']}, freeze {x['freeze_at'] or x['acted_at']}, "
                     f"{x['delay_minutes']} min, ratchet {x['ratchet_result']}")
    sd = sc["seeds"]
    L += ["", "## Seeds, reviews, shadow verdicts", "",
          f"- Seeds caught {sd['caught']}, missed {sd['missed']}, void {sd['void']}, undecided "
          f"{sd['undecided']}, not run {sd['not_run_positions']} (identified by {sd['identified_by']}).",
          f"- Review median {sc['reviews']['median_measured_minutes']} min measured, "
          f"{sc['reviews']['median_entered_minutes']} entered, {sc['reviews']['median_upper_bound_minutes']} "
          f"upper bound, over {sc['reviews']['count_in_median']} (pass line "
          f"{sc['reviews']['pass_line_minutes']}, falsifier {sc['reviews']['falsifier_minutes']}).",
          f"- Shadow verdict agrees on {len(sc['shadow_verdicts']['agree'])}, disagrees on "
          f"{sc['shadow_verdicts']['disagree']}."]
    inc = sc["inconclusives"]
    L += ["", "## Inconclusives by reason", ""]
    for k, v in inc["by_reason_runs"].items():
        L.append(f"- {k}: {v} run(s) on {inc['by_reason_prs'][k]}")
    if not inc["by_reason_runs"]:
        L.append("- none")
    L += ["", "## Pull requests (closed or merged)", "",
          "| PR | kind | item | outcome | rung | class-scope | go-test | shadow | decision | minutes (label) | "
          "minutes (entered) | tokens in/out | agent min | CI min |",
          "|---:|---|---|---|---|---|---|---|---|---:|---:|---|---:|---:|"]
    for x in sc["prs"]:
        rv = x["review"] or {}
        L.append(f"| #{x['pr']} | {x['kind']} | {x['queue_position']} {x['module']} {x['version']} | "
                 f"{x['outcome']} | {x['rung_at_open']} | {x['class_scope_final']['verdict']} | "
                 f"{x['go_test_final']} | {x['shadow']['verdict']} | {rv.get('decision')} | "
                 f"{rv.get('measured_minutes')} | {rv.get('entered_minutes')} | "
                 f"{x['usage']['input_tokens']}/{x['usage']['output_tokens']} | {x['usage']['agent_minutes']} | "
                 f"{x['usage']['ci_minutes']} |")
    ch = sc["changes_after_switch_on"]
    L += ["", "## Lifts and other changes after the switch-on", "", f"- Switch-on: {ch['switch_on_at']}"]
    for x in ch["ruleset_records"]:
        L.append(f"- ruleset record {x['file']}")
    for x in ch["ruleset_events"]:
        L.append(f"- ruleset difference {x['key']} at {x['acted_at']} (ratchet {x['ratchet_result']})")
    for k in ("svc_commits", "controls_commits"):
        for x in ch[k]:
            L.append(f"- {k.split('_')[0]} {x['sha'][:12]} {x['date']}: {', '.join(x['files'])}")
    L += ["", "## Pins", "", f"- Model {sc['model'] or sc['pins']['model']}; harness {sc['harness']}; "
          f"skill {sc['skill_releases']}; verifier {sc['verifier_versions']}."]
    L += ["", "## Human-entered fields (declared before the run)", ""]
    for f in sc["human_entered_fields"]:
        L.append(f"- `{f['field']}`: {f['source']} ({f['kind']}; {f['why']})")
    L.append(f"- {sc['review_form']}")
    return "\n".join(L) + "\n"


# -- gather (I/O) -------------------------------------------------------------

def _slim_pr(p: dict) -> dict:
    return {"number": p["number"], "state": p.get("state"), "created_at": p.get("created_at"),
            "closed_at": p.get("closed_at"), "merged_at": p.get("merged_at"),
            "merge_commit_sha": p.get("merge_commit_sha") if p.get("merged_at") else None,
            "head": {"sha": (p.get("head") or {}).get("sha"), "ref": (p.get("head") or {}).get("ref")},
            "base": {"ref": (p.get("base") or {}).get("ref")},
            "user": {"login": (p.get("user") or {}).get("login")}}


def _slim_event(e: dict) -> dict:
    out = {"id": e.get("id"), "event": e.get("event"), "created_at": e.get("created_at"),
           "actor": {"login": (e.get("actor") or {}).get("login")},
           "performed_via_github_app": ({"slug": e["performed_via_github_app"].get("slug")}
                                        if e.get("performed_via_github_app") else None)}
    if e.get("label"):
        out["label"] = {"name": e["label"].get("name")}
    return out


def _slim_check(c: dict) -> dict:
    m = st._DETAILS.search(c.get("details_url") or c.get("html_url") or "")
    return {"id": c.get("id"), "name": c.get("name"), "app_id": (c.get("app") or {}).get("id"),
            "head_sha": c.get("head_sha"), "status": c.get("status"), "conclusion": c.get("conclusion"),
            "started_at": c.get("started_at"), "completed_at": c.get("completed_at"),
            "run_id": int(m.group(1)) if m else None, "job_id": int(m.group(2)) if m else c.get("id")}


def _slim_job(j: dict) -> dict:
    return {"id": j.get("id"), "run_id": j.get("run_id"), "name": j.get("name"), "status": j.get("status"),
            "conclusion": j.get("conclusion"), "run_attempt": j.get("run_attempt", 1),
            "started_at": j.get("started_at"), "completed_at": j.get("completed_at"),
            "steps": [{"name": s.get("name"), "conclusion": s.get("conclusion")} for s in j.get("steps", [])]}


def _try(fn, default):
    try:
        return fn()
    except ApiError as e:
        if e.status in (403, 404, 410):
            return default
        raise


def gather(gh_svc, records: Records, cf: dict, *, controls_repo: str = CONTROLS_REPO,
           org_github_repo: str = ORG_GITHUB_REPO, meta: dict | None = None, now: datetime | None = None) -> dict:
    """Every read the scorecard needs, as one snapshot. Nothing per pull
    request is read for a pull request that is still open."""
    svc = cf["repo"]
    gh_c = records.gh
    integ = cf["checks"]["integration_id"]
    cs_name = cf["checks"]["class_scope"]["name"]
    full_events: dict[int, list] = {}

    def read_events(n: int) -> list:
        # One read per pull request: the state's close events and the
        # scorecard's own rows come from the same answer.
        if n not in full_events:
            full_events[n] = gh_svc.paginate(f"repos/{svc}/issues/{n}/events")
        return full_events[n]

    base = st.gather(gh_svc, records, cf, controls_repo=controls_repo, read_events=read_events)
    base["prs"] = {str(n): _slim_pr(p) for n, p in base["prs"].items()}
    base["close_events"] = {str(n): [_slim_event(e) for e in v] for n, v in base["close_events"].items()}
    # as_of is part of the inputs: the escape (a) rule needs the time of the
    # read to tell an open window from a missing run.
    snap: dict[str, Any] = {"schema": SCHEMA, "as_of": st.iso(now or st.utcnow()), "state": base, "events": {},
                            "reviews": {}, "pr_commits": {}, "check_runs": {}, "cs_jobs": {}, "cs_verdicts": {},
                            "push_runs": {}, "controls_jobs": {}}
    closed = sorted(int(n) for n, p in base["prs"].items() if p["state"] != "open")
    qnums = {r.get("pr_number") for r in base["run_records"]}

    def add_checks(sha):
        if not sha or sha in snap["check_runs"]:
            return
        crs = [_slim_check(c) for c in gh_svc.paginate(f"repos/{svc}/commits/{sha}/check-runs", filter="all")]
        snap["check_runs"][sha] = sorted(crs, key=lambda c: c["id"] or 0)
        for c in crs:
            if c["name"] == cs_name and c["app_id"] == integ and c["job_id"]:
                jid = str(c["job_id"])
                if jid not in snap["cs_jobs"]:
                    snap["cs_jobs"][jid] = _try(lambda: _slim_job(gh_svc.get(f"repos/{svc}/actions/jobs/{c['job_id']}")), None)
                cid = str(c["id"])
                if cid not in snap["cs_verdicts"] and c["status"] == "completed":
                    ann = st.read_verdict_annotation(gh_svc, svc, c["id"])
                    art = ann["verdict"]
                    snap["cs_verdicts"][cid] = ({k: art.get(k) for k in ("verdict", "reason_class", "reason_codes",
                                                                         "pass_kind", "verifier_version", "pr",
                                                                         "head_sha", "decided_at")}
                                                if isinstance(art, dict) else {"error": ann["error"]})

    for n in closed:
        snap["events"][str(n)] = [_slim_event(e) for e in read_events(n)]
        if n in qnums:
            snap["reviews"][str(n)] = [
                {"id": r.get("id"), "user": {"login": (r.get("user") or {}).get("login")}, "state": r.get("state"),
                 "submitted_at": r.get("submitted_at"), "commit_id": r.get("commit_id"), "body": r.get("body")}
                for r in gh_svc.paginate(f"repos/{svc}/pulls/{n}/reviews")]
            shas = [c["sha"] for c in gh_svc.paginate(f"repos/{svc}/pulls/{n}/commits")]
            snap["pr_commits"][str(n)] = shas
            for sha in shas + [base["prs"][str(n)]["head"]["sha"]]:
                add_checks(sha)
    wf, job = cf["checks"]["go_test"]["workflow"], cf["checks"]["go_test"]["job"]
    merge_shas = {p["merge_commit_sha"] for n, p in base["prs"].items()
                  if int(n) in qnums and p.get("merge_commit_sha")}
    for e in base["ledger"]:
        if e.get("type") == "a" and (e.get("event") or {}).get("merge_commit_sha"):
            merge_shas.add(e["event"]["merge_commit_sha"])
    for sha in sorted(merge_shas):
        out = []
        for run in gh_svc.paginate(f"repos/{svc}/actions/workflows/{wf}/runs", event="push", head_sha=sha):
            jobs = gh_svc.paginate(f"repos/{svc}/actions/runs/{run['id']}/attempts/1/jobs")
            out.append({"run": {k: run.get(k) for k in ("id", "event", "head_sha", "status", "conclusion",
                                                          "created_at", "updated_at", "run_attempt")},
                        "jobs": [_slim_job(j) for j in jobs]})
        snap["push_runs"][sha] = sorted(out, key=lambda r: r["run"]["id"] or 0)
    for r in base["run_records"]:
        rid, att = r.get("controls_run_id"), r.get("controls_run_attempt") or 1
        n = r.get("pr_number")
        if rid and str(n) in base["prs"] and base["prs"][str(n)]["state"] != "open":
            snap["controls_jobs"][f"{rid}-{att}"] = _try(lambda: [
                _slim_job(j) for j in gh_c.paginate(f"repos/{controls_repo}/actions/runs/{rid}/attempts/{att}/jobs")], [])
    snap["heartbeat"] = records.read("detector/heartbeat.json")
    snap["seed_records"] = {re.match(r"^seeds/(\d+)\.json$", p).group(1): records.read(p) or {}
                            for p in records.list("seeds/") if re.match(r"^seeds/(\d+)\.json$", p)}
    snap["seed_key"] = records.read("close/seed-key.json")
    snap["fixtures"] = []
    for p in records.list("fixtures/"):
        f = records.read(p) or {}
        snap["fixtures"].append({k: f.get(k) for k in ("id", "case", "claim", "outcome", "pr_number", "finished_at")})
    listing = _try(lambda: gh_c.get(f"repos/{org_github_repo}/contents/rulesets/svc-hello"), [])
    snap["ruleset_records"] = sorted(x["name"] for x in listing or [] if isinstance(x, dict) and x.get("type") == "file")
    on = sorted(SWITCH_ON_RE.match(n).group(1) for n in snap["ruleset_records"] if SWITCH_ON_RE.match(n))
    snap["svc_commits"], snap["controls_commits"] = [], []
    if on:
        t0 = on[0]
        since = f"{t0[0:4]}-{t0[4:6]}-{t0[6:8]}T{t0[9:11]}:{t0[11:13]}:{t0[13:15]}Z"
        for key, client, repo in (("svc_commits", gh_svc, svc), ("controls_commits", gh_c, controls_repo)):
            for c in client.paginate(f"repos/{repo}/commits", sha="main", since=since):
                full = client.get(f"repos/{repo}/commits/{c['sha']}")
                snap[key].append({"sha": c["sha"], "date": (full.get("commit") or {}).get("committer", {}).get("date"),
                                  "title": ((full.get("commit") or {}).get("message") or "").split("\n", 1)[0],
                                  "files": sorted(f.get("filename") for f in full.get("files", []))})
    cfc = _try(lambda: gh_c.paginate(f"repos/{controls_repo}/commits", sha="main",
                                     path="classes/go-module-bump.yaml"), [])
    snap["class_file_commit"] = cfc[0]["sha"] if cfc else None
    snap["meta"] = meta or {}
    return snap


# -- promotion record ------------------------------------------------------------

def promotion_record(sc: dict, cf: dict) -> tuple[str, str, str]:
    """(branch, path, YAML text) of the promotion record for the current streak.

    Keyed by the streak's first merge, so later runs during the same streak
    find the same branch and write nothing new. Strings are written as JSON
    strings, which YAML reads as double-quoted scalars.
    """
    prs = sc["promotion"]["streak_prs"]
    first, end = prs[0], prs[-1]
    branch = f"promotion/{cf['class']}-from-pr{first}"
    path = f"classes/records/promotion-{cf['class']}-pr{end}.yaml"
    q = json.dumps
    lines = [
        "# A promotion record: the frozen rule's streak of N clean merges, written by the scorecard job.",
        "# The listed human's approval of the pull request that adds this file is the signature;",
        "# the owner then applies the one-field ruleset edit by hand, recorded before and after.",
        "schema: 1",
        "kind: promotion",
        f"class: {q(cf['class'])}",
        f"n: {cf['n']}",
        f"streak_start: {q(sc['streak']['start'])}",
        f"streak_end_pr: {end}",
        "streak_prs: [" + ", ".join(str(p) for p in prs) + "]",
        f"scorecard: {q('records:scorecard/' + cf['class'] + '.json')}",
        f"scorecard_inputs_sha256: {q(sc['inputs_sha256'])}",
        f"class_file_commit: {q(sc['class_file_commit'])}",
        f"ruleset_change: {q('rung entry minimum_approvals 1 -> 0 (rulesets/svc-hello/l1.json -> l2.json)')}",
        f"l1_sha256: {q(cf['rulesets']['l1_sha256'])}",
        f"l2_sha256: {q(cf['rulesets']['l2_sha256'])}",
    ]
    return branch, path, "\n".join(lines) + "\n"


def write_promotion(gh_c, controls_repo: str, sc: dict, cf: dict) -> dict:
    """Write the record to its branch (once) and open the issue (once)."""
    import base64
    from . import ratchet
    branch, path, text = promotion_record(sc, cf)
    out: dict[str, Any] = {"branch": branch, "path": path}
    existing = _try(lambda: gh_c.get(f"repos/{controls_repo}/git/ref/heads/{branch}"), None)
    if existing is None:
        main = gh_c.get(f"repos/{controls_repo}/git/ref/heads/main")["object"]["sha"]
        gh_c.post(f"repos/{controls_repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": main})
        out["branch_created_from"] = main
    have = _try(lambda: gh_c.get(f"repos/{controls_repo}/contents/{path}", ref=branch), None)
    if have is None:
        gh_c.put(f"repos/{controls_repo}/contents/{path}",
                 {"message": f"promotion record: {cf['class']} streak of {cf['n']} ending at #{sc['promotion']['streak_prs'][-1]}",
                  "branch": branch, "content": base64.b64encode(text.encode()).decode()})
        out["written"] = True
    else:
        out["written"] = False
    title = f"promotion: {cf['class']} reached {cf['n']} clean merges (streak from #{sc['promotion']['streak_prs'][0]})"
    body = (f"The scorecard counted {cf['n']} consecutive clean merges under the frozen rule "
            f"(pull requests {', '.join('#' + str(p) for p in sc['promotion']['streak_prs'])}).\n\n"
            f"The promotion record is on branch `{branch}` at `{path}`. The delegate opens the pull request "
            f"that adds it to `main`, linking the scorecard (inputs SHA-256 `{sc['inputs_sha256']}`) and the "
            f"ruleset diff it authorises (rung entry `minimum_approvals` 1 -> 0). The listed human approves "
            f"and merges it; the owner then applies the edit by hand, recorded before and after "
            f"(docs/RUNBOOK.md, step 12).")
    out["issue"] = ratchet.open_issue(gh_c, controls_repo, title, body)
    return out


# -- CLI -----------------------------------------------------------------------

def write_outputs(sc: dict, snap: dict, out_dir: Path, cls: str) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {f"{cls}.json": dumps(sc), f"{cls}.md": render_markdown(sc), f"{cls}.inputs.json": dumps(withhold(snap))}
    for name, text in files.items():
        (out_dir / name).write_text(text)
    return files


# A live scorecard run does not start with fewer requests than this left in
# the hour on the token it reads with. The workflow's GITHUB_TOKEN may make
# 1,000 requests an hour for the whole repository, and the detector, the
# merge step and the open job share it. A scorecard read grows with the
# number of closed pull requests (about 150 early, perhaps 500 late in the
# run); the machinery that holds the rung needs about 80 an hour. Deferring
# is harmless: nothing is written, so `scorecard_due` stays true and the
# next agent run tries again.
MIN_REQUESTS_LEFT = 600


def requests_left(gh) -> int | None:
    """Requests left this hour on `gh`'s token, or None when GitHub does not
    say. `GET /rate_limit` does not count against the limit (GitHub's REST
    documentation)."""
    try:
        res = gh.get("rate_limit")
    except ApiError:
        return None
    core = ((res or {}).get("resources") or {}).get("core") if isinstance(res, dict) else None
    left = (core or {}).get("remaining")
    return left if isinstance(left, int) and not isinstance(left, bool) else None


def main(argv: list[str] | None = None, env: dict | None = None) -> int:
    env = dict(os.environ if env is None else env)
    ap = argparse.ArgumentParser(prog="python3 -m factory.scorecard")
    ap.add_argument("--out", required=True, help="directory for the JSON, Markdown and inputs files")
    ap.add_argument("--snapshot", help="recompute from a saved inputs file; no reads, no writes")
    ap.add_argument("--dry-run", action="store_true", help="read live state, write nothing to GitHub")
    ap.add_argument("--allow-placeholders", action="store_true")
    args = ap.parse_args(argv)
    try:
        cf = classfile.require(env, {"human_logins[0]"},
                               allow_placeholders=args.allow_placeholders and bool(args.dry_run or args.snapshot))
    except classfile.NotReady as e:
        return classfile.report_not_ready(e.reason, {"phase": "scorecard"}, None, env)
    l1, l2 = st.load_committed_rulesets(cf)
    controls_repo = env.get("CONTROLS_REPO", CONTROLS_REPO)
    if args.snapshot:
        snap = json.loads(Path(args.snapshot).read_text())
        sc = compute(snap, cf, l1, l2)
        write_outputs(sc, snap, Path(args.out), cf["class"])
        print(json.dumps({"inputs_sha256": sc["inputs_sha256"], "out": args.out, "recomputed": True}))
        return 0
    token_c = env.get("CONTROLS_TOKEN")
    token_s = env.get("SVC_TOKEN") or token_c
    if not token_c:
        print(json.dumps({"error": "CONTROLS_TOKEN is required"}))
        return 2
    gh_s: Any = GitHub(token_s)
    gh_c: Any = GitHub(token_c)
    if args.dry_run:
        gh_s, gh_c = DryRunGitHub(gh_s), DryRunGitHub(gh_c)
    left = requests_left(gh_s)
    if left is not None and left < MIN_REQUESTS_LEFT:
        print(f"::notice title=scorecard deferred::{left} requests left this hour on the reading token, fewer "
              f"than {MIN_REQUESTS_LEFT}; nothing read or written, the next due run tries again", file=sys.stderr)
        print(json.dumps({"deferred": True, "requests_left": left, "min_requests_left": MIN_REQUESTS_LEFT}))
        return 0
    records = Records(gh_c, controls_repo, branch_from_env(env))
    meta = {"run_id": env.get("GITHUB_RUN_ID"), "run_attempt": env.get("GITHUB_RUN_ATTEMPT"),
            "read_at": st.iso(st.utcnow())}
    snap = gather(gh_s, records, cf, controls_repo=controls_repo, meta=meta)
    sc = compute(snap, cf, l1, l2)
    files = write_outputs(sc, snap, Path(args.out), cf["class"])
    result: dict[str, Any] = {"inputs_sha256": sc["inputs_sha256"], "out": args.out,
                              "streak": sc["streak"]["current"], "promotion_due": sc["promotion"]["due"]}
    # The scorecard files are replaced in place on the records branch (with the
    # heartbeat, the only files that are; docs/RECORDS.md). Under --dry-run
    # the controls client records the writes instead of sending them.
    for name, text in files.items():
        records.replace_text(f"scorecard/{name}", text, f"scorecard: {name}")
    if sc["promotion"]["due"]:
        result["promotion"] = write_promotion(gh_c, controls_repo, sc, cf)
    if args.dry_run:
        result["dry_run_writes"] = list(gh_c.writes) + list(gh_s.writes)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
