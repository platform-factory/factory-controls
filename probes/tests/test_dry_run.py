"""Every step of every probe script, in --dry-run: calls printed, nothing executed."""

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "lib"))
import probe  # noqa: E402

for d in ("p1-patterns", "p2-class-scope-attachment", "p3-approvals-labels", "p4-harness"):
    sys.path.insert(0, str(KIT / d))
import p1  # noqa: E402
import p2  # noqa: E402
import p3  # noqa: E402
import p4  # noqa: E402

RUNS = [
    (p1, ["create"]), (p1, ["prs"]), (p1, ["one-ruleset"]),
    (p1, ["ruleset", "--variant", "anchored", "--mode", "evaluate", "--config", "R"]),
    (p1, ["ruleset", "--variant", "unanchored", "--mode", "active", "--config", "W"]),
    (p1, ["ruleset", "--variant", "dotfiles", "--mode", "active", "--config", "W"]), (p1, ["read"]),
    (p1, ["l1-roundtrip"]), (p1, ["ratchet-rehearsal"]), (p1, ["cleanup"]),
    (p2, ["stub"]), (p2, ["ruleset"]), (p2, ["pr"]), (p2, ["read"]), (p2, ["push"]), (p2, ["green"]),
    (p2, ["two-prs"]), (p2, ["cleanup"]),
    (p3, ["bases"]), (p3, ["ruleset"]), (p3, ["human-prs"]), (p3, ["a-open"]), (p3, ["a-read", "--pr", "5"]), (p3, ["a-merge", "--pr", "5"]),
    (p3, ["a-push-content", "--pr", "5"]), (p3, ["a-push-empty", "--pr", "5"]), (p3, ["a-update-branch", "--pr", "5"]),
    (p3, ["c-approve", "--pr", "6", "--pr2", "7"]), (p3, ["d-labels", "--pr", "6"]), (p3, ["e-reads", "--pr", "6"]),
    (p3, ["b-open-merge"]), (p3, ["f-open"]), (p3, ["f-workflow-base"]),
    (p3, ["f-update-branch", "--pr", "8"]), (p3, ["f-remake", "--pr", "8"]),
    (p4, ["read-run", "--run", "1"]), (p4, ["read-pr", "--pr", "5"]), (p4, ["read-merge", "--pr", "5"]),
]


class DryRunTest(unittest.TestCase):
    def setUp(self):
        self.real_run = probe.subprocess.run
        probe.subprocess.run = lambda *a, **k: self.fail(f"executed in a dry run: {a}")
        # The state of an executed run (state.local.json, ignored by git) is
        # not read here, so the tests say the same before and after the probes.
        self.real_load = probe.load_state
        probe.load_state = lambda probe_dir: {}
        self.state_before = {p: p.read_bytes() for p in KIT.glob("*/state.local.json")}

    def tearDown(self):
        probe.subprocess.run = self.real_run
        probe.load_state = self.real_load
        self.assertEqual({p: p.read_bytes() for p in KIT.glob("*/state.local.json")}, self.state_before,
                         "a dry run wrote a state file")

    def test_every_step_prints_calls_and_executes_nothing(self):
        for mod, args in RUNS:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(mod.main(args + ["--dry-run"]), 0, (mod.__name__, args))
            lines = [json.loads(x) for x in buf.getvalue().splitlines() if x.startswith("{")]
            self.assertTrue(lines, (mod.__name__, args))
            for line in lines:
                self.assertIn(line["method"], ("GET", "POST", "PUT", "PATCH", "DELETE"))
                self.assertTrue(line["path"].startswith(("repos/", "orgs/", "graphql")), line)

    def test_the_ratchet_rehearsal_plans_a_one_field_raise_from_the_committed_bodies(self):
        """ADV-08: in a dry run the plan is made from l2.json; it must be a
        write of exactly the rung field, so the PUT body the step sends is
        the ratchet's own."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            p1.main(["ratchet-rehearsal", "--dry-run"])
        out = buf.getvalue()
        self.assertIn('"action": "write"', out)
        puts = [json.loads(x) for x in out.splitlines() if x.startswith("{") and '"method": "PUT"' in x]
        self.assertTrue(puts)
        ratchet, rs = p1.controls()
        self.assertEqual(rs.diff(rs.normalize(p1.adapted("l1", "ratchet")), rs.normalize(puts[-1]["body"])), [])

    def test_a_mode_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            p1.main(["read"])

    def test_p1_expected_entries_and_bodies(self):
        self.assertEqual(p1.EXPECTED["go.mod"], "rung")
        self.assertTrue(all(v == "wall" for k, v in p1.EXPECTED.items() if k != "go.mod"))
        # One ruleset per entry: GitHub refused both in one (P1, 2026-11-12).
        rr = []
        for entry in p1.ENTRIES:
            body = p1.ruleset_body("anchored", "active", "W", entry)
            self.assertEqual(len(body["rules"][0]["parameters"]["required_reviewers"]), 1)
            self.assertEqual(body["bypass_actors"], [])
            rr += body["rules"][0]["parameters"]["required_reviewers"]
        self.assertEqual([(e["file_patterns"], e["minimum_approvals"]) for e in rr],
                         [(["/go.mod", "/go.sum"], 0), (["*", ".github/**", "!/go.mod", "!/go.sum"], 1)])
        self.assertEqual(len(p1.one_ruleset_body("anchored", "active", "W")["rules"][0]["parameters"]
                             ["required_reviewers"]), 2)
        # Each configuration holds its own base branch and never main, so no
        # ruleset is edited from one configuration to another.
        self.assertEqual(body["conditions"]["ref_name"]["include"], ["refs/heads/p1-base/anchored-W"])
        self.assertNotEqual(p1.base_branch("anchored", "R"), p1.base_branch("anchored", "W"))
        for purpose in ("l1", "ratchet"):
            self.assertNotIn("~DEFAULT_BRANCH", json.dumps(p1.adapted("l1", purpose)["conditions"]))

    def test_p1_sends_no_loosening(self):
        """A second ruleset call for one configuration may only go from
        evaluate to active; cleanup deletes no ruleset."""
        st = {"rulesets": {"anchored-R": 1}, "modes": {"anchored-R": "active"}}
        api = p1.probe.Api(False, out=io.StringIO())
        with self.assertRaises(SystemExit):
            p1.step_ruleset(api, st, "anchored", "evaluate", "R")
        api = p1.probe.Api(False, out=io.StringIO())
        p1.step_cleanup(api, {"prs": {"anchored-R": {"go.mod": 3}}})
        self.assertEqual([c["method"] for c in api.calls], ["PATCH"])

    def test_execute_passes_an_app_token_through_the_environment(self):
        seen = {}

        class R:
            returncode, stdout, stderr = 0, '{"number": 9}', ""

        def runner(argv, input=None, capture_output=None, text=None, env=None):
            seen.update(argv=argv, env=env, input=input)
            return R()

        api = probe.Api(True, out=io.StringIO(), runner=runner, token="ghs_x")
        self.assertEqual(api.call("POST", "repos/x/y/pulls", {"a": 1}), {"number": 9})
        self.assertEqual(seen["env"]["GH_TOKEN"], "ghs_x")
        self.assertNotIn("ghs_x", " ".join(seen["argv"]))
        self.assertEqual(json.loads(seen["input"]), {"a": 1})


if __name__ == "__main__":
    unittest.main()
