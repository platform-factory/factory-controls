"""How many requests each run makes on the controls repo's own token (B4).

GitHub gives a workflow's GITHUB_TOKEN 1,000 REST requests an hour for the
whole repository, shared by every controls workflow; over it, every
controls workflow fails until the hour resets. svc-hello is read with the
two Apps' installation tokens, which have budgets of their own, except in
`prepare` and the scorecard, which read it with GITHUB_TOKEN.

Each run below is a real main() call (tests/test_cli.py's harness) against
a world in the L2 programme's shape: 20 run records (15 merged items, 4
settled seeds, 1 open pull request), 25 fixture records, 5 ledger entries
and one merged fixture record under classes/records/. Requests on the
controls repo are counted by path. Since probe P1 there are two rulesets on
svc-hello; reading the wall ruleset adds one request per detector pass on
the ratchet App's token, none on the controls token.
"""

from __future__ import annotations

import unittest

from factory import classfile, detector, merge_step
from factory import open_pr as op
from factory import seeds as sd
from tests import fakes
from tests.fakes import HUMAN, SEED_KEY, ev, go_mod_with
from tests.scorecard_world import ScoreWorld
from tests.test_cli import Cli, ago

CONTROLS_PREFIX = f"repos/{fakes.CONTROLS}/"
SVC_PREFIX = f"repos/{fakes.SVC}/"
PASS = [("decide", "success"), ("verdict-inconclusive", "success"), ("verdict-fail", "success")]
SHORT = {"golang.org/x/sync": "sync", "golang.org/x/text": "text", "github.com/jackc/pgx/v5": "pgx"}

# The ceiling, per process, on the controls token:
#   2       the records snapshot: the branch's commit, then its tarball
#   1 + 3k  the class records on main: the directory listing, then for each
#           of the k records its content, its first commit and that
#           commit's pull request (state.read_class_records; k = 1 here)
#   w       the records it writes: the detector its heartbeat, each merge
#           step phase its merge-step record; settlement here none
# None of these grows with the number of run, fixture, seed or ledger
# records, which the doubling test below checks.
CLASS_RECORDS = 1
SNAPSHOT = 2
CLASS_READS = 1 + 3 * CLASS_RECORDS
CEILING = {"detector": SNAPSHOT + CLASS_READS + 1, "pre-merge": SNAPSHOT + CLASS_READS + 1,
           "merge": SNAPSHOT + CLASS_READS + 1, "settle": SNAPSHOT + CLASS_READS}


def budget_world(extra_fixtures: int = 0, extra_ledger: int = 0) -> ScoreWorld:
    w = ScoreWorld()
    cf = w.cf
    queue = classfile.queue(cf)
    drawn = sorted(s["position"] for s in sd.schedule(SEED_KEY, cf))
    versions, n, merges, t = {}, 0, 0, 2000
    seeded = set()
    while n < 19:
        n += 1
        item = queue[merges]
        if merges in drawn and merges not in seeded:
            # A settled seed: closed by the listed human, recorded, labelled.
            w.queue_pr(n, item["position"], item["module"], item["version"], ago(t), state="closed",
                       closed_at=ago(t - 5))
            w.prs[n]["labels"] = [{"name": "seeded"}]
            w.records.files[f"seeds/{n}.json"] = {"schema": 1, "pr_number": n, "status": "decided"}
            seeded.add(merges)
        else:
            w.queue_pr(n, item["position"], item["module"], item["version"], ago(t), state="closed",
                       merged_at=ago(t - 10), merge_sha=f"m{n}")
            w.push_run(5000 + n, f"m{n}", [("completed", "success", ago(t - 15))])
            versions[SHORT[item["module"]]] = item["version"]
            merges += 1
        t -= 60
    w.set_main(f"m{n}", go_mod_with(**versions))
    item = queue[merges]
    w.queue_pr(20, item["position"], item["module"], item["version"], ago(30), head_sha="h20")
    w.class_scope_job("h20", 9001, "success", PASS)
    w.go_test_check("h20", "success")
    w.reviews[20] = [{"id": 1, "user": {"login": HUMAN}, "state": "APPROVED", "commit_id": "h20",
                      "submitted_at": ago(20), "body": "minutes: 3; sections: verdict; decision: approve; reason: ok"}]
    w.merge_handler = lambda pr, body: fakes.ApiError(405, {"message": "Repository rule violations found"}, "merge")
    for i in range(25 + extra_fixtures):
        w.records.files[f"fixtures/case-{i}-1-1.json"] = {"id": f"case-{i}", "outcome": "as-expected",
                                                          **({"pr_number": 100 + i} if i < 5 else {})}
    for i, pr in enumerate([1, 2, 3, 5, 6][:5] + list(range(7, 7 + extra_ledger))):
        w.records.files[f"ledger/escape-a-pr{pr}.json"] = {
            "schema": 1, "key": f"escape-a-pr{pr}", "type": "a", "acted_at": ago(1500 - i),
            "event": {"pr": pr, "event_time": ago(1500 - i)}, "ratchet": {"result": "already-1"}}
    w.class_records.append({"name": "fixture-c15.yaml", "kind": "fixture", "merged_at": ago(3000), "pr": 3})
    w.heartbeat(ago(5))
    return w.serve_records_branch()


def count(w, run) -> dict:
    start = len(w.gh.calls)
    run()
    calls = w.gh.calls[start:]
    return {"controls": len([c for c in calls if c["path"].startswith(CONTROLS_PREFIX)]),
            "svc": len([c for c in calls if c["path"].startswith(SVC_PREFIX) or c["path"] == "graphql"])}


def measure(w) -> dict:
    cli = Cli(w.gh)
    try:
        out = {
            "detector": count(w, lambda: cli.run(detector, [])),
            "pre-merge": count(w, lambda: cli.run(merge_step, ["--phase", "pre-merge"])),
            "merge": count(w, lambda: cli.run(merge_step, ["--phase", "merge"],
                                               {"SEED_KEY": SEED_KEY, "PRE_MERGE_OK": "true"})),
            "settle": count(w, lambda: cli.run(op, ["--settle-seeds"], {"SEED_KEY": SEED_KEY})),
        }
        out["merge-result"] = w.records.files["merge-step/300-1-merge.json"].get("result")
    finally:
        cli.close()
    return out


class BudgetTest(unittest.TestCase):
    def test_controls_requests_per_run(self):
        w = budget_world()
        self.assertEqual(len([p for p in w.records.files if p.startswith("runs/")]), 20)
        got = measure(w)
        self.assertEqual(got["merge-result"], "refused")     # the open pull request reached the merge API
        for phase, ceiling in CEILING.items():
            self.assertLessEqual(got[phase]["controls"], ceiling, (phase, got[phase]))
        # Per hour, at six detector runs and two agent runs with a pull
        # request open: 6 x 7 + 2 x (7 + 7 + 6) = 82 requests, against 1,000.
        hour = 6 * got["detector"]["controls"] + 2 * sum(got[p]["controls"] for p in ("pre-merge", "merge", "settle"))
        self.assertLessEqual(hour, 6 * CEILING["detector"] + 2 * (CEILING["pre-merge"] + CEILING["merge"]
                                                                  + CEILING["settle"]))

    def test_the_count_does_not_grow_with_the_records(self):
        small = measure(budget_world())
        big = measure(budget_world(extra_fixtures=25, extra_ledger=5))
        for phase in CEILING:
            self.assertEqual(small[phase]["controls"], big[phase]["controls"], phase)

    def test_the_scorecard_reads_the_records_once_and_each_events_list_once(self):
        """The scorecard reads svc-hello with GITHUB_TOKEN too, so every
        request it makes counts against the same 1,000."""
        import os
        from factory import scorecard as sc
        w = budget_world()
        cli = Cli(w.gh)
        try:
            start = len(w.gh.calls)
            rc, _ = cli.run(sc, ["--out", os.path.join(cli.dir, "scorecard")])
        finally:
            cli.close()
        calls = [c["path"] for c in w.gh.calls[start:]]
        self.assertEqual(rc, 0)
        self.assertEqual(w.tarballs_served, 1)
        events = [p for p in calls if p.endswith("/events")]
        self.assertEqual(len(events), len(set(events)))

    def test_one_snapshot_per_process(self):
        w = budget_world()
        cli = Cli(w.gh)
        try:
            cli.run(merge_step, ["--phase", "pre-merge"])
        finally:
            cli.close()
        self.assertEqual(w.tarballs_served, 1)
        self.assertEqual(len([c for c in w.gh.calls if c["path"].startswith(CONTROLS_PREFIX + "contents/")
                              and not c["path"].startswith(CONTROLS_PREFIX + "contents/classes/")
                              and c["method"] == "GET"]), 0)


if __name__ == "__main__":
    unittest.main()
