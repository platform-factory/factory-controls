"""Streak, near-misses, seeds, next item, labels in force and class-scope's verdict."""

import unittest

from factory import state as st
from tests import fakes
from tests.fakes import HUMAN, AGENT_BOT, World, ev, go_mod_with

SYNC, TEXT = "golang.org/x/sync", "golang.org/x/text"


def derive(w: World, positions=None):
    snap = st.gather(w.gh, w.records, w.cf)
    return st.derive_state(snap, w.cf, fakes.l1(), fakes.l2(), positions)


def derive_with_push_runs(w: World, positions=None, as_of=None):
    """As the scorecard derives: its snapshot also carries the push runs on
    each merge commit and the time of the read."""
    snap = st.gather(w.gh, w.records, w.cf)
    push = {}
    for r in w.runs:
        attempt1 = w.jobs.get((r["id"], 1), [])
        push.setdefault(r["head_sha"], []).append({"run": r, "jobs": attempt1})
    snap["push_runs"] = push
    snap["as_of"] = as_of
    return st.derive_state(snap, w.cf, fakes.l1(), fakes.l2(), positions)


def t(minute: int, hour: int = 10) -> str:
    return f"2026-11-12T{hour:02d}:{minute:02d}:00Z"


class StreakTest(unittest.TestCase):
    def test_merges_count_and_a_near_miss_resets(self):
        w = World()
        w.queue_pr(1, 1, SYNC, "v0.18.0", t(0), state="closed", merged_at=t(5), merge_sha="m1")
        w.queue_pr(2, 2, SYNC, "v0.19.0", t(10), state="closed", merged_at=t(15), merge_sha="m2")
        w.queue_pr(3, 3, SYNC, "v0.20.0", t(20), state="closed", closed_at=t(25))
        w.queue_pr(4, 3, SYNC, "v0.21.0", t(30), state="closed", merged_at=t(35), merge_sha="m4")
        s = derive(w)
        self.assertEqual([m["pr"] for m in s.merged], [1, 2, 4])
        self.assertEqual([(n["pr"], n["closed_by"]) for n in s.near_misses], [(3, HUMAN)])
        self.assertEqual(s.streak, 1)
        self.assertEqual(s.streak_start, t(35))

    def test_near_miss_counts_even_when_reopened_and_merged_later(self):
        w = World()
        w.queue_pr(1, 1, SYNC, "v0.18.0", t(0), state="closed", merged_at=t(30), merge_sha="m1")
        w.events[1].insert(0, ev("closed", t(10), "someone"))
        w.events[1].insert(1, ev("reopened", t(12), "someone"))
        s = derive(w)
        self.assertEqual([n["closed_at"] for n in s.near_misses], [t(10)])
        self.assertEqual(s.streak, 1)  # the near-miss at :10 reset, the merge at :30 counts

    def test_escape_in_the_ledger_resets(self):
        w = World()
        w.queue_pr(1, 1, SYNC, "v0.18.0", t(0), state="closed", merged_at=t(5), merge_sha="m1")
        w.queue_pr(2, 2, SYNC, "v0.19.0", t(10), state="closed", merged_at=t(15), merge_sha="m2")
        w.records.files["ledger/escape-a-pr2.json"] = {"key": "escape-a-pr2", "type": "a",
                                                       "event": {"pr": 2, "event_time": t(20)}, "acted_at": t(24)}
        w.queue_pr(3, 3, SYNC, "v0.20.0", t(25), state="closed", merged_at=t(40), merge_sha="m3")
        s = derive(w)
        self.assertEqual(s.streak, 1)
        self.assertEqual([e["key"] for e in s.escapes], ["escape-a-pr2"])

    def test_a_ruleset_difference_does_not_reset_the_streak(self):
        w = World()
        w.queue_pr(1, 1, SYNC, "v0.18.0", t(0), state="closed", merged_at=t(5), merge_sha="m1")
        w.records.files["ledger/ruleset-x.json"] = {"key": "ruleset-x", "type": "ruleset",
                                                    "event": {"event_time": t(7)}, "acted_at": t(7)}
        self.assertEqual(derive(w).streak, 1)

    def test_cleared_inconclusive_then_merged_counts_one(self):
        """The inconclusive and the clearance are label and check events; only the merge counts."""
        w = World()
        w.queue_pr(1, 1, SYNC, "v0.18.0", t(0), state="closed", merged_at=t(40), merge_sha="m1")
        w.events[1][:0] = [ev("labeled", t(1), "github-actions[bot]", "needs-human", app="github-actions"),
                           ev("labeled", t(20), HUMAN, "human-cleared")]
        s = derive(w)
        self.assertEqual(s.streak, 1)
        self.assertEqual(s.near_misses, [])

    def test_seeds_are_neither_merges_nor_near_misses(self):
        w = World()
        w.queue_pr(1, 1, SYNC, "v0.18.0", t(0), state="closed", merged_at=t(5), merge_sha="m1")
        w.queue_pr(2, 2, SYNC, "v0.19.0", t(10), state="closed", merged_at=t(15), merge_sha="m2")
        # after the 2nd merge, the first PR opened is the seed for k=2; it is closed unmerged
        w.queue_pr(3, 3, SYNC, "v0.20.0", t(20), state="closed", closed_at=t(25))
        w.queue_pr(4, 3, SYNC, "v0.20.0", t(30), state="closed", merged_at=t(35), merge_sha="m4")
        with_key = derive(w, positions=[2, 9, 12, 15])
        self.assertEqual(with_key.seeds, [3])
        self.assertEqual(with_key.near_misses, [])
        self.assertEqual(with_key.streak, 3)
        # Without the key and before seeds/3.json exists, the close looks like a near-miss.
        self.assertEqual(derive(w).streak, 1)
        # The opening job's seeds/<n>.json record settles it for every reader.
        w.records.files["seeds/3.json"] = {"pr_number": 3}
        self.assertEqual(derive(w).streak, 3)

    def test_fixtures_are_not_queue_items(self):
        w = World()
        w.queue_pr(1, 1, SYNC, "v0.18.0", t(0), state="closed", merged_at=t(5), merge_sha="m1")
        w.queue_pr(9, 2, SYNC, "v0.19.0", t(6), state="closed", closed_at=t(8))
        w.records.files["fixtures/c15-readme.json"] = {"pr_number": 9}
        s = derive(w)
        self.assertEqual(s.near_misses, [])
        self.assertNotIn(9, s.queue_item_prs)
        self.assertEqual(s.streak, 1)

    def test_promotable_at_n_on_l1_only(self):
        w = World()
        for i in range(1, 21):
            w.queue_pr(i, i, SYNC if i <= 8 else TEXT, "v0.18.0", t(i, 10), state="closed",
                       merged_at=t(i, 11), merge_sha=f"m{i}")
            w.push_run(700 + i, f"m{i}", [("completed", "success", t(i + 5, 11))])
        s = derive_with_push_runs(w, as_of=t(0, 13))
        self.assertEqual(s.streak, 20)
        self.assertTrue(s.promotable, s.promotion_blockers)
        w.set_rung(0)
        self.assertFalse(derive_with_push_runs(w, as_of=t(0, 13)).promotable)

    def test_no_promotion_while_the_20th_merge_window_is_open(self):
        """Finding ADV-04: the 20th merge exists but its push run has not
        concluded and its 30-minute window is open: not promotable yet."""
        w = World()
        for i in range(1, 21):
            w.queue_pr(i, i, SYNC, "v0.18.0", t(i, 10), state="closed", merged_at=t(i, 11), merge_sha=f"m{i}")
            if i < 20:
                w.push_run(700 + i, f"m{i}", [("completed", "success", t(i + 5, 11))])
        w.push_run(720, "m20", [("in_progress", None, None)])
        s = derive_with_push_runs(w, as_of=t(25, 11))
        self.assertEqual(s.streak, 20)
        self.assertFalse(s.promotable)
        self.assertIn("escape (a) window not clear for [20]", s.promotion_blockers)
        # Without push runs read (the detector's and merge step's snapshots),
        # promotion is never offered.
        self.assertFalse(derive(w).promotable)

    def test_an_escape_ignored_by_the_cutoff_still_resets_from_the_push_runs(self):
        """Finding ADV-04: the push run of #2 failed before a record merged, so
        the detector ignores the event and no ledger entry exists; the push
        runs themselves still reset the streak."""
        w = World()
        w.queue_pr(1, 1, SYNC, "v0.18.0", t(0), state="closed", merged_at=t(5), merge_sha="m1")
        w.queue_pr(2, 2, SYNC, "v0.19.0", t(10), state="closed", merged_at=t(15), merge_sha="m2")
        w.queue_pr(3, 3, SYNC, "v0.20.0", t(30), state="closed", merged_at=t(40), merge_sha="m3")
        w.push_run(701, "m1", [("completed", "success", t(8))])
        w.push_run(702, "m2", [("completed", "failure", t(20))])
        w.push_run(703, "m3", [("completed", "success", t(44))])
        w.class_records.append({"name": "promotion-1.yaml", "kind": "promotion", "merged_at": t(23)})
        s = derive_with_push_runs(w, as_of=t(0, 12))
        self.assertEqual(s.escapes, [])
        self.assertEqual(s.streak, 1)
        self.assertEqual(s.streak_start, t(40))

    def test_a_verifier_change_resets_the_streak(self):
        """Finding ADV-05: a commit on main touching what go-test or
        class-scope decides resets the streak at its commit time."""
        w = World()
        w.queue_pr(1, 1, SYNC, "v0.18.0", t(0), state="closed", merged_at=t(5), merge_sha="m1")
        w.queue_pr(2, 2, SYNC, "v0.19.0", t(10), state="closed", merged_at=t(15), merge_sha="m2")
        snap = st.gather(w.gh, w.records, w.cf)
        commits = [{"sha": "c1", "date": t(12), "files": [".github/class-scope/class_scope.py"]},
                   {"sha": "c0", "date": t(1), "files": ["main_test.go"]},       # before the switch-on
                   {"sha": "c2", "date": t(13), "files": ["README.md"]}]
        snap["verifier_changes"] = st.verifier_changes(commits, t(2))
        s = st.derive_state(snap, w.cf, fakes.l1(), fakes.l2())
        self.assertEqual([c["sha"] for c in s.verifier_changes], ["c1"])
        self.assertEqual(s.streak, 1)
        self.assertTrue(st.is_verifier_path("x/y_test.go"))
        self.assertTrue(st.is_verifier_path(".github/workflows/validate.yml"))
        self.assertFalse(st.is_verifier_path("go.mod"))

    def test_a_record_after_the_streak_began_means_the_streak_was_used(self):
        w = World()
        for i in range(1, 21):
            w.queue_pr(i, i, SYNC, "v0.18.0", t(i, 10), state="closed", merged_at=t(i, 11), merge_sha=f"m{i}")
        w.class_records.append({"name": "promotion-1.yaml", "kind": "promotion", "merged_at": t(30, 11)})
        self.assertFalse(derive(w).promotable)


class VerdictAnnotationTest(unittest.TestCase):
    """ADV-03 / M3: the reason class and codes come from the class-scope check
    run's annotation, which is readable without a token (read 2026-11-11: the
    annotations endpoint answered 200 with no token on a svc-hello check run;
    the REST description marks it allowsPublicRead)."""

    def recorded(self):
        import json
        from pathlib import Path
        return json.loads((Path(__file__).parent / "data" / "check-run-annotations-read-2026-11-11.json").read_text())

    def test_the_runners_own_annotations_are_not_a_verdict(self):
        anns = self.recorded()
        self.assertEqual({a["annotation_level"] for a in anns}, {"warning", "notice"})
        self.assertEqual(st.verdict_from_annotations(anns)["verdict"], None)

    def test_the_verdict_annotation_beside_the_runners_own(self):
        import json
        anns = self.recorded() + [{**self.recorded()[1], "title": "class-scope-verdict", "message": json.dumps(
            {"schema": 1, "verdict": "inconclusive", "reason_class": "clearable", "reason_codes": ["forced"]})}]
        v = st.verdict_from_annotations(anns)["verdict"]
        self.assertEqual((v["verdict"], v["reason_class"], v["reason_codes"]), ("inconclusive", "clearable", ["forced"]))

    def test_two_verdict_annotations_are_not_trusted(self):
        a = {"title": "class-scope-verdict", "message": '{"verdict": "pass"}'}
        self.assertIn("none trusted", st.verdict_from_annotations([a, a])["error"])
        self.assertIsNone(st.verdict_from_annotations([{**a, "message": "not json"}])["verdict"])

    def test_class_scope_verdict_takes_the_reason_class_from_the_annotation(self):
        import json
        w = World()
        w.class_scope_job("h9", 7001, "failure", [("decide", "success"), ("verdict-inconclusive", "failure"),
                                                   ("verdict-fail", "skipped")])
        w.gh.route("GET", r"repos/platform-factory/svc-hello/check-runs/7001/annotations", [
            {"title": "class-scope-verdict", "message": json.dumps({"verdict": "inconclusive", "reason_class": "rerun",
                                                                    "reason_codes": ["proxy-unreachable"]})}])
        v = st.class_scope_verdict(w.gh, fakes.SVC, "h9")
        self.assertEqual((v["verdict"], v["reason_class"], v["reason_codes"]), ("inconclusive", "rerun",
                                                                                ["proxy-unreachable"]))


class L2AcceptedTest(unittest.TestCase):
    def test_a_demotion_with_no_acted_at_counts_as_after_the_record(self):
        """ADV-20 (4): an unreadable time never keeps L2 accepted."""
        recs = [{"kind": "promotion", "merged_at": t(0)}]
        self.assertTrue(st.l2_accepted(recs, []))
        self.assertFalse(st.l2_accepted(recs, [{"type": "a", "key": "k"}]))
        self.assertTrue(st.l2_accepted(recs, [{"type": "a", "key": "k", "acted_at": t(0, 9)}]))


class IdentityTest(unittest.TestCase):
    def test_a_foreign_repo_run_record_with_a_colliding_number_changes_nothing(self):
        """Finding B1: probe P4's run record names m4-probe and a pull request
        number svc-hello also has. It must change no merge, near-miss or seed."""
        w = World()
        w.queue_pr(1, 1, SYNC, "v0.18.0", t(0), state="closed", merged_at=t(5), merge_sha="m1")
        w.human_pr(19, t(6), state="closed")
        w.human_pr(4, t(7), merged_at=t(8), merge_sha="h4")
        before = derive(w)
        for n in (4, 19):
            w.records.files[f"runs/p4-{n}.json"] = {
                "schema": 1, "class": "go-module-bump", "repo": "platform-factory/m4-probe",
                "queue_position": 1, "module": SYNC, "version": "v0.18.0", "pr_number": n,
                "branch": "agent/go-module-bump/golang.org-x-sync-v0.18.0", "opened_at": t(6)}
        after = derive(w)
        self.assertEqual((after.merged, after.near_misses, after.streak, after.next_item),
                         (before.merged, before.near_misses, before.streak, before.next_item))
        self.assertNotIn(4, after.queue_item_prs)
        self.assertNotIn(19, after.queue_item_prs)

    def test_a_record_whose_branch_is_not_the_prs_head_is_ignored(self):
        w = World()
        w.queue_pr(1, 1, SYNC, "v0.18.0", t(0), state="closed", merged_at=t(5), merge_sha="m1")
        w.records.files["runs/1001-1.json"]["branch"] = "someone-else"
        self.assertEqual(derive(w).merged, [])

    def test_a_close_reopen_close_is_one_near_miss(self):
        w = World()
        w.queue_pr(3, 1, SYNC, "v0.18.0", t(0), state="closed", closed_at=t(5))
        w.events[3] += [ev("reopened", t(6)), ev("closed", t(7))]
        s = derive(w)
        self.assertEqual([(n["pr"], n["closed_at"]) for n in s.near_misses], [(3, t(5))])

    def test_a_base_changed_away_from_main_is_a_near_miss_and_a_tamper(self):
        """Finding ADV-15: the agent App's token can retarget its pull request.
        It drops out of the base=main listing, is read by number, counts as
        closed unmerged at the change, and frees the open slot."""
        w = World()
        w.queue_pr(5, 1, SYNC, "v0.18.0", t(0))
        w.prs[5]["base"] = {"ref": "elsewhere"}
        w.events[5].append(ev("base_ref_changed", t(3), AGENT_BOT))

        def pulls(m, p, b):
            st_ = p.get("state", "open")
            return [v for _, v in sorted(w.prs.items()) if (st_ == "all" or v["state"] == st_)
                    and (not p.get("base") or v["base"]["ref"] == p["base"])]

        w.gh.route("GET", rf"repos/{fakes.SVC.replace('/', '/')}/pulls", pulls)
        s = derive(w)
        self.assertEqual([(n["pr"], n["closed_at"], n.get("base_changed")) for n in s.near_misses],
                         [(5, t(3), True)])
        self.assertEqual(s.open_agent_prs, [])
        self.assertEqual(s.next_item["version"], "v0.19.0")
        self.assertTrue(any(u["type"] == "tamper-base-changed" and u["pr"] == 5 for u in s.unknowns))


class NextItemTest(unittest.TestCase):
    def test_first_item_above_main(self):
        w = World()
        w.set_main("mainA", go_mod_with(sync="v0.19.0"))
        self.assertEqual(derive(w).next_item, {"position": 3, "module": SYNC, "version": "v0.20.0"})

    def test_item_closed_by_a_near_miss_is_skipped(self):
        w = World()
        w.queue_pr(3, 1, SYNC, "v0.18.0", t(0), state="closed", closed_at=t(5))
        self.assertEqual(derive(w).next_item["version"], "v0.19.0")

    def test_a_reverted_item_is_queued_again(self):
        w = World()
        w.queue_pr(1, 1, SYNC, "v0.18.0", t(0), state="closed", merged_at=t(5), merge_sha="m1")
        w.set_main("m1", go_mod_with(sync="v0.18.0"))
        self.assertEqual(derive(w).next_item["version"], "v0.19.0")
        w.set_main("rev", go_mod_with(sync="v0.17.0"))  # the revert merged
        self.assertEqual(derive(w).next_item["version"], "v0.18.0")

    def test_text_after_sync_and_none_when_done(self):
        w = World()
        w.set_main("a", go_mod_with(sync="v0.25.0", text="v0.31.0"))
        self.assertEqual(derive(w).next_item["version"], "v0.32.0")
        w.set_main("b", go_mod_with(sync="v0.25.0", text="v0.44.0", pgx="v5.13.0"))
        self.assertIsNone(derive(w).next_item)

    def test_go_mod_parser(self):
        self.assertEqual(st.go_mod_requires(fakes.GO_MOD_BASE), {
            "github.com/jackc/pgx/v5": "v5.11.0", "github.com/jackc/pgpassfile": "v1.0.0",
            "github.com/jackc/puddle/v2": "v2.2.2", SYNC: "v0.17.0", TEXT: "v0.29.0"})


class OpenAgentPrTest(unittest.TestCase):
    def test_the_one_open_queue_item(self):
        w = World()
        w.queue_pr(1, 1, SYNC, "v0.18.0", t(0), state="closed", merged_at=t(5), merge_sha="m1")
        w.queue_pr(2, 2, SYNC, "v0.19.0", t(6))
        w.human_pr(50, t(1))
        s = derive(w)
        self.assertEqual(s.open_agent_pr["number"], 2)
        self.assertEqual(s.open_agent_pr["queue_position"], 2)
        self.assertEqual([p["number"] for p in s.open_agent_prs], [2])

    def test_an_agent_authored_pr_without_a_run_record_is_an_agent_pr(self):
        w = World()
        w.human_pr(51, t(1))["user"] = {"login": AGENT_BOT}
        s = derive(w)
        self.assertIsNone(s.open_agent_pr)
        self.assertEqual([p["number"] for p in s.open_agent_prs], [51])


class LabelsInForceTest(unittest.TestCase):
    H = [HUMAN]

    def test_human_label_in_force_until_human_removes_it(self):
        evs = [ev("labeled", t(1), HUMAN, "escape:revert")]
        self.assertEqual(st.labels_in_force_from_events(evs, self.H)["escape:revert"]["actor"], HUMAN)
        evs.append(ev("unlabeled", t(2), HUMAN, "escape:revert"))
        self.assertIsNone(st.labels_in_force_from_events(evs, self.H)["escape:revert"])

    def test_app_label_does_not_count_and_is_unknown(self):
        res = st.labels_in_force_from_events([ev("labeled", t(1), AGENT_BOT, "human-cleared",
                                                 app="platform-factory-agent")], self.H)
        self.assertIsNone(res["human-cleared"])
        self.assertEqual(len(res["unknown_label_events"]), 1)

    def test_human_login_through_an_app_does_not_count(self):
        res = st.labels_in_force_from_events([ev("labeled", t(1), HUMAN, "escape:revert", app="some-app")], self.H)
        self.assertIsNone(res["escape:revert"])

    def test_app_removal_is_ignored_and_counted_as_unknown(self):
        evs = [ev("labeled", t(1), HUMAN, "escape:revert"),
               ev("unlabeled", t(2), AGENT_BOT, "escape:revert", app="platform-factory-agent")]
        res = st.labels_in_force_from_events(evs, self.H)
        self.assertIsNotNone(res["escape:revert"])
        self.assertEqual(res["unknown_label_events"][0]["event"], "unlabeled")

    def test_class_scope_removal_on_a_new_head_ends_force(self):
        evs = [ev("labeled", t(1), HUMAN, "human-cleared"),
               ev("unlabeled", t(2), "github-actions[bot]", "human-cleared", app="github-actions")]
        self.assertIsNone(st.labels_in_force_from_events(evs, self.H)["human-cleared"])

    def test_login_comparison_ignores_case(self):
        evs = [ev("labeled", t(1), "Listed-Human", "reviewing")]
        self.assertIsNotNone(st.labels_in_force_from_events(evs, self.H)["reviewing"])


class VerdictTest(unittest.TestCase):
    STEPS = ["set up job", "decide", "verdict-inconclusive", "verdict-fail"]

    def job(self, conclusion, concl_by_step, status="completed"):
        return {"status": status, "conclusion": conclusion,
                "steps": [{"name": n, "conclusion": concl_by_step.get(n, "success")} for n in self.STEPS]}

    def test_pass(self):
        self.assertEqual(st.verdict_from_job(self.job("success", {}))["verdict"], "pass")

    def test_fail_from_the_verdict_fail_step(self):
        self.assertEqual(st.verdict_from_job(self.job("failure", {"verdict-fail": "failure"}))["verdict"], "fail")

    def test_inconclusive_from_the_verdict_inconclusive_step(self):
        v = st.verdict_from_job(self.job("failure", {"verdict-inconclusive": "failure", "verdict-fail": "skipped"}))
        self.assertEqual(v["verdict"], "inconclusive")
        self.assertIsNone(v["reason_class"])

    def test_any_other_failed_step_is_no_verdict_rerun(self):
        v = st.verdict_from_job(self.job("failure", {"decide": "failure", "verdict-inconclusive": "skipped",
                                                     "verdict-fail": "skipped"}))
        self.assertEqual((v["verdict"], v["reason_class"]), ("inconclusive", "rerun"))

    def test_cancelled_is_no_verdict(self):
        v = st.verdict_from_job(self.job("cancelled", {"decide": "cancelled"}))
        self.assertEqual((v["verdict"], v["reason_class"]), ("inconclusive", "rerun"))

    def test_success_without_verdict_steps_is_not_a_pass(self):
        job = {"status": "completed", "conclusion": "success", "steps": [{"name": "set up job",
                                                                          "conclusion": "success"}]}
        self.assertEqual(st.verdict_from_job(job)["verdict"], "inconclusive")

    def test_in_progress_is_pending(self):
        self.assertEqual(st.verdict_from_job(self.job(None, {}, status="in_progress"))["verdict"], "pending")

    def test_class_scope_verdict_reads_latest_check_run_and_its_job(self):
        w = World()
        w.class_scope_job("h1", 5551, "failure", [("decide", "success"), ("verdict-inconclusive", "failure"),
                                                  ("verdict-fail", "skipped")], started="2026-11-12T10:00:00Z")
        w.class_scope_job("h1", 5561, "success", [("decide", "success"), ("verdict-inconclusive", "success"),
                                                  ("verdict-fail", "success")], started="2026-11-12T10:05:00Z")
        v = st.class_scope_verdict(w.gh, fakes.SVC, "h1")
        self.assertEqual((v["verdict"], v["job_id"], v["run_id"]), ("pass", 5561, 556))

    def test_a_check_named_class_scope_from_another_app_is_ignored(self):
        w = World()
        w.class_scope_job("h1", 5551, "success", [("decide", "success"), ("verdict-inconclusive", "success"),
                                                  ("verdict-fail", "success")])
        w.check_runs["h1"][0]["app"] = {"id": 999}
        self.assertEqual(st.class_scope_verdict(w.gh, fakes.SVC, "h1")["verdict"], "none")


class RecordsAndL2Test(unittest.TestCase):
    REC = [{"kind": "promotion", "merged_at": "2026-11-20T10:00:00Z"}]

    def test_l2_accepted_after_a_record_until_the_next_demotion(self):
        self.assertFalse(st.l2_accepted([], []))
        self.assertTrue(st.l2_accepted(self.REC, []))
        demo = [{"type": "a", "acted_at": "2026-11-21T10:00:00Z"}]
        self.assertFalse(st.l2_accepted(self.REC, demo))
        later = self.REC + [{"kind": "fixture", "merged_at": "2026-11-22T10:00:00Z"}]
        self.assertTrue(st.l2_accepted(later, demo))

    def test_a_ruleset_event_is_a_demotion_too(self):
        self.assertFalse(st.l2_accepted(self.REC, [{"type": "ruleset", "acted_at": "2026-11-20T11:00:00Z"}]))

    def test_class_records_need_a_merged_pull_request(self):
        w = World()
        w.class_records = [{"name": "promotion-1.yaml", "kind": "promotion", "merged_at": t(0, 9)},
                           {"name": "fixture-1.yaml", "kind": "fixture", "merged_at": None}]
        recs, unknowns = st.read_class_records(w.gh, fakes.CONTROLS)
        self.assertEqual([r["path"] for r in recs], ["classes/records/promotion-1.yaml"])
        self.assertEqual(unknowns[0]["path"], "classes/records/fixture-1.yaml")


if __name__ == "__main__":
    unittest.main()
