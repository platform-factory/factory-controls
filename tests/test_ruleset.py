"""l1.json is the note's L1 listing built on the live ruleset; l2.json differs in one field;
wall.json holds the wall entry in a ruleset of its own (probe P1's fallback)."""

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
        # The rung entry only: GitHub refuses a second entry for the same team
        # in one ruleset (probe P1), so the wall entry is in wall.json.
        self.assertEqual(p["required_reviewers"], [{"minimum_approvals": 1, "file_patterns": ["/go.mod", "/go.sum"],
                                                    "reviewer": {"id": 18810690, "type": "Team"}}])
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
            "rules/required_status_checks",
        ])
        self.assertEqual(paths, expected)

    def test_live_today_is_not_l1(self):
        chk = detector.ruleset_check({"rung": LIVE, "wall": None, "listing": LISTING}, fakes.l1(), fakes.l2(),
                                     fakes.wall(), False, fakes.load_cf()["rulesets"]["repository_rulesets"])
        self.assertFalse(chk["ok"])
        self.assertNotEqual(chk["diff_vs_l1"], [])
        self.assertEqual(chk["wall_diff"], [{"path": [], "expected": "ruleset", "actual": "not found"}])
        self.assertNotEqual(chk["list_diff"], [])   # class-wall is not on svc-hello yet


class WallTest(unittest.TestCase):
    """The wall entry in a ruleset of its own, saying nothing else."""

    def test_wall_is_the_notes_wall_entry_alone(self):
        w = fakes.wall()
        self.assertEqual((w["name"], w["target"], w["enforcement"], w["bypass_actors"]),
                         ("class-wall", "branch", "active", []))
        self.assertEqual(w["conditions"], fakes.l1()["conditions"])
        self.assertEqual([r["type"] for r in w["rules"]], ["pull_request"])
        self.assertEqual(pr_params(w)["required_reviewers"], [{
            "minimum_approvals": 1, "file_patterns": ["*", ".github/**", "!/go.mod", "!/go.sum"],
            "reviewer": {"id": 18810690, "type": "Team"}}])
        # Every other pull_request parameter is the rung ruleset's, so the two
        # layered say what one ruleset with both entries would have said.
        strip = lambda p: {k: v for k, v in p.items() if k != "required_reviewers"}  # noqa: E731
        self.assertEqual(strip(pr_params(w)), strip(pr_params(fakes.l1())))
        self.assertEqual(rs.wall_errors(fakes.l1(), w, 1), [])
        self.assertEqual(rs.wall_errors(fakes.l2(), w, 1), [])

    def test_wall_errors(self):
        cases = []
        w = fakes.wall()
        pr_params(w)["dismiss_stale_reviews_on_push"] = False
        cases.append((w, "differ besides required_reviewers"))
        w = fakes.wall()
        w["enforcement"] = "disabled"
        cases.append((w, "not active"))
        w = fakes.wall()
        w["rules"].append({"type": "deletion"})
        cases.append((w, "exactly one rule"))
        w = fakes.wall()
        pr_params(w)["required_reviewers"] = pr_params(fakes.l1())["required_reviewers"]
        cases.append((w, "the rung entry's patterns"))
        w = fakes.wall()
        pr_params(w)["required_reviewers"][0]["minimum_approvals"] = 0
        cases.append((w, "minimum_approvals is 0"))
        w = fakes.wall()
        w["bypass_actors"] = [{"actor_id": 5, "actor_type": "Integration", "bypass_mode": "always"}]
        cases.append((w, "bypass actors"))
        for body, words in cases:
            errors = rs.wall_errors(fakes.l1(), body, 1)
            self.assertTrue(any(words in e for e in errors), (words, errors))
        two = fakes.l1()
        pr_params(two)["required_reviewers"].append(pr_params(fakes.wall())["required_reviewers"][0])
        self.assertTrue(any("one required-reviewers entry" in e for e in rs.wall_errors(two, fakes.wall(), 1)))

    def test_the_committed_bodies_are_checked_together(self):
        from factory import state as st
        cf = fakes.load_cf()
        bodies = st.committed_bodies(cf)
        self.assertEqual(sorted(bodies), ["l1", "l2", "wall"])
        cf["rulesets"]["wall_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            st.committed_bodies(cf)


class NormalizeTest(unittest.TestCase):
    def test_get_form_with_ids_timestamps_and_reordering_equals_put_form(self):
        got = fakes.as_get(fakes.l1())
        pr_params(got)["required_reviewers"].reverse()
        pr_params(got)["allowed_merge_methods"] = ["squash"]
        self.assertEqual(rs.diff(rs.normalize(fakes.l1()), rs.normalize(got)), [])

    def test_pattern_order_is_meaningful(self):
        got = copy.deepcopy(fakes.wall())
        pr_params(got)["required_reviewers"][0]["file_patterns"] = ["!/go.mod", "!/go.sum", "*", ".github/**"]
        self.assertNotEqual(rs.diff(rs.normalize(fakes.wall()), rs.normalize(got)), [])

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
        pr_params(bad)["required_approving_review_count"] = 1
        with self.assertRaises(rs.RungError):
            rs.rung_path(fakes.l1(), bad)

    def test_rung_path_refuses_l2_that_differs_elsewhere(self):
        bad = fakes.l1()
        bad["enforcement"] = "evaluate"
        with self.assertRaises(rs.RungError):
            rs.rung_path(fakes.l1(), bad)


if __name__ == "__main__":
    unittest.main()
