"""The committed class file holds the readiness note's frozen numbers.

Before the class file's first commit the repository holds only its template
(templates/go-module-bump.yaml, five placeholders); these tests validate
whichever of the two is there.
"""

import hashlib
import unittest
from pathlib import Path

from factory import classfile
from tests import fakes

ROOT = Path(__file__).resolve().parent.parent
SOURCE = classfile.committed_path(ROOT)
TEXT = SOURCE.read_text()
# Five placeholders; the wall ruleset's id stands in two places.
PLACEHOLDERS = ["human_logins[0]", "seeds.key_sha256", "skill.tag", "skill.commit",
                "rulesets.wall_ruleset_id", "rulesets.repository_rulesets[1].id"]

# The note's queue, written out independently of the class file.
NOTE_QUEUE = (
    [("golang.org/x/sync", f"v0.{m}.0") for m in range(18, 26)]
    + [("golang.org/x/text", f"v0.{m}.0") for m in range(30, 45)]
    + [("github.com/jackc/pgx/v5", "v5.12.0"), ("github.com/jackc/pgx/v5", "v5.13.0")]
)


class ClassFileTest(unittest.TestCase):
    def setUp(self):
        self.cf = classfile.load(SOURCE)

    def test_queue_is_the_notes_25_items_in_order(self):
        q = classfile.queue(self.cf)
        self.assertEqual([(i["module"], i["version"]) for i in q], NOTE_QUEUE)
        self.assertEqual([i["position"] for i in q], list(range(1, 26)))

    def test_frozen_numbers(self):
        cf = self.cf
        self.assertEqual(cf["repo"], "platform-factory/svc-hello")
        self.assertEqual(cf["paths"], ["go.mod", "go.sum"])
        self.assertEqual((cf["n"], cf["window_weeks"], cf["min_release_age_days"]), (20, 8, 7))
        self.assertEqual((cf["escape_a_minutes"], cf["detector_stale_minutes"], cf["freeze_pass_minutes"]),
                         (30, 30, 60))
        self.assertEqual((cf["review_median_pass_minutes"], cf["review_median_falsifier_minutes"]), (5, 15))
        self.assertEqual(cf["concurrency"], 1)
        self.assertEqual(cf["rungs"], {"l1": 1, "l2": 0, "max_rung": 2, "wall_minimum_approvals": 1})
        self.assertEqual(cf["schedules"]["detector_minutes"], [4, 14, 24, 34, 44, 54])
        self.assertEqual(cf["schedules"]["agent_minutes"], [23, 53])
        self.assertEqual((cf["seeds"]["count"], cf["seeds"]["positions"]), (4, [2, 18]))
        self.assertEqual(len(cf["seeds"]["types"]), 6)
        self.assertEqual(cf["agent"]["model"], "claude-sonnet-5-5")
        self.assertIn("@sha256:", cf["agent"]["builder_image"])
        self.assertEqual(cf["checks"]["integration_id"], 15368)
        self.assertEqual(cf["checks"]["class_scope"]["name"], "class-scope")
        self.assertEqual(cf["checks"]["go_test"]["name"], "go-test")
        self.assertEqual(cf["labels"]["human"], ["reviewing", "human-cleared", "escape:revert"])
        self.assertEqual(cf["labels"]["machinery"], ["needs-human", "fixture", "seeded"])
        self.assertEqual(cf["variables"], {"force_inconclusive": "M4_FORCE_INCONCLUSIVE",
                                           "inject_post_merge": "M4_INJECT_POST_MERGE"})

    def test_the_class_file_or_its_template_never_both(self):
        """B3: the class file's first commit is the filled one, and the same
        pull request deletes the template. So the repository holds exactly one
        of the two: the template with its five placeholders, or the class file
        with none."""
        real, template = ROOT / classfile.CLASS_PATH, ROOT / classfile.TEMPLATE_PATH
        self.assertNotEqual(real.is_file(), template.is_file())
        if template.is_file():
            self.assertEqual(classfile.unfilled(self.cf), PLACEHOLDERS)
            self.assertEqual(self.cf["human_logins"], ["LISTED-HUMAN-LOGIN"])
        else:
            self.assertEqual(classfile.unfilled(self.cf), [])

    def test_placeholders_are_the_named_ones(self):
        self.assertEqual(classfile.unfilled(classfile.load(fakes.placeholder_class_file_text())), PLACEHOLDERS)
        filled = fakes.class_file_text(key_sha256="a" * 64, skill_tag="go-module-bump/v0.1.0-rc.1",
                                       skill_commit="b" * 40, wall_id=21000001)
        self.assertEqual(classfile.unfilled(classfile.load(filled)), [])

    def test_two_rulesets_with_the_wall_id_filled_alike(self):
        """Probe P1: the wall entry is in a ruleset of its own, so the class
        file names both and the listing GitHub must return holds both. The
        wall's id is filled in two places, and they must agree."""
        cf = classfile.load(fakes.class_file_text(wall_id=21000001))
        r = cf["rulesets"]
        self.assertEqual((r["ruleset_name"], r["wall_ruleset_name"], r["wall_path"]),
                         ("main-protection", "class-wall", "rulesets/svc-hello/wall.json"))
        self.assertEqual(sorted((x["id"], x["name"]) for x in r["repository_rulesets"]),
                         [(20204893, "main-protection"), (21000001, "class-wall")])
        half = fakes.class_file_text(wall_id=21000001).replace("wall_ruleset_id: 21000001", "wall_ruleset_id: 21000002")
        with self.assertRaises(classfile.ClassFileError) as cm:
            classfile.load(half)
        self.assertIn("rulesets.repository_rulesets", str(cm.exception))
        with self.assertRaises(classfile.ClassFileError):
            classfile.load(fakes.class_file_text(wall_id='"21000001"'))   # an id is a number
        third = fakes.class_file_text(wall_id=21000001).replace(
            "    - {id: 21000001, name: class-wall",
            "    - {id: 1, name: org-wide, source_type: Organization, source: platform-factory, target: branch}\n"
            "    - {id: 21000001, name: class-wall")
        with self.assertRaises(classfile.ClassFileError):
            classfile.load(third)

    def test_a_run_that_reads_the_rulesets_waits_for_the_wall_id(self):
        import tempfile
        from pathlib import Path
        text = fakes.class_file_text(key_sha256="a" * 64, skill_tag="t", skill_commit="b" * 40,
                                     wall_id="WALL-RULESET-ID")              # only the wall id unfilled
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cf.yaml"
            p.write_text(text)
            with self.assertRaises(classfile.NotReady) as cm:
                classfile.require({"CLASS_FILE": str(p)}, {"human_logins[0]", "rulesets"})
            self.assertIn("rulesets.wall_ruleset_id", cm.exception.reason)
            self.assertEqual(classfile.require({"CLASS_FILE": str(p)}, {"human_logins[0]"})["class"], "go-module-bump")

    def test_ruleset_hashes_match_committed_files(self):
        for which in ("l1", "l2"):
            raw = (ROOT / self.cf["rulesets"][f"{which}_path"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), self.cf["rulesets"][f"{which}_sha256"])

    def test_the_hash_script_checks_whichever_is_there(self):
        import contextlib
        import io
        import runpy
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mod = runpy.run_path(str(ROOT / "scripts/ruleset_hashes.py"), run_name="ruleset_hashes")
            self.assertEqual(mod["main"](["--check"]), 0)
        self.assertIn(f"class file: {SOURCE.relative_to(ROOT)}", buf.getvalue())

    def test_unknown_key_raises(self):
        with self.assertRaises(classfile.ClassFileError) as cm:
            classfile.load(TEXT + "\nextra_rule: 3\n")
        self.assertIn("extra_rule: unknown key", str(cm.exception))

    def test_missing_key_raises(self):
        with self.assertRaises(classfile.ClassFileError) as cm:
            classfile.load(TEXT.replace("\nn: 20\n", "\n"))
        self.assertIn("n: missing", str(cm.exception))

    def test_nested_unknown_key_raises(self):
        with self.assertRaises(classfile.ClassFileError) as cm:
            classfile.load(TEXT.replace("  max_rung: 2\n", "  max_rung: 2\n  l3: 0\n"))
        self.assertIn("rungs.l3: unknown key", str(cm.exception))

    def test_queue_out_of_order_raises(self):
        bad = TEXT.replace("{module: golang.org/x/sync, version: v0.19.0}",
                           "{module: golang.org/x/sync, version: v0.17.0}")
        with self.assertRaises(classfile.ClassFileError):
            classfile.load(bad)

    def test_bool_is_not_an_int(self):
        with self.assertRaises(classfile.ClassFileError):
            classfile.load(TEXT.replace("\nconcurrency: 1\n", "\nconcurrency: true\n"))

    def test_load_accepts_text(self):
        self.assertEqual(classfile.load(TEXT)["class"], "go-module-bump")


if __name__ == "__main__":
    unittest.main()
