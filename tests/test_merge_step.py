"""The merge step: pre-merge refusals, and merge, close, update or wait on the one agent PR."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from factory import detector, merge_step
from factory import state as st
from factory.gh import ApiError
from tests import fakes
from tests.fakes import World, go_mod_with

SYNC = "golang.org/x/sync"
T0 = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
PASS_STEPS = [("decide", "success"), ("verdict-inconclusive", "success"), ("verdict-fail", "success")]
FAIL_STEPS = [("decide", "success"), ("verdict-inconclusive", "success"), ("verdict-fail", "failure")]
INC_STEPS = [("decide", "success"), ("verdict-inconclusive", "failure"), ("verdict-fail", "skipped")]


def at(m):
    return st.iso(T0 + timedelta(minutes=m))


def ctx(w, now_min, phase):
    return detector.Context(cf=w.cf, l1=fakes.l1(), l2=fakes.l2(), gh_svc=w.gh, records=w.records,
                            now=T0 + timedelta(minutes=now_min), run_id="88", run_attempt="1", phase=phase)


def base_world():
    """Item 1 merged at T0 (m1, go-test passed on push); item 2 open as PR 2 on head h2."""
    w = World()
    w.queue_pr(1, 1, SYNC, "v0.18.0", at(-30), state="closed", merged_at=at(0), merge_sha="m1")
    w.set_main("m1", go_mod_with(sync="v0.18.0"), parent="p0")
    w.push_run(501, "m1", [("completed", "success", at(9))])
    w.queue_pr(2, 2, SYNC, "v0.19.0", at(10), head_sha="h2")
    w.heartbeat(at(35))  # five minutes before the usual test clock of T0+40
    return w


class DecideMergeTest(unittest.TestCase):
    PR = {"number": 2, "queue_item": True, "head_sha": "h2"}
    PASS = {"verdict": "pass", "head_sha": "h2"}

    def d(self, prs=None, seeds=(), key=True, behind=0, cs=None, gt=None):
        return merge_step.decide_merge([self.PR] if prs is None else prs, list(seeds), key, behind,
                                       cs if cs is not None else self.PASS, gt if gt is not None else {"verdict": "pass"})

    def test_table(self):
        self.assertEqual(self.d(prs=[])["action"], "none")
        self.assertEqual(self.d(prs=[self.PR, {**self.PR, "number": 3}])["action"], "refuse")
        self.assertEqual(self.d(prs=[{**self.PR, "queue_item": False}])["action"], "skip")
        self.assertEqual(self.d(key=False)["action"], "refuse")
        self.assertEqual(self.d(seeds=[2], cs={"verdict": "fail"})["action"], "wait-seed")
        self.assertEqual(self.d(behind=1, cs={"verdict": "fail"})["action"], "update-branch")
        self.assertEqual(self.d(cs={"verdict": "fail"})["action"], "close")
        self.assertEqual(self.d(gt={"verdict": "fail"})["action"], "close")
        self.assertEqual(self.d(cs={"verdict": "inconclusive"})["action"], "wait")
        self.assertEqual(self.d(cs={"verdict": "inconclusive"}, gt={"verdict": "fail"})["action"], "close")
        self.assertEqual(self.d(gt={"verdict": "pending"})["action"], "wait")
        self.assertEqual(self.d(cs={"verdict": "pass", "head_sha": "old"})["action"], "wait")
        self.assertEqual(self.d()["action"], "merge")

    def test_seed_rows(self):
        D = merge_step.decide_merge
        pr, ok = [self.PR], {"verdict": "pass", "head_sha": "h2"}
        # Before the decision a seed follows the item's path, except that it is never merged or closed.
        self.assertEqual(D(pr, [2], True, 1, ok, ok)["action"], "update-branch")
        self.assertEqual(D(pr, [2], True, 0, ok, ok, review_hold=True)["action"], "wait")
        self.assertEqual(D(pr, [], True, 0, ok, ok, review_hold=True)["action"], "wait")
        self.assertEqual(D(pr, [], True, 0, ok, ok, review_hold=True, approved=True)["action"], "merge")
        self.assertEqual(D(pr, [2], True, 0, {"verdict": "fail"}, ok)["action"], "wait-seed")
        self.assertEqual(D(pr, [2], True, 0, ok, ok, review_hold=False)["action"], "wait-seed")
        self.assertEqual(D(pr, [2], True, 1, ok, ok, seed_decided=True)["action"], "wait-seed")


class PreMergeTest(unittest.TestCase):
    def test_all_clear_is_ok(self):
        out = merge_step.pre_merge(ctx(base_world(), 40, "pre-merge"))
        self.assertTrue(out["ok"], out["reasons"])

    def test_refuses_while_the_wall_ruleset_differs(self):
        """The wall entry is in a ruleset of its own (probe P1's fallback); a
        wall that is disabled, changed or gone stops merges as a difference in
        the rung ruleset does, and the refusal stands until it is restored."""
        for change in ("disabled", "missing"):
            w = base_world()
            if change == "disabled":
                w.wall["enforcement"] = "disabled"
            else:
                w.wall = None
            out = merge_step.pre_merge(ctx(w, 40, "pre-merge"))
            self.assertFalse(out["ok"], change)
            self.assertIn("a ruleset difference stands", out["reasons"])
            self.assertEqual(len(out["detector"]["acted"]), 1)
            again = merge_step.pre_merge(ctx(w, 45, "pre-merge"))
            self.assertEqual((again["ok"], again["detector"]["acted"]), (False, []))

    def test_refuses_while_mains_latest_push_go_test_has_not_succeeded(self):
        w = base_world()
        w.runs.clear()
        w.push_run(501, "m1", [("completed", "failure", at(9))])
        w.records.files["ledger/escape-a-pr1.json"] = {"key": "escape-a-pr1", "type": "a",
                                                       "event": {"event_time": at(9)}, "acted_at": at(10)}
        out = merge_step.pre_merge(ctx(w, 40, "pre-merge"))
        self.assertFalse(out["ok"])
        self.assertIn("go-test has not succeeded", out["reasons"][0])

    def test_a_passing_rerun_on_main_unblocks_merges_but_the_escape_stands(self):
        w = base_world()
        w.runs.clear()
        w.push_run(501, "m1", [("completed", "failure", at(9)), ("completed", "success", at(25))])
        out = merge_step.pre_merge(ctx(w, 40, "pre-merge"))
        self.assertEqual(out["detector"]["acted"], ["escape-a-pr1"])  # demoted first
        self.assertTrue(out["ok"], out["reasons"])

    def test_refuses_when_no_push_run_exists_for_mains_tip(self):
        w = base_world()
        w.set_main("h-human", go_mod_with(sync="v0.18.0"))
        out = merge_step.pre_merge(ctx(w, 40, "pre-merge"))
        self.assertFalse(out["ok"])
        self.assertIn("no push run", " ".join(out["reasons"]))

    def test_refuses_while_the_detector_heartbeat_is_stale(self):
        w = base_world()
        w.heartbeat(at(40 - 31))
        out = merge_step.pre_merge(ctx(w, 40, "pre-merge"))
        self.assertFalse(out["ok"])
        self.assertIn("older than 30 minutes", out["reasons"][0])
        del w.records.files["detector/heartbeat.json"]
        self.assertIn("no complete detector run", merge_step.pre_merge(ctx(w, 40, "pre-merge"))["reasons"][0])

    def test_refuses_while_a_ruleset_difference_stands(self):
        w = base_world()
        w.ruleset["enforcement"] = "disabled"
        out = merge_step.pre_merge(ctx(w, 40, "pre-merge"))
        self.assertFalse(out["ok"])
        self.assertIn("ruleset difference", out["reasons"][0])

    def test_demotes_first_on_an_unhandled_event(self):
        w = base_world()
        w.class_records.append({"name": "promotion-1.yaml", "kind": "promotion", "merged_at": at(-60)})
        w.set_rung(0)
        w.queue_pr(3, 3, SYNC, "v0.20.0", at(41), state="closed", merged_at=at(45), merge_sha="m3")
        w.set_main("m3", go_mod_with(sync="v0.20.0"))
        w.push_run(503, "m3", [("completed", "failure", at(50))])
        out = merge_step.pre_merge(ctx(w, 51, "pre-merge"))
        self.assertEqual(out["detector"]["acted"], ["escape-a-pr3"])
        self.assertEqual(w.rung(), 1)
        self.assertFalse(out["ok"])  # main's push run failed
        self.assertIsNone(w.records.files["detector/heartbeat.json"].get("run_id"))  # pre-merge writes none


class MergePhaseTest(unittest.TestCase):
    def setUp(self):
        self.p = mock.patch.object(st, "seed_positions_from_key", lambda key, cf: [2, 9, 12, 15])
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_merge_sends_verified_head_squash_and_fixed_text(self):
        w = base_world()
        w.review(2, "h2")
        w.class_scope_job("h2", 7001, "success", PASS_STEPS)
        w.go_test_check("h2", "success")
        out = merge_step.merge(ctx(w, 40, "merge"), fakes.SEED_KEY, True)
        self.assertEqual((out["action"], out["result"]), ("merge", "merged"))
        put = w.gh.writes("PUT", "repos/platform-factory/svc-hello/pulls/2/merge")[0]["body"]
        self.assertEqual(put["sha"], "h2")
        self.assertEqual(put["merge_method"], "squash")
        self.assertEqual(put["commit_title"], "go-module-bump: golang.org/x/sync v0.19.0 (queue item 2)")
        self.assertIn("Head commit verified by class-scope: h2", put["commit_message"])
        self.assertIn("Controls run: 88, attempt 1", put["commit_message"])

    def test_the_record_holds_review_decision_and_merge_state_status(self):
        """C-15's L1 refusal is graded on GitHub's own view at decision time."""
        w = base_world()
        w.class_scope_job("h2", 7001, "success", PASS_STEPS)
        w.go_test_check("h2", "success")
        waiting = merge_step.merge(ctx(w, 40, "merge"), fakes.SEED_KEY, True)
        self.assertEqual(waiting["action"], "wait")
        self.assertEqual(waiting["pr_status"], {"reviewDecision": "REVIEW_REQUIRED", "mergeStateStatus": "BLOCKED",
                                                "headRefOid": "h2", "error": None})
        w.review(2, "h2")
        merged = merge_step.merge(ctx(w, 41, "merge"), fakes.SEED_KEY, True)
        self.assertEqual((merged["action"], merged["pr_status"]["reviewDecision"],
                          merged["pr_status"]["mergeStateStatus"]), ("merge", "APPROVED", "CLEAN"))
        # One read per run, and it is a query: nothing is written by it.
        self.assertEqual(len([c for c in w.gh.calls if c["path"] == "graphql"]), 2)
        self.assertEqual(w.gh.writes("POST", "graphql"), [])

    def test_a_failed_status_read_is_recorded_and_decides_nothing(self):
        w = base_world()
        w.review(2, "h2")
        w.class_scope_job("h2", 7001, "success", PASS_STEPS)
        w.go_test_check("h2", "success")
        w.gh.route("POST", "graphql", lambda m, p, b: ApiError(502, {"message": "Bad Gateway"}, "graphql"))
        out = merge_step.merge(ctx(w, 40, "merge"), fakes.SEED_KEY, True)
        self.assertEqual((out["action"], out["result"]), ("merge", "merged"))
        self.assertEqual(out["pr_status"]["error"], {"status": 502, "message": "Bad Gateway"})
        self.assertIsNone(out["pr_status"]["reviewDecision"])

    def test_405_is_a_recorded_refusal_not_an_error(self):
        w = base_world()
        w.review(2, "h2")
        w.class_scope_job("h2", 7001, "success", PASS_STEPS)
        w.go_test_check("h2", "success")
        w.merge_handler = lambda n, b: ApiError(405, {"message": "Pull Request is not mergeable"}, "merge")
        out = merge_step.merge(ctx(w, 40, "merge"), fakes.SEED_KEY, True)
        self.assertEqual(out["result"], "refused")
        self.assertEqual(out["response"], {"status": 405, "body": {"message": "Pull Request is not mergeable"}})
        self.assertTrue(out["agent_pr_open"])
        with mock.patch.object(detector, "context_from_env", lambda *a, **k: ctx(w, 40, "merge")):
            with tempfile.NamedTemporaryFile("r", delete=False) as f:
                env = {"SEED_KEY": fakes.SEED_KEY, "PRE_MERGE_OK": "true", "GITHUB_OUTPUT": f.name}
                with mock.patch("sys.stdout"):
                    rc = merge_step.main(["--phase", "merge"], env)
                self.assertEqual(rc, 0)
                with open(f.name) as out_file:
                    self.assertEqual(out_file.read(), "action=merge\nagent_pr_open=true\nscorecard_due=true\n")
            os.unlink(f.name)
        rec = w.records.read("merge-step/88-1-merge.json")
        self.assertEqual(rec["result"], "refused")

    def test_closes_a_queue_item_whose_class_scope_failed(self):
        w = base_world()
        w.class_scope_job("h2", 7001, "failure", FAIL_STEPS)
        w.go_test_check("h2", "success")
        out = merge_step.merge(ctx(w, 40, "merge"), fakes.SEED_KEY, True)
        self.assertEqual(out["action"], "close")
        self.assertEqual(w.prs[2]["state"], "closed")
        self.assertEqual(len(w.gh.writes("POST", "repos/platform-factory/svc-hello/issues/2/comments")), 1)
        self.assertFalse(out["agent_pr_open"])
        self.assertEqual(w.gh.writes("PUT", "repos/platform-factory/svc-hello/pulls/2/merge"), [])

    def test_closes_when_go_test_failed(self):
        w = base_world()
        w.class_scope_job("h2", 7001, "success", PASS_STEPS)
        w.go_test_check("h2", "failure")
        self.assertEqual(merge_step.merge(ctx(w, 40, "merge"), fakes.SEED_KEY, True)["action"], "close")

    def test_never_closes_an_inconclusive(self):
        w = base_world()
        w.class_scope_job("h2", 7001, "failure", INC_STEPS)
        w.go_test_check("h2", "success")
        out = merge_step.merge(ctx(w, 40, "merge"), fakes.SEED_KEY, True)
        self.assertEqual(out["action"], "wait")
        self.assertEqual(w.prs[2]["state"], "open")

    def test_never_merges_or_closes_a_seed(self):
        w = World()
        w.queue_pr(1, 1, SYNC, "v0.18.0", at(-60), state="closed", merged_at=at(-50), merge_sha="m0")
        w.queue_pr(5, 2, SYNC, "v0.19.0", at(-40), state="closed", merged_at=at(-30), merge_sha="m1")
        w.set_main("m1", go_mod_with(sync="v0.19.0"))
        w.push_run(501, "m1", [("completed", "success", at(-25))])
        w.queue_pr(6, 3, SYNC, "v0.20.0", at(-20), head_sha="h6")   # first PR after the 2nd merge: a seed
        w.class_scope_job("h6", 7001, "failure", FAIL_STEPS)
        w.go_test_check("h6", "failure")
        out = merge_step.merge(ctx(w, 40, "merge"), fakes.SEED_KEY, True)
        self.assertEqual(out["action"], "wait-seed")
        self.assertEqual(w.gh.writes(), [])

    def test_a_pull_request_opened_at_l2_at_a_drawn_position_is_a_real_item(self):
        """Seeds run at L1 only: the opening job made no seed at L2, and the
        merge phase reads the same `rung_at_open`, so the item merges rather
        than waiting for a decision on a seed that is not one."""
        def world(rung_at_open):
            w = World()
            w.queue_pr(1, 1, SYNC, "v0.18.0", at(-60), state="closed", merged_at=at(-50), merge_sha="m0")
            w.queue_pr(5, 2, SYNC, "v0.19.0", at(-40), state="closed", merged_at=at(-30), merge_sha="m1")
            w.set_main("m1", go_mod_with(sync="v0.19.0"))
            w.queue_pr(6, 3, SYNC, "v0.20.0", at(-20), head_sha="h6")   # first PR after the 2nd merge
            w.records.files["runs/1006-1.json"]["rung_at_open"] = rung_at_open
            w.set_rung(0)
            w.class_scope_job("h6", 7001, "success", PASS_STEPS)
            w.go_test_check("h6", "success")
            return w
        at_l2 = merge_step.merge(ctx(world(0), 40, "merge"), fakes.SEED_KEY, True)
        self.assertEqual((at_l2["action"], at_l2["result"]), ("merge", "merged"))
        at_l1 = merge_step.merge(ctx(world(1), 40, "merge"), fakes.SEED_KEY, True)
        self.assertEqual(at_l1["action"], "wait-seed")

    def test_updates_a_queue_item_left_behind_main(self):
        w = base_world()
        w.behind["h2"] = 1
        w.class_scope_job("h2", 7001, "failure", FAIL_STEPS)
        out = merge_step.merge(ctx(w, 40, "merge"), fakes.SEED_KEY, True)
        self.assertEqual(out["action"], "update-branch")
        self.assertEqual(w.gh.writes("PUT", "repos/platform-factory/svc-hello/pulls/2/update-branch")[0]["body"],
                         {"expected_head_sha": "h2"})
        self.assertEqual(w.prs[2]["state"], "open")

    def test_refuses_without_seed_key_or_pre_merge_ok(self):
        w = base_world()
        w.class_scope_job("h2", 7001, "success", PASS_STEPS)
        w.go_test_check("h2", "success")
        self.assertEqual(merge_step.merge(ctx(w, 40, "merge"), None, True)["action"], "refuse")
        out = merge_step.merge(ctx(w, 40, "merge"), fakes.SEED_KEY, False)
        self.assertEqual(out["action"], "refuse")
        self.assertTrue(out["agent_pr_open"])
        self.assertEqual(w.gh.writes(), [])

    def test_no_open_agent_pr(self):
        w = base_world()
        w.prs[2]["state"] = "closed"
        w.prs[2]["merged_at"] = at(20)
        w.prs[2]["merge_commit_sha"] = "m2"
        out = merge_step.merge(ctx(w, 40, "merge"), fakes.SEED_KEY, True)
        self.assertEqual((out["action"], out["agent_pr_open"]), ("none", False))


class SeedLooksLikeAnItemTest(unittest.TestCase):
    """Until the listed human decides, the merge phase's output (printed to a
    public log and written to the records branch) is the same for a seed as
    for a real item (ADR-0024 §11)."""

    def world(self):
        w = World()
        w.queue_pr(1, 1, SYNC, "v0.18.0", at(-60), state="closed", merged_at=at(-50), merge_sha="m0")
        w.queue_pr(5, 2, SYNC, "v0.19.0", at(-40), state="closed", merged_at=at(-30), merge_sha="m1")
        w.set_main("m1", go_mod_with(sync="v0.19.0"))
        w.push_run(501, "m1", [("completed", "success", at(-25))])
        w.queue_pr(6, 3, SYNC, "v0.20.0", at(-20), head_sha="h6")   # first PR after the 2nd merge
        w.heartbeat(at(35))
        return w

    def run_with(self, positions, w):
        with mock.patch.object(st, "seed_positions_from_key", lambda key, cf: positions):
            return merge_step.merge(ctx(w, 40, "merge"), fakes.SEED_KEY, True)

    def comparable(self, out):
        return json.dumps(out, sort_keys=True, default=str)

    def test_passing_unapproved_seed_and_item_print_the_same(self):
        outs = []
        for positions in ([2, 9, 12, 15], [3, 9, 12, 15]):   # PR 6 is a seed, then is not
            w = self.world()
            w.class_scope_job("h6", 7001, "success", PASS_STEPS)
            w.go_test_check("h6", "success")
            outs.append(self.run_with(positions, w))
            self.assertEqual(w.gh.writes(), [])
        self.assertEqual(outs[0]["action"], "wait")
        self.assertEqual(self.comparable(outs[0]), self.comparable(outs[1]))

    def test_a_seed_behind_main_is_updated_like_an_item(self):
        outs = []
        for positions in ([2, 9, 12, 15], [3, 9, 12, 15]):
            w = self.world()
            w.behind["h6"] = 1
            w.class_scope_job("h6", 7001, "success", PASS_STEPS)
            w.go_test_check("h6", "success")
            outs.append(self.run_with(positions, w))
            self.assertEqual(len(w.gh.writes("PUT", f"repos/{fakes.SVC}/pulls/6/update-branch")), 1)
        self.assertEqual(self.comparable(outs[0]), self.comparable(outs[1]))

    def test_after_the_decision_a_seed_waits_for_the_settlement(self):
        for state, body in (("APPROVED", "decision: approve"), ("COMMENTED", "minutes: 2; decision: close; reason: x")):
            w = self.world()
            w.class_scope_job("h6", 7001, "success", PASS_STEPS)
            w.go_test_check("h6", "success")
            w.review(6, "h6", at(30), state=state, body=body)
            out = self.run_with([2, 9, 12, 15], w)
            self.assertEqual(out["action"], "wait-seed")
            self.assertTrue(out["agent_pr_open"])
            self.assertEqual(w.gh.writes(), [])
        # The same approval on a real item merges it.
        w = self.world()
        w.class_scope_job("h6", 7001, "success", PASS_STEPS)
        w.go_test_check("h6", "success")
        w.review(6, "h6", at(30))
        self.assertEqual(self.run_with([3, 9, 12, 15], w)["action"], "merge")

    def test_a_review_by_anyone_else_is_no_decision(self):
        w = self.world()
        w.class_scope_job("h6", 7001, "success", PASS_STEPS)
        w.go_test_check("h6", "success")
        w.review(6, "h6", at(30), actor="maintainer")
        w.review(6, "h6", at(31), actor=fakes.AGENT_BOT)
        self.assertEqual(self.run_with([2, 9, 12, 15], w)["action"], "wait")

    def test_hold_ends_after_the_last_seed_position_and_at_l2(self):
        d = merge_step.decide_merge([{"number": 2, "queue_item": True, "head_sha": "h2"}], [], True, 0,
                                    {"verdict": "pass", "head_sha": "h2"}, {"verdict": "pass"},
                                    review_hold=True, approved=False)
        self.assertEqual(d["action"], "wait")
        w = base_world()
        cf = w.cf
        state = st.derive_state(st.gather(w.gh, w.records, cf), cf, fakes.l1(), fakes.l2(), None)
        self.assertTrue(merge_step.review_hold(state, cf))            # L1, one merge
        state.merged = [{}] * 19
        self.assertFalse(merge_step.review_hold(state, cf))           # past position 18
        state.merged = [{}]
        state.rung = 0
        self.assertFalse(merge_step.review_hold(state, cf))           # L2: no seed is ever open there
        # At L2 an unapproved item goes to the merge API, as C-15's L2 cases need.
        w.set_rung(0)
        w.class_scope_job("h2", 7001, "success", PASS_STEPS)
        w.go_test_check("h2", "success")
        with mock.patch.object(st, "seed_positions_from_key", lambda key, cf: [2, 9, 12, 15]):
            out = merge_step.merge(ctx(w, 40, "merge"), fakes.SEED_KEY, True)
        self.assertEqual(out["action"], "merge")


class FindingsTest(unittest.TestCase):
    def test_a_wrong_seed_key_is_refused_before_any_schedule(self):
        """ADV-17: a well-formed key that is not the class file's is refused."""
        w = base_world()
        w.review(2, "h2")
        w.class_scope_job("h2", 7001, "success", PASS_STEPS)
        w.go_test_check("h2", "success")
        out = merge_step.merge(ctx(w, 40, "merge"), "ff" * 32, True)
        self.assertEqual(out["action"], "refuse")
        self.assertIn("key_sha256", out["decision"]["reason"])
        self.assertEqual(w.gh.writes("PUT"), [])

    def test_seed_only_reads_are_made_for_every_item(self):
        """ADV-11: the events and the seed record are read for an item too."""
        w = base_world()
        w.class_scope_job("h2", 7001, "success", PASS_STEPS)
        w.go_test_check("h2", "success")
        merge_step.merge(ctx(w, 40, "merge"), fakes.SEED_KEY, True)
        paths = [c["path"] for c in w.gh.calls]
        self.assertIn("repos/platform-factory/svc-hello/issues/2/events", paths)

    def test_update_branch_refused_for_a_workflow_change_remakes_the_branch(self):
        """M5: the agent App cannot bring main's workflow change into its
        branch; the merge step makes the change again on main's tip and moves
        the branch, and never closes the pull request."""
        w = base_world()
        w.behind["h2"] = 1
        w.files[("h2", "go.mod")] = go_mod_with(sync="v0.19.0")
        w.files[("h2", "go.sum")] = "sum2\n"
        w.gh.route("PUT", r"repos/platform-factory/svc-hello/pulls/2/update-branch", lambda m, p, b: ApiError(
            422, {"message": "refusing to allow a GitHub App to create or update workflow "
                             "`.github/workflows/validate.yml` without `workflows` permission"}, m.group(0)))
        w.gh.route("POST", r"repos/platform-factory/svc-hello/git/refs", lambda m, p, b: {"ref": b["ref"]})
        w.gh.route("PATCH", r"repos/platform-factory/svc-hello/git/refs/heads/(.+)", lambda m, p, b: {"ok": True})
        w.gh.route("DELETE", r"repos/platform-factory/svc-hello/git/refs/heads/(.+)", lambda m, p, b: None)
        w.gh.route("POST", "graphql", lambda m, p, b: {"data": {"createCommitOnBranch": {"commit": {"oid": "r2"}}}})
        out = merge_step.merge(ctx(w, 40, "merge"), fakes.SEED_KEY, True)
        self.assertEqual(out["action"], "update-branch")
        self.assertEqual(out["remake"]["result"], "remade")
        self.assertEqual(out["remake"]["new_head"], "r2")
        moved = w.gh.writes("PATCH", "repos/platform-factory/svc-hello/git/refs/heads/agent/")
        self.assertEqual(moved[0]["body"], {"sha": "r2", "force": True})
        gql = w.gh.writes("POST", "graphql")[0]["body"]["variables"]["input"]
        self.assertEqual(gql["expectedHeadOid"], "m1")
        self.assertEqual(sorted(a["path"] for a in gql["fileChanges"]["additions"]), ["go.mod", "go.sum"])
        self.assertEqual(w.prs[2]["state"], "open")

    def test_scorecard_due_once_after_the_window_closes(self):
        """B4: the scorecard runs on each merge and once after the latest
        merge's escape window closes, not on every run in the hour after."""
        w = base_world()
        s = st.derive_state(st.gather(w.gh, w.records, w.cf), w.cf, fakes.l1(), fakes.l2())
        due = merge_step.scorecard_due
        now = lambda m: T0 + timedelta(minutes=m)  # noqa: E731
        self.assertFalse(due(s, w.cf, now(20), None))           # the window is open
        self.assertTrue(due(s, w.cf, now(31), None))            # closed; no scorecard yet
        self.assertTrue(due(s, w.cf, now(31), at(1)))           # only the merge's own, read before the close
        self.assertFalse(due(s, w.cf, now(61), at(32)))         # a scorecard read after the close: done
        self.assertTrue(due(s, w.cf, now(600), at(1)))          # a failed scorecard run leaves it due

    def test_scorecard_due_is_read_from_the_records_and_does_not_repeat(self):
        w = base_world()                                        # PR 2 open, no checks yet: the phase waits
        w.records.files["scorecard/go-module-bump.inputs.json"] = {"as_of": at(1)}   # the merge's scorecard
        first = merge_step.merge(ctx(w, 33, "merge"), fakes.SEED_KEY, True)
        self.assertEqual((first["action"], first["scorecard_due"]), ("wait", True))
        self.assertEqual(first["last_scorecard_read_at"], at(1))
        w.records.files["scorecard/go-module-bump.inputs.json"] = {"as_of": at(34)}  # that run's scorecard
        second = merge_step.merge(ctx(w, 63, "merge"), fakes.SEED_KEY, True)
        self.assertFalse(second["scorecard_due"])


if __name__ == "__main__":
    unittest.main()
