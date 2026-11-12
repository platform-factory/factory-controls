"""ratchet.py may only set the rung entry's minimum_approvals to 1."""

import contextlib
import copy
import io
import unittest

from factory import ratchet
from factory.gh import ApiError
from factory import ruleset as rs
from tests import fakes
from tests.fakes import World


def pr_params(body):
    return next(r for r in body["rules"] if r["type"] == "pull_request")["parameters"]


class RefusingRecords(fakes.MemoryRecords):
    """A records branch that refuses writes, as when the controls repo's
    token has used its hourly requests. Each attempt is logged in the fake
    GitHub's calls, so its order against the ruleset PUT can be read."""

    def __init__(self, gh, refuse=lambda path: True):
        super().__init__(gh)
        self.refuse = refuse

    def write(self, path, obj, message):
        self.gh.calls.append({"method": "RECORD", "path": path, "params": {}, "body": None})
        if self.refuse(path):
            raise ApiError(403, {"message": "API rate limit exceeded for installation"}, path)
        super().write(path, obj, message)


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.l1, self.l2 = fakes.l1(), fakes.l2()

    def test_live_at_l2_is_raised_to_exactly_l1(self):
        plan = ratchet.plan_raise(fakes.as_get(self.l2), self.l1, self.l2)
        self.assertEqual(plan["action"], "write")
        self.assertEqual(rs.diff(rs.normalize(self.l1), rs.normalize(plan["body"])), [])
        self.assertEqual(sorted(plan["body"]), sorted(rs.PUT_FIELDS))  # no GET-only field is sent

    def test_live_at_l1_needs_no_write(self):
        self.assertEqual(ratchet.plan_raise(fakes.as_get(self.l1), self.l1, self.l2)["action"], "noop")

    def test_other_differences_are_kept_not_repaired(self):
        """With stale dismissal turned off and the rung at 0, only the rung is raised."""
        live = fakes.as_get(self.l2)
        pr_params(live)["dismiss_stale_reviews_on_push"] = False
        plan = ratchet.plan_raise(live, self.l1, self.l2)
        self.assertEqual(plan["action"], "write")
        d = rs.diff(rs.normalize(live), rs.normalize(plan["body"]))
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["path"], rs.rung_path(self.l1, self.l2))
        self.assertIs(pr_params(plan["body"])["dismiss_stale_reviews_on_push"], False)

    def test_refuses_to_lower_a_rung_above_one(self):
        live = fakes.as_get(self.l1)
        pr_params(live)["required_reviewers"][0]["minimum_approvals"] = 2
        plan = ratchet.plan_raise(live, self.l1, self.l2)
        self.assertEqual(plan["action"], "refuse")
        self.assertIn("not be a raise", plan["reason"])

    def test_refuses_when_rung_entry_missing(self):
        live = fakes.as_get(self.l2)
        pr_params(live)["required_reviewers"] = pr_params(live)["required_reviewers"][1:]
        self.assertEqual(ratchet.plan_raise(live, self.l1, self.l2)["action"], "refuse")

    def test_refuses_when_rung_entry_appears_twice(self):
        live = fakes.as_get(self.l2)
        rr = pr_params(live)["required_reviewers"]
        rr.append(copy.deepcopy(rr[0]))
        self.assertEqual(ratchet.plan_raise(live, self.l1, self.l2)["action"], "refuse")

    def test_refuses_without_bypass_actors(self):
        live = fakes.as_get(self.l2)
        del live["bypass_actors"]
        self.assertEqual(ratchet.plan_raise(live, self.l1, self.l2)["action"], "refuse")

    def test_refuses_when_ruleset_missing(self):
        self.assertEqual(ratchet.plan_raise(None, self.l1, self.l2)["action"], "refuse")

    def test_refuses_a_body_that_would_change_anything_else(self):
        """The guard: if building the body changed another field, nothing is written.

        put_body drops fields a PUT does not send; if the live ruleset carried a
        PUT field the normal form keeps but the body lost, the diff would show
        it. Simulate a body builder that also flips enforcement.
        """
        live = fakes.as_get(self.l2)
        orig = rs.put_body

        def bad_put_body(r):
            b = orig(r)
            b["enforcement"] = "evaluate"
            return b

        rs.put_body = bad_put_body
        try:
            plan = ratchet.plan_raise(live, self.l1, self.l2)
        finally:
            rs.put_body = orig
        self.assertEqual(plan["action"], "refuse")
        self.assertIn("more than the rung field", plan["reason"])


class RaiseRungTest(unittest.TestCase):
    def test_raise_puts_once_and_records_before_and_after(self):
        w = World()
        w.set_rung(0)
        res = ratchet.raise_rung(w.gh, w.records, w.cf, fakes.l1(), fakes.l2(), "escape-a-pr5", {"pr": 5})
        self.assertEqual(res["result"], "raised")
        self.assertEqual(w.rung(), 1)
        self.assertEqual(len(w.gh.writes("PUT", "repos/platform-factory/svc-hello/rulesets")), 1)
        before = w.records.read("ratchet/escape-a-pr5-before.json")
        after = w.records.read("ratchet/escape-a-pr5-after.json")
        self.assertEqual(before["plan"]["action"], "write")
        self.assertEqual(before["ruleset"]["rules"][-1]["parameters"]["required_reviewers"][0]["minimum_approvals"], 0)
        self.assertEqual(after["result"], "raised")
        self.assertEqual(w.issues, [])

    def test_refusal_writes_nothing_and_opens_one_issue(self):
        w = World()
        pr_params(w.ruleset)["required_reviewers"][0]["minimum_approvals"] = 3
        for _ in range(2):
            res = ratchet.raise_rung(w.gh, w.records, w.cf, fakes.l1(), fakes.l2(), "escape-a-pr5", {"pr": 5})
            self.assertEqual(res["result"], "refused")
        self.assertEqual(w.gh.writes("PUT", "repos/platform-factory/svc-hello"), [])
        self.assertEqual(len(w.issues), 1)
        self.assertIn("refused to write", w.issues[0]["title"])

    def test_the_put_comes_first_and_a_refused_records_branch_does_not_stop_it(self):
        """B4: the records use the controls token, whose hourly limit every
        controls workflow shares; the raise must not wait for it."""
        w = World()
        w.set_rung(0)
        rec = RefusingRecords(w.gh)
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            res = ratchet.raise_rung(w.gh, rec, w.cf, fakes.l1(), fakes.l2(), "escape-a-pr5", {"pr": 5})
        self.assertEqual((res["result"], w.rung()), ("raised", 1))
        self.assertIn("::warning::ratchet: ratchet/escape-a-pr5-before.json could not be written (403)",
                      log.getvalue())
        order = [(c["method"], c["path"]) for c in w.gh.calls if c["method"] in ("PUT", "RECORD")]
        self.assertEqual(order[0], ("PUT", "repos/platform-factory/svc-hello/rulesets/20204893"))
        self.assertEqual([u["path"] for u in res["unrecorded"]],
                         ["ratchet/escape-a-pr5-before.json", "ratchet/escape-a-pr5-after.json"])
        before = res["unrecorded"][0]["record"]
        self.assertEqual(pr_params(before["ruleset"])["required_reviewers"][0]["minimum_approvals"], 0)
        self.assertEqual(res["unrecorded"][0]["error"]["status"], 403)

    def test_a_failed_before_record_is_carried_by_the_after_record(self):
        w = World()
        w.set_rung(0)
        rec = RefusingRecords(w.gh, refuse=lambda p: p.endswith("-before.json"))
        with contextlib.redirect_stderr(io.StringIO()):
            ratchet.raise_rung(w.gh, rec, w.cf, fakes.l1(), fakes.l2(), "escape-a-pr5", {"pr": 5})
        after = rec.read("ratchet/escape-a-pr5-after.json")
        self.assertEqual(after["result"], "raised")
        self.assertEqual([u["path"] for u in after["unrecorded"]], ["ratchet/escape-a-pr5-before.json"])
        self.assertEqual(after["unrecorded"][0]["record"]["reason"], {"pr": 5})

    def test_a_refused_issue_does_not_stop_the_ratchet(self):
        w = World()
        w.set_rung(0)
        w.gh.route("PUT", r"repos/platform-factory/svc-hello/rulesets/20204893",
                   lambda m, p, b: ApiError(502, {"message": "Bad Gateway"}, m.group(0)))
        w.gh.route("GET", r"repos/platform-factory/factory-controls/issues",
                   lambda m, p, b: ApiError(403, {"message": "API rate limit exceeded"}, m.group(0)))
        res = ratchet.raise_rung(w.gh, w.records, w.cf, fakes.l1(), fakes.l2(), "escape-a-pr5", {"pr": 5})
        self.assertEqual((res["result"], res["issue"], res["error_status"]), ("write-failed", None, 502))
        self.assertEqual(res["issue_errors"][0]["status"], 403)
        self.assertEqual(w.records.read("ratchet/escape-a-pr5-after.json")["error"]["status"], 502)

    def test_the_ratchet_sends_no_request_to_the_wall_ruleset(self):
        """The wall ruleset (probe P1's fallback: the wall entry in a ruleset
        of its own) is the same on every rung. The ratchet raises, refuses or
        does nothing, and in every case neither reads nor writes the wall,
        even while the wall itself differs."""
        wall_path = f"repos/{fakes.SVC}/rulesets/{fakes.WALL_ID}"
        for rung, wall_change in ((0, "disabled"), (1, "disabled"), (0, None), (3, "lowered")):
            w = World()
            w.set_rung(rung)
            if wall_change == "disabled":
                w.wall["enforcement"] = "disabled"
            elif wall_change == "lowered":
                pr_params(w.wall)["required_reviewers"][0]["minimum_approvals"] = 0
            res = ratchet.raise_rung(w.gh, w.records, w.cf, fakes.l1(), fakes.l2(), "ruleset-x", {"x": 1})
            self.assertIn(res["result"], ("raised", "already-1", "refused"))
            self.assertEqual([c for c in w.gh.calls if c["path"].startswith(wall_path)], [], (rung, wall_change))
            touched = {c["path"] for c in w.gh.calls if "/rulesets" in c["path"]}
            self.assertLessEqual(touched, {f"repos/{fakes.SVC}/rulesets/{fakes.RUNG_ID}"})

    def test_already_at_one_writes_no_ruleset_and_opens_no_issue(self):
        w = World()
        res = ratchet.raise_rung(w.gh, w.records, w.cf, fakes.l1(), fakes.l2(), "escape-b-pr9", {"pr": 9})
        self.assertEqual(res["result"], "already-1")
        self.assertEqual(w.gh.writes("PUT", "repos/platform-factory/svc-hello"), [])
        self.assertEqual(w.issues, [])
        self.assertIsNotNone(w.records.read("ratchet/escape-b-pr9-after.json"))


if __name__ == "__main__":
    unittest.main()
