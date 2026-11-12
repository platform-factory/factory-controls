"""The agent and reveal workflows hold the shape ADR-0024 §7 and §11 require.

actionlint checks that the files are valid workflows; these tests check the
decisions in them: job order, which job sees which secret, what the model's
job may do, and which commands it runs.
"""

import json
import re
import unittest
from pathlib import Path

from factory import classfile
from tests import fakes

ROOT = Path(__file__).resolve().parent.parent
SHA_PIN = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")


def load(name):
    wf = classfile.parse_yaml((ROOT / ".github/workflows" / name).read_text())
    wf["on"] = wf.pop(True, wf.get("on"))   # YAML 1.1 reads the key `on` as true
    return wf


def steps_text(job):
    return "\n".join(str(s) for s in job.get("steps", []))


class AgentWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.wf = load("agent.yml")
        self.jobs = self.wf["jobs"]
        self.cf = fakes.load_cf()

    def test_jobs_in_order_and_chained(self):
        self.assertEqual(list(self.jobs), ["guard", "pre-merge", "merge", "prepare", "agent", "open", "c21-record",
                                           "scorecard"])
        self.assertEqual(self.jobs["pre-merge"]["needs"], "guard")
        self.assertEqual(self.jobs["merge"]["needs"], "pre-merge")
        self.assertEqual(self.jobs["prepare"]["needs"], "merge")
        self.assertEqual(self.jobs["prepare"]["if"], "needs.merge.outputs.agent_pr_open == 'false'")
        self.assertEqual(self.jobs["agent"]["needs"], "prepare")
        self.assertEqual(self.jobs["agent"]["if"], "needs.prepare.outputs.ready == 'true'")
        self.assertEqual(self.jobs["open"]["needs"], ["prepare", "agent"])
        # C-17: the scorecard runs after a merge (ADV-06, M2), as a reusable workflow.
        self.assertEqual(self.jobs["scorecard"]["needs"], "merge")
        self.assertEqual(self.jobs["scorecard"]["if"], "needs.merge.outputs.scorecard_due == 'true'")
        self.assertEqual(self.jobs["scorecard"]["uses"], "./.github/workflows/scorecard.yml")
        self.assertIn("workflow_call", load("scorecard.yml")["on"])
        # C-21's model-job record runs only on a dispatch that asked for it.
        self.assertEqual(self.jobs["c21-record"]["needs"], "agent")
        self.assertIn("inputs.c21_model_job", self.jobs["c21-record"]["if"])

    def test_schedule_and_concurrency(self):
        minutes = self.cf["schedules"]["agent_minutes"]
        self.assertEqual(self.wf["on"]["schedule"], [{"cron": f"{','.join(map(str, minutes))} * * * *"}])
        self.assertIn("workflow_dispatch", self.wf["on"])
        self.assertEqual(self.wf["concurrency"], {"group": "agent", "cancel-in-progress": False})
        self.assertEqual(self.jobs["pre-merge"]["concurrency"], {"group": "ratchet", "cancel-in-progress": False})
        self.assertEqual(load("detector.yml")["jobs"]["detect"]["concurrency"]["group"], "ratchet")
        self.assertEqual(self.wf["permissions"], {})

    def test_environments_and_secrets(self):
        env = {n: j.get("environment") for n, j in self.jobs.items()}
        self.assertEqual(env, {"guard": None, "pre-merge": "ratchet", "merge": "agent-app", "prepare": None,
                               "agent": "agent-model", "open": "agent-app", "c21-record": None, "scorecard": None})
        # The c21 attempts step names other environments' secrets only to show
        # they come back empty in the model's job; it is left out here and
        # checked below.
        def text(j):
            return "\n".join(str(s) for s in j.get("steps", []) if s.get("id") != "c21")
        secrets = {n: sorted(set(re.findall(r"secrets\.(\w+)", text(j)))) for n, j in self.jobs.items()}
        self.assertEqual(secrets, {"guard": [], "pre-merge": ["RATCHET_APP_PRIVATE_KEY"],
                                   "merge": ["AGENT_APP_PRIVATE_KEY", "SEED_KEY"], "prepare": [],
                                   "agent": ["ANTHROPIC_API_KEY"],
                                   "open": ["AGENT_APP_PRIVATE_KEY", "SEED_KEY", "SUMMARY_KEY"],
                                   "c21-record": [], "scorecard": []})
        c21 = [s for s in self.jobs["agent"]["steps"] if s.get("id") == "c21"][0]
        self.assertEqual(c21["if"], "github.event_name == 'workflow_dispatch' && inputs.c21_model_job")
        self.assertTrue(all(k.startswith("C21_SECRET_") for k in c21["env"]))
        self.assertNotIn("ANTHROPIC_API_KEY", str(c21))

    def test_the_model_job_holds_no_github_credential(self):
        job = self.jobs["agent"]
        self.assertEqual(job["permissions"], {})
        text = steps_text(job)
        self.assertNotIn("actions/checkout", text)
        self.assertNotIn("github.token", text)
        self.assertNotIn("GITHUB_TOKEN", text)
        self.assertNotIn("create-github-app-token", text)
        self.assertEqual(job["container"]["image"], "${{ needs.prepare.outputs.builder_image }}")
        self.assertEqual(job["env"]["GOTOOLCHAIN"], "local")
        self.assertEqual(job["env"]["GOFLAGS"], "")
        # Only the model step sees the model credential.
        with_key = [s for s in job["steps"] if "ANTHROPIC_API_KEY" in str(s.get("env", {}))]
        self.assertEqual([s["id"] for s in with_key], ["model"])
        up = [s for s in job["steps"] if "upload-artifact" in s.get("uses", "")]
        self.assertEqual(len(up), 1)
        self.assertEqual(up[0]["with"]["name"], "agent-output")
        # ADV-09: the summary is uploaded only padded and encrypted.
        self.assertEqual(up[0]["with"]["path"].split(), ["output/go.mod", "output/go.sum", "output/summary.md.cms"])
        seal = [s for s in job["steps"] if s.get("name") == "seal the summary for the opening job"][0]
        self.assertIn("agenttool pad", seal["run"])
        self.assertIn("openssl cms -encrypt", seal["run"])
        self.assertIn("rm -f output/summary.md", seal["run"])
        self.assertLess(job["steps"].index(seal), job["steps"].index(up[0]))

    def test_claude_code_is_pinned_by_version_and_checksum(self):
        text = steps_text(self.jobs["agent"])
        self.assertIn("sha256sum -c", text)
        self.assertIn("claude-code-releases/${CC_VERSION}/${CC_PLATFORM}/claude", text)
        outs = self.jobs["prepare"]["outputs"]
        for k in ("builder_image", "harness_version", "harness_platform", "harness_sha256", "skill_sha256"):
            self.assertIn(k, outs)

    def test_tokens_are_scoped_to_svc_hello(self):
        for name in ("pre-merge", "merge", "open"):
            tok = [s for s in self.jobs[name]["steps"] if "create-github-app-token" in s.get("uses", "")]
            self.assertEqual(len(tok), 1, name)
            self.assertEqual((tok[0]["with"]["owner"], tok[0]["with"]["repositories"]),
                             ("platform-factory", "svc-hello"), name)
        open_tok = [s for s in self.jobs["open"]["steps"] if "create-github-app-token" in s.get("uses", "")][0]
        self.assertEqual({k: v for k, v in open_tok["with"].items() if k.startswith("permission-")},
                         {"permission-contents": "write", "permission-pull-requests": "write"})
        self.assertIn(self.cf["agent"]["app_token_action"]["commit"], open_tok["uses"])

    def test_every_action_is_pinned_to_a_commit(self):
        for name in ("agent.yml", "reveal.yml", "detector.yml"):
            for job in load(name)["jobs"].values():
                for s in job.get("steps", []):
                    if "uses" in s:
                        self.assertRegex(s["uses"], SHA_PIN, f"{name}: {s['uses']}")

    def test_job_permissions(self):
        p = {n: j.get("permissions") for n, j in self.jobs.items()}
        self.assertEqual(p, {"guard": {"contents": "read"}, "pre-merge": {"contents": "write", "issues": "write"},
                             "merge": {"contents": "write"}, "prepare": {"contents": "read"},
                             "agent": {}, "open": {"contents": "write", "actions": "write"},
                             "c21-record": {"contents": "write", "issues": "write"},
                             "scorecard": {"contents": "write", "issues": "write", "actions": "read"}})

    def test_seed_settlement_runs_before_prepare(self):
        steps = [s.get("id") for s in self.jobs["merge"]["steps"]]
        self.assertLess(steps.index("merge"), steps.index("settle"))
        self.assertIn("--settle-seeds", self.jobs["merge"]["steps"][steps.index("settle")]["run"])


SCHEDULED = ("agent.yml", "detector.yml", "scorecard.yml")
# Every workflow that runs the class reads the class file, so each waits for it.
CLASS_WORKFLOWS = SCHEDULED + ("fixtures.yml", "reveal.yml")
GUARDED = "needs.guard.outputs.ready == 'true'"
# Five placeholders; the wall ruleset's id stands in two places.
PLACEHOLDERS = ["human_logins[0]", "seeds.key_sha256", "skill.tag", "skill.commit",
                "rulesets.wall_ruleset_id", "rulesets.repository_rulesets[1].id"]


def as_list(needs):
    return [needs] if isinstance(needs, str) else list(needs or [])


def class_root(d, text=None):
    """A copy of the repository's layout as the guard sees it: the class file
    with `text`, or none at all (the state before its first commit)."""
    from pathlib import Path
    if text is not None:
        (Path(d) / "classes").mkdir()
        (Path(d) / "classes/go-module-bump.yaml").write_text(text)
    return Path(d)


class CronGuardTest(unittest.TestCase):
    """The scheduled workflows start on their cron as soon as main is pushed,
    and main is pushed without the class file (its first commit is the
    filled one). Each workflow that runs the class must stop at once, green,
    with a clear log line, and write nothing, both while the class file is
    absent and while it holds a placeholder."""

    def gated(self, jobs, name, seen=()):
        """Runs only once the guard reported ready: it needs the guard with the
        guard's condition, or needs only jobs that are gated. A job whose
        condition holds `always()` must also need a non-empty output of a gated
        job (a skipped job's outputs are empty)."""
        job = jobs[name]
        cond = str(job.get("if", ""))
        needs = as_list(job.get("needs"))
        if "guard" in needs:
            return GUARDED in cond and "always()" not in cond
        if not needs or name in seen:
            return False
        if "always()" in cond:
            return any(f"needs.{n}.outputs." in cond and "!= ''" in cond and self.gated(jobs, n, seen + (name,))
                       for n in needs)
        return all(self.gated(jobs, n, seen + (name,)) for n in needs)

    def test_every_class_workflow_waits_for_a_filled_class_file(self):
        for name in CLASS_WORKFLOWS:
            wf = load(name)
            if name in SCHEDULED:
                self.assertIn("schedule", wf["on"], name)
            jobs = wf["jobs"]
            self.assertEqual(list(jobs)[0], "guard", name)
            guard = jobs["guard"]
            self.assertNotIn("environment", guard)
            self.assertNotIn("needs", guard)
            self.assertNotIn("if", guard)
            self.assertEqual(guard["permissions"], {"contents": "read"}, name)
            self.assertNotIn("secrets.", str(guard))
            self.assertNotIn("create-github-app-token", str(guard))
            self.assertEqual(guard["outputs"], {"ready": "${{ steps.guard.outputs.ready }}"})
            step = [s for s in guard["steps"] if s.get("id") == "guard"][0]
            self.assertEqual(step["run"].strip(), "python3 -m factory.guard")
            self.assertNotIn("env", step)                     # no token reaches it
            for job in jobs:
                if job != "guard":
                    self.assertTrue(self.gated(jobs, job), f"{name}: job {job} can run before the guard")

    def run_guard(self, root, env=None):
        import contextlib
        import io
        import tempfile
        from unittest import mock
        from factory import classfile, guard
        with tempfile.NamedTemporaryFile("r") as gho, mock.patch.object(classfile, "ROOT", root), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("no network")):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = guard.main([], {"GITHUB_OUTPUT": gho.name, **(env or {})})
            return rc, gho.read(), err.getvalue(), json.loads(out.getvalue())

    def test_the_guard_stops_green_while_the_class_file_is_absent(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            rc, outputs, log, doc = self.run_guard(class_root(d))
        self.assertEqual((rc, outputs), (0, "ready=false\n"))
        self.assertTrue(log.startswith("::notice title=class file not ready::there is no "
                                       "classes/go-module-bump.yaml yet"), log)
        self.assertIn("this run does nothing and writes nothing", log)
        self.assertEqual((doc["ready"], doc["unfilled"]), (False, None))

    def test_the_guard_stops_green_while_the_class_file_holds_a_placeholder(self):
        import tempfile
        from tests import fakes
        with tempfile.TemporaryDirectory() as d:
            rc, outputs, log, doc = self.run_guard(class_root(d, fakes.placeholder_class_file_text()))
        self.assertEqual((rc, outputs), (0, "ready=false\n"))
        self.assertTrue(log.startswith("::notice title=class file not ready::the class file still holds "
                                       "placeholders at human_logins[0]"), log)
        self.assertEqual((doc["ready"], doc["unfilled"]), (False, PLACEHOLDERS))

    def test_the_guard_lets_a_filled_class_file_through(self):
        import tempfile
        from pathlib import Path
        from tests.test_cli import filled_class_file
        with tempfile.TemporaryDirectory() as d:
            text = Path(filled_class_file(d)).read_text()
            with tempfile.TemporaryDirectory() as r:
                rc, outputs, log, doc = self.run_guard(class_root(r, text))
        self.assertEqual((rc, outputs, doc["ready"]), (0, "ready=true\n", True))
        self.assertTrue(log.startswith("::notice title=class file ready::"))

    def test_the_repositorys_own_state(self):
        """Whatever state the repository is in, the guard agrees with it: not
        ready before the class file's first commit, ready after."""
        from factory import classfile
        rc, outputs, _, doc = self.run_guard(ROOT)
        self.assertEqual(rc, 0)
        self.assertEqual(doc["ready"], (ROOT / classfile.CLASS_PATH).is_file())

    def test_a_class_file_that_does_not_load_or_a_missing_copy_fails_the_guard(self):
        import contextlib
        import io
        import tempfile
        from factory import guard
        with tempfile.TemporaryDirectory() as d:
            root = class_root(d, "schema: 1\n")
            from unittest import mock
            from factory import classfile
            with mock.patch.object(classfile, "ROOT", root), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(guard.main([], {}), 1)
                self.assertEqual(guard.main([], {"CLASS_FILE": f"{d}/probe/p4-class.yaml"}), 1)
        self.assertTrue(err.getvalue().startswith("::error::"))


class RunModelScriptTest(unittest.TestCase):
    def setUp(self):
        self.sh = (ROOT / "agent/run-model.sh").read_text()

    def test_the_command_line(self):
        for flag in ('--tools "Read,Write,Bash"', '--allowedTools "Bash(go get *),Bash(go mod tidy),Write"',
                     "--permission-mode dontAsk", "--output-format json", "--no-session-persistence",
                     "--strict-mcp-config", '--append-system-prompt-file "$IN/skill/SKILL.md"',
                     '--model "$MODEL"', '--settings "$WORK/settings.json"'):
            self.assertIn(flag, self.sh)
        self.assertNotIn("--dangerously-skip-permissions", self.sh)
        self.assertNotIn("bypassPermissions", self.sh)
        self.assertIn("unset ANTHROPIC_API_KEY", self.sh)
        self.assertIn('"apiKeyHelper"', self.sh)
        self.assertNotIn("export CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1", self.sh)
        self.assertIn('"PreToolUse"', self.sh)

    def test_never_prints_the_model_result(self):
        self.assertNotIn("cat \"$WORK/result.json\"", self.sh)
        self.assertNotIn(".result", self.sh)


class RevealWorkflowTest(unittest.TestCase):
    def test_dispatch_only_with_confirmation(self):
        wf = load("reveal.yml")
        self.assertEqual(list(wf["on"]), ["workflow_dispatch"])
        job = wf["jobs"]["reveal"]
        self.assertEqual(job["if"], "needs.guard.outputs.ready == 'true' && inputs.confirm == 'the window is closed'")
        self.assertEqual(job["environment"], "agent-app")
        self.assertEqual(job["permissions"], {"contents": "write", "actions": "read"})
        self.assertIn("factory.reveal", steps_text(job))


if __name__ == "__main__":
    unittest.main()
