"""The only code that writes svc-hello's ruleset: it raises the rung entry to 1.

Why it is this narrow: the ratchet App's `administration` permission could
also lower an entry, edit bypass_actors, disable or delete the ruleset
(ADR-0024 §7). "Only raises approvals" is held by this code, not by GitHub,
so the code can do exactly one thing: set the rung entry's
minimum_approvals from 0 to 1, leaving every other field as the live
ruleset has it.

Before writing, `plan_raise` builds the body it will PUT from the live
ruleset and checks that the two differ in that one field and nothing else.
If they would differ in anything else, or the rung entry cannot be found
exactly once, or the value is already above 1 (setting 1 would lower it), it
writes nothing and an issue is opened in the controls repo. If the rung
entry is already 1, there is nothing to write.

It never repairs another difference. The detector raises the rung on any
difference and opens an issue; a person restores the rest by hand.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from . import ruleset as rs
from .gh import ApiError, DryRunGitHub
from .records import RecordExists


def plan_raise(live: dict | None, l1: dict, l2: dict) -> dict:
    """{"action": "write"|"noop"|"refuse", "body", "diff", "reason"}. Pure."""
    path = rs.rung_path(l1, l2)
    if live is None:
        return {"action": "refuse", "body": None, "diff": [], "reason": "ruleset not found"}
    if "bypass_actors" not in live:
        return {"action": "refuse", "body": None, "diff": [],
                "reason": "bypass_actors not returned: the token cannot verify the whole ruleset"}
    entries = rs.find_rung_entries(live, path)
    if len(entries) != 1:
        return {"action": "refuse", "body": None, "diff": [],
                "reason": f"rung entry found {len(entries)} times, expected once"}
    current = entries[0].get("minimum_approvals")
    if current == 1:
        return {"action": "noop", "body": None, "diff": [], "reason": "rung entry already 1"}
    if not isinstance(current, int) or isinstance(current, bool) or current > 1:
        return {"action": "refuse", "body": None, "diff": [],
                "reason": f"rung entry is {current!r}; setting 1 would not be a raise"}
    body = rs.put_body(live)
    for e in rs.find_rung_entries(body, path):
        e["minimum_approvals"] = 1
    d = rs.diff(rs.normalize(live), rs.normalize(body))
    expected = [{"path": path, "expected": current, "actual": 1}]
    if d != expected:
        return {"action": "refuse", "body": None, "diff": d,
                "reason": "the body to PUT differs from the live ruleset in more than the rung field"}
    return {"action": "write", "body": body, "diff": d, "reason": f"rung entry {current} -> 1"}


def open_issue(gh_controls, controls_repo: str, title: str, body: str) -> str | None:
    """Open an issue in the controls repo unless one with this title is open.

    Returns its URL (or the existing one's).
    """
    for issue in gh_controls.paginate(f"repos/{controls_repo}/issues", state="open"):
        if issue.get("title") == title and "pull_request" not in issue:
            return issue.get("html_url")
    created = gh_controls.post(f"repos/{controls_repo}/issues", {"title": title, "body": body})
    url = created.get("html_url") if isinstance(created, dict) else None
    return url or f"opened: {title}"  # a dry run's stub carries no URL


def _save(records, path: str, obj: dict, unrecorded: list) -> None:
    """Write one ratchet record. A write the records branch refuses (the
    controls token's hourly limit, say) does not stop the ratchet: the record
    is kept in `unrecorded`, carried by the next record written, and printed
    to the job's log."""
    try:
        records.write(path, obj, f"ratchet: {path}")
    except RecordExists:
        pass  # another run acting on the same event wrote it first
    except ApiError as e:
        unrecorded.append({"path": path, "error": {"status": e.status, "message": e.message}, "record": obj})
        print(f"::warning::ratchet: {path} could not be written ({e.status}); it is carried by the next record",
              file=sys.stderr)
        # The record itself, without what it carries (printed already).
        print(json.dumps({"unrecorded": path, "record": {k: v for k, v in obj.items() if k != "unrecorded"}},
                         sort_keys=True, default=str), file=sys.stderr)


def try_open_issue(records, title: str, body: str, errors: list) -> str | None:
    """open_issue, where a refused request is recorded and does not stop the ratchet."""
    try:
        return open_issue(records.gh, records.repo, title, body)
    except ApiError as e:
        errors.append({"title": title, "status": e.status, "message": e.message})
        return None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def raise_rung(gh_svc, records, cf: dict, l1: dict, l2: dict, event_key: str,
               reason: dict, *, issue_key: str | None = None) -> dict:
    """Raise the rung entry to 1 on the live ruleset, recording before and after.

    Returns {"result": "raised"|"raised-unverified"|"already-1"|"refused"|"write-failed", "issue", "plan_reason",
    "before_path", "after_path", ...}. Issues go to the controls repo through
    the records client (the controls repo's own token); `issue_key` names
    the issue when it is not `event_key` (a retry reuses its event's issue).

    The ruleset PUT is made before any record is written (B4). The records
    and the issues use the controls repo's token, whose hourly limit every
    controls workflow shares; the PUT uses the ratchet App's. So a records
    branch that cannot be written never delays the raise. What could not be
    recorded is carried by the next record that is written: the after-record
    holds a before-record that failed, and the result (which the detector
    puts in its ledger entry) holds both as `unrecorded`.
    """
    repo = cf["repo"]
    rid = cf["rulesets"]["ruleset_id"]
    path = f"repos/{repo}/rulesets/{rid}"
    title_key = issue_key or event_key
    try:
        live = gh_svc.get(path)
    except ApiError as e:
        if e.status != 404:
            raise
        live = None
    plan = plan_raise(live, l1, l2)
    before_path = f"ratchet/{event_key}-before.json"
    after_path = f"ratchet/{event_key}-after.json"
    before = {"schema": 1, "event_key": event_key, "reason": reason, "read_at": _now(),
              "plan": {k: plan[k] for k in ("action", "reason", "diff")}, "ruleset": live}
    result = {"write": "raised", "noop": "already-1", "refuse": "refused"}[plan["action"]]
    after, error, unverified = live, None, None
    if plan["action"] == "write":
        try:
            gh_svc.put(path, plan["body"])
        except ApiError as e:
            # The first real PUT the ratchet makes is C-18's injection (a),
            # so a refused write must not crash the detector (ADV-08). It is
            # recorded, an issue is opened, and the caller still labels the
            # open pull requests and writes the ledger entry. The live
            # ruleset then stands at L2 after a demotion, which the ruleset
            # comparison reports as a difference, so the merge step
            # refuses. The comparison does not act on it again (the ledger
            # entry records that ruleset version as seen); instead each
            # later detector run and pre-merge run calls this again while
            # the rung entry is still 0 (detector.raise_to_retry, B5).
            result, error, error_message = "write-failed", {"status": e.status, "body": e.body}, e.message
        else:
            try:
                after = gh_svc.get(path)
            except ApiError as e:
                after, result = None, "raised-unverified"
                unverified = [{"path": [], "expected": "the ruleset read back", "actual": f"read failed ({e.status})"}]
            if after is not None and not isinstance(gh_svc, DryRunGitHub):
                d = rs.diff(rs.normalize(plan["body"]), rs.normalize(after))
                if d:
                    result, unverified = "raised-unverified", d
    unrecorded: list[dict] = []
    issue_errors: list[dict] = []
    _save(records, before_path, before, unrecorded)
    issue = None
    if result == "write-failed":
        issue = try_open_issue(records, f"ratchet: {title_key}: the ruleset write failed",
                           _issue_body(event_key, reason, f"PUT answered {error['status']}: {error_message}",
                                       plan["diff"]), issue_errors)
    elif result == "raised-unverified":
        issue = try_open_issue(records, f"ratchet: {title_key}: ruleset after the write differs from the body sent",
                           _issue_body(event_key, reason, "The ruleset read back after the PUT differs "
                                       "from the body sent.", unverified or []), issue_errors)
    elif plan["action"] == "refuse":
        issue = try_open_issue(records, f"ratchet: {title_key}: refused to write",
                           _issue_body(event_key, reason, plan["reason"], plan["diff"]), issue_errors)
    after_record = {"schema": 1, "event_key": event_key, "result": result, "read_at": _now(), "ruleset": after}
    if error:
        after_record["error"] = error
    if unrecorded:
        after_record["unrecorded"] = list(unrecorded)
    _save(records, after_path, after_record, unrecorded)
    out = {"result": result, "issue": issue, "plan_reason": plan["reason"],
           "before_path": before_path, "after_path": after_path,
           "before_updated_at": (live or {}).get("updated_at"),
           "after_updated_at": (after or {}).get("updated_at")}
    if error:
        out["error_status"] = error["status"]
    if unrecorded:
        out["unrecorded"] = unrecorded
    if issue_errors:
        out["issue_errors"] = issue_errors
    return out


def _issue_body(event_key: str, reason: dict, why: str, diff: list) -> str:
    lines = [
        f"The ratchet acted on event `{event_key}`.",
        "",
        f"Reason: {why}",
        "",
        "Event:",
        "```json",
        json.dumps(reason, indent=2, sort_keys=True),
        "```",
    ]
    if diff:
        lines += ["", "Difference:", "```json", json.dumps(diff, indent=2, sort_keys=True), "```"]
    lines += ["", "The ratchet never loosens or repairs a ruleset. A person compares the live ruleset "
              "with `rulesets/svc-hello/l1.json` and restores it by hand, recorded before and after."]
    return "\n".join(lines)
