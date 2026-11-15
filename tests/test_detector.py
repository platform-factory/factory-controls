"""Escape (a), escape (b), the cutoff, once-per-event, and the ruleset comparison."""

import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

from factory.gh import ApiError
from factory import detector
from factory import state as st
from tests import fakes
from tests.fakes import HUMAN, AGENT_BOT, World, ev, go_mod_with

SYNC = "golang.org/x/sync"
T0 = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)


def at(minutes: float) -> str:
    return st.iso(T0 + timedelta(minutes=minutes))


def ctx(w: World, now_min: float) -> detector.Context:
    return detector.Context(cf=w.cf, l1=fakes.l1(), l2=fakes.l2(), gh_svc=w.gh, records=w.records,
                            now=T0 + timedelta(minutes=now_min), run_id="77", run_attempt="1")


def run(w, now_min, attempts=None):
    # Ledger times (acted_at) come from the clock, not from ctx.now: keep the
    # clock beside the world's, or a test's result depends on the day it runs.
    with mock.patch.object(st, "utcnow", lambda: T0 + timedelta(minutes=now_min)):
        return detector.run(ctx(w, now_min))


def world_with_merge(**kw) -> World:
    """Queue item 1 merged at T0 as m1 (parent p0); queue item 2 open."""
    w = World()
    w.files[("p0", "go.mod")] = fakes.GO_MOD_BASE
    w.files[("p0", "go.sum")] = "sum0\n"
    w.parents["m1"] = "p0"
    w.queue_pr(1, 1, SYNC, "v0.18.0", at(-30), state="closed", merged_at=at(0), merge_sha="m1")
    w.set_main("m1", go_mod_with(sync="v0.18.0"), "sum1\n", parent="p0")
    w.queue_pr(2, 2, SYNC, "v0.19.0", at(1))
    return w


def job(status, concl, done_min):
    return (status, concl, at(done_min) if done_min is not None else None)


class EscapeAPureTest(unittest.TestCase):
    def runs(self, *attempt1_jobs, sha="m1", run_status="completed"):
        out = []
        for i, (status, concl, done) in enumerate(attempt1_jobs):
            out.append({"run": {"id": i + 1, "event": "push", "head_sha": sha, "status": run_status},
                        "jobs": [{"id": 10 + i, "name": "go-test", "status": status, "conclusion": concl,
                                  "completed_at": done, "run_attempt": 1}]})
        return out

    def status(self, runs, now_min):
        return detector.escape_a_status(at(0), "m1", runs, T0 + timedelta(minutes=now_min), 30, "go-test")

    def test_first_attempt_success_within_30_minutes_clears(self):
        self.assertEqual(self.status(self.runs(job("completed", "success", 12)), 13)["status"], "clear")

    def test_first_attempt_failed_is_an_escape_at_its_completion(self):
        r = self.status(self.runs(job("completed", "failure", 8)), 9)
        self.assertEqual((r["status"], r["event_time"], r["reason"]), ("escape", at(8), "failure"))

    def test_cancelled_is_an_escape(self):
        r = self.status(self.runs(job("completed", "cancelled", 5)), 6)
        self.assertEqual((r["status"], r["reason"]), ("escape", "cancelled"))

    def test_missing_run_is_pending_before_the_deadline_and_an_escape_after(self):
        self.assertEqual(self.status([], 10)["status"], "pending")
        r = self.status([], 31)
        self.assertEqual((r["status"], r["event_time"], r["reason"]), ("escape", at(30), "missing"))

    def test_success_after_31_minutes_is_an_escape_at_the_deadline(self):
        r = self.status(self.runs(job("completed", "success", 31)), 35)
        self.assertEqual((r["status"], r["event_time"], r["reason"]), ("escape", at(30), "late"))

    def test_still_running_at_the_deadline_is_an_escape(self):
        self.assertEqual(self.status(self.runs(job("in_progress", None, None), run_status="in_progress"), 20)["status"],
                         "pending")
        r = self.status(self.runs(job("in_progress", None, None), run_status="in_progress"), 30)
        self.assertEqual((r["status"], r["event_time"]), ("escape", at(30)))

    def test_a_run_on_another_commit_does_not_clear(self):
        r = self.status(self.runs(job("completed", "success", 5), sha="other"), 31)
        self.assertEqual((r["status"], r["reason"]), ("escape", "missing"))

    def test_a_later_attempts_job_passed_by_mistake_is_ignored(self):
        runs = self.runs(job("completed", "success", 5))
        runs[0]["jobs"][0]["run_attempt"] = 2
        self.assertEqual(self.status(runs, 31)["status"], "escape")


class EscapeAWorldTest(unittest.TestCase):
    def test_rerun_success_after_failed_first_attempt_is_still_an_escape(self):
        w = world_with_merge()
        w.push_run(501, "m1", [job("completed", "failure", 8), job("completed", "success", 20)])
        out = run(w, 21)
        self.assertEqual(out["acted"], ["escape-a-pr1"])
        entry = w.records.read("ledger/escape-a-pr1.json")
        self.assertEqual((entry["type"], entry["event"]["event_time"]), ("a", at(8)))
        self.assertEqual(w.rung(), 1)

    def test_first_attempt_success_is_no_escape(self):
        w = world_with_merge()
        w.push_run(501, "m1", [job("completed", "success", 9)])
        self.assertEqual(run(w, 40)["acted"], [])

    def test_missing_run_after_30_minutes_is_an_escape(self):
        w = world_with_merge()
        self.assertEqual(run(w, 20)["pending"], [{"key": "escape-a-pr1", "pr": 1, "deadline": at(30)}])
        self.assertEqual(run(w, 31)["acted"], ["escape-a-pr1"])

    def test_freeze_at_l2_raises_labels_open_agent_prs_and_writes_everything(self):
        w = world_with_merge()
        w.class_records.append({"name": "promotion-1.yaml", "kind": "promotion", "merged_at": at(-60)})
        w.set_rung(0)
        w.push_run(501, "m1", [job("completed", "failure", 8)])
        w.human_pr(60, at(2))  # a person's pull request is not labelled
        out = run(w, 10)
        self.assertEqual(out["acted"], ["escape-a-pr1"])
        self.assertEqual(w.rung(), 1)
        labels = w.gh.writes("POST", "repos/platform-factory/svc-hello/issues/")
        self.assertEqual([(c["path"], c["body"]) for c in labels],
                         [("repos/platform-factory/svc-hello/issues/2/labels", {"labels": ["needs-human"]})])
        entry = w.records.read("ledger/escape-a-pr1.json")
        self.assertEqual(entry["labelled_needs_human"], [2])
        self.assertEqual(entry["ratchet"]["result"], "raised")
        for p in ("ratchet/escape-a-pr1-before.json", "ratchet/escape-a-pr1-after.json", "detector/heartbeat.json"):
            self.assertIsNotNone(w.records.read(p), p)
        self.assertTrue(out["ruleset"]["ok"])  # back at L1, which is always accepted
        self.assertEqual(out["rung"], 1)

    def test_a_refused_ruleset_write_is_recorded_not_a_crash(self):
        """ADV-08: the ratchet's first real PUT is C-18's injection (a). A 4xx
        there leaves the run standing: ledger entry, labels and an issue, and
        the merge step refuses because a ruleset difference stands."""
        w = world_with_merge()
        w.class_records.append({"name": "promotion-1.yaml", "kind": "promotion", "merged_at": at(-60)})
        w.set_rung(0)
        w.push_run(501, "m1", [job("completed", "failure", 8)])
        w.gh.route("PUT", r"repos/platform-factory/svc-hello/rulesets/20204893", lambda m, p, b: ApiError(
            422, {"message": "Invalid property /rules/0: data matches no possible input"}, m.group(0)))
        out = run(w, 10)
        self.assertIn("escape-a-pr1", out["acted"])
        entry = w.records.read("ledger/escape-a-pr1.json")
        self.assertEqual(entry["ratchet"]["result"], "write-failed")
        self.assertEqual(entry["labelled_needs_human"], [2])
        self.assertTrue(any("the ruleset write failed" in i["title"] for i in w.issues))
        self.assertEqual(w.rung(), 0)
        # In a run, acted_at is after the record, so L2 is no longer accepted
        # and the ruleset comparison reports the difference (the merge step
        # refuses); that rule is tested in RulesetTest. Here the fake world's
        # times lie after the real clock, so it is not asserted.

    def test_the_rung_rises_when_the_records_branch_cannot_be_written(self):
        """B4: the controls token over its hourly limit must not hold back a
        demotion. The rung rises and the open pull request is labelled; the
        run then fails at the ledger write, and the next run finishes it."""
        import contextlib
        import io
        from tests.test_ratchet import RefusingRecords
        w = world_with_merge()
        w.class_records.append({"name": "promotion-1.yaml", "kind": "promotion", "merged_at": at(-60)})
        w.set_rung(0)
        w.push_run(501, "m1", [job("completed", "failure", 8)])
        refusing = RefusingRecords(w.gh)
        refusing.files = w.records.files
        c = ctx(w, 10)
        c.records = refusing
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(ApiError):
            detector.run(c)
        self.assertEqual(w.rung(), 1)
        self.assertEqual([x["path"] for x in w.gh.writes("POST", "repos/platform-factory/svc-hello/issues/")],
                         ["repos/platform-factory/svc-hello/issues/2/labels"])
        out = run(w, 20)                                   # the branch is writable again
        self.assertEqual(out["acted"], ["escape-a-pr1"])
        self.assertEqual(w.records.read("ledger/escape-a-pr1.json")["ratchet"]["result"], "already-1")

    def test_double_run_is_idempotent(self):
        w = world_with_merge()
        w.class_records.append({"name": "promotion-1.yaml", "kind": "promotion", "merged_at": at(-60)})
        w.set_rung(0)
        w.push_run(501, "m1", [job("completed", "failure", 8)])
        first = run(w, 10)
        writes_after_first = len([c for c in w.gh.calls if c["method"] != "GET"])
        files_after_first = dict(w.records.files)
        second = run(w, 20)
        self.assertEqual(first["acted"], ["escape-a-pr1"])
        self.assertEqual(second["acted"], [])
        self.assertEqual(len([c for c in w.gh.calls if c["method"] != "GET"]), writes_after_first)
        self.assertEqual(len(w.gh.writes("PUT", "repos/platform-factory/svc-hello/rulesets")), 1)
        changed = {k for k in w.records.files if w.records.files[k] != files_after_first.get(k)}
        self.assertLessEqual(changed, {"detector/heartbeat.json"})
        self.assertEqual(set(w.records.files), set(files_after_first))

    def test_event_before_the_latest_record_is_ignored(self):
        w = world_with_merge()
        w.push_run(501, "m1", [job("completed", "failure", 8)])
        w.class_records.append({"name": "fixture-1.yaml", "kind": "fixture", "merged_at": at(15)})
        w.set_rung(0)
        out = run(w, 40)
        self.assertEqual(out["acted"], [])
        self.assertEqual(out["ignored_before_latest_record"], ["escape-a-pr1"])
        self.assertEqual(w.rung(), 0)
        self.assertTrue(out["ruleset"]["ok"])  # L2 accepted from the fixture record

    def test_event_after_the_latest_record_is_acted_on(self):
        w = world_with_merge()
        w.push_run(501, "m1", [job("completed", "failure", 25)])
        w.class_records.append({"name": "fixture-1.yaml", "kind": "fixture", "merged_at": at(15)})
        w.set_rung(0)
        self.assertEqual(run(w, 26)["acted"], ["escape-a-pr1"])
        self.assertEqual(w.rung(), 1)


class RetryTest(unittest.TestCase):
    """B5: after a failed ruleset PUT, each later detector and pre-merge run
    tries the raise again while the rung entry is still 0."""

    def world(self):
        w = world_with_merge()
        w.class_records.append({"name": "promotion-1.yaml", "kind": "promotion", "merged_at": at(-60)})
        w.set_rung(0)
        w.push_run(501, "m1", [job("completed", "failure", 8)])
        self.put_fails = True

        def put(m, p, b):
            if self.put_fails:
                return ApiError(502, {"message": "Bad Gateway"}, m.group(0))
            w.ruleset = fakes.as_get(b, at(50))
            return w.ruleset

        w.gh.route("PUT", r"repos/platform-factory/svc-hello/rulesets/20204893", put)
        return w

    def run_at(self, w, now_min, phase="detector"):
        # Ledger times come from the clock; keep it beside the world's.
        with mock.patch.object(st, "utcnow", lambda: T0 + timedelta(minutes=now_min)):
            c = ctx(w, now_min)
            c.phase, c.run_id = phase, str(now_min)
            if phase == "pre-merge":
                from factory import merge_step
                return merge_step.pre_merge(c)["detector"]
            return detector.run(c)

    def test_retried_on_each_run_until_the_rung_is_1(self):
        w = self.world()
        first = self.run_at(w, 10)
        self.assertEqual((first["acted"], first["retried"], w.rung()), (["escape-a-pr1"], [], 0))
        self.assertEqual(w.records.read("ledger/escape-a-pr1.json")["ratchet"]["result"], "write-failed")
        self.assertFalse(first["ruleset"]["ok"])                       # the merge step refuses meanwhile
        second = self.run_at(w, 20)                                     # the PUT still fails
        self.assertEqual(second["retried"], ["escape-a-pr1-retry-detector-20-1"])
        entry = w.records.read("ledger/escape-a-pr1-retry-detector-20-1.json")
        self.assertEqual((entry["type"], entry["retry_of"], entry["ratchet"]["result"]),
                         ("ratchet-retry", "escape-a-pr1", "write-failed"))
        self.assertIsNotNone(w.records.read("ratchet/escape-a-pr1-retry-detector-20-1-before.json"))
        self.assertEqual(len([i for i in w.issues if "the ruleset write failed" in i["title"]]), 1)
        self.put_fails = False
        third = self.run_at(w, 30)
        self.assertEqual(third["retried"], ["escape-a-pr1-retry-detector-30-1"])
        self.assertEqual(w.rung(), 1)
        self.assertTrue(third["ruleset"]["ok"])
        self.assertEqual(w.records.read("ledger/escape-a-pr1-retry-detector-30-1.json")["ratchet"]["result"],
                         "raised")
        self.assertEqual(self.run_at(w, 40)["retried"], [])             # the rung is 1: nothing to retry
        self.assertEqual(len(w.gh.writes("PUT", "repos/platform-factory/svc-hello/rulesets")), 3)
        self.assertEqual([e["key"] for e in st.escapes_from_ledger(
            [w.records.read(p) for p in w.records.list("ledger/")])], ["escape-a-pr1"])   # one demotion

    def test_a_pre_merge_run_retries_too(self):
        w = self.world()
        w.heartbeat(at(5))
        self.run_at(w, 10)
        self.put_fails = False
        det = self.run_at(w, 15, phase="pre-merge")
        self.assertEqual(det["retried"], ["escape-a-pr1-retry-pre-merge-15-1"])
        self.assertEqual(w.rung(), 1)
        self.assertEqual(w.records.read("ledger/escape-a-pr1-retry-pre-merge-15-1.json")["phase"], "pre-merge")

    def test_when_not_to_retry(self):
        cf = fakes.load_cf()
        failed = {"key": "escape-a-pr1", "type": "a", "acted_at": at(10), "ratchet": {"result": "write-failed"}}
        record = [{"kind": "promotion", "merged_at": at(-60)}]
        f = detector.raise_to_retry
        self.assertEqual(f([failed], record, 0, cf, set()), failed)
        self.assertIsNone(f([failed], record, 1, cf, set()))                      # the rung is 1
        self.assertIsNone(f([failed], record, -1, cf, set()))                     # rung entry not found once
        self.assertIsNone(f([failed], record, 0, cf, {"escape-a-pr1"}))           # acted on in this run
        self.assertIsNone(f([{**failed, "ratchet": {"result": "refused"}}], record, 0, cf, set()))
        later = record + [{"kind": "fixture", "merged_at": at(20)}]
        self.assertIsNone(f([failed], later, 0, cf, set()))                       # L2 is a person's act again
        raised_later = {"key": "ruleset-x", "type": "ruleset", "acted_at": at(15), "ratchet": {"result": "raised"}}
        self.assertIsNone(f([failed, raised_later], record, 0, cf, set()))        # the latest demotion raised
        retry = {"key": "escape-a-pr1-retry-detector-12-1", "type": "ratchet-retry", "acted_at": at(12),
                 "ratchet": {"result": "write-failed"}}
        self.assertEqual(f([failed, retry], record, 0, cf, set()), failed)        # a retry is not a demotion


class EscapeBTest(unittest.TestCase):
    def revert_world(self, label_event):
        w = world_with_merge()
        w.push_run(501, "m1", [job("completed", "success", 9)])
        w.human_pr(70, at(40), merged_at=at(60), merge_sha="r70")
        w.files[("r70", "go.mod")] = fakes.GO_MOD_BASE
        w.files[("r70", "go.sum")] = "sum0\n"
        if label_event:
            w.events[70].append(label_event)
        return w

    def test_listed_human_revert_with_label_is_an_escape(self):
        w = self.revert_world(ev("labeled", at(55), HUMAN, "escape:revert"))
        out = run(w, 61)
        self.assertEqual(out["acted"], ["escape-b-pr70"])
        entry = w.records.read("ledger/escape-b-pr70.json")
        self.assertEqual((entry["event"]["reverted_pr"], entry["event"]["event_time"]), (1, at(60)))

    def test_label_from_an_app_is_not_an_escape(self):
        w = self.revert_world(ev("labeled", at(55), AGENT_BOT, "escape:revert", app="platform-factory-agent"))
        out = run(w, 61)
        self.assertEqual(out["acted"], [])
        self.assertTrue(any(u["type"] == "label-event-not-counted" for u in out["unknowns"]))

    def test_app_removal_of_the_human_label_does_not_stop_the_escape(self):
        w = self.revert_world(ev("labeled", at(55), HUMAN, "escape:revert"))
        w.events[70].append(ev("unlabeled", at(58), AGENT_BOT, "escape:revert", app="platform-factory-agent"))
        self.assertEqual(run(w, 61)["acted"], ["escape-b-pr70"])

    def test_label_without_a_matching_revert_is_an_unknown_not_an_escape(self):
        w = self.revert_world(ev("labeled", at(55), HUMAN, "escape:revert"))
        w.files[("r70", "go.sum")] = "something else\n"
        out = run(w, 61)
        self.assertEqual(out["acted"], [])
        self.assertTrue(any(u["type"] == "escape-revert-unmatched" for u in out["unknowns"]))

    def test_no_label_no_escape(self):
        self.assertEqual(run(self.revert_world(None), 61)["acted"], [])


class RulesetComparisonTest(unittest.TestCase):
    def test_l2_without_a_record_is_raised_and_reported(self):
        w = world_with_merge()
        w.push_run(501, "m1", [job("completed", "success", 9)])
        w.set_rung(0)
        out = run(w, 40)
        self.assertEqual(len(out["acted"]), 1)
        self.assertTrue(out["acted"][0].startswith("ruleset-"))
        self.assertEqual(w.rung(), 1)
        self.assertEqual(len(w.issues), 1)
        self.assertTrue(out["ruleset"]["ok"])

    def test_l2_accepted_from_a_record_until_the_next_demotion(self):
        w = world_with_merge()
        w.push_run(501, "m1", [job("completed", "success", 9)])
        w.class_records.append({"name": "promotion-1.yaml", "kind": "promotion", "merged_at": at(5)})
        w.set_rung(0)
        out = run(w, 40)
        self.assertEqual(out["acted"], [])
        self.assertTrue(out["ruleset"]["l2_accepted"])
        # a demotion after the record: from then on only L1 is accepted
        w.records.files["ledger/escape-b-pr99.json"] = {"key": "escape-b-pr99", "type": "b",
                                                        "event": {"event_time": at(41)}, "acted_at": at(42)}
        w.set_rung(0)  # someone set L2 again by hand, without a new record
        out = run(w, 50)
        self.assertTrue(out["acted"][0].startswith("ruleset-"))
        self.assertEqual(w.rung(), 1)
        # a record merged after the demotion makes L2 acceptable again
        w.class_records.append({"name": "fixture-1.yaml", "kind": "fixture", "merged_at": at(55)})
        w.set_rung(0)
        out = run(w, 60)
        self.assertEqual(out["acted"], [])
        self.assertTrue(out["ruleset"]["ok"])

    def test_any_other_difference_is_acted_on_once_and_stands(self):
        w = world_with_merge()
        w.push_run(501, "m1", [job("completed", "success", 9)])
        for r in w.ruleset["rules"]:
            if r["type"] == "pull_request":
                r["parameters"]["dismiss_stale_reviews_on_push"] = False  # stale dismissal turned off
        out = run(w, 40)
        self.assertEqual(len(out["acted"]), 1)
        self.assertFalse(out["ruleset"]["ok"])
        self.assertEqual(len(w.issues), 1)
        self.assertEqual(w.gh.writes("PUT", "repos/platform-factory/svc-hello/rulesets"), [])  # rung already 1
        out2 = run(w, 50)
        self.assertEqual(out2["acted"], [])
        self.assertFalse(out2["ruleset"]["ok"])
        self.assertEqual(len(w.issues), 1)

    def test_rung_raise_with_another_difference_is_not_acted_on_twice(self):
        w = world_with_merge()
        w.push_run(501, "m1", [job("completed", "success", 9)])
        w.set_rung(0)
        w.ruleset["enforcement"] = "evaluate"
        out = run(w, 40)
        self.assertEqual(len(out["acted"]), 1)
        self.assertEqual(w.rung(), 1)
        self.assertEqual(w.ruleset["enforcement"], "evaluate")  # never repaired by the ratchet
        self.assertEqual(run(w, 50)["acted"], [])

    def test_an_extra_ruleset_on_the_repository_is_a_difference(self):
        w = world_with_merge()
        w.push_run(501, "m1", [job("completed", "success", 9)])
        w.extra_rulesets.append({"id": 1, "name": "org-wide", "target": "branch", "source_type": "Organization",
                                 "source": "platform-factory", "enforcement": "active",
                                 "updated_at": "2026-11-12T00:00:00Z"})
        out = run(w, 40)
        self.assertFalse(out["ruleset"]["ok"])
        self.assertEqual(out["ruleset"]["list_diff"][0]["path"], [])  # the list itself differs
        self.assertEqual(len(out["acted"]), 1)

    def test_timestamps_and_ids_are_not_differences(self):
        live = fakes.as_get(fakes.l1(), "2030-01-01T00:00:00Z")
        live["id"] = 1
        wall = fakes.as_get(fakes.wall(), "2031-01-01T00:00:00Z", fakes.WALL_ID)
        listing = [{"id": fakes.RUNG_ID, "name": "main-protection", "source_type": "Repository",
                    "source": fakes.SVC, "target": "branch", "updated_at": "x"},
                   {"id": fakes.WALL_ID, "name": "class-wall", "source_type": "Repository",
                    "source": fakes.SVC, "target": "branch", "updated_at": "y"}]
        chk = detector.ruleset_check({"rung": live, "wall": wall, "listing": listing}, fakes.l1(), fakes.l2(),
                                     fakes.wall(), False, fakes.load_cf()["rulesets"]["repository_rulesets"])
        self.assertTrue(chk["ok"])
        self.assertEqual(chk["wall_diff"], [])


class WallRulesetTest(unittest.TestCase):
    """Probe P1: GitHub refuses two required-reviewers entries for one team in
    one ruleset, so the wall entry is in a ruleset of its own (the note's
    fallback). It must equal wall.json, active, on every rung; anything else
    is a ruleset difference with the same consequences as one in the rung
    ruleset, and nothing ever writes the wall."""

    WALL_PATH = f"repos/{fakes.SVC}/rulesets/{fakes.WALL_ID}"

    def world(self):
        w = world_with_merge()
        w.push_run(501, "m1", [job("completed", "success", 9)])
        return w

    def assert_difference(self, w, rung_before):
        out = run(w, 40)
        self.assertEqual(len(out["acted"]), 1, out["ruleset"])
        self.assertTrue(out["acted"][0].startswith("ruleset-"))
        self.assertFalse(out["ruleset"]["ok"])
        self.assertNotEqual(out["ruleset"]["wall_diff"], [])
        self.assertEqual(w.rung(), 1)                              # raised from 0, or already 1
        self.assertEqual(len(w.issues), 1)
        self.assertIn("wall_diff", w.issues[0]["body"])
        self.assertEqual([c for c in w.gh.calls if c["path"].startswith(self.WALL_PATH) and c["method"] != "GET"], [])
        entry = w.records.read(f"ledger/{out['acted'][0]}.json")
        self.assertEqual(entry["ratchet"]["result"], "raised" if rung_before == 0 else "already-1")
        self.assertEqual(run(w, 50)["acted"], [])                  # acted on once; it stands
        return out

    def test_the_wall_disabled_is_a_difference(self):
        w = self.world()
        w.wall["enforcement"] = "disabled"
        out = self.assert_difference(w, 1)
        self.assertEqual(out["ruleset"]["wall_diff"][0]["path"], ["enforcement"])
        self.assertEqual(out["ruleset"]["matches"], "l1")          # the rung ruleset is fine

    def test_the_wall_changed_at_l2_raises_the_rung(self):
        w = self.world()
        w.class_records.append({"name": "promotion-1.yaml", "kind": "promotion", "merged_at": at(5)})
        w.set_rung(0)                                              # L2, accepted from the record
        for r in w.wall["rules"]:
            r["parameters"]["required_reviewers"][0]["minimum_approvals"] = 0
        self.assert_difference(w, 0)

    def test_the_wall_missing_is_a_difference(self):
        w = self.world()
        w.wall = None                                              # deleted: not found, and not listed
        out = self.assert_difference(w, 1)
        self.assertEqual(out["ruleset"]["wall_diff"], [{"path": [], "expected": "ruleset", "actual": "not found"}])
        self.assertNotEqual(out["ruleset"]["list_diff"], [])

    def test_the_wall_at_l2_is_unchanged_and_accepted(self):
        w = self.world()
        w.class_records.append({"name": "promotion-1.yaml", "kind": "promotion", "merged_at": at(5)})
        w.set_rung(0)
        out = run(w, 40)
        self.assertEqual((out["acted"], out["ruleset"]["ok"], out["ruleset"]["wall_diff"]), ([], True, []))

    def test_a_wall_edit_is_a_new_version_of_the_rulesets(self):
        """The event key covers the wall's version: a second, later wall edit is acted on too."""
        w = self.world()
        w.wall["enforcement"] = "disabled"
        first = run(w, 40)["acted"]
        w.wall["enforcement"] = "evaluate"
        w.wall["updated_at"] = "2026-11-12T12:00:00.000-08:00"
        second = run(w, 50)["acted"]
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first, second)


class HeartbeatTest(unittest.TestCase):
    def test_heartbeat_written_only_when_asked(self):
        w = world_with_merge()
        w.push_run(501, "m1", [job("completed", "success", 9)])
        detector.run(ctx(w, 40), write_heartbeat=False)
        self.assertIsNone(w.records.read("detector/heartbeat.json"))
        detector.run(ctx(w, 40))
        hb = w.records.read("detector/heartbeat.json")
        self.assertEqual((hb["run_id"], hb["ruleset_ok"], hb["rung"]), ("77", True, 1))


if __name__ == "__main__":
    unittest.main()
