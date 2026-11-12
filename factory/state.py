"""Derive the class's state from GitHub's record and the records branch.

Nothing here keeps its own state between runs. The streak, the next queue
item, the open agent pull request and the rung are recomputed each time from
what GitHub and the records say, so a person can recompute them too and a
dropped run loses nothing.

The rules, from the readiness note's "The frozen numbers":

- A queue-item pull request is one whose number is in a run record
  (`runs/*.json`) written for this class's repository (the record's `repo`)
  and whose head branch is the record's `branch`. A record for another
  repository (probe P4 runs the same workflow against the throwaway
  repository) is ignored, so a colliding pull request number changes
  nothing. A fixture is one in `fixtures/*.json`. A seed is the first
  queue-item pull request opened after the k-th merge, for each drawn k
  (only a holder of SEED_KEY can compute this before the reveal), or one with
  a `seeds/<n>.json` record. None of these is ever read from a label, title
  or branch.
- A near-miss is a non-seed queue-item pull request closed unmerged, by
  anyone, read from its first `closed` issue event: a close that is later
  reopened still counts, so a close is never a free retry; it counts once per
  pull request. A queue-item pull request whose base was changed away from
  main is treated as closed unmerged at that change, and reported as a
  tamper: it no longer reaches main, and listing pull requests by base would
  otherwise lose it.
- The streak counts merged non-seed queue-item pull requests in time order
  and resets on a near-miss, an escape of type (a) or (b), and a change to
  what go-test or class-scope decides. Type (a) is read from the push runs
  themselves where the snapshot carries them (the scorecard's does), with the
  ledger as the record that the ratchet acted; so an escape the detector
  ignored because it fell before a record still resets. An inconclusive that
  was cleared and then merged is just a merge: it counts one.
- Promotion is offered only when each of the N merges it counts (the first
  N of the streak) has a push run of go-test whose first attempt succeeded
  within the escape window: a merge whose window is still open, or whose runs
  were not read, holds it back.
- The next item is the first queue item whose version is above main's
  go.mod and that no near-miss closed. A reverted item is above main again,
  so it is queued again.
"""

from __future__ import annotations

import base64
import io
import json
import re
import zipfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import classfile
from . import ruleset as rs
from .gh import ApiError

ROOT = Path(__file__).resolve().parent.parent
HUMAN_LABELS = ("reviewing", "human-cleared", "escape:revert")
# Labels machinery applies. An App's event on one of these is not a person's
# act and is listed as an unknown by the scorecard (C-21: each such act is
# "recorded as an unknown or a tamper").
MACHINERY_LABELS = ("needs-human", "seeded")
# What class-scope writes on its decide step, as a check-run annotation: one
# notice with this title whose message is the verdict's fixed-enum fields as
# JSON. Check-run annotations are readable without a token (REST description,
# allowsPublicRead), unlike the verdict artifact, whose download needs
# `actions: read` on svc-hello.
VERDICT_ANNOTATION_TITLE = "class-scope-verdict"
VERDICT_ARTIFACT_PREFIX = "class-scope-verdict"
# Paths on svc-hello whose change changes what go-test or class-scope decides
# (the note: such a change "resets the streak"). Read mechanically from each
# commit on main after the switch-on.
VERIFIER_PATHS = (".github/workflows/validate.yml", ".github/workflows/class-scope.yml")
VERIFIER_PREFIXES = (".github/class-scope/",)
VERIFIER_SUFFIXES = ("_test.go",)
# A `closed` event within this many seconds of merged_at is the merge itself.
MERGE_CLOSE_TOLERANCE = timedelta(seconds=60)


# -- time --------------------------------------------------------------------

def parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# -- go.mod ------------------------------------------------------------------

def go_mod_requires(text: str) -> dict[str, str]:
    """{module: version} for every require in a go.mod, single or block form."""
    out: dict[str, str] = {}
    in_block = False
    for raw in text.splitlines():
        s = raw.split("//", 1)[0].strip()
        if in_block:
            if s.startswith(")"):
                in_block = False
                continue
            parts = s.split()
            if len(parts) >= 2:
                out[parts[0]] = parts[1]
            continue
        if re.match(r"^require\s*\($", s):
            in_block = True
            continue
        m = re.match(r"^require\s+(\S+)\s+(\S+)$", s)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _vt(v: str) -> tuple:
    try:
        return classfile.semver(v)
    except ValueError:
        # A pseudo-version or pre-release on main: compare by its release
        # core, so a queue item above it is still found.
        m = re.match(r"^v(\d+)\.(\d+)\.(\d+)", v)
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


# -- labels ------------------------------------------------------------------

def _is_class_scope_removal(ev: dict) -> bool:
    """class-scope removes a human label on `synchronize` with the workflow's
    own token: a new head needs a new label. That removal ends the label's
    force. The agent App cannot act as github-actions."""
    app = ev.get("performed_via_github_app") or {}
    actor = (ev.get("actor") or {}).get("login", "")
    return app.get("slug") == "github-actions" or actor == "github-actions[bot]"


def labels_in_force_from_events(events: list[dict], human_logins: list[str],
                                labels: tuple = HUMAN_LABELS) -> dict:
    """The human labels in force, from issue events alone.

    A label is in force from a listed human's `labeled` event until a listed
    human's `unlabeled` event, counting only events with no
    performed_via_github_app. A removal by class-scope (github-actions, on a
    new head) also ends it. Any other actor's label event changes nothing and
    is returned under "unknown_label_events" (an App removing the listed
    human's label is a tamper to record, not a removal).
    """
    humans = {h.lower() for h in human_logins}
    state: dict[str, Any] = {name: None for name in labels}
    unknown: list[dict] = []
    for ev in sorted(events, key=lambda e: (e.get("created_at") or "", e.get("id") or 0)):
        kind = ev.get("event")
        if kind not in ("labeled", "unlabeled"):
            continue
        name = (ev.get("label") or {}).get("name")
        if name not in state:
            continue
        actor = (ev.get("actor") or {}).get("login", "")
        app = ev.get("performed_via_github_app")
        if actor.lower() in humans and not app:
            state[name] = {"actor": actor, "at": ev.get("created_at")} if kind == "labeled" else None
        elif kind == "unlabeled" and _is_class_scope_removal(ev):
            state[name] = None
        else:
            unknown.append({"event": kind, "label": name, "actor": actor,
                            "app": (app or {}).get("slug"), "at": ev.get("created_at"),
                            "id": ev.get("id")})
    state["unknown_label_events"] = unknown
    return state


def labels_in_force(gh, repo: str, pr_number: int, cf: dict) -> dict:
    """Same rule class-scope uses, read from the pull request's issue events."""
    events = gh.paginate(f"repos/{repo}/issues/{pr_number}/events")
    return labels_in_force_from_events(events, cf["human_logins"])


# -- checks ------------------------------------------------------------------

_DETAILS = re.compile(r"/actions/runs/(\d+)/job/(\d+)")


def latest_check_run(check_runs: list[dict], name: str, integration_id: int) -> dict | None:
    mine = [c for c in check_runs if c.get("name") == name
            and (c.get("app") or {}).get("id") == integration_id]
    if not mine:
        return None
    return max(mine, key=lambda c: (c.get("started_at") or "", c.get("id") or 0))


def verdict_from_job(job: dict) -> dict:
    """class-scope's verdict from its job's step names (the contract's encoding).

    After `decide`, `verdict-inconclusive` fails iff the verdict is
    inconclusive and `verdict-fail` fails iff it is fail. A fail is reported
    only when the steps show exactly that sequence, because a fail closes the
    pull request; a failure anywhere else means no verdict, which is an
    inconclusive of the re-run kind. A job that reports success without its
    verdict steps succeeding (skipped, renamed) is not a pass.
    """
    if job.get("status") != "completed":
        return {"verdict": "pending", "reason_class": None, "failed_steps": []}
    steps = {s.get("name"): s.get("conclusion") for s in job.get("steps", [])}
    failed = [n for n, c in steps.items() if c in ("failure", "timed_out", "cancelled")]
    decide = steps.get("decide")
    vinc = steps.get("verdict-inconclusive")
    vfail = steps.get("verdict-fail")
    if job.get("conclusion") == "success" and not failed:
        if decide == "success" and vinc == "success" and vfail == "success":
            return {"verdict": "pass", "reason_class": None, "failed_steps": []}
        return {"verdict": "inconclusive", "reason_class": "rerun", "failed_steps": [],
                "note": "job succeeded without its verdict steps succeeding"}
    if failed == ["verdict-fail"] and decide == "success" and vinc == "success":
        return {"verdict": "fail", "reason_class": None, "failed_steps": failed}
    if failed == ["verdict-inconclusive"] and decide == "success":
        return {"verdict": "inconclusive", "reason_class": None, "failed_steps": failed}
    return {"verdict": "inconclusive", "reason_class": "rerun", "failed_steps": failed,
            "note": "no verdict"}


def verdict_artifact_name(attempt: int | None) -> str:
    """class-scope names its artifact per run attempt, so a re-run never
    replaces an earlier attempt's verdict."""
    return f"{VERDICT_ARTIFACT_PREFIX}-attempt-{attempt or 1}"


def _read_verdict_artifact(gh, repo: str, run_id: int, attempt: int | None = None) -> dict | None:
    """The full verdict.json, where the token can download artifacts (the
    scorecard's cannot; it reads the annotation instead)."""
    try:
        arts = gh.get(f"repos/{repo}/actions/runs/{run_id}/artifacts", per_page=100)
        items = arts.get("artifacts", []) if isinstance(arts, dict) else []
        if attempt is not None:
            items = [a for a in items if a.get("name") == verdict_artifact_name(attempt)]
        else:
            items = [a for a in items if str(a.get("name", "")).startswith(VERDICT_ARTIFACT_PREFIX)]
        if not items:
            return None
        art = max(items, key=lambda a: a.get("id", 0))
        raw = gh.get_raw(f"repos/{repo}/actions/artifacts/{art['id']}/zip")
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            return json.loads(z.read("verdict.json"))
    except (ApiError, KeyError, ValueError, zipfile.BadZipFile, OSError):
        return None


ANNOTATION_VERDICTS = ("pass", "fail", "inconclusive")


def verdict_from_annotations(annotations: list[dict] | None) -> dict:
    """{"verdict": <the decoded notice> | None, "error": <why not> | None}.

    Exactly one annotation titled class-scope-verdict must exist, and its
    message must be a JSON object with a known verdict. Two or more mean that
    something other than class-scope's own line printed one (class-scope
    stops workflow commands around every line it prints that a pull request
    could influence), so none is trusted and the reader says why."""
    mine = [a for a in annotations or [] if a.get("title") == VERDICT_ANNOTATION_TITLE]
    if not mine:
        return {"verdict": None, "error": "no class-scope-verdict annotation"}
    if len(mine) > 1:
        return {"verdict": None, "error": f"{len(mine)} class-scope-verdict annotations; none trusted"}
    try:
        d = json.loads(mine[0].get("message") or "")
    except ValueError:
        return {"verdict": None, "error": "the class-scope-verdict annotation is not JSON"}
    if not isinstance(d, dict) or d.get("verdict") not in ANNOTATION_VERDICTS:
        return {"verdict": None, "error": "the class-scope-verdict annotation has no known verdict"}
    return {"verdict": d, "error": None}


def read_verdict_annotation(gh, repo: str, check_run_id: int | None) -> dict:
    if not check_run_id:
        return {"verdict": None, "error": "no check run"}
    try:
        anns = gh.paginate(f"repos/{repo}/check-runs/{check_run_id}/annotations")
    except ApiError as e:
        return {"verdict": None, "error": f"annotations unreadable ({e.status})"}
    return verdict_from_annotations(anns)


def class_scope_verdict(gh, repo: str, head_sha: str, *, name: str = "class-scope",
                        integration_id: int = 15368, read_artifact: bool = True) -> dict:
    """{"verdict": pass|fail|inconclusive|pending|none, "reason_class", "run_id", ...}

    Read from the latest class-scope check run on `head_sha` from GitHub
    Actions, then that job's steps through the Actions API. The verdict
    annotation on that check run (public) adds the reason class and codes;
    the attempt's artifact is read only when the annotation is missing and
    `read_artifact` asks for it. The step names decide; the annotation only
    explains. Never from a label.
    """
    data = gh.get(f"repos/{repo}/commits/{head_sha}/check-runs", check_name=name,
                  filter="all", per_page=100)
    cr = latest_check_run(data.get("check_runs", []) if isinstance(data, dict) else [],
                          name, integration_id)
    if cr is None:
        return {"verdict": "none", "reason_class": None, "run_id": None, "job_id": None,
                "check_run_id": None, "head_sha": head_sha}
    m = _DETAILS.search(cr.get("details_url") or cr.get("html_url") or "")
    run_id, job_id = (int(m.group(1)), int(m.group(2))) if m else (None, cr["id"])
    job = gh.get(f"repos/{repo}/actions/jobs/{job_id}")
    v = verdict_from_job(job)
    run_id = run_id or job.get("run_id")
    out = {**v, "run_id": run_id, "job_id": job_id, "check_run_id": cr.get("id"),
           "head_sha": cr.get("head_sha", head_sha), "artifact": None, "annotation": None}
    if v["verdict"] == "pending":
        return out
    ann = read_verdict_annotation(gh, repo, cr.get("id"))
    out["annotation_error"] = ann["error"]
    if ann["verdict"] is not None:
        a = ann["verdict"]
        out["annotation"] = a
        out["reason_codes"] = list(a.get("reason_codes") or [])
        if v["verdict"] == "inconclusive" and out.get("reason_class") is None:
            out["reason_class"] = a.get("reason_class")
        if a.get("verdict") != v["verdict"]:
            out["annotation_disagrees"] = True
    elif read_artifact and run_id:
        art = _read_verdict_artifact(gh, repo, run_id, job.get("run_attempt"))
        if art is not None:
            out["artifact"] = art
            if v["verdict"] == "inconclusive" and out.get("reason_class") is None:
                out["reason_class"] = art.get("reason_class")
            if art.get("verdict") != v["verdict"]:
                out["artifact_disagrees"] = True
    return out


def check_run_verdict(gh, repo: str, head_sha: str, name: str, integration_id: int = 15368) -> dict:
    """pass|fail|pending|none for a plain check such as go-test (no inconclusive)."""
    data = gh.get(f"repos/{repo}/commits/{head_sha}/check-runs", check_name=name,
                  filter="all", per_page=100)
    cr = latest_check_run(data.get("check_runs", []) if isinstance(data, dict) else [],
                          name, integration_id)
    return {**check_conclusion(cr), "check_run_id": cr.get("id") if cr else None}


def check_conclusion(cr: dict | None) -> dict:
    if cr is None:
        return {"verdict": "none"}
    if cr.get("status") != "completed":
        return {"verdict": "pending"}
    c = cr.get("conclusion")
    if c == "success":
        return {"verdict": "pass"}
    if c in ("failure", "timed_out"):
        return {"verdict": "fail", "conclusion": c}
    # cancelled, neutral, skipped, stale, action_required: not a pass to merge
    # on, and not a fail to close on; a re-run settles it.
    return {"verdict": "pending", "conclusion": c}


# -- the state ---------------------------------------------------------------

@dataclass
class ClassState:
    rung: int                 # live minimum_approvals of the rung entry; -1 if it cannot be found
    merged: list[dict]        # queue-item PRs merged, in order
    near_misses: list[dict]   # non-seed queue-item PRs closed unmerged
    escapes: list[dict]       # ledger events of type a/b
    streak: int
    open_agent_pr: dict | None
    next_item: dict | None    # first queue item above main's version, not closed by a near-miss
    promotable: bool
    # Additions beyond the contract's fields, for the detector and scorecard.
    open_agent_prs: list[dict] = field(default_factory=list)
    open_queue_prs: list[dict] = field(default_factory=list)
    seeds: list[int] = field(default_factory=list)
    seeds_complete: bool = False   # True when seeds were computed from SEED_KEY
    seeds_skipped_at_l2: list[int] = field(default_factory=list)   # drawn positions reached at L2: no seed
    main_sha: str | None = None
    main_versions: dict = field(default_factory=dict)
    latest_record: dict | None = None
    l2_accepted: bool = False
    streak_start: str | None = None
    queue_item_prs: dict = field(default_factory=dict)  # pr number -> run record
    fixture_prs: list[int] = field(default_factory=list)
    unknowns: list[dict] = field(default_factory=list)
    escape_a: dict | None = None            # pr -> escape (a) status from push runs, when read
    verifier_changes: list[dict] = field(default_factory=list)
    promotion_blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def own_run_records(run_records: list[dict], cf: dict) -> list[dict]:
    """Run records written for this class's repository. A record without a
    `repo`, or for another repository (probe P4), is not a queue item here."""
    return [r for r in run_records if isinstance(r, dict) and r.get("repo") == cf["repo"]]


def queue_item_prs(run_records: list[dict], fixture_prs: set[int],
                   prs: dict[int, dict] | None = None) -> dict[int, dict]:
    """{pr number: run record}. With `prs`, a record whose pull request's head
    branch is not the record's `branch` is ignored: the number then belongs
    to some other pull request, not to the one the open job made."""
    out: dict[int, dict] = {}
    for r in sorted(run_records, key=lambda r: (r.get("opened_at") or "", r.get("controls_run_id") or 0)):
        n = r.get("pr_number")
        if not isinstance(n, int) or n in fixture_prs or n in out:
            continue
        pr = (prs or {}).get(n)
        if pr is not None and ((pr.get("head") or {}).get("ref") or None) != (r.get("branch") or None):
            continue
        out[n] = r
    return out


def base_is_main(pr: dict) -> bool:
    ref = (pr.get("base") or {}).get("ref")
    return ref in (None, "main")


def merged_queue_items(qprs: dict[int, dict], prs: dict[int, dict]) -> list[dict]:
    out = []
    for n, rec in qprs.items():
        pr = prs.get(n)
        if pr and pr.get("merged_at"):
            out.append({"pr": n, "position": rec.get("queue_position"), "module": rec.get("module"),
                        "version": rec.get("version"), "merged_at": pr["merged_at"],
                        "merge_commit_sha": pr.get("merge_commit_sha"),
                        "head_sha": (pr.get("head") or {}).get("sha")})
    return sorted(out, key=lambda m: (parse_time(m["merged_at"]), m["pr"]))


def identify_seeds(qprs: dict[int, dict], prs: dict[int, dict], merged: list[dict],
                   positions: list[int], l2_rung: int | None = None,
                   skipped: list[int] | None = None) -> set[int]:
    """For each drawn k, the first queue-item PR opened after the k-th queue merge.

    Seeds run at L1 only (the readiness note: "Seeds: four, at L1"). The
    opening job makes no seed while the live rung entry reads L2's value
    (`l2_rung`), and writes the value it read in every run record
    (`rung_at_open`), so when that first pull request was opened at L2, the
    position has no seed: it is added to `skipped`, and no later pull request
    takes its place."""
    seeds: set[int] = set()
    opened = []
    for n, rec in qprs.items():
        created = (prs.get(n) or {}).get("created_at") or rec.get("opened_at")
        if created:
            opened.append((parse_time(created), n))
    opened.sort()
    for k in positions:
        if k < 1 or len(merged) < k:
            continue
        t = parse_time(merged[k - 1]["merged_at"])
        later = [n for (c, n) in opened if c > t]
        if not later:
            continue
        if l2_rung is not None and (qprs.get(later[0]) or {}).get("rung_at_open") == l2_rung:
            if skipped is not None:
                skipped.append(k)
            continue
        seeds.add(later[0])
    return seeds


def near_misses_from(qprs: dict[int, dict], prs: dict[int, dict],
                     close_events: dict[int, list[dict]], seeds: set[int]) -> list[dict]:
    """One near-miss per non-seed queue-item pull request, at its first close
    that is not a merge's own close, or at its first change of base away from
    main (a pull request that no longer targets main is closed to the class)."""
    out = []
    for n, rec in qprs.items():
        if n in seeds:
            continue
        pr = prs.get(n) or {}
        merged_at = parse_time(pr.get("merged_at"))
        events = close_events.get(n)
        base = {"pr": n, "position": rec.get("queue_position"), "module": rec.get("module"),
                "version": rec.get("version")}
        if events is None:
            # No events read: fall back to the pull request's own fields.
            if pr.get("state") == "closed" and not merged_at:
                out.append({**base, "closed_at": pr.get("closed_at"), "closed_by": None})
            elif not base_is_main(pr):
                out.append({**base, "closed_at": pr.get("closed_at") or pr.get("updated_at"), "closed_by": None,
                            "base_changed": True})
            continue
        first = None
        for ev in sorted(events, key=lambda e: (e.get("created_at") or "", e.get("id") or 0)):
            kind = ev.get("event")
            if kind == "closed":
                t = parse_time(ev.get("created_at"))
                if merged_at and t and abs(t - merged_at) <= MERGE_CLOSE_TOLERANCE:
                    continue  # the close that a merge makes
                first = {**base, "closed_at": ev.get("created_at"),
                         "closed_by": (ev.get("actor") or {}).get("login")}
                break
            if kind == "base_ref_changed" and not base_is_main(pr):
                first = {**base, "closed_at": ev.get("created_at"),
                         "closed_by": (ev.get("actor") or {}).get("login"), "base_changed": True}
                break
        if first is None and not base_is_main(pr) and not merged_at:
            first = {**base, "closed_at": pr.get("closed_at") or pr.get("updated_at"), "closed_by": None,
                     "base_changed": True}
        if first is not None:
            out.append(first)
    return sorted(out, key=lambda x: (parse_time(x["closed_at"]) or datetime.min.replace(tzinfo=timezone.utc), x["pr"]))


def escapes_from_ledger(ledger: list[dict]) -> list[dict]:
    out = []
    for e in ledger:
        if e.get("type") in ("a", "b"):
            ev = e.get("event", {})
            out.append({"key": e.get("key"), "type": e["type"], "pr": ev.get("pr"),
                        "event_time": ev.get("event_time"), "acted_at": e.get("acted_at")})
    return sorted(out, key=lambda x: parse_time(x["event_time"]) or datetime.min.replace(tzinfo=timezone.utc))


def escape_a_by_pr(merged: list[dict], push_runs: dict[str, list] | None, cf: dict,
                   as_of: datetime | None) -> dict[int, dict] | None:
    """{pr: escape (a) status} for each merge, from the push runs themselves
    (detector.escape_a_status, the rule the detector applies). None when the
    snapshot carries no push runs. Without a read time, nothing is called an
    escape for lack of time alone: a merge whose window is open stays
    pending, which holds promotion back and resets nothing."""
    if push_runs is None:
        return None
    from . import detector  # the same pure rule; imported here to avoid a cycle
    job = cf["checks"]["go_test"]["job"]
    out = {}
    for m in merged:
        runs = push_runs.get(m.get("merge_commit_sha") or "", [])
        now = as_of or parse_time(m["merged_at"])
        out[m["pr"]] = detector.escape_a_status(m["merged_at"], m.get("merge_commit_sha"), runs, now,
                                                cf["escape_a_minutes"], job)
    return out


def push_run_escapes(merged: list[dict], by_pr: dict[int, dict] | None) -> list[dict]:
    if not by_pr:
        return []
    return [{"key": f"escape-a-pr{m['pr']}", "type": "a", "pr": m["pr"], "event_time": by_pr[m["pr"]]["event_time"],
             "acted_at": None, "source": "push runs"}
            for m in merged if (by_pr.get(m["pr"]) or {}).get("status") == "escape"]


def is_verifier_path(path: str) -> bool:
    return (path in VERIFIER_PATHS or path.startswith(VERIFIER_PREFIXES)
            or path.endswith(VERIFIER_SUFFIXES))


def verifier_changes(commits: list[dict], since: str | None) -> list[dict]:
    """Commits on svc-hello's main after `since` (the L1 switch-on) that touch
    a file deciding what go-test or class-scope decides. Each resets the
    streak at its commit time. The rule is mechanical and errs toward a
    reset: a comment change in validate.yml resets too."""
    if since is None:
        return []
    t0 = parse_time(since)
    out = []
    for c in commits or []:
        t = parse_time(c.get("date"))
        files = [f for f in c.get("files", []) if is_verifier_path(f)]
        if t and t > t0 and files:
            out.append({"sha": c.get("sha"), "date": c.get("date"), "files": sorted(files)})
    return sorted(out, key=lambda x: (x["date"], x["sha"] or ""))


def streak_from(merged: list[dict], near_misses: list[dict], escapes: list[dict],
                verifier: list[dict] | None = None) -> tuple[int, str | None]:
    """(streak, time of the streak's first merge). At equal times a merge is
    taken before a reset, so an escape of the item just merged resets it."""
    events = [(parse_time(m["merged_at"]), 0, "merge", m) for m in merged]
    events += [(parse_time(n["closed_at"]), 1, "reset", n) for n in near_misses if n.get("closed_at")]
    events += [(parse_time(e["event_time"]), 1, "reset", e) for e in escapes if e.get("event_time")]
    events += [(parse_time(c["date"]), 1, "reset", c) for c in verifier or [] if c.get("date")]
    events.sort(key=lambda x: (x[0], x[1]))
    streak, start = 0, None
    for t, _, kind, _ in events:
        if kind == "merge":
            if streak == 0:
                start = iso(t)
            streak += 1
        else:
            streak, start = 0, None
    return streak, start


def next_item_from(queue: list[dict], main_versions: dict[str, str],
                   near_misses: list[dict]) -> dict | None:
    closed = {(n["module"], n["version"]) for n in near_misses}
    for item in queue:
        cur = main_versions.get(item["module"])
        if cur is None:
            continue
        if _vt(item["version"]) > _vt(cur) and (item["module"], item["version"]) not in closed:
            return item
    return None


def latest_record(class_records: list[dict]) -> dict | None:
    merged = [r for r in class_records if r.get("merged_at") and r.get("kind") in ("promotion", "fixture")]
    return max(merged, key=lambda r: parse_time(r["merged_at"])) if merged else None


def ledger_demotions(ledger: list[dict]) -> list[dict]:
    """Ledger entries that demote: every event the ratchet acted on."""
    return [e for e in ledger if e.get("type") in ("a", "b", "ruleset")]


def l2_accepted(class_records: list[dict], ledger: list[dict]) -> bool:
    """L2 is accepted from a merged promotion or fixture record until the
    ledger's next demotion; from a demotion until a later record merges, only
    L1 is."""
    rec = latest_record(class_records)
    if rec is None:
        return False
    rt = parse_time(rec["merged_at"])
    # A demotion with no readable acted_at is taken as after the record: an
    # unreadable time must never keep L2 accepted.
    far = datetime.max.replace(tzinfo=timezone.utc)
    return not any((parse_time(d.get("acted_at")) or far) > rt for d in ledger_demotions(ledger))


def live_rung(live: dict | None, l1: dict, l2: dict) -> int:
    if not live:
        return -1
    entries = rs.find_rung_entries(live, rs.rung_path(l1, l2))
    if len(entries) != 1:
        return -1
    v = entries[0].get("minimum_approvals")
    return v if isinstance(v, int) else -1


def derive_state(snap: dict, cf: dict, l1: dict, l2: dict,
                 seed_positions: list[int] | None = None) -> ClassState:
    """The pure part: a snapshot of reads in, the class state out."""
    prs = {int(k): v for k, v in snap.get("prs", {}).items()}
    close_events = {int(k): v for k, v in snap.get("close_events", {}).items()}
    fixtures = set(snap.get("fixture_prs", []))
    qprs = queue_item_prs(own_run_records(snap.get("run_records", []), cf), fixtures, prs)
    all_merged = merged_queue_items(qprs, prs)
    skipped: list[int] = []
    seeds = set(snap.get("seed_record_prs", [])) & set(qprs) | (
        identify_seeds(qprs, prs, all_merged, seed_positions, cf["rungs"]["l2"], skipped)
        if seed_positions else set())
    merged = [m for m in all_merged if m["pr"] not in seeds]
    near = near_misses_from(qprs, prs, close_events, seeds)
    ledger = snap.get("ledger", [])
    escapes = escapes_from_ledger(ledger)
    by_pr = escape_a_by_pr(merged, snap.get("push_runs"), cf, parse_time(snap.get("as_of")))
    ledger_a = {e["pr"] for e in escapes if e["type"] == "a"}
    resets = escapes + [e for e in push_run_escapes(merged, by_pr) if e["pr"] not in ledger_a]
    verifier = list(snap.get("verifier_changes") or [])
    streak, start = streak_from(merged, near, resets, verifier)
    main_versions = go_mod_requires(snap.get("main_go_mod") or "")
    nxt = next_item_from(classfile.queue(cf), main_versions, near)
    rung = live_rung(snap.get("ruleset_live"), l1, l2)
    records = snap.get("class_records", [])
    rec = latest_record(records)
    bot = classfile.agent_bot_login(cf)
    # A queue-item pull request whose base is no longer main is closed to the
    # class (a near-miss above), so it no longer holds the one open slot.
    open_prs = [p for p in prs.values() if p.get("state") == "open"
                and not (p["number"] in qprs and not base_is_main(p))]
    open_queue = sorted([p for p in open_prs if p["number"] in qprs], key=lambda p: p["number"])
    open_agent = sorted([p for p in open_prs if p["number"] in qprs or p["number"] in fixtures
                         or (p.get("user") or {}).get("login") == bot], key=lambda p: p["number"])
    unknowns = list(snap.get("unknowns", []))
    for n in near:
        if n.get("base_changed"):
            unknowns.append({"type": "tamper-base-changed", "pr": n["pr"], "at": n.get("closed_at"),
                             "actor": n.get("closed_by"), "base": ((prs.get(n["pr"]) or {}).get("base") or {}).get("ref")})
    # The N merges the promotion would count: the first N of the streak. A
    # later merge whose window is still open does not hold back a promotion
    # its predecessors earned; if it escapes, it resets the streak as usual.
    streak_merges = [m for m in merged if start and parse_time(m["merged_at"]) >= parse_time(start)][:cf["n"]]
    blockers = []
    if rung != cf["rungs"]["l1"]:
        blockers.append(f"the live rung entry is {rung}, not L1")
    if streak < cf["n"]:
        blockers.append(f"streak {streak} is below N = {cf['n']}")
    if rec and start and parse_time(rec["merged_at"]) > parse_time(start):
        blockers.append("a promotion or fixture record merged after the streak began")
    if by_pr is None:
        blockers.append("no push runs read: escape (a) windows unconfirmed")
    else:
        unclear = [m["pr"] for m in streak_merges if (by_pr.get(m["pr"]) or {}).get("status") != "clear"]
        if unclear:
            blockers.append(f"escape (a) window not clear for {unclear}")
    promotable = not blockers
    return ClassState(
        rung=rung, merged=merged, near_misses=near, escapes=escapes, streak=streak,
        open_agent_pr=_pr_summary(open_queue[0], qprs) if len(open_queue) == 1 else None,
        next_item=nxt, promotable=bool(promotable),
        open_agent_prs=[_pr_summary(p, qprs) for p in open_agent],
        open_queue_prs=[_pr_summary(p, qprs) for p in open_queue],
        seeds=sorted(seeds), seeds_complete=seed_positions is not None, seeds_skipped_at_l2=sorted(skipped),
        main_sha=snap.get("main_sha"), main_versions=main_versions,
        latest_record=rec, l2_accepted=l2_accepted(records, ledger), streak_start=start,
        queue_item_prs={n: r for n, r in qprs.items()}, fixture_prs=sorted(fixtures),
        unknowns=unknowns, escape_a=by_pr, verifier_changes=verifier,
        promotion_blockers=blockers,
    )


def _pr_summary(pr: dict, qprs: dict[int, dict]) -> dict:
    rec = qprs.get(pr["number"]) or {}
    return {"number": pr["number"], "head_sha": (pr.get("head") or {}).get("sha"),
            "head_ref": (pr.get("head") or {}).get("ref"), "author": (pr.get("user") or {}).get("login"),
            "created_at": pr.get("created_at"), "queue_item": pr["number"] in qprs,
            "queue_position": rec.get("queue_position"), "module": rec.get("module"),
            "version": rec.get("version")}


# -- reads -------------------------------------------------------------------

def load_committed_rulesets(cf: dict, root: Path = ROOT) -> tuple[dict, dict]:
    """The committed L1 and L2 bodies, after checking their hashes against the class file."""
    import hashlib
    out = []
    for which in ("l1", "l2"):
        raw = (root / cf["rulesets"][f"{which}_path"]).read_bytes()
        h = hashlib.sha256(raw).hexdigest()
        if h != cf["rulesets"][f"{which}_sha256"]:
            raise ValueError(f"{which}.json hash {h} differs from the class file's {cf['rulesets'][f'{which}_sha256']}")
        out.append(json.loads(raw))
    rs.rung_path(out[0], out[1])  # exactly one field apart, or raise
    return out[0], out[1]


def read_class_records(gh_controls, controls_repo: str, ref: str = "main") -> tuple[list[dict], list[dict]]:
    """Promotion and fixture records under classes/records/ on the controls
    repo's main, each with the merged_at of the pull request that added it.
    Returns (records, unknowns): a record with no merged pull request is not
    counted and is reported."""
    try:
        listing = gh_controls.get(f"repos/{controls_repo}/contents/classes/records", ref=ref)
    except ApiError as e:
        if e.status == 404:
            return [], []
        raise
    records, unknowns = [], []
    for item in listing if isinstance(listing, list) else []:
        name = item.get("name", "")
        if item.get("type") != "file" or not re.match(r"^(promotion|fixture|close)-.+\.ya?ml$", name):
            continue
        meta = gh_controls.get(f"repos/{controls_repo}/contents/{item['path']}", ref=ref)
        body = classfile.parse_yaml(base64.b64decode(meta.get("content", "")).decode()) or {}
        commits = gh_controls.paginate(f"repos/{controls_repo}/commits", sha=ref, path=item["path"])
        merged_at, pr_number = None, None
        if commits:
            first = commits[-1]["sha"]
            pulls = gh_controls.get(f"repos/{controls_repo}/commits/{first}/pulls")
            merged = [p for p in pulls or [] if p.get("merged_at")]
            if merged:
                merged_at, pr_number = merged[0]["merged_at"], merged[0]["number"]
        kind = body.get("kind") if isinstance(body, dict) else None
        if not merged_at or kind not in ("promotion", "fixture", "close") or name.split("-")[0] != kind:
            unknowns.append({"type": "class-record-unusable", "path": item["path"], "kind": kind,
                             "merged_at": merged_at})
            continue
        records.append({"path": item["path"], "kind": kind, "merged_at": merged_at, "pr": pr_number,
                        "body": body})
    return records, unknowns


def read_file_at(gh, repo: str, path: str, ref: str) -> str | None:
    try:
        meta = gh.get(f"repos/{repo}/contents/{path}", ref=ref)
    except ApiError as e:
        if e.status == 404:
            return None
        raise
    return base64.b64decode(meta.get("content", "")).decode()


def read_queue_prs(gh, svc: str, run_records: list[dict], fixture_prs, prs: dict[int, dict]) -> dict[int, dict]:
    """The pull request list by base `main`, plus every run-record pull
    request read by number: one whose base was changed away from main drops
    out of the listing but is still a queue item (closed to the class)."""
    out = dict(prs)
    for n in sorted({r.get("pr_number") for r in run_records} - set(fixture_prs) - set(prs)):
        if not isinstance(n, int):
            continue
        try:
            out[n] = gh.get(f"repos/{svc}/pulls/{n}")
        except ApiError as e:
            if e.status != 404:
                raise
    return out


CLOSE_EVENTS = ("closed", "reopened", "merged", "base_ref_changed")


def gather(gh_svc, records, cf: dict, *, controls_repo: str | None = None, read_events=None) -> dict:
    """Every read derive_state needs, as one JSON-able snapshot.

    `read_events(n)` reads a pull request's issue events; the scorecard
    passes one that keeps them, so it reads each pull request's events once."""
    svc = cf["repo"]
    if read_events is None:
        def read_events(n):
            return gh_svc.paginate(f"repos/{svc}/issues/{n}/events")
    run_records = own_run_records([r for r in (records.read(p) for p in records.list("runs/")) if r], cf)
    fixture_prs = []
    for p in records.list("fixtures/"):
        f = records.read(p) or {}
        if isinstance(f.get("pr_number"), int):
            fixture_prs.append(f["pr_number"])
    seed_record_prs = []
    for p in records.list("seeds/"):
        m = re.match(r"^seeds/(\d+)\.json$", p)
        if m:
            seed_record_prs.append(int(m.group(1)))
    ledger = [e for e in (records.read(p) for p in records.list("ledger/")) if e]
    prs = {p["number"]: p for p in gh_svc.paginate(f"repos/{svc}/pulls", state="all", base="main")}
    prs = read_queue_prs(gh_svc, svc, run_records, fixture_prs, prs)
    qnums = {r.get("pr_number") for r in run_records} - set(fixture_prs)
    close_events = {}
    for n in sorted(x for x in qnums if isinstance(x, int) and x in prs):
        close_events[n] = [e for e in read_events(n) if e.get("event") in CLOSE_EVENTS]
    main_sha = gh_svc.get(f"repos/{svc}/branches/main")["commit"]["sha"]
    main_go_mod = read_file_at(gh_svc, svc, "go.mod", main_sha)
    try:
        live = gh_svc.get(f"repos/{svc}/rulesets/{cf['rulesets']['ruleset_id']}")
    except ApiError as e:
        if e.status != 404:
            raise
        live = None
    class_records, unknowns = read_class_records(records.gh, controls_repo or records.repo)
    return {"run_records": run_records, "fixture_prs": fixture_prs, "seed_record_prs": seed_record_prs,
            "ledger": ledger, "prs": prs, "close_events": close_events, "main_sha": main_sha,
            "main_go_mod": main_go_mod, "ruleset_live": live, "class_records": class_records,
            "unknowns": unknowns}


def seed_positions_from_key(seed_key: str, cf: dict) -> list[int]:
    """Positions from factory.seeds.schedule, whichever of the agreed shapes it returns."""
    from . import seeds  # built separately; imported only where SEED_KEY is held
    sched = seeds.schedule(seed_key, cf)
    if isinstance(sched, dict):
        sched = sched.get("positions", sched.get("schedule", []))
    out = []
    for s in sched:
        out.append(int(s["position"]) if isinstance(s, dict) else int(s))
    return sorted(out)


def read_state(gh_svc, records, cf: dict, seed_key: str | None = None) -> ClassState:
    l1, l2 = load_committed_rulesets(cf)
    snap = gather(gh_svc, records, cf)
    positions = seed_positions_from_key(seed_key, cf) if seed_key else None
    return derive_state(snap, cf, l1, l2, positions)
