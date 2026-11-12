"""The reveal: refuses while the agent workflow is enabled; checks every body against its sealed summary."""

import unittest
from unittest import mock

from factory import open_pr as op
from factory import reveal as rv
from factory import seeds as sd
from tests import fakes
from tests.test_open_pr import KEY, SUMMARY, OpenWorld, at, sched_with
from tests.fakes import go_mod_with

SYNC = "golang.org/x/sync"


class RevealTest(unittest.TestCase):
    def world(self, workflow_state="disabled_manually"):
        ow = OpenWorld()
        w = ow.w
        w.files[("m1", "go.mod")] = go_mod_with(sync="v0.19.0")
        w.files[("newhead", "go.mod")] = go_mod_with(sync="v0.20.0")
        w.gh.route("GET", rf"repos/{fakes.CONTROLS}/actions/workflows/agent\.yml", {"state": workflow_state})
        return ow

    def open_one(self, ow, sched):
        with mock.patch.object(sd, "schedule", lambda key, cf: sched):
            op.open_item(ow.ctx(), ow.files(), ow.prepared(), KEY, {}, None)

    def reveal(self, ow, sched, close_record=True):
        cf = ow.w.cf
        cf["seeds"]["key_sha256"] = sd.key_sha256(KEY)
        if close_record and not any(r["kind"] == "close" for r in ow.w.class_records):
            ow.w.class_records.append({"name": "close-go-module-bump.yaml", "kind": "close",
                                       "merged_at": "2026-12-01T00:00:00Z"})
        for pr in ow.w.prs.values():
            if pr["state"] == "open" and close_record:
                pr["state"] = "closed"
                pr["closed_at"] = "2026-11-30T00:00:00Z"
        with mock.patch.object(sd, "schedule", lambda key, cf: sched):
            return rv.reveal(ow.w.gh, ow.w.records, cf, KEY, fakes.CONTROLS, {"run_id": "9"},
                             fakes.l1(), fakes.l2())

    def test_a_seed_and_a_real_item_both_verify(self):
        for sched, is_seed in ((sched_with(2), True), (sched_with(9), False)):
            ow = self.world()
            self.open_one(ow, sched)
            doc = self.reveal(ow, sched)
            row = [r for r in doc["verification"] if r["pr"] == 6][0]
            self.assertEqual((row["seed"], row["ok"], row["body_matches_record"]), (is_seed, True, True), row)
            self.assertTrue(doc["matches_class_file"])
            self.assertEqual(doc["key_hex"], KEY)
            self.assertEqual(ow.w.records.read("close/seed-key.json")["key_sha256"], sd.key_sha256(KEY))

    def test_an_altered_body_fails_verification(self):
        ow = self.world()
        self.open_one(ow, sched_with(9))
        ow.w.prs[6]["body"] = ow.w.prs[6]["body"].replace("- Tests: 10", "- Tests: 12")
        row = [r for r in self.reveal(ow, sched_with(9))["verification"] if r["pr"] == 6][0]
        self.assertFalse(row["ok"])
        self.assertFalse(row["body_matches_record"])

    def test_refuses_while_the_agent_workflow_is_enabled(self):
        ow = self.world("active")
        with self.assertRaises(SystemExit):
            self.reveal(ow, sched_with(9))
        self.assertIsNone(ow.w.records.read("close/seed-key.json"))

    def test_refuses_mid_run_while_the_agent_workflow_is_disabled_for_fixtures(self):
        """ADV-18: a disabled workflow alone is not the close."""
        ow = self.world()
        self.open_one(ow, sched_with(9))
        with self.assertRaises(SystemExit) as e:
            self.reveal(ow, sched_with(9), close_record=False)
        msg = str(e.exception)
        self.assertIn("queue-item pull request is open", msg)
        self.assertIn("window ends", msg)
        self.assertIsNone(ow.w.records.read("close/seed-key.json"))

    def test_close_guards(self):
        from factory import state as st
        w = fakes.World()
        w.queue_pr(1, 1, "golang.org/x/sync", "v0.18.0", "2026-11-12T10:00:00Z", state="closed",
                   merged_at="2026-11-12T10:30:00Z", merge_sha="m1")
        snap = st.gather(w.gh, w.records, w.cf)
        state = st.derive_state(snap, w.cf, fakes.l1(), fakes.l2())
        from datetime import datetime, timezone
        early, late = datetime(2026, 11, 20, tzinfo=timezone.utc), datetime(2027, 1, 20, tzinfo=timezone.utc)
        self.assertTrue(any("window ends" in r for r in rv.close_guards(state, snap, w.cf, early)))
        self.assertEqual(rv.close_guards(state, snap, w.cf, late), [])
        w.set_rung(0)
        state0 = st.derive_state(st.gather(w.gh, w.records, w.cf), w.cf, fakes.l1(), fakes.l2())
        self.assertTrue(any("not 1" in r for r in rv.close_guards(state0, snap, w.cf, late)))

    def test_summary_in_body(self):
        body = op.pr_body({"position": 1, "module": SYNC, "version": "v0.18.0"}, SUMMARY, "1", "1")
        self.assertEqual(rv.summary_in_body(body), SUMMARY.rstrip())
        self.assertIsNone(rv.summary_in_body("no heading"))


if __name__ == "__main__":
    unittest.main()
