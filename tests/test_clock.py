"""The clock dispatches each workflow at the minutes its cron line names.

The tests move a made-up time forward; nothing here sleeps or reads the
real time.
"""

import http.client
import os
import shlex
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from factory import clock
from factory.gh import ApiError
from tests import fakes
from tests.test_workflows import load

REPO = "platform-factory/factory-controls"


def at(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class Time:
    """A time that moves only when the clock sleeps."""

    def __init__(self, start):
        self.t = at(start)

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += timedelta(seconds=seconds)


def dispatched(gh):
    return [(c["path"].split("/")[-2], c["body"]) for c in gh.writes("POST")]


def fake_gh(refuse=(), lose=None):
    """`refuse`: workflows GitHub answers 422 for. `lose`: a workflow name to
    a list of exceptions, one raised per dispatch until the list is used up
    (a request that got no answer)."""
    gh = fakes.FakeGitHub()
    lose = {name: list(errors) for name, errors in (lose or {}).items()}

    def handler(m, params, body):
        name = m.group(1)
        if lose.get(name):
            raise lose[name].pop(0)
        if name in refuse:
            return ApiError(422, {"message": "Workflow is disabled"}, m.group(0))
        return None

    gh.route("POST", rf"repos/{REPO}/actions/workflows/([\w.-]+)/dispatches", handler)
    return gh


class CronTest(unittest.TestCase):
    def test_the_forms_this_repository_uses(self):
        self.assertEqual(clock.parse_cron("4,14,24,34,44,54 * * * *"), ([4, 14, 24, 34, 44, 54], None))
        self.assertEqual(clock.parse_cron("17 6 * * *"), ([17], [6]))
        self.assertEqual(clock.parse_cron("53,23 0,12 * * *"), ([23, 53], [0, 12]))

    def test_anything_else_is_refused(self):
        for bad in ("*/5 * * * *", "* * * * *", "4 * * * 1", "4 * 1 * *", "60 * * * *", "4 24 * * *",
                    "4 * * *", "4-10 * * * *", ""):
            with self.assertRaises(clock.ClockError, msg=bad):
                clock.parse_cron(bad)

    def test_the_three_workflows_cron_lines_are_ones_the_clock_keeps(self):
        cf = fakes.load_cf()
        self.assertEqual(clock.workflow_crons("detector.yml"), [(cf["schedules"]["detector_minutes"], None)])
        self.assertEqual(clock.workflow_crons("agent.yml"), [(cf["schedules"]["agent_minutes"], None)])
        self.assertEqual(clock.workflow_crons("scorecard.yml"), [([17], [6])])
        with self.assertRaises(clock.ClockError):
            clock.workflow_crons("reveal.yml")   # dispatch only: no cron line

    def test_due_is_after_exclusive_upto_inclusive(self):
        crons = [([4, 14], None)]
        self.assertEqual(clock.due(crons, at("2026-11-16T03:03:40Z"), at("2026-11-16T03:03:59Z")), [])
        self.assertEqual(clock.due(crons, at("2026-11-16T03:03:40Z"), at("2026-11-16T03:04:00Z")),
                         [at("2026-11-16T03:04:00Z")])
        self.assertEqual(clock.due(crons, at("2026-11-16T03:04:00Z"), at("2026-11-16T03:04:20Z")), [])
        self.assertEqual(clock.due(crons, at("2026-11-16T03:03:00Z"), at("2026-11-16T03:14:10Z")),
                         [at("2026-11-16T03:04:00Z"), at("2026-11-16T03:14:00Z")])

    def test_due_keeps_the_hour_and_crosses_midnight(self):
        daily = [([17], [6])]
        self.assertEqual(clock.due(daily, at("2026-11-16T05:00:00Z"), at("2026-11-16T07:30:00Z")),
                         [at("2026-11-16T06:17:00Z")])
        self.assertEqual(clock.due(daily, at("2026-11-16T07:00:00Z"), at("2026-11-16T08:30:00Z")), [])
        self.assertEqual(clock.due([([4], None)], at("2026-11-16T23:58:00Z"), at("2026-11-17T00:05:00Z")),
                         [at("2026-11-17T00:04:00Z")])


class RunTest(unittest.TestCase):
    def setUp(self):
        self.targets = {"detector.yml": [([4, 14, 24, 34, 44, 54], None)], "agent.yml": [([23, 53], None)],
                        "scorecard.yml": [([17], [6])]}
        self.lines = []

    def run_clock(self, gh, start, minutes, since=None):
        t = Time(start)
        last = clock.run(gh, REPO, "main", self.targets, since=since, run_seconds=minutes * 60,
                         now=t.now, sleep=t.sleep, log=self.lines.append)
        return last

    def test_each_minute_is_served_once_and_then_the_clock_hands_over(self):
        gh = fake_gh()
        last = self.run_clock(gh, "2026-11-16T03:03:50Z", 21)
        self.assertEqual(dispatched(gh), [
            ("detector.yml", {"ref": "main"}),      # 03:04
            ("detector.yml", {"ref": "main"}),      # 03:14
            ("agent.yml", {"ref": "main"}),         # 03:23
            ("detector.yml", {"ref": "main"}),      # 03:24
            ("clock.yml", {"ref": "main", "inputs": {"since": clock.iso(last)}}),
        ])
        self.assertEqual(clock.iso(last), "2026-11-16T03:24:50Z")
        self.assertIn("clock: 2026-11-16T03:04:10Z dispatched detector.yml for 03:04", self.lines)

    def test_a_start_by_hand_serves_nothing_from_before_it(self):
        gh = fake_gh()
        self.run_clock(gh, "2026-11-16T03:04:30Z", 1)
        self.assertEqual([name for name, _ in dispatched(gh)], ["clock.yml"])

    def test_a_handover_serves_the_minute_between_the_two_runs(self):
        gh = fake_gh()
        self.run_clock(gh, "2026-11-16T03:04:30Z", 1, since="2026-11-16T03:03:50Z")
        self.assertEqual([name for name, _ in dispatched(gh)], ["detector.yml", "clock.yml"])

    def test_a_since_that_is_no_handover_is_ignored(self):
        for since in ("2026-11-16T02:40:00Z",      # older than fifteen minutes: the chain was broken
                      "2026-11-16T03:10:00Z",      # in the future
                      "yesterday"):
            gh = fake_gh()
            self.run_clock(gh, "2026-11-16T03:04:30Z", 1, since=since)
            self.assertEqual([name for name, _ in dispatched(gh)], ["clock.yml"], since)

    def test_the_daily_workflow_runs_at_its_hour_only(self):
        gh = fake_gh()
        self.run_clock(gh, "2026-11-16T06:16:30Z", 1)
        self.assertEqual([name for name, _ in dispatched(gh)], ["scorecard.yml", "clock.yml"])
        gh = fake_gh()
        self.run_clock(gh, "2026-11-16T07:16:30Z", 1)
        self.assertEqual([name for name, _ in dispatched(gh)], ["clock.yml"])

    def test_a_refused_dispatch_does_not_stop_the_others(self):
        gh = fake_gh(refuse=("agent.yml",))
        self.run_clock(gh, "2026-11-16T03:22:50Z", 2)
        self.assertEqual([name for name, _ in dispatched(gh)], ["agent.yml", "detector.yml", "clock.yml"])
        self.assertTrue(any(line.startswith("::warning::clock:") and "agent.yml" in line for line in self.lines))

    def test_a_dispatch_that_gets_no_answer_does_not_end_the_clock(self):
        for lost in (urllib.error.URLError("connection reset"), TimeoutError("timed out"),
                     http.client.IncompleteRead(b"")):
            self.lines = []
            gh = fake_gh(lose={"detector.yml": [lost]})
            self.run_clock(gh, "2026-11-16T03:03:50Z", 21)
            # 03:04's request was lost; 03:14, 03:23 and 03:24 are still served, and the clock hands over.
            self.assertEqual([name for name, _ in dispatched(gh)],
                             ["detector.yml", "detector.yml", "agent.yml", "detector.yml", "clock.yml"], lost)
            self.assertEqual(len([line for line in self.lines if line.startswith("::warning::clock:")]), 1, lost)

    def test_a_handover_that_gets_no_answer_is_tried_again(self):
        gh = fake_gh(lose={"clock.yml": [urllib.error.URLError("connection reset"), http.client.IncompleteRead(b"")]})
        self.run_clock(gh, "2026-11-16T03:05:00Z", 1)
        self.assertEqual(len([n for n, _ in dispatched(gh) if n == "clock.yml"]), 3)
        self.assertTrue(any(line.startswith("clock: handed over") for line in self.lines))

    def test_a_handover_that_never_gets_an_answer_ends_the_run_red(self):
        gh = fake_gh(lose={"clock.yml": [OSError("network is unreachable")] * clock.HANDOFF_TRIES})
        with self.assertRaises(clock.ClockError):
            self.run_clock(gh, "2026-11-16T03:05:00Z", 1)

    def test_a_handover_that_keeps_failing_ends_the_run_red(self):
        gh = fake_gh(refuse=("clock.yml",))
        with self.assertRaises(clock.ClockError):
            self.run_clock(gh, "2026-11-16T03:05:00Z", 1)
        self.assertEqual(len([n for n, _ in dispatched(gh) if n == "clock.yml"]), clock.HANDOFF_TRIES)


class ClockWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.wf = load("clock.yml")
        self.job = self.wf["jobs"]["clock"]

    def test_it_holds_a_dispatch_token_and_nothing_else(self):
        self.assertEqual(self.wf["permissions"], {})
        self.assertEqual(list(self.wf["jobs"]), ["clock"])
        self.assertEqual(self.job["permissions"], {"actions": "write", "contents": "read"})
        self.assertNotIn("environment", self.job)
        self.assertNotIn("secrets.", str(self.job))
        self.assertEqual(self.job["if"], "github.ref == 'refs/heads/main'")

    def test_one_clock_at_a_time_started_by_dispatch_only(self):
        self.assertEqual(list(self.wf["on"]), ["workflow_dispatch"])
        self.assertEqual(self.wf["concurrency"], {"group": "clock", "cancel-in-progress": False})

    def test_it_keeps_the_three_timed_workflows_and_ends_before_the_job_limit(self):
        run = self.job["steps"][-1]["run"]
        self.assertEqual([w for w in run.split() if w.endswith(".yml")], ["detector.yml", "agent.yml", "scorecard.yml"])
        self.assertIn("--run-minutes 300", run)
        self.assertGreater(self.job["timeout-minutes"], 300)
        self.assertLessEqual(self.job["timeout-minutes"], 360)   # GitHub-hosted job limit
        # Every workflow with a cron line is one the clock keeps.
        timed = sorted(p.name for p in (Path(__file__).resolve().parent.parent / ".github/workflows").glob("*.yml")
                       if "schedule" in (load(p.name)["on"] or {}))
        self.assertEqual(timed, ["agent.yml", "detector.yml", "scorecard.yml"])

    def test_the_command_the_workflow_runs_parses_and_starts_the_clock(self):
        run = self.job["steps"][-1]["run"].replace("\\\n", " ")
        argv = shlex.split(run.replace("$SINCE", "2026-11-16T03:03:50Z"))
        self.assertEqual(argv[:3], ["python3", "-m", "factory.clock"])
        env = {"GITHUB_REPOSITORY": REPO, "GH_TOKEN": "t"}
        with mock.patch.dict(os.environ, env), mock.patch.object(clock, "run") as started:
            self.assertEqual(clock.main(argv[3:]), 0)
        (gh, repo, ref, targets), kwargs = started.call_args
        self.assertEqual((repo, ref, list(targets)), (REPO, "main", ["detector.yml", "agent.yml", "scorecard.yml"]))
        self.assertEqual((kwargs["since"], kwargs["run_seconds"]), ("2026-11-16T03:03:50Z", 300 * 60))
        self.assertEqual(self.job["steps"][-1]["env"], {"GH_TOKEN": "${{ github.token }}", "SINCE": "${{ inputs.since }}"})

    def test_without_a_token_or_a_repository_it_stops(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(clock, "run") as started:
            self.assertEqual(clock.main(["--workflow", "detector.yml"]), 2)
        started.assert_not_called()


if __name__ == "__main__":
    unittest.main()
