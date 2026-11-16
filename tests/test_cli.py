"""Every CLI the workflows run, driven through main() with the workflow's arguments.

The other tests call the functions behind each CLI. These call main() itself:
the real argument parser, the real class-file loader on a filled copy of the
class file, the real Records client over a fake records branch, and only the
network replaced (each module's `GitHub` returns the fake). So a missing
import, or an argument a workflow passes that the parser lacks, fails here.
The review found prepare.main() calling branch_from_env without importing it
(B2); no test reached that line.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from factory import classfile, detector, guard, merge_step
from factory import fixtures as fx
from factory import open_pr as op
from factory import prepare as pp
from factory import reveal as rv
from factory import scorecard as sc
from factory import seeds as sd
from factory import state as st
from tests import fakes
from tests.fakes import HUMAN, SEED_KEY, World, go_mod_with

ROOT = fakes.ROOT
WORKFLOWS = ROOT / ".github/workflows"
SYNC = "golang.org/x/sync"
# Every module a workflow runs with `python3 -m factory.<name>`.
MODULES = {"merge_step": merge_step, "open_pr": op, "prepare": pp, "fixtures": fx, "detector": detector,
           "reveal": rv, "scorecard": sc, "guard": guard}
INVOCATION = re.compile(r"python3 -m factory\.(\w+)(.*)$")
# The clock reads no class file and takes no part in the class's state: it
# only dispatches the workflows whose own first job is the guard. Its
# arguments and its workflow are checked in tests/test_clock.py.
NOT_THE_CLASS = {"clock"}
# Values for the shell variables the workflows put on a command line.
SHELL_VALUES = {"FIXTURE_CASE": "c15-readme", "RUNNER_TEMP": "/runner/temp"}
SKILL_TAG = "go-module-bump/v0.1.0-rc.1"
SKILL_COMMIT = "c" * 40


def invocations() -> list[tuple[str, str, str]]:
    """(workflow, module, arguments) for every `python3 -m factory.<x>` a workflow runs."""
    out = []
    for f in sorted(WORKFLOWS.glob("*.yml")):
        for line in f.read_text().splitlines():
            s = line.strip()
            if s.startswith("#"):
                continue
            m = INVOCATION.search(s)
            if m and m.group(1) not in NOT_THE_CLASS:
                out.append((f.name, m.group(1), m.group(2).strip()))
    return out


def argv_of(args: str) -> list[str]:
    return shlex.split(re.sub(r"\$(\w+)", lambda m: SHELL_VALUES[m.group(1)], args))


def ago(minutes: float) -> str:
    """A time relative to the real clock: these CLIs read it themselves."""
    return st.iso(st.utcnow() - timedelta(minutes=minutes))


def filled_class_file(d: str, tag: str = SKILL_TAG, commit: str = SKILL_COMMIT) -> str:
    """The class file (or its template) with the five values filled as the
    class file's first commit fills them, the test key's hash and login in."""
    text = fakes.class_file_text(human=HUMAN, key_sha256=sd.key_sha256(SEED_KEY), skill_tag=tag,
                                 skill_commit=commit, wall_id=fakes.WALL_ID)
    p = Path(d) / "go-module-bump.yaml"
    p.write_text(text)
    return str(p)


class Cli:
    """One main() call: a temporary class file and GITHUB_OUTPUT, the fake
    GitHub in place of each module's client, stdout captured."""

    def __init__(self, gh, **cf_args):
        self.gh = gh
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.output = Path(self.dir) / "github_output"
        self.output.write_text("")
        self.env = {"CLASS_FILE": filled_class_file(self.dir, **cf_args), "SVC_TOKEN": "svc-token",
                    "CONTROLS_TOKEN": "controls-token", "GITHUB_RUN_ID": "300", "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_OUTPUT": str(self.output)}

    def close(self):
        self.tmp.cleanup()

    def run(self, module, argv, env=None, **kwargs):
        env = {**self.env, **(env or {})}
        fake = lambda token, *a, **k: self.gh  # noqa: E731
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            # merge_step builds its clients in detector.context_from_env.
            for target in {module, detector} - {merge_step}:
                stack.enter_context(mock.patch.object(target, "GitHub", fake))
            stack.enter_context(mock.patch.dict(os.environ, env))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            rc = module.main(argv) if module is detector else module.main(argv, env, **kwargs)
        self.stderr = err.getvalue()
        return rc, json.loads(out.getvalue())

    def outputs(self) -> dict:
        return dict(line.split("=", 1) for line in self.output.read_text().splitlines() if "=" in line)


class Parsed(Exception):
    """Raised in place of running a main() once its arguments parsed."""


class WorkflowArgumentsTest(unittest.TestCase):
    def parse(self, module, argv):
        orig = argparse.ArgumentParser.parse_args

        def capture(parser, args=None, namespace=None):
            raise Parsed(orig(parser, args, namespace))

        with mock.patch.object(argparse.ArgumentParser, "parse_args", capture), \
                contextlib.redirect_stderr(io.StringIO()):
            module.main(argv) if module is detector else module.main(argv, {})

    def test_every_flag_a_workflow_passes_exists(self):
        seen = set()
        for wf, name, args in invocations():
            self.assertIn(name, MODULES, f"{wf} runs factory.{name}, which this test does not know")
            with self.assertRaises(Parsed, msg=f"{wf}: python3 -m factory.{name} {args}"):
                self.parse(MODULES[name], argv_of(args))
            seen.add(name)
        self.assertEqual(seen, set(MODULES))

    def test_a_flag_the_parser_lacks_is_caught(self):
        with self.assertRaises(SystemExit):
            self.parse(pp, ["--out", "x", "--no-such-flag"])


# What each workflow invocation writes to $GITHUB_OUTPUT when it does nothing.
NOT_READY_OUTPUTS = {
    "detector": {}, "guard": {"ready": "false"},
    "merge_step --phase pre-merge": {"ok": "false"},
    "merge_step --phase merge": {"action": "none", "agent_pr_open": "true", "scorecard_due": "false"},
    "open_pr --settle-seeds": {"closed_open_pr": "false"},
    "open_pr --artifact-dir agent-output": {"pr_number": ""},
    "prepare --out agent-input": {"ready": "false"},
    "scorecard --out \"$RUNNER_TEMP/scorecard\"": {},
    "fixtures --case \"$FIXTURE_CASE\"": {"outcome": "not-run"},
    "reveal": {},
}


class NotReadyCliTest(unittest.TestCase):
    """Item 9 with item 8: main is pushed without the class file, and its
    first commit is the filled one. Each CLI a workflow runs treats a missing
    class file as it treats one holding a placeholder: exit 0, one notice
    line, the outputs that mean nothing was done, and not one request to
    GitHub, so nothing is written to svc-hello or the records branch."""

    def run_state(self, module, argv, root):
        gh = fakes.FakeGitHub()                       # no routes: any request would be recorded
        with tempfile.TemporaryDirectory() as d:
            out_path = Path(d) / "github_output"
            out_path.write_text("")
            env = {"SVC_TOKEN": "svc-token", "CONTROLS_TOKEN": "controls-token", "SEED_KEY": SEED_KEY,
                   "PRE_MERGE_OK": "true", "GITHUB_RUN_ID": "300", "GITHUB_RUN_ATTEMPT": "1",
                   "GITHUB_OUTPUT": str(out_path), "PATH": os.environ.get("PATH", "")}
            out, err = io.StringIO(), io.StringIO()
            with contextlib.ExitStack() as stack:
                for target in {module, detector} - {merge_step, guard}:
                    stack.enter_context(mock.patch.object(target, "GitHub", lambda *a, **k: gh))
                stack.enter_context(mock.patch.object(classfile, "ROOT", root))
                stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
                stack.enter_context(contextlib.redirect_stdout(out))
                stack.enter_context(contextlib.redirect_stderr(err))
                rc = module.main(argv) if module is detector else module.main(argv, env)
            outputs = dict(x.split("=", 1) for x in out_path.read_text().splitlines() if "=" in x)
        return rc, outputs, err.getvalue(), json.loads(out.getvalue().split("\n", 1)[0] if module is guard
                                                         else out.getvalue()), gh.calls

    def states(self):
        absent = tempfile.TemporaryDirectory()
        placeholder = tempfile.TemporaryDirectory()
        (Path(placeholder.name) / "classes").mkdir()
        (Path(placeholder.name) / classfile.CLASS_PATH).write_text(fakes.placeholder_class_file_text())
        self.addCleanup(absent.cleanup)
        self.addCleanup(placeholder.cleanup)
        return {"absent": Path(absent.name), "placeholder": Path(placeholder.name)}

    def test_every_workflow_invocation_does_nothing_in_both_states(self):
        checked = set()
        for state, root in self.states().items():
            for wf, name, args in invocations():
                if (name, args) == ("fixtures", "--case c21-model-job"):
                    continue    # reads no class file; its job needs the model's job, which needs the guard
                key = f"{name} {args}".strip()
                with self.subTest(state=state, invocation=key, workflow=wf):
                    rc, outputs, log, doc, calls = self.run_state(MODULES[name], argv_of(args), root)
                    self.assertEqual(rc, 0)
                    self.assertEqual(calls, [])
                    notices = [x for x in log.splitlines() if x.startswith("::")]
                    self.assertEqual(len(notices), 1, log)
                    self.assertTrue(notices[0].startswith("::notice title=class file not ready::"), notices[0])
                    self.assertIn("this run does nothing and writes nothing", notices[0])
                    self.assertIn("there is no classes/go-module-bump.yaml" if state == "absent"
                                  else "still holds placeholders", notices[0])
                    self.assertIs(doc["ready"], False)
                    self.assertEqual(outputs, NOT_READY_OUTPUTS[key])
                    checked.add(name)
        self.assertEqual(checked, set(MODULES))

    def test_a_local_dry_run_reads_the_template_while_the_class_file_is_absent(self):
        """--allow-placeholders (dry runs only) still works before the first
        commit: it reads templates/go-module-bump.yaml."""
        absent = self.states()["absent"]
        self.assertEqual(classfile.path_for_run({}, allow_template=True, root=absent),
                         absent / classfile.TEMPLATE_PATH)
        self.assertIsNone(classfile.path_for_run({}, root=absent))
        cf = classfile.require({}, {"human_logins[0]"}, allow_placeholders=True,
                               root=fakes.CLASS_SOURCE.parent.parent)
        self.assertEqual(cf["class"], "go-module-bump")


def merged_world() -> World:
    """Queue item 1 merged an hour ago, its push run green; item 2 open as PR 2
    on head h2 with both checks green and the listed human's approval."""
    w = World()
    w.queue_pr(1, 1, SYNC, "v0.18.0", ago(90), state="closed", merged_at=ago(60), merge_sha="m1")
    w.set_main("m1", go_mod_with(sync="v0.18.0"), parent="p0")
    w.push_run(501, "m1", [("completed", "success", ago(55))])
    w.queue_pr(2, 2, SYNC, "v0.19.0", ago(30), head_sha="h2")
    # Started on the same clock as the approval: the merge step counts an
    # approval only when it is no older than class-scope's first run on the head.
    w.class_scope_job("h2", 7001, "success", [("decide", "success"), ("verdict-inconclusive", "success"),
                                              ("verdict-fail", "success")], started=ago(28))
    w.go_test_check("h2", "success")
    w.review(2, "h2", ago(10))
    w.heartbeat(ago(5))
    return w.serve_records_branch()


class DetectorAndMergeStepCliTest(unittest.TestCase):
    def setUp(self):
        self.w = merged_world()
        self.cli = Cli(self.w.gh)

    def tearDown(self):
        self.cli.close()

    def test_detector(self):
        rc, out = self.cli.run(detector, [])
        self.assertEqual(rc, 0)
        self.assertEqual((out["acted"], out["rung"]), ([], 1))
        self.assertEqual(self.w.records.files["detector/heartbeat.json"]["run_id"], "300")

    def test_pre_merge(self):
        rc, out = self.cli.run(merge_step, ["--phase", "pre-merge"])
        self.assertEqual((rc, out["ok"]), (0, True), out["reasons"])
        self.assertEqual(self.cli.outputs(), {"ok": "true"})
        self.assertTrue(self.w.records.files["merge-step/300-1-pre-merge.json"]["ok"])

    def test_merge(self):
        rc, out = self.cli.run(merge_step, ["--phase", "merge"], {"SEED_KEY": SEED_KEY, "PRE_MERGE_OK": "true"})
        self.assertEqual((rc, out["action"], out["result"]), (0, "merge", "merged"))
        self.assertEqual(self.cli.outputs(), {"action": "merge", "agent_pr_open": "false", "scorecard_due": "true"})
        self.assertEqual(self.w.records.files["merge-step/300-1-merge.json"]["result"], "merged")

    def test_settle_seeds(self):
        rc, out = self.cli.run(op, ["--settle-seeds"], {"SEED_KEY": SEED_KEY})
        self.assertEqual((rc, out["phase"], out["settled"]), (0, "settle-seeds", []))
        self.assertEqual(self.cli.outputs(), {"closed_open_pr": "false"})


class PrepareCliTest(unittest.TestCase):
    """prepare.main() past the placeholder check (B2)."""

    def git(self, cwd, *args):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                               "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
                              cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    def repo(self, d, files):
        os.makedirs(d, exist_ok=True)
        self.git(d, "init", "-q", "-b", "main")
        for path, text in files.items():
            Path(d, path).parent.mkdir(parents=True, exist_ok=True)
            Path(d, path).write_text(text)
        self.git(d, "add", "-A")
        self.git(d, "commit", "-q", "-m", "c")
        return self.git(d, "rev-parse", "HEAD")

    def info(self, age_days):
        t = st.iso(st.utcnow() - timedelta(days=age_days))
        return lambda url: json.dumps({"Version": "v0.18.0", "Time": t}).encode() if url.endswith(".info") \
            else b"module golang.org/x/sync\n"

    def test_the_workflows_arguments_with_a_young_release(self):
        w = World().serve_records_branch()
        cli = Cli(w.gh)
        try:
            rc, out = cli.run(pp, ["--out", os.path.join(cli.dir, "agent-input")], fetch=self.info(2))
        finally:
            cli.close()
        self.assertEqual(rc, 0)
        self.assertEqual((out["ready"], out["item"]["position"]), (False, 1))
        self.assertIn("younger than 7 days", out["reason"])

    def test_ready_builds_the_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            svc_sha = self.repo(os.path.join(d, "svc"), {"go.mod": fakes.GO_MOD_BASE, "go.sum": "s\n",
                                                         "main.go": "package main\n"})
            skill_dir = os.path.join(d, "skill")
            skill_sha = self.repo(skill_dir, {"skills/go-module-bump/SKILL.md": "the skill\n"})
            self.git(skill_dir, "tag", SKILL_TAG)
            w = World()
            w.set_main(svc_sha, fakes.GO_MOD_BASE)
            w.serve_records_branch()
            cli = Cli(w.gh, commit=skill_sha)
            try:
                ws = os.path.join(cli.dir, "agent-input")
                rc, out = cli.run(pp, ["--out", ws, "--svc-git-url", "file://" + os.path.join(d, "svc"),
                                       "--skill-git-url", "file://" + skill_dir], fetch=self.info(30))
                outputs = cli.outputs()
                self.assertEqual((rc, out["ready"]), (0, True), out.get("reason"))
                self.assertEqual(Path(ws, "skill/SKILL.md").read_text(), "the skill\n")
                self.assertTrue(Path(ws, "svc/main.go").is_file())
            finally:
                cli.close()
        self.assertEqual(outputs["ready"], "true")
        self.assertEqual(json.loads(outputs["prepared"])["main_sha"], svc_sha)


class OpenCliTest(unittest.TestCase):
    def test_the_sealed_artifact_opens_a_pull_request(self):
        from tests.test_open_pr import SUMMARY, OpenWorld, SealedSummaryTest, _openssl_cms
        if not _openssl_cms():
            self.skipTest("openssl with cms is not installed")
        ow = OpenWorld()
        ow.w.serve_records_branch()
        sealer = SealedSummaryTest("test_sealed_artifact_opens_to_the_three_files")
        sealer.setUp()
        cli = Cli(ow.w.gh)
        try:
            art = Path(cli.dir, "agent-output")
            art.mkdir()
            (art / "go.mod").write_text(go_mod_with(sync="v0.20.0"))
            (art / "go.sum").write_text("sum\n")
            sealer.seal(SUMMARY.encode(), art)
            env = {"SEED_KEY": SEED_KEY, "SUMMARY_KEY": sealer.key.read_text(),
                   "PREPARED": json.dumps(ow.prepared()), "AGENT_USAGE": json.dumps({"input_tokens": "5"}),
                   "HARNESS_VERSION": "2.1.322"}
            rc, out = cli.run(op, ["--artifact-dir", str(art)], env)
            outputs = cli.outputs()
        finally:
            cli.close()
            sealer.tearDown()
        self.assertEqual((rc, out["result"], out["pr_number"]), (0, "opened", 6), out)
        self.assertEqual(outputs, {"pr_number": "6"})
        rec = ow.w.records.files["runs/300-1.json"]
        self.assertEqual((rec["pr_number"], rec["queue_position"]), (6, 3))


class ScorecardCliTest(unittest.TestCase):
    def test_writes_the_three_files_to_the_records_branch(self):
        from tests.scorecard_world import T0, ScoreWorld, clean_streak
        w = ScoreWorld()
        clean_streak(w, 2)
        w.serve_records_branch()
        cli = Cli(w.gh)
        try:
            # The CLI reads the clock itself; keep it at the world's start.
            with mock.patch.object(st, "utcnow", lambda: T0):
                rc, out = cli.run(sc, ["--out", os.path.join(cli.dir, "scorecard")])
        finally:
            cli.close()
        self.assertEqual((rc, out["streak"]), (0, 2))
        self.assertEqual(w.records.files["scorecard/go-module-bump.json"]["inputs_sha256"], out["inputs_sha256"])
        self.assertIsInstance(w.records.files["scorecard/go-module-bump.md"], str)
        self.assertIn("as_of", w.records.files["scorecard/go-module-bump.inputs.json"])


    def _run_with_requests_left(self, left):
        from tests.scorecard_world import T0, ScoreWorld, clean_streak
        w = ScoreWorld()
        clean_streak(w, 2)
        w.serve_records_branch()
        w.gh.route("GET", "rate_limit", {"resources": {"core": {"limit": 1000, "remaining": left}}})
        cli = Cli(w.gh)
        try:
            with mock.patch.object(st, "utcnow", lambda: T0):
                rc, out = cli.run(sc, ["--out", os.path.join(cli.dir, "scorecard")])
        finally:
            cli.close()
        return w, rc, out

    def test_defers_when_the_hour_has_too_few_requests_left(self):
        """The detector and the merge step share the token's hourly limit;
        a scorecard read must not use up what they need."""
        w, rc, out = self._run_with_requests_left(sc.MIN_REQUESTS_LEFT - 1)
        self.assertEqual((rc, out["deferred"], out["requests_left"]), (0, True, sc.MIN_REQUESTS_LEFT - 1))
        self.assertEqual([c["path"] for c in w.gh.calls], ["rate_limit"])
        self.assertNotIn("scorecard/go-module-bump.json", w.records.files)

    def test_runs_when_enough_requests_are_left_or_github_does_not_say(self):
        w, rc, out = self._run_with_requests_left(sc.MIN_REQUESTS_LEFT)
        self.assertEqual((rc, out["streak"]), (0, 2))
        self.assertIn("scorecard/go-module-bump.json", w.records.files)
        self.assertEqual(sc.requests_left(fakes.FakeGitHub()), None)     # no route: a 404


class FixturesCliTest(unittest.TestCase):
    def test_one_case(self):
        from tests.test_fixtures import FixtureWorld
        w = FixtureWorld()
        w.serve_records_branch()
        cli = Cli(w.gh)
        try:
            rc, out = cli.run(fx, ["--case", "c21-ruleset-put"])
            outputs = cli.outputs()
        finally:
            cli.close()
        self.assertEqual((rc, out["record"]["outcome"]), (0, "as-expected"))
        self.assertEqual(w.records.files["fixtures/c21-ruleset-put-300-1.json"]["outcome"], "as-expected")
        self.assertEqual(outputs["outcome"], "as-expected")

    def test_the_model_jobs_record(self):
        from tests.test_fixtures import FixtureWorld, ModelJobTest
        w = FixtureWorld()
        w.serve_records_branch()
        cli = Cli(w.gh)
        try:
            result = ModelJobTest("test_missing_attempts_are_incomplete").result()
            rc, out = cli.run(fx, ["--case", "c21-model-job"], {"MODEL_JOB_RESULT": json.dumps(result)})
        finally:
            cli.close()
        self.assertEqual((rc, out["record"]["outcome"]), (0, "as-expected"))
        self.assertIn(out["record_path"], w.records.files)


class RevealCliTest(unittest.TestCase):
    def test_publishes_the_key_once_the_run_is_closed(self):
        from tests.test_open_pr import OpenWorld
        ow = OpenWorld()
        w = ow.w
        w.gh.route("GET", rf"repos/{fakes.CONTROLS}/actions/workflows/agent\.yml", {"state": "disabled_manually"})
        w.class_records.append({"name": "close-go-module-bump.yaml", "kind": "close", "merged_at": ago(60)})
        w.serve_records_branch()
        cli = Cli(w.gh)
        try:
            rc, out = cli.run(rv, [], {"SEED_KEY": SEED_KEY})
        finally:
            cli.close()
        self.assertTrue(out["matches_class_file"])
        self.assertTrue(out["written"])
        self.assertEqual(w.records.files["close/seed-key.json"]["key_sha256"], sd.key_sha256(SEED_KEY))
        self.assertIn(rc, (0, 1))   # 1 only when a body or seed record does not verify


if __name__ == "__main__":
    unittest.main()
