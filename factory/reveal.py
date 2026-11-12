"""The reveal: publish SEED_KEY at the close, and check everything it unlocks.

Why: the seed schedule was secret during the run so that neither the
delegate nor the reviewer could know which pull requests were seeds
(ADR-0024 §11). At the close the key is published to the records branch
(`close/seed-key.json`), and anyone can then recompute the schedule and check
it against the hash the class file has held since its first commit.

This job also opens every run record's sealed summary (factory/open_pr.py)
and checks each pull request body against it: a real item's body must carry
the agent's summary unchanged, a seed's must carry exactly the misreport the
schedule drew. So the reveal shows not only which pull requests were seeds
but that nothing else was altered.

It refuses to run while the agent's workflow is enabled: the close-out
disables that workflow first (the readiness note's "Close-out"). The agent's
workflow is also disabled for every fixture session, so that alone would let
one dispatch publish the schedule mid-run (ADV-18). It therefore also
refuses while a queue-item pull request is open, while the live rung entry
is not 1 (the close-out sets it to 1 first), and while queue items remain
and the window (eight weeks from the first agent pull request) has not
ended. A close record, `classes/records/close-go-module-bump.yaml` with
`kind: close`, added by a pull request the listed human approves, overrides
the last one: the window closes "when the queue and the runbooks are done,
whichever comes first", which only a person can say.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from typing import Any

from . import classfile
from . import open_pr
from . import prepare
from . import seeds as sd
from . import state as st
from .gh import ApiError, DryRunGitHub, GitHub
from .records import RecordExists, Records, branch_from_env

CONTROLS_REPO = "platform-factory/factory-controls"
SUMMARY_HEADING = "### Agent summary (unverified)\n\n"


def summary_in_body(body: str) -> str | None:
    i = body.find(SUMMARY_HEADING)
    return None if i < 0 else body[i + len(SUMMARY_HEADING):].rstrip()


def verify_records(gh_svc, cf: dict, seed_key: str, run_records: list[dict], seeds_by_pr: dict[int, dict]) -> list[dict]:
    svc = cf["repo"]
    out = []
    for rec in sorted(run_records, key=lambda r: (r.get("opened_at") or "", r.get("pr_number") or 0)):
        n = rec.get("pr_number")
        row: dict[str, Any] = {"pr": n, "run": f"{rec.get('controls_run_id')}-{rec.get('controls_run_attempt')}",
                               "seed": n in seeds_by_pr, "type": (seeds_by_pr.get(n) or {}).get("type")}
        try:
            original = open_pr.unseal(seed_key, rec["sealed_summary"]).decode("utf-8")
            pr = gh_svc.get(f"repos/{svc}/pulls/{n}")
            shown = summary_in_body(pr.get("body") or "")
            if row["seed"]:
                main_mod = st.read_file_at(gh_svc, svc, "go.mod", rec["base_sha"]) or ""
                head_mod = st.read_file_at(gh_svc, svc, "go.mod", rec["head_sha"]) or ""
                item = {"position": rec["queue_position"], "module": rec["module"], "version": rec["version"]}
                expected = sd.substitute(original, row["type"], open_pr.facts_from(main_mod, head_mod, item))
            else:
                expected = original
            row["ok"] = shown == expected.rstrip()
            row["body_matches_record"] = open_pr.sha256_text(pr.get("body") or "") == rec.get("body_sha256")
            if not row["ok"]:
                row["detail"] = "the pull request body's summary is not the one the schedule implies"
        except (KeyError, ValueError, ApiError, sd.SummaryFormatError) as e:
            row["ok"] = False
            row["detail"] = f"{type(e).__name__}: {e}"
        out.append(row)
    return out


def close_guards(state: st.ClassState, snap: dict, cf: dict, now) -> list[str]:
    """Why the reveal may not run yet; empty when it may. Pure."""
    close = [r for r in snap.get("class_records", []) if r.get("kind") == "close"]
    reasons = []
    if state.open_queue_prs:
        reasons.append(f"a queue-item pull request is open: {[p['number'] for p in state.open_queue_prs]}")
    if state.rung != cf["rungs"]["l1"]:
        reasons.append(f"the live rung entry is {state.rung}, not {cf['rungs']['l1']}; the close-out sets it to 1 first")
    if close:
        return reasons
    opened = [st.parse_time(r.get("opened_at")) for r in state.queue_item_prs.values() if r.get("opened_at")]
    first = min(opened) if opened else None
    ends = first + timedelta(weeks=cf["window_weeks"]) if first else None
    if state.next_item is not None and (ends is None or now < ends):
        reasons.append(f"queue item {state.next_item['position']} remains and the window ends {st.iso(ends)}; "
                       "a merged close record (classes/records/close-*.yaml, kind: close) says the run is done")
    return reasons


def reveal(gh_svc, records: Records, cf: dict, seed_key: str, controls_repo: str, run: dict,
           l1: dict | None = None, l2: dict | None = None, now=None) -> dict:
    wf = records.gh.get(f"repos/{controls_repo}/actions/workflows/agent.yml")
    if (wf or {}).get("state") != "disabled_manually":
        raise SystemExit(json.dumps({"error": "the agent workflow is not disabled; disable it before the reveal",
                                     "state": (wf or {}).get("state")}))
    if l1 is None or l2 is None:
        l1, l2 = st.load_committed_rulesets(cf)
    full = st.gather(gh_svc, records, cf, controls_repo=controls_repo)
    state = st.derive_state(full, cf, l1, l2, None)
    guards = close_guards(state, full, cf, now or st.utcnow())
    if guards:
        raise SystemExit(json.dumps({"error": "the run is not closed; the seed key stays secret", "reasons": guards}))
    h = sd.key_sha256(seed_key)
    sched = sd.schedule(seed_key, cf)
    snap = prepare.gather_light(gh_svc, records, cf)
    positions = sorted(s["position"] for s in sched)
    prs = {int(k): v for k, v in snap.get("prs", {}).items()}
    qprs = st.queue_item_prs(snap.get("run_records", []), set(snap.get("fixture_prs", [])), prs)
    merged = st.merged_queue_items(qprs, prs)
    seeds_by_pr: dict[int, dict] = {}
    identified = []
    for s in sched:
        found = sorted(st.identify_seeds(qprs, prs, merged, [s["position"]], cf["rungs"]["l2"]))
        identified.append({**s, "pr": found[0] if found else None})
        if found:
            seeds_by_pr[found[0]] = s
    recorded = sorted(snap.get("seed_record_prs", []))
    verification = verify_records(gh_svc, cf, seed_key, snap.get("run_records", []), seeds_by_pr)
    doc = {
        "schema": 1, "class": cf["class"], "key_hex": seed_key.strip().lower(), "key_sha256": h,
        "class_file_key_sha256": cf["seeds"]["key_sha256"], "matches_class_file": h == cf["seeds"]["key_sha256"],
        "rule": cf["seeds"]["rule"], "schedule": identified,
        "seed_records": recorded,
        "seed_records_agree": sorted(seeds_by_pr) == recorded,
        "verification": verification,
        "all_bodies_verified": all(r.get("ok") for r in verification),
        "revealed_at": st.iso(st.utcnow()), "run": run,
    }
    try:
        records.write("close/seed-key.json", doc, "reveal: the seed key, the schedule and its checks")
        doc["written"] = True
    except RecordExists:
        doc["written"] = False
        doc["note"] = "close/seed-key.json already exists; nothing was overwritten"
    return doc


def main(argv: list[str] | None = None, env: dict | None = None) -> int:
    env = dict(os.environ if env is None else env)
    ap = argparse.ArgumentParser(prog="python3 -m factory.reveal")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        # The key is published only against a filled class file: its hash is
        # what the reveal checks the key against.
        cf = classfile.require(env, {"human_logins[0]", "seeds.key_sha256"})
    except classfile.NotReady as e:
        return classfile.report_not_ready(e.reason, {"phase": "reveal"}, None, env)
    key = env.get("SEED_KEY")
    token = env.get("CONTROLS_TOKEN")
    if not key or not token:
        print(json.dumps({"error": "SEED_KEY and CONTROLS_TOKEN are required"}))
        return 2
    gh: Any = GitHub(token)
    if args.dry_run:
        gh = DryRunGitHub(gh)
    controls = env.get("CONTROLS_REPO", CONTROLS_REPO)
    # svc-hello is public: its pull requests and files are read with the same token.
    doc = reveal(gh, Records(gh, controls, branch_from_env(env)), cf, key, controls,
                 {"run_id": env.get("GITHUB_RUN_ID"), "run_attempt": env.get("GITHUB_RUN_ATTEMPT")})
    if args.dry_run:
        doc["dry_run_writes"] = [{"method": w["method"], "path": w["path"]} for w in gh.writes]
    print(json.dumps(doc, indent=2, sort_keys=True, default=str))
    return 0 if doc["matches_class_file"] and doc["all_bodies_verified"] and doc["seed_records_agree"] else 1


if __name__ == "__main__":
    sys.exit(main())
