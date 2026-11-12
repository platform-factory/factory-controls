"""l1.json is the note's L1 listing built on the live ruleset; l2.json differs in one field."""

import copy
import json
import unittest

from factory import ruleset as rs
from factory import detector
from tests import fakes

LIVE = fakes.load_json("svc-hello-ruleset-20204893-read-2026-11-11.json")
LISTING = fakes.load_json("svc-hello-rulesets-list-read-2026-11-11.json")


def pr_params(body):
    return next(r for r in body["rules"] if r["type"] == "pull_request")["parameters"]


class L1L2Test(unittest.TestCase):
    def test_l2_differs_from_l1_in_exactly_the_rung_field(self):
        p = rs.rung_path(fakes.l1(), fakes.l2())
        self.assertEqual(p[:3], ["rules", "pull_request", "required_reviewers"])
        self.assertEqual(json.loads(p[3]), ["Team", 18810690, ["/go.mod", "/go.sum"]])
        self.assertEqual(p[4], "minimum_approvals")

    def test_l1_is_the_notes_listing(self):
        l1 = fakes.l1()
        self.assertEqual((l1["name"], l1["target"], l1["enforcement"]), ("main-protection", "branch", "active"))
        self.assertEqual(l1["conditions"], {"ref_name": {"exclude": [], "include": ["~DEFAULT_BRANCH"]}})
        self.assertEqual(l1["bypass_actors"], [])
        p = pr_params(l1)
        self.assertEqual(p["required_approving_review_count"], 0)
        self.assertIs(p["dismiss_stale_reviews_on_push"], True)
        self.assertIs(p["require_last_push_approval"], False)
        self.assertIs(p["require_code_owner_review"], False)
        self.assertIs(p["required_review_thread_resolution"], False)
        self.assertIs(p["require_extra_approval_for_unattributed_changes"], True)
        self.assertEqual(p["allowed_merge_methods"], ["squash"])
        rung, wall = p["required_reviewers"]
        self.assertEqual(rung, {"minimum_approvals": 1, "file_patterns": ["/go.mod", "/go.sum"],
                                "reviewer": {"id": 18810690, "type": "Team"}})
        self.assertEqual(wall, {"minimum_approvals": 1, "file_patterns": ["*", ".github/**", "!/go.mod", "!/go.sum"],
                                "reviewer": {"id": 18810690, "type": "Team"}})
        checks = next(r for r in l1["rules"] if r["type"] == "required_status_checks")["parameters"]
        self.assertIs(checks["strict_required_status_checks_policy"], True)
        self.assertEqual(checks["required_status_checks"], [{"context": "class-scope", "integration_id": 15368},
                                                            {"context": "go-test", "integration_id": 15368}])
        self.assertEqual({r["type"] for r in l1["rules"]},
                         {"pull_request", "non_fast_forward", "deletion", "required_status_checks"})

    def test_l1_changes_only_the_named_fields_of_the_live_ruleset(self):
        """A field GitHub returns that the note does not name keeps its 2026-11-11 value."""
        d = rs.diff(rs.normalize(LIVE), rs.normalize(fakes.l1()))
        paths = sorted(rs.path_str(x["path"]) for x in d)
        expected = sorted([
            "rules/pull_request/dismiss_stale_reviews_on_push",
            "rules/pull_request/allowed_merge_methods",
            "rules/pull_request/required_reviewers/" + rs.reviewer_key(pr_params(fakes.l1())["required_reviewers"][0]),
            "rules/pull_request/required_reviewers/" + rs.reviewer_key(pr_params(fakes.l1())["required_reviewers"][1]),
            "rules/required_status_checks",
        ])
        self.assertEqual(paths, expected)

    def test_live_today_is_not_l1(self):
        chk = detector.ruleset_check(LIVE, LISTING, fakes.l1(), fakes.l2(), False,
                                     fakes.load_cf()["rulesets"]["repository_rulesets"])
        self.assertFalse(chk["ok"])
        self.assertEqual(chk["list_diff"], [])  # the list of rulesets already matches


class NormalizeTest(unittest.TestCase):
    def test_get_form_with_ids_timestamps_and_reordering_equals_put_form(self):
        got = fakes.as_get(fakes.l1())
        pr_params(got)["required_reviewers"].reverse()
        pr_params(got)["allowed_merge_methods"] = ["squash"]
        self.assertEqual(rs.diff(rs.normalize(fakes.l1()), rs.normalize(got)), [])

    def test_pattern_order_is_meaningful(self):
        got = copy.deepcopy(fakes.l1())
        pr_params(got)["required_reviewers"][1]["file_patterns"] = ["!/go.mod", "!/go.sum", "*", ".github/**"]
        self.assertNotEqual(rs.diff(rs.normalize(fakes.l1()), rs.normalize(got)), [])

    def test_missing_bypass_actors_is_a_difference(self):
        got = fakes.as_get(fakes.l1())
        del got["bypass_actors"]
        d = rs.diff(rs.normalize(fakes.l1()), rs.normalize(got))
        self.assertEqual([x["path"] for x in d], [["bypass_actors"]])

    def test_a_duplicate_rule_type_is_a_difference(self):
        got = fakes.as_get(fakes.l1())
        got["rules"].append({"type": "deletion"})
        self.assertNotEqual(rs.diff(rs.normalize(fakes.l1()), rs.normalize(got)), [])

    def test_rung_path_refuses_l2_that_differs_in_two_fields(self):
        bad = fakes.l2()
        pr_params(bad)["required_reviewers"][1]["minimum_approvals"] = 0
        with self.assertRaises(rs.RungError):
            rs.rung_path(fakes.l1(), bad)

    def test_rung_path_refuses_l2_that_differs_elsewhere(self):
        bad = fakes.l1()
        bad["enforcement"] = "evaluate"
        with self.assertRaises(rs.RungError):
            rs.rung_path(fakes.l1(), bad)


if __name__ == "__main__":
    unittest.main()
