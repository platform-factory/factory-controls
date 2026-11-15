"""The opening job: artifact intake, the run record, opening, and seed settlement."""

import base64
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from factory import open_pr as op
from factory import seeds as sd
from factory import state as st
from factory.gh import ApiError
from tests import fakes
from tests.fakes import World, go_mod_with, ev

SYNC = "golang.org/x/sync"
T0 = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
KEY = "0123456789abcdef" * 4
SUMMARY = (Path(__file__).resolve().parent / "data" / "summaries" / "x-sync-v0.20.0.md").read_text()
PASS_STEPS = [("decide", "success"), ("verdict-inconclusive", "success"), ("verdict-fail", "success")]
FAIL_STEPS = [("decide", "success"), ("verdict-inconclusive", "success"), ("verdict-fail", "failure")]


def at(m):
    return st.iso(T0 + timedelta(minutes=m))


def write_files(d, names=op.ALLOWED_FILES, summary=SUMMARY, go_mod=None):
    for n in names:
        data = {"go.mod": go_mod or go_mod_with(sync="v0.20.0"), "go.sum": "sum\n", "summary.md": summary}.get(n, "x")
        Path(d, n).write_text(data)


class IntakeTest(unittest.TestCase):
    def test_exactly_the_three_files(self):
        with tempfile.TemporaryDirectory() as d:
            write_files(d)
            self.assertEqual(sorted(op.intake(d)), ["go.mod", "go.sum", "summary.md"])

    def refused(self, setup):
        with tempfile.TemporaryDirectory() as d:
            setup(d)
            with self.assertRaises(op.IntakeRefused):
                op.intake(d)

    def test_refusals(self):
        self.refused(lambda d: write_files(d, ("go.mod", "go.sum")))                                  # missing
        self.refused(lambda d: write_files(d, op.ALLOWED_FILES + ("main.go",)))                       # extra
        self.refused(lambda d: write_files(d, op.ALLOWED_FILES + (".hidden",)))                       # hidden
        self.refused(lambda d: write_files(d, op.ALLOWED_FILES + ("go.mod.orig",)))                   # near name
        self.refused(lambda d: (write_files(d, ("go.mod", "summary.md")), os.mkdir(Path(d, "go.sum"))))
        self.refused(lambda d: (write_files(d, ("go.mod", "summary.md")),
                                Path(d, "elsewhere").write_text("x"), os.symlink("/etc/passwd", Path(d, "go.sum")),
                                os.unlink(Path(d, "elsewhere"))))
        self.refused(lambda d: write_files(d, summary="x" * (op.SIZE_LIMITS["summary.md"] + 1)))
        self.refused(lambda d: write_files(d, summary="nul\x00byte"))
        self.refused(lambda d: (write_files(d), Path(d, "summary.md").write_bytes(b"\xff\xfe bad utf-8")))
        with self.assertRaises(op.IntakeRefused):
            op.intake("/no/such/dir")


class SealTest(unittest.TestCase):
    def test_round_trip_fixed_size_and_tamper(self):
        a = op.seal(KEY, "1-1", b"short")
        b = op.seal(KEY, "2-1", SUMMARY.encode())
        self.assertEqual(len(a["ct"]), len(b["ct"]))           # the length says nothing
        self.assertEqual(op.unseal(KEY, a), b"short")
        self.assertEqual(op.unseal(KEY, b), SUMMARY.encode())
        self.assertNotIn(b"Target module", base64.b64decode(b["ct"]))
        bad = dict(b, ct=base64.b64encode(b"\x00" + base64.b64decode(b["ct"])[1:]).decode())
        with self.assertRaises(ValueError):
            op.unseal(KEY, bad)
        with self.assertRaises(ValueError):
            op.unseal("ff" * 32, b)
        with self.assertRaises(ValueError):
            op.seal(KEY, "x", b"x" * op.SEAL_SIZE)


class RunRecordTest(unittest.TestCase):
    CONTRACT_KEYS = {"schema", "class", "queue_position", "module", "version", "pr_number", "head_sha",
                     "body_sha256", "controls_run_id", "controls_run_attempt", "job", "opened_at", "skill",
                     "model", "harness", "usage"}

    def record(self, summary, usage):
        cf = fakes.load_cf()
        item = {"position": 3, "module": SYNC, "version": "v0.20.0"}
        body = op.pr_body(item, summary, "77", "1")
        return op.run_record(cf=cf, item=item, pr_number=12, head_sha="h", body=body, base_sha="b",
                             branch="agent/go-module-bump/x", run_id="77", run_attempt="1",
                             opened_at=at(0), skill={"tag": "t", "commit": "c" * 40, "content_sha256": "s" * 64},
                             harness_version="2.1.322", usage=usage, sealed=op.seal(KEY, "77-1", SUMMARY.encode()),
                             rung_at_open=1)

    def test_shape(self):
        rec = self.record(SUMMARY, {"input_tokens": "120", "output_tokens": "30", "agent_seconds": "61",
                                    "cost_usd_estimate": "0.012300", "models": "claude-sonnet-5-5"})
        self.assertTrue(self.CONTRACT_KEYS <= set(rec))
        self.assertEqual(set(rec) - self.CONTRACT_KEYS, {"base_sha", "branch", "sealed_summary", "repo",
                                                         "rung_at_open"})
        self.assertEqual(rec["repo"], "platform-factory/svc-hello")
        self.assertEqual((rec["schema"], rec["class"], rec["job"]), (1, "go-module-bump", "open"))
        self.assertEqual((rec["controls_run_id"], rec["controls_run_attempt"]), (77, 1))
        self.assertEqual(rec["usage"], {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": None,
                                        "cache_creation_input_tokens": None, "agent_seconds": 61,
                                        "num_turns": None, "cost_usd_estimate": 0.0123})
        self.assertEqual(rec["harness"], {"name": "claude-code", "version": "2.1.322"})
        self.assertEqual(rec["model"], "claude-sonnet-5-5")
        self.assertEqual(rec["body_sha256"], op.sha256_text(op.pr_body(
            {"position": 3, "module": SYNC, "version": "v0.20.0"}, SUMMARY, "77", "1")))
        json.dumps(rec)

    def test_a_seed_record_has_the_same_fields_and_sizes(self):
        real = self.record(SUMMARY, {})
        seed = self.record(sd.substitute(SUMMARY, "wrong-test-count", {}), {})
        self.assertEqual(sorted(real), sorted(seed))
        self.assertEqual({k: type(v) for k, v in real.items()}, {k: type(v) for k, v in seed.items()})
        self.assertEqual(len(real["sealed_summary"]["ct"]), len(seed["sealed_summary"]["ct"]))


class ReviewerDecisionTest(unittest.TestCase):
    def r(self, state, body="", actor=fakes.HUMAN, at_="2026-11-12T10:00:00Z", commit="h"):
        return {"id": 1, "user": {"login": actor}, "state": state, "body": body, "submitted_at": at_,
                "commit_id": commit}

    def test_rules(self):
        D, H = op.reviewer_decision, [fakes.HUMAN]
        self.assertEqual(D([self.r("APPROVED")], [], H)["kind"], "approve")
        self.assertEqual(D([self.r("COMMENTED", "minutes: 3; sections: x; decision: close; reason: y")], [], H)["kind"], "close")
        self.assertEqual(D([self.r("CHANGES_REQUESTED")], [], H)["kind"], "close")
        self.assertIsNone(D([self.r("COMMENTED", "looking")], [], H))
        self.assertIsNone(D([self.r("APPROVED", actor="maintainer")], [], H))
        self.assertIsNone(D([], [ev("closed", "2026-11-12T10:00:00Z", actor="someone")], H))
        self.assertIsNone(D([], [ev("closed", "2026-11-12T10:00:00Z", app="platform-factory-agent")], H))
        self.assertEqual(D([], [ev("closed", "2026-11-12T10:00:00Z")], H)["source"], "close-event")
        first = D([self.r("APPROVED", at_="2026-11-12T10:05:00Z"),
                   self.r("COMMENTED", "decision: close", at_="2026-11-12T10:01:00Z")], [], H)
        self.assertEqual(first["kind"], "close")

    def test_a_later_close_undoes_an_earlier_approval(self):
        """ADV-16: the listed human's latest decision on the head counts."""
        H = [fakes.HUMAN]
        approve = self.r("APPROVED", commit="h2", at_="2026-11-12T10:00:00Z")
        changes = self.r("CHANGES_REQUESTED", commit="h2", at_="2026-11-12T10:05:00Z")
        close = {**self.r("COMMENTED", commit="h2", at_="2026-11-12T10:05:00Z"),
                 "body": "minutes: 2; sections: a; decision: close; reason: x"}
        comment = {**self.r("COMMENTED", commit="h2", at_="2026-11-12T10:06:00Z"), "body": "looks fine"}
        A = lambda reviews: op.approved_on_head(reviews, H, "h2", head_seen_at="2026-11-12T09:50:00Z")
        self.assertFalse(A([approve, changes]))
        self.assertFalse(A([approve, close]))
        self.assertTrue(A([approve, comment]))   # a plain comment decides nothing
        self.assertTrue(A([changes, {**approve, "submitted_at": "2026-11-12T10:09:00Z"}]))

    def test_a_dismissed_review_is_an_approval_only_without_a_later_close(self):
        """ADV-20 (5): DISMISSED does not say what was dismissed."""
        H = [fakes.HUMAN]
        dismissed = {**self.r("DISMISSED", commit="h1", at_="2026-11-12T10:00:00Z"), "body": ""}
        later = {**self.r("COMMENTED", commit="h2", at_="2026-11-12T10:05:00Z"),
                 "body": "minutes: 2; sections: a; decision: close; reason: x"}
        self.assertEqual(op.reviewer_decision([dismissed], [], H)["kind"], "approve")
        self.assertEqual(op.reviewer_decision([dismissed, later], [], H)["kind"], "close")
        formed = {**dismissed, "body": "minutes: 1; sections: a; decision: close; reason: x"}
        self.assertEqual(op.reviewer_decision([formed], [], H)["kind"], "close")

    def test_approved_on_head(self):
        seen = "2026-11-12T09:50:00Z"   # class-scope first ran on h2 ten minutes before the review
        A = lambda reviews, at=seen: op.approved_on_head(reviews, [fakes.HUMAN], "h2", head_seen_at=at)
        self.assertTrue(A([self.r("APPROVED", commit="h2")]))
        self.assertFalse(A([self.r("APPROVED", commit="h1")]))
        self.assertFalse(A([self.r("DISMISSED", commit="h2")]))
        self.assertFalse(A([self.r("APPROVED", actor="x", commit="h2")]))

    def test_an_approval_older_than_the_head_does_not_count(self):
        """Probe P3: after an empty commit or an update from the base GitHub
        keeps the approval and re-points its commit_id to the new head. The
        review's submitted_at stays, and the head's first class-scope run is
        later, so the approval is not one made on this head."""
        carried = self.r("APPROVED", commit="h2", at_="2026-11-12T10:00:00Z")   # commit_id already re-pointed
        A = lambda at: op.approved_on_head([carried], [fakes.HUMAN], "h2", head_seen_at=at)
        self.assertFalse(A("2026-11-12T10:00:30Z"))   # h2 appeared after the approval
        self.assertTrue(A("2026-11-12T10:00:00Z"))    # the same second counts as on the head
        self.assertTrue(A("2026-11-12T09:59:00Z"))
        self.assertFalse(A(None))                     # no server-side time for the head: nothing counts
        S = lambda reviews, at: op.approval_on_head(reviews, [fakes.HUMAN], "h2", head_seen_at=at)
        self.assertEqual(S([carried], "2026-11-12T10:00:30Z"), "older")
        self.assertEqual(S([carried], "2026-11-12T09:59:00Z"), "on-head")
        self.assertEqual(S([], "2026-11-12T09:59:00Z"), "none")
        self.assertEqual(S([self.r("DISMISSED", commit="h2")], "2026-11-12T09:59:00Z"), "none")
        self.assertEqual(S([self.r("APPROVED", commit="h1")], "2026-11-12T09:59:00Z"), "older")
        again = self.r("APPROVED", commit="h2", at_="2026-11-12T10:02:00Z")
        self.assertTrue(op.approved_on_head([carried, again], [fakes.HUMAN], "h2",
                                            head_seen_at="2026-11-12T10:00:30Z"))   # approved again: counts


class OpenWorld:
    """Two merged items and main at x/sync v0.19.0; the next item is position 3 (v0.20.0)."""

    def __init__(self):
        self.w = w = World()
        w.queue_pr(1, 1, SYNC, "v0.18.0", at(-60), state="closed", merged_at=at(-50), merge_sha="m0")
        w.queue_pr(5, 2, SYNC, "v0.19.0", at(-40), state="closed", merged_at=at(-30), merge_sha="m1")
        w.set_main("m1", go_mod_with(sync="v0.19.0"))
        self.order: list[str] = []
        self.created = {}
        s, c = "platform-factory/svc-hello", fakes.CONTROLS

        def refs(m, p, b):
            self.order.append("create-branch")
            if b["ref"] in self.created:
                return ApiError(422, {"message": "Reference already exists"}, "git/refs")
            self.created[b["ref"]] = b["sha"]
            return {"ref": b["ref"]}

        def graphql(m, p, b):
            self.order.append("commit")
            self.mutation = b
            return {"data": {"createCommitOnBranch": {"commit": {"oid": "newhead", "url": "u"}}}}

        def pulls(m, p, b):
            self.order.append("open-pr")
            n = 6
            w.prs[n] = {"number": n, "state": "open", "created_at": at(1), "merged_at": None, "closed_at": None,
                        "head": {"sha": "newhead", "ref": b["head"]}, "base": {"ref": "main"},
                        "user": {"login": fakes.AGENT_BOT}, "labels": [], "body": b["body"]}
            w.events.setdefault(n, [])
            return w.prs[n]

        def arts(m, p, b):
            return {"artifacts": [{"id": 4242, "name": "agent-output"}]}

        def delete_art(m, p, b):
            self.order.append("delete-artifact")
            return None

        w.gh.route("POST", rf"repos/{s}/git/refs", refs)
        w.gh.route("POST", "graphql", graphql)
        w.gh.route("POST", rf"repos/{s}/pulls", pulls)
        w.gh.route("GET", rf"repos/{c}/actions/runs/(\d+)/artifacts", arts)
        w.gh.route("DELETE", rf"repos/{c}/actions/artifacts/(\d+)", delete_art)
        real_write = w.records.write

        def write(path, obj, message):
            self.order.append(f"record:{path.split('/')[0]}")
            return real_write(path, obj, message)

        w.records.write = write

    def ctx(self):
        return op.Ctx(cf=self.w.cf, l1=fakes.l1(), l2=fakes.l2(), gh_svc=self.w.gh, records=self.w.records,
                      run_id="77", run_attempt="1", now=T0)

    def prepared(self):
        return {"item": {"position": 3, "module": SYNC, "version": "v0.20.0"}, "main_sha": "m1",
                "skill": {"tag": "go-module-bump/v1.0.0", "commit": "c" * 40, "content_sha256": "s" * 64}}

    def files(self, summary=SUMMARY):
        return {"go.mod": go_mod_with(sync="v0.20.0").encode(), "go.sum": b"sum\n", "summary.md": summary.encode()}


def sched_with(position, seed_type="wrong-test-count"):
    return [{"j": 0, "position": position, "type": seed_type}, {"j": 1, "position": 17, "type": "wrong-module"}]


class OpenItemTest(unittest.TestCase):
    def open_with(self, sched, ow=None):
        ow = ow or OpenWorld()
        with mock.patch.object(sd, "schedule", lambda key, cf: sched):
            out = op.open_item(ow.ctx(), ow.files(), ow.prepared(), KEY, {"input_tokens": 5}, "2.1.322")
        return ow, out

    def test_opens_commits_and_records_in_order(self):
        ow, out = self.open_with(sched_with(9))
        self.assertEqual(ow.order, ["delete-artifact", "create-branch", "commit", "open-pr", "record:runs"])
        self.assertEqual(out["result"], "opened")
        self.assertEqual(out["pr_number"], 6)
        inp = ow.mutation["variables"]["input"]
        self.assertEqual(inp["expectedHeadOid"], "m1")
        self.assertEqual(inp["branch"], {"repositoryNameWithOwner": "platform-factory/svc-hello",
                                         "branchName": "agent/go-module-bump/golang.org-x-sync-v0.20.0"})
        self.assertEqual([a["path"] for a in inp["fileChanges"]["additions"]], ["go.mod", "go.sum"])
        rec = ow.w.records.read("runs/77-1.json")
        self.assertEqual((rec["pr_number"], rec["head_sha"], rec["queue_position"]), (6, "newhead", 3))
        self.assertEqual(rec["body_sha256"], op.sha256_text(ow.w.prs[6]["body"]))
        self.assertIn(SUMMARY.rstrip(), ow.w.prs[6]["body"])
        self.assertEqual(op.unseal(KEY, rec["sealed_summary"]).decode(), SUMMARY)

    def test_a_seed_differs_only_in_the_summary_text(self):
        real_ow, real = self.open_with(sched_with(9))           # position 3 is not drawn: a real item
        seed_ow, seed = self.open_with(sched_with(2))           # 2 merges, none opened since: a seed
        self.assertEqual(sorted(real), sorted(seed))
        diff = {k for k in real if real[k] != seed[k]}
        self.assertEqual(diff, {"body_sha256"})
        rb, sb = real_ow.w.prs[6]["body"], seed_ow.w.prs[6]["body"]
        self.assertIn("- Tests: 10 test functions", rb)
        self.assertIn("- Tests: 11 test functions", sb)
        self.assertEqual(rb.replace("- Tests: 10 test", "- Tests: 11 test"), sb)
        rr, sr = real_ow.w.records.read("runs/77-1.json"), seed_ow.w.records.read("runs/77-1.json")
        self.assertEqual(sorted(rr), sorted(sr))
        self.assertEqual(real_ow.order, seed_ow.order)
        # The sealed field holds the agent's own words in both.
        self.assertEqual(op.unseal(KEY, sr["sealed_summary"]).decode(), SUMMARY)

    def test_after_the_seed_the_next_pull_request_is_not_one(self):
        fresh = OpenWorld()
        self.assertEqual(op.seed_for_new_pr(st.gather(fresh.w.gh, fresh.w.records, fresh.w.cf),
                                            sched_with(2), 1, fresh.w.cf)["position"], 2)
        ow = OpenWorld()
        ow.w.queue_pr(6, 3, SYNC, "v0.20.0", at(-20), state="closed", closed_at=at(-10))   # the seed, settled
        ow.w.records.files["seeds/6.json"] = {"pr_number": 6}
        snap = st.gather(ow.w.gh, ow.w.records, ow.w.cf)
        self.assertIsNone(op.seed_for_new_pr(snap, sched_with(2), 1, ow.w.cf))

    def test_no_seed_opens_at_l2_and_every_reader_agrees(self):
        """Seeds run at L1 only (the note: "Seeds: four, at L1"). After a
        fixture rise a drawn position can be reached at L2; no seed opens,
        and no reader of the schedule takes that pull request for one."""
        from factory import scorecard as sc
        l2_ow = OpenWorld()
        l2_ow.w.set_rung(0)
        self.open_with(sched_with(2), l2_ow)                     # position 2 drawn, reached at L2
        self.assertIn("- Tests: 10 test functions", l2_ow.w.prs[6]["body"])        # the agent's own words
        self.assertEqual(l2_ow.w.records.read("runs/77-1.json")["rung_at_open"], 0)
        l1_ow, _ = self.open_with(sched_with(2))                 # the same at L1: a seed
        self.assertIn("- Tests: 11 test functions", l1_ow.w.prs[6]["body"])
        self.assertEqual(l1_ow.w.records.read("runs/77-1.json")["rung_at_open"], 1)

        def state(ow):
            return st.derive_state(st.gather(ow.w.gh, ow.w.records, ow.w.cf), ow.w.cf, fakes.l1(), fakes.l2(),
                                   [2, 17])
        s2, s1 = state(l2_ow), state(l1_ow)
        self.assertEqual((s2.seeds, s2.seeds_skipped_at_l2), ([], [2]))
        self.assertEqual((s1.seeds, s1.seeds_skipped_at_l2), ([6], []))
        self.assertIsNone(op._position_of(6, st.gather(l2_ow.w.gh, l2_ow.w.records, l2_ow.w.cf),
                                          sched_with(2), 0))
        seeds = sc._seeds([], [2, 17], {}, s2)
        self.assertEqual((seeds["not_run_positions"], seeds["skipped_at_l2_positions"]), ([2, 17], [2]))
        self.assertIsNone(op.seed_for_new_pr({}, sched_with(2), 0, l2_ow.w.cf))

    def test_refusals_write_nothing(self):
        cases = []
        ow = OpenWorld()
        ow.w.queue_pr(6, 3, SYNC, "v0.20.0", at(-5))                      # an agent PR is open
        cases.append((ow, ow.files()))
        ow = OpenWorld()
        ow.w.set_main("m2", go_mod_with(sync="v0.19.0"))                  # main moved
        cases.append((ow, ow.files()))
        ow = OpenWorld()
        ow.w.set_main("m1", go_mod_with(sync="v0.20.0"))                  # next item is no longer position 3
        cases.append((ow, ow.files()))
        ow = OpenWorld()
        cases.append((ow, {**ow.files(), "go.mod": go_mod_with(sync="v0.19.0").encode()}))   # unchanged
        ow = OpenWorld()
        cases.append((ow, ow.files(summary="prose without the fields\n")))
        for ow, files in cases:
            with mock.patch.object(sd, "schedule", lambda key, cf: sched_with(9)):
                with self.assertRaises(op.OpenRefused):
                    op.open_item(ow.ctx(), files, ow.prepared(), KEY, {}, None)
            self.assertEqual([c for c in ow.w.gh.calls if c["method"] != "GET"], [])
            self.assertEqual(ow.order, [])

    def test_branch_collision_takes_the_run_suffix(self):
        ow = OpenWorld()
        ow.created["refs/heads/agent/go-module-bump/golang.org-x-sync-v0.20.0"] = "old"
        _, out = self.open_with(sched_with(9), ow)
        self.assertEqual(out["branch"], "agent/go-module-bump/golang.org-x-sync-v0.20.0-r77-1")

    def test_cli_refusal_is_recorded_not_a_failure(self):
        with tempfile.TemporaryDirectory() as d, tempfile.NamedTemporaryFile("r") as gho:
            write_files(d, op.ALLOWED_FILES + ("extra",))
            ow = OpenWorld()
            with mock.patch.object(op, "context_from_env", lambda env, dry, allow: ow.ctx()), \
                    mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                cf = ow.w.cf
                cf["seeds"]["key_sha256"] = sd.key_sha256(KEY)
                rc = op.main(["--artifact-dir", d], {"SEED_KEY": KEY, "GITHUB_OUTPUT": gho.name})
            self.assertEqual(rc, 0)
            with open(gho.name) as f:
                self.assertEqual(f.read(), "pr_number=\n")

    def test_wrong_seed_key_is_refused(self):
        cf = fakes.load_cf()
        cf["seeds"]["key_sha256"] = sd.key_sha256(KEY)
        with self.assertRaises(op.OpenRefused):
            op._seed_key({"SEED_KEY": "ff" * 32}, cf, False)
        with self.assertRaises(op.OpenRefused):
            op._seed_key({}, cf, False)
        self.assertEqual(op._seed_key({"SEED_KEY": KEY}, cf, False), KEY)


class SettleWorld(OpenWorld):
    """PR 6 is open on head h6: the first queue-item PR after the 2nd merge, so a seed when 2 is drawn."""

    def __init__(self):
        super().__init__()
        w = self.w
        w.queue_pr(6, 3, SYNC, "v0.20.0", at(-20), head_sha="h6")
        w.prs[6]["body"] = op.pr_body({"position": 3, "module": SYNC, "version": "v0.20.0"}, SUMMARY, "77", "1")
        w.records.files["runs/1006-1.json"]["body_sha256"] = op.sha256_text(w.prs[6]["body"])
        w.class_scope_job("h6", 7001, "success", PASS_STEPS)
        w.go_test_check("h6", "success")
        w.pr_commits[6] = [{"sha": "h6", "parents": [{"sha": "m1"}], "committer": {"login": "web-flow"},
                            "commit": {"verification": {"verified": True}}}]

        def edits(m, p, b):
            return {"data": {"repository": {"pullRequest": {"userContentEdits": {"nodes": self.edit_nodes}}}}}

        self.edit_nodes = []
        w.gh.route("POST", "graphql", edits)

    def settle(self, sched=None):
        with mock.patch.object(sd, "schedule", lambda key, cf: sched or sched_with(2)):
            return op.settle_seeds(self.ctx(), KEY)


class SettleTest(unittest.TestCase):
    def test_no_decision_changes_nothing_and_prints_nothing_specific(self):
        sw = SettleWorld()
        out = sw.settle()
        self.assertEqual((out["settled"], out["finished"], out["closed_open_pr"]), ([], [], False))
        self.assertEqual([c for c in sw.w.gh.calls if c["method"] != "GET"], [])
        # The same output when PR 6 is a real item.
        real = SettleWorld().settle(sched_with(9))
        self.assertEqual(real, out)

    def test_approved_seed_is_recorded_then_labelled_and_closed(self):
        sw = SettleWorld()
        sw.w.review(6, "h6", at(5))
        out = sw.settle()
        rec = sw.w.records.read("seeds/6.json")
        self.assertEqual((rec["status"], rec["decision"]["kind"], rec["caught"]), ("decided", "approve", False))
        self.assertEqual((rec["position"], rec["type"]), (2, "wrong-test-count"))
        self.assertTrue(out["closed_open_pr"])
        self.assertEqual(sw.w.prs[6]["state"], "closed")
        writes = [c["path"] for c in sw.w.gh.calls if c["method"] != "GET"]
        self.assertEqual(writes, ["repos/platform-factory/svc-hello/issues/6/labels",
                                  "repos/platform-factory/svc-hello/issues/6/comments",
                                  "repos/platform-factory/svc-hello/pulls/6"])
        self.assertEqual(sw.w.gh.writes("POST", "repos/platform-factory/svc-hello/issues/6/labels")[0]["body"],
                         {"labels": ["seeded"]})
        self.assertLess(sw.order.index("record:seeds"), len(sw.order))   # written before any svc write
        self.assertEqual(sw.order, ["record:seeds"])

    def test_closed_by_the_reviewer_with_the_form(self):
        sw = SettleWorld()
        sw.w.review(6, "h6", at(5), state="COMMENTED",
                    body="minutes: 4; sections: tests; decision: close; reason: test count 11, go-test says 10")
        sw.w.prs[6]["state"] = "closed"
        sw.w.events[6].append(ev("closed", at(6)))
        out = sw.settle()
        rec = sw.w.records.read("seeds/6.json")
        self.assertEqual((rec["status"], rec["decision"]["kind"], rec["caught"]), ("decided", "close", True))
        self.assertIn("test count 11", rec["decision"]["body"])
        self.assertFalse(out["closed_open_pr"])
        self.assertEqual([c["path"] for c in sw.w.gh.calls if c["method"] != "GET"],
                         ["repos/platform-factory/svc-hello/issues/6/labels"])

    def test_head_changed_before_the_decision_is_tampered(self):
        sw = SettleWorld()
        sw.w.prs[6]["head"]["sha"] = "h6b"
        sw.w.pr_commits[6].append({"sha": "h6b", "parents": [{"sha": "h6"}], "committer": {"login": "someone"},
                                   "commit": {"verification": {"verified": False}}})
        sw.w.class_scope_job("h6b", 7002, "success", PASS_STEPS)
        sw.w.go_test_check("h6b", "success")
        sw.w.review(6, "h6b", at(5))
        sw.settle()
        rec = sw.w.records.read("seeds/6.json")
        self.assertEqual(rec["status"], "void-tampered")
        self.assertIn("head at the decision", rec["tamper"]["reasons"][0])

    def test_an_update_branch_merge_is_not_tampering(self):
        sw = SettleWorld()
        sw.w.prs[6]["head"]["sha"] = "h6u"
        sw.w.pr_commits[6].append({"sha": "h6u", "parents": [{"sha": "h6"}, {"sha": "m1x"}],
                                   "committer": {"login": "web-flow"}, "commit": {"verification": {"verified": True}}})
        sw.w.class_scope_job("h6u", 7003, "success", PASS_STEPS)
        sw.w.go_test_check("h6u", "success")
        sw.w.review(6, "h6u", at(5))
        sw.settle()
        self.assertEqual(sw.w.records.read("seeds/6.json")["status"], "decided")

    def test_body_edited_before_the_decision_is_tampered_after_is_not(self):
        for edit_at, status in ((at(3), "void-tampered"), (at(9), "decided")):
            sw = SettleWorld()
            sw.w.prs[6]["body"] += "\nedited"
            sw.edit_nodes = [{"editedAt": at(-20), "editor": {"login": fakes.AGENT_BOT}},   # creation
                             {"editedAt": edit_at, "editor": {"login": "someone"}}]
            sw.w.review(6, "h6", at(5))
            sw.settle()
            self.assertEqual(sw.w.records.read("seeds/6.json")["status"], status, edit_at)

    def test_failed_check_voids_the_seed(self):
        sw = SettleWorld()
        sw.w.check_runs["h6"] = []
        sw.w.class_scope_job("h6", 7004, "failure", FAIL_STEPS)
        sw.w.go_test_check("h6", "success")
        out = sw.settle()
        self.assertEqual(sw.w.records.read("seeds/6.json")["status"], "void-check-failed")
        self.assertTrue(out["closed_open_pr"])

    def test_a_fail_only_because_the_head_is_behind_main_is_not_counted(self):
        """B6: class-scope fails a head behind main (head-behind-main); the
        merge phase updates the branch, and the seed must not be voided."""
        sw = SettleWorld()
        sw.w.check_runs["h6"] = []
        sw.w.class_scope_job("h6", 7005, "failure", FAIL_STEPS)
        sw.w.go_test_check("h6", "success")
        sw.w.behind["h6"] = 2
        out = sw.settle()
        self.assertEqual((out["settled"], out["finished"], out["closed_open_pr"]), ([], [], False))
        self.assertIsNone(sw.w.records.read("seeds/6.json"))
        self.assertEqual(sw.w.prs[6]["state"], "open")
        self.assertEqual([c for c in sw.w.gh.calls if c["method"] != "GET"], [])
        # The same compare is made for a real item, so the reads do not differ (ADV-11).
        real = SettleWorld()
        real.w.behind["h6"] = 2
        real.settle(sched_with(9))
        compares = lambda w: [c["path"] for c in w.gh.calls if "/compare/" in c["path"]]  # noqa: E731
        self.assertEqual(compares(sw.w), compares(real.w))
        self.assertEqual(compares(sw.w), ["repos/platform-factory/svc-hello/compare/m1...h6"])

    def test_a_fail_on_a_head_up_to_date_with_main_still_voids(self):
        sw = SettleWorld()
        sw.w.check_runs["h6"] = []
        sw.w.class_scope_job("h6", 7006, "failure", FAIL_STEPS)
        sw.w.go_test_check("h6", "success")
        sw.w.behind["h6"] = 0
        sw.settle()
        rec = sw.w.records.read("seeds/6.json")
        self.assertEqual((rec["status"], rec["behind_by"]), ("void-check-failed", 0))

    def test_idempotent_and_finishes_a_half_done_settlement(self):
        sw = SettleWorld()
        sw.w.review(6, "h6", at(5))
        sw.w.records.files["seeds/6.json"] = {"pr_number": 6, "status": "decided", "type": "wrong-test-count",
                                              "position": 2}
        out = sw.settle()                                  # record exists, PR still open: finish it
        self.assertEqual(out["settled"], [])
        self.assertEqual(sw.w.prs[6]["state"], "closed")
        sw.w.prs[6]["labels"] = [{"name": "seeded"}]
        calls = len(sw.w.gh.calls)
        self.assertEqual(sw.settle()["finished"], [])
        self.assertEqual([c for c in sw.w.gh.calls[calls:] if c["method"] != "GET"], [])

    def test_a_real_item_is_never_settled(self):
        sw = SettleWorld()
        sw.w.review(6, "h6", at(5))
        out = sw.settle(sched_with(9))
        self.assertEqual(out["settled"], [])
        self.assertIsNone(sw.w.records.read("seeds/6.json"))


class FactsTest(unittest.TestCase):
    def test_forced_and_directive(self):
        base = go_mod_with(pgx="v5.11.0")
        new = go_mod_with(pgx="v5.12.0").replace("puddle/v2 v2.2.2", "puddle/v2 v2.2.3")
        f = op.facts_from(base, new, {"position": 24, "module": "github.com/jackc/pgx/v5", "version": "v5.12.0"})
        self.assertEqual((f["old"], f["new"], f["go_directive"]), ("v5.11.0", "v5.12.0", "1.26.0"))
        self.assertEqual(f["forced"], [{"module": "github.com/jackc/puddle/v2", "old": "v2.2.2", "new": "v2.2.3"}])
        self.assertEqual(op.branch_name({"module": "github.com/jackc/pgx/v5", "version": "v5.12.0"}),
                         "agent/go-module-bump/github.com-jackc-pgx-v5-v5.12.0")


def _openssl_cms() -> bool:
    import shutil
    import subprocess
    exe = shutil.which("openssl")
    if not exe:
        return False
    return subprocess.run([exe, "cms", "-help"], capture_output=True).returncode in (0, 1)


@unittest.skipUnless(_openssl_cms(), "openssl with cms is not installed")
class SealedSummaryTest(unittest.TestCase):
    """ADV-09: the model's job uploads the summary padded and encrypted; only
    the opening job's key opens it. Uses the host's openssl, as the workflow
    uses the runner's."""

    def setUp(self):
        import subprocess
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.key, self.cert = d / "key.pem", d / "cert.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(self.key),
                        "-out", str(self.cert), "-days", "1", "-subj", "/CN=test summary recipient"],
                       check=True, capture_output=True)

    def tearDown(self):
        self.tmp.cleanup()

    def seal(self, text: bytes, d: Path) -> None:
        import subprocess
        n = len(text)
        block = n.to_bytes(4, "big") + text + bytes(op.PADDED_SUMMARY_SIZE - 4 - n)
        (d / "padded").write_bytes(block)
        subprocess.run(["openssl", "cms", "-encrypt", "-binary", "-aes256", "-outform", "DER", "-in",
                        str(d / "padded"), "-out", str(d / op.SEALED_SUMMARY), str(self.cert)],
                       check=True, capture_output=True)
        (d / "padded").unlink()

    def test_sealed_artifact_opens_to_the_three_files(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "go.mod").write_text("module x\n")
            (d / "go.sum").write_text("")
            self.seal(SUMMARY.encode(), d)
            files = op.intake(d, self.key.read_text(), sealed=True)
            self.assertEqual(sorted(files), ["go.mod", "go.sum", "summary.md"])
            self.assertEqual(files["summary.md"].decode(), SUMMARY)

    def test_every_summary_seals_to_the_same_size(self):
        sizes = set()
        for text in (b"", b"short", SUMMARY.encode(), b"x" * 8192):
            with tempfile.TemporaryDirectory() as t:
                self.seal(text, Path(t))
                sizes.add((Path(t) / op.SEALED_SUMMARY).stat().st_size)
        self.assertEqual(len(sizes), 1, sizes)

    def test_a_clear_summary_or_a_wrong_key_is_refused(self):
        import subprocess
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            write_files(d)
            with self.assertRaises(op.IntakeRefused):
                op.intake(d, self.key.read_text(), sealed=True)       # summary.md in clear
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "go.mod").write_text("module x\n")
            (d / "go.sum").write_text("")
            self.seal(SUMMARY.encode(), d)
            other = d.parent / f"{d.name}-other.pem"
            subprocess.run(["openssl", "genrsa", "-out", str(other), "2048"], check=True, capture_output=True)
            try:
                with self.assertRaises(op.IntakeRefused):
                    op.intake(d, other.read_text(), sealed=True)
                with self.assertRaises(op.IntakeRefused):
                    op.intake(d, None, sealed=True)
            finally:
                other.unlink()

    def test_unpad_refuses_a_block_not_in_pad_form(self):
        with self.assertRaises(op.IntakeRefused):
            op.unpad_summary(b"\x00" * 10)
        block = (3).to_bytes(4, "big") + b"abc" + b"x" + bytes(op.PADDED_SUMMARY_SIZE - 8)
        with self.assertRaises(op.IntakeRefused):
            op.unpad_summary(block)


class GoSumUtf8Test(unittest.TestCase):
    def test_a_go_sum_that_is_not_utf8_is_refused(self):
        """ADV-14: intake checked go.mod and summary.md only."""
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            write_files(d)
            (d / "go.sum").write_bytes(b"\xff\xfe")
            with self.assertRaises(op.IntakeRefused):
                op.intake(d)


if __name__ == "__main__":
    unittest.main()
