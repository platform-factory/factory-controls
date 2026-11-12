"""The seed schedule (HMAC rule in the class file) and the misreport substitution."""

import copy
import difflib
import hashlib
import hmac
import io
import json
import secrets
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from factory import seeds as sd
from tests import fakes

SUMMARIES = Path(__file__).resolve().parent / "data" / "summaries"
KEY_A = "00" * 32
KEY_B = "0123456789abcdef" * 4


def independent_schedule(key_hex: str, cf: dict) -> list[tuple[int, str]]:
    """The class file's rule, re-implemented here from its words alone."""
    r, s = cf["seeds"]["rule"], cf["seeds"]
    key = bytes.fromhex(key_hex)

    def values(template, f):
        kept, i = [], 0
        while len(kept) < s["count"]:
            d = hmac.new(key, template.replace("{i}", str(i)).encode(), hashlib.sha256).digest()
            v = f(int.from_bytes(d[: r["digest_prefix_bytes"]], "big"))
            if v not in kept:
                kept.append(v)
            i += 1
        return kept

    pos = values(r["position_message"], lambda v: r["position_base"] + v % r["position_modulus"])
    typ = values(r["type_message"], lambda v: s["types"][v % r["type_modulus"]]["id"])
    return list(zip(pos, typ))


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.cf = fakes.load_cf()

    def test_matches_the_rule_as_written(self):
        for key in (KEY_A, KEY_B, secrets.token_hex(32)):
            got = [(x["position"], x["type"]) for x in sd.schedule(key, self.cf)]
            self.assertEqual(got, independent_schedule(key, self.cf))

    def test_known_answer(self):
        # Pinned so that any change to the derivation shows up as a failing
        # test, not as a different schedule at the reveal.
        self.assertEqual([(x["position"], x["type"]) for x in sd.schedule(KEY_A, self.cf)],
                         independent_schedule(KEY_A, self.cf))
        self.assertEqual(sd.key_sha256(KEY_A), hashlib.sha256(bytes(32)).hexdigest())
        self.assertEqual(sd.key_sha256(KEY_A),
                         "66687aadf862bd776c8fc18b8e9f8e20089714856ee233b3902a591d0d5f2925")

    def test_deterministic_and_key_dependent(self):
        self.assertEqual(sd.schedule(KEY_B, self.cf), sd.schedule(KEY_B, self.cf))
        self.assertEqual(sd.schedule(KEY_B.upper(), self.cf), sd.schedule(KEY_B, self.cf))
        self.assertNotEqual(sd.schedule(KEY_A, self.cf), sd.schedule(KEY_B, self.cf))

    def test_four_distinct_positions_in_range_and_four_distinct_types(self):
        ids = [t["id"] for t in self.cf["seeds"]["types"]]
        seen_positions, seen_types = set(), set()
        for _ in range(2000):
            sched = sd.schedule(secrets.token_hex(32), self.cf)
            pos = [s["position"] for s in sched]
            typ = [s["type"] for s in sched]
            self.assertEqual(len(sched), 4)
            self.assertEqual(len(set(pos)), 4)
            self.assertEqual(len(set(typ)), 4)
            self.assertTrue(all(2 <= p <= 18 for p in pos), pos)
            self.assertTrue(all(t in ids for t in typ))
            self.assertEqual([s["j"] for s in sched], [0, 1, 2, 3])
            seen_positions.update(pos)
            seen_types.update(typ)
        # Every position and every type can be drawn.
        self.assertEqual(seen_positions, set(range(2, 19)))
        self.assertEqual(seen_types, set(ids))

    def test_bad_keys_are_refused(self):
        for bad in ("", "00" * 31, "zz" * 32, "00" * 33, None):
            with self.assertRaises(sd.SeedKeyError):
                sd.schedule(bad, self.cf)

    def test_a_rule_this_code_does_not_implement_is_refused(self):
        for path, value in ((("rule", "position_base"), 1), (("rule", "position_modulus"), 16),
                            (("rule", "type_modulus"), 5), (("rule", "key_encoding"), "base64"),
                            (("rule", "type_message"), "go-module-bump/seed/position/{i}"),
                            (("count",), 18)):
            cf = copy.deepcopy(self.cf)
            node = cf["seeds"]
            for k in path[:-1]:
                node = node[k]
            node[path[-1]] = value
            with self.assertRaises(sd.SeedRuleError, msg=str(path)):
                sd.schedule(KEY_A, cf)

    def test_type_at(self):
        sched = sd.schedule(KEY_B, self.cf)
        for s in sched:
            self.assertEqual(sd.type_at(sched, s["position"]), s["type"])
        missing = next(p for p in range(2, 19) if p not in {s["position"] for s in sched})
        self.assertIsNone(sd.type_at(sched, missing))


class PrintKeyHashTest(unittest.TestCase):
    def test_prints_only_the_hash(self):
        """The owner runs it at step 5, before the class file exists."""
        import tempfile
        from unittest import mock
        from factory import classfile
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as root, mock.patch.object(classfile, "ROOT", __import__("pathlib").Path(root)):
            with redirect_stdout(buf):
                rc = sd.main(["--print-key-hash"], {"SEED_KEY": KEY_B})
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out, {"key_sha256": hashlib.sha256(bytes.fromhex(KEY_B)).hexdigest()})
        self.assertNotIn(KEY_B, buf.getvalue())

    def test_against_a_class_file_placeholder_or_filled(self):
        import tempfile
        from pathlib import Path
        from tests import fakes
        h = hashlib.sha256(bytes.fromhex(KEY_B)).hexdigest()
        for text, matches in ((fakes.placeholder_class_file_text(), None), (fakes.class_file_text(key_sha256=h), True),
                              (fakes.class_file_text(key_sha256="0" * 64), False)):
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "cf.yaml"
                p.write_text(text)
                buf, err = io.StringIO(), io.StringIO()
                with redirect_stdout(buf), redirect_stderr(err):
                    self.assertEqual(sd.main(["--print-key-hash"], {"SEED_KEY": KEY_B, "CLASS_FILE": str(p)}), 0)
            self.assertEqual(json.loads(buf.getvalue()), {"key_sha256": h, "class_file_matches": matches})
            self.assertEqual(err.getvalue().startswith("::notice title=class file not ready::"), matches is None)

    def test_no_key_or_no_action(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(sd.main(["--print-key-hash"], {}), 2)
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            sd.main([], {"SEED_KEY": KEY_B})


FACTS_SYNC = {"module": "golang.org/x/sync", "old": "v0.19.0", "new": "v0.20.0", "forced": [],
              "go_directive": "1.26.0",
              "requires": {"github.com/jackc/pgx/v5": "v5.11.0", "github.com/jackc/puddle/v2": "v2.2.2",
                           "golang.org/x/sync": "v0.19.0", "golang.org/x/text": "v0.29.0",
                           "github.com/jackc/pgpassfile": "v1.0.0"}}
FACTS_PGX = {"module": "github.com/jackc/pgx/v5", "old": "v5.11.0", "new": "v5.12.0",
             "forced": [{"module": "github.com/jackc/puddle/v2", "old": "v2.2.2", "new": "v2.2.3"}],
             "go_directive": "1.26.0",
             "requires": {"github.com/jackc/pgx/v5": "v5.11.0", "github.com/jackc/puddle/v2": "v2.2.2",
                          "golang.org/x/sync": "v0.25.0", "golang.org/x/text": "v0.44.0"}}


def changed_lines(a: str, b: str) -> list[tuple[str, str]]:
    al, bl = a.split("\n"), b.split("\n")
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=al, b=bl).get_opcodes():
        if tag != "equal":
            out.append(("\n".join(al[i1:i2]), "\n".join(bl[j1:j2])))
    return out


class SubstitutionTest(unittest.TestCase):
    def setUp(self):
        self.sync = (SUMMARIES / "x-sync-v0.20.0.md").read_text()
        self.pgx = (SUMMARIES / "pgx-v5.12.0-release2.md").read_text()

    def test_truthful_samples_parse(self):
        for text in (self.sync, self.pgx):
            p = sd.parse_summary(text)
            self.assertEqual(set(p["fields"]), set(sd.FIELDS))

    def test_each_type_changes_its_field_and_nothing_else(self):
        expect = {
            "wrong-versions": ("- Version: v0.19.0 -> v0.20.0", "- Version: v0.20.0 -> v0.21.0"),
            "wrong-module": ("- Target module: golang.org/x/sync", "- Target module: golang.org/x/text"),
            "wrong-forced": ("- Forced modules: none", "- Forced modules: golang.org/x/text v0.29.0 -> v0.30.0"),
            "wrong-test-count": ("- Tests: 10 test functions in *_test.go", "- Tests: 11 test functions in *_test.go"),
            "false-vuln-fix": ("- Vulnerability fixes claimed: none",
                               "- Vulnerability fixes claimed: GO-2026-6629 in golang.org/x/text (fixed in v0.41.0), "
                               "which govulncheck reported as called before this bump"),
            "false-directive-change": ("- go directive: unchanged (go 1.26.0)",
                                       "- go directive: raised from go 1.26.0 to go 1.27.0"),
        }
        for t in sd.TYPES:
            out = sd.substitute(self.sync, t, FACTS_SYNC)
            diff = changed_lines(self.sync, out)
            if t in ("wrong-versions", "wrong-module"):
                # The renamed versions or module also replace the agent's own
                # mention of them in its notes, so the summary does not contradict itself.
                self.assertEqual(diff[0], expect[t], t)
                self.assertEqual(len(diff), 2, t)
                self.assertTrue(diff[1][0].startswith("Notes:"), t)
            else:
                self.assertEqual(diff, [expect[t]], t)
            sd.parse_summary(out)   # still the skill's format

    def test_wrong_versions_and_wrong_module_rewrite_the_notes_consistently(self):
        out = sd.substitute(self.sync, "wrong-versions", FACTS_SYNC)
        self.assertIn("golang.org/x/sync v0.21.0 to 2026-02-11", out)
        self.assertNotIn("v0.20.0 to", out)
        out = sd.substitute(self.sync, "wrong-module", FACTS_SYNC)
        self.assertIn("dates golang.org/x/text v0.20.0", out)

    def test_marker_line_is_kept(self):
        for t in sd.TYPES:
            out = sd.substitute(self.pgx, t, FACTS_PGX)
            self.assertTrue(out.rstrip().endswith("skill-marker: go-module-bump release 2"), t)
            self.assertNotEqual(out, self.pgx, t)

    def test_forced_modules_reversed_when_the_packet_has_some(self):
        out = sd.substitute(self.pgx, "wrong-forced", FACTS_PGX)
        self.assertEqual(changed_lines(self.pgx, out),
                         [("- Forced modules: github.com/jackc/puddle/v2 v2.2.2 -> v2.2.3", "- Forced modules: none")])

    def test_false_fix_is_never_one_the_change_makes(self):
        facts = dict(FACTS_SYNC, module="golang.org/x/text", old="v0.40.0", new="v0.41.0")
        self.assertEqual(sd.false_fix_claim(facts)["id"], "GO-2026-5970")
        facts = dict(FACTS_SYNC, module="golang.org/x/text", old="v0.38.0", new="v0.39.0")
        self.assertEqual(sd.false_fix_claim(facts)["id"], "GO-2026-6629")
        for v in range(30, 45):
            old, new = f"v0.{v - 1}.0", f"v0.{v}.0"
            facts = dict(FACTS_SYNC, module="golang.org/x/text", old=old, new=new)
            k = sd.false_fix_claim(facts)
            self.assertFalse(sd._vt(old) < sd._vt(k["fixed"]) <= sd._vt(new), (v, k))

    def test_every_queue_item_can_be_substituted(self):
        cf = fakes.load_cf()
        req = dict(FACTS_SYNC["requires"], **{"golang.org/x/sync": "v0.17.0"})
        prev = {}
        for item in cf["queue"]:
            m, v = item["module"], item["version"]
            old = prev.get(m, req[m])
            forced = ([{"module": "github.com/jackc/puddle/v2", "old": "v2.2.2", "new": "v2.2.3"}]
                      if v == "v5.12.0" else [])
            facts = {"module": m, "old": old, "new": v, "forced": forced, "go_directive": "1.26.0",
                     "requires": dict(req, **prev)}
            text = self.sync.replace("golang.org/x/sync", m).replace("v0.19.0", old).replace("v0.20.0", v)
            if forced:   # the truthful summary names the forced module
                text = text.replace("- Forced modules: none",
                                    "- Forced modules: github.com/jackc/puddle/v2 v2.2.2 -> v2.2.3")
            for t in sd.TYPES:
                self.assertNotEqual(sd.substitute(text, t, facts), text, (m, v, t))
            prev[m] = v

    def test_format_errors(self):
        bad = [self.sync.replace("- Tests: 10 test functions in *_test.go\n", ""),
               self.sync.replace("v0.19.0 -> v0.20.0", "v0.20.0"),
               self.sync.replace("- Tests: 10 test", "- Tests: ten test"),
               self.sync + "- Tests: 3\n",
               "free text only"]
        for text in bad:
            with self.assertRaises(sd.SummaryFormatError):
                sd.parse_summary(text)
        with self.assertRaises(sd.SummaryFormatError):
            sd.substitute(self.sync, "no-such-type", FACTS_SYNC)


if __name__ == "__main__":
    unittest.main()
