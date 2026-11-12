"""Records against a hand-written emulation of the ref, tarball and contents APIs."""

import base64
import io
import json
import re
import subprocess
import tarfile
import tempfile
import unittest

from factory.gh import ApiError
from factory.records import RecordExists, Records, blob_sha, read_archive
from tests.fakes import FakeGitHub

REPO = "platform-factory/factory-controls"


class BranchWorld:
    """The records branch: a dict of path -> text, served as GitHub serves it."""

    def __init__(self):
        self.files = {}  # path -> text
        self.gh = FakeGitHub()
        self.commits = 0
        r = re.escape(REPO)
        self.gh.route("GET", rf"repos/{r}/git/ref/heads/records", self.ref)
        self.gh.route("GET", rf"repos/{r}/tarball/(\w+)", self.tarball)
        self.gh.route("GET", rf"repos/{r}/contents/(.+)", self.get)
        self.gh.route("PUT", rf"repos/{r}/contents/(.+)", self.put)

    def sha(self):
        return f"c{self.commits:039d}"

    def ref(self, m, p, b):
        return {"ref": "refs/heads/records", "object": {"type": "commit", "sha": self.sha()}}

    def tarball(self, m, p, b):
        assert m.group(1) == self.sha(), "the archive is of the commit the ref named"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            top = "platform-factory-factory-controls-abc1234"
            d = tarfile.TarInfo(top)
            d.type = tarfile.DIRTYPE
            tf.addfile(d)
            for path, text in self.files.items():
                data = text.encode()
                info = tarfile.TarInfo(f"{top}/{path}")
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            link = tarfile.TarInfo(f"{top}/link.json")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tf.addfile(link)
        return buf.getvalue()

    def get(self, m, p, b):
        assert p.get("ref") == "records"
        path = m.group(1)
        if path not in self.files:
            return ApiError(404, {"message": "Not Found"}, path)
        data = self.files[path].encode()
        return {"path": path, "sha": blob_sha(data), "content": base64.b64encode(data).decode()}

    def put(self, m, p, b):
        assert b["branch"] == "records"
        path = m.group(1)
        if path in self.files:
            if b.get("sha") is None:
                return ApiError(422, {"message": "Invalid request.\n\n\"sha\" wasn't supplied."}, path)
            if b["sha"] != blob_sha(self.files[path].encode()):
                return ApiError(409, {"message": f"{path} does not match {b['sha']}"}, path)
        self.commits += 1
        self.files[path] = base64.b64decode(b["content"]).decode()
        return {"content": {"path": path}}

    def gets(self):
        return [c["path"] for c in self.gh.calls if c["method"] == "GET"]


def records(w, slept=None):
    return Records(w.gh, REPO, sleep=(slept.append if slept is not None else lambda s: None))


class SnapshotTest(unittest.TestCase):
    def test_every_read_comes_from_one_snapshot(self):
        """B4: two requests for the whole branch, however many records it holds."""
        w = BranchWorld()
        for i in range(50):
            w.files[f"runs/{i}-1.json"] = json.dumps({"pr_number": i})
        w.files["ledger/escape-a-pr1.json"] = json.dumps({"key": "escape-a-pr1"})
        rec = records(w)
        self.assertEqual(len(rec.list("runs/")), 50)
        self.assertEqual([rec.read(p)["pr_number"] for p in rec.list("runs/")][:3], [0, 1, 10])
        self.assertEqual(rec.read("ledger/escape-a-pr1.json"), {"key": "escape-a-pr1"})
        self.assertIsNone(rec.read("seeds/9.json"))
        self.assertIsNone(rec.read("link.json"))          # a link in the archive is not a record
        self.assertEqual(w.gets(), [f"repos/{REPO}/git/ref/heads/records", f"repos/{REPO}/tarball/{w.sha()}"])
        self.assertEqual(rec.commit, w.sha())

    def test_a_missing_branch_is_an_empty_snapshot(self):
        w = BranchWorld()
        w.gh.route("GET", rf"repos/{re.escape(REPO)}/git/ref/heads/records",
                   lambda m, p, b: ApiError(404, {"message": "Not Found"}, "ref"))
        rec = records(w)
        self.assertEqual(rec.list(""), [])
        self.assertIsNone(rec.commit)

    def test_a_write_updates_the_snapshot_and_refresh_sees_other_writers(self):
        w = BranchWorld()
        rec = records(w)
        rec.write("runs/1-1.json", {"pr_number": 1}, "m")
        self.assertEqual(rec.list("runs/"), ["runs/1-1.json"])
        w.files["runs/2-1.json"] = json.dumps({"pr_number": 2})        # another job's write
        w.commits += 1
        self.assertEqual(rec.list("runs/"), ["runs/1-1.json"])
        rec.refresh()
        self.assertEqual(rec.list("runs/"), ["runs/1-1.json", "runs/2-1.json"])

    def test_read_archive(self):
        w = BranchWorld()
        w.files = {"a.json": "{}", "dir/b.json": "[1]"}
        self.assertEqual(read_archive(w.tarball(re.match(r"(.*)", w.sha()), {}, None)),
                         {"a.json": b"{}", "dir/b.json": b"[1]"})

    def test_blob_sha_is_gits(self):
        with tempfile.NamedTemporaryFile("wb") as f:
            f.write(b'{\n  "a": 1\n}\n')
            f.flush()
            want = subprocess.run(["git", "hash-object", f.name], capture_output=True, text=True,
                                  check=True).stdout.strip()
        self.assertEqual(blob_sha(b'{\n  "a": 1\n}\n'), want)


class WriteTest(unittest.TestCase):
    def test_write_creates_and_refuses_to_overwrite(self):
        w = BranchWorld()
        rec = records(w)
        rec.write("ledger/escape-a-pr7.json", {"key": "escape-a-pr7"}, "m")
        self.assertEqual(rec.read("ledger/escape-a-pr7.json"), {"key": "escape-a-pr7"})
        puts = len(w.gh.writes("PUT"))
        with self.assertRaises(RecordExists):
            rec.write("ledger/escape-a-pr7.json", {"key": "other"}, "m")
        self.assertEqual(len(w.gh.writes("PUT")), puts)          # known from the snapshot: no request
        self.assertEqual(json.loads(w.files["ledger/escape-a-pr7.json"]), {"key": "escape-a-pr7"})

    def test_a_record_written_after_the_snapshot_is_record_exists(self):
        w = BranchWorld()
        rec = records(w)
        rec.list("")                                            # snapshot taken: empty
        w.files["ledger/k.json"] = json.dumps({"k": 1})         # another run writes the fact
        with self.assertRaises(RecordExists):
            rec.write("ledger/k.json", {"k": 2}, "m")
        self.assertEqual(rec.read("ledger/k.json"), {"k": 1})   # the snapshot now holds the winner's record
        self.assertEqual(json.loads(w.files["ledger/k.json"]), {"k": 1})

    def test_lost_branch_race_is_retried(self):
        """A 409 on the branch update while the file is still absent is a lost
        race with another writer (GitHub: parallel contents writes conflict);
        the write is retried, not raised (finding M4)."""
        w = BranchWorld()
        slept = []
        rec = records(w, slept)
        orig_put = w.put
        calls = {"n": 0}

        def put_conflict_twice(m, p, b):
            calls["n"] += 1
            if calls["n"] <= 2:
                return ApiError(409, {"message": "is at abc but expected def"}, m.group(1))
            return orig_put(m, p, b)

        w.gh.route("PUT", rf"repos/{re.escape(REPO)}/contents/(.+)", put_conflict_twice)
        rec.write("runs/7-1.json", {"pr_number": 7}, "m")
        self.assertEqual(rec.read("runs/7-1.json"), {"pr_number": 7})
        self.assertEqual(calls["n"], 3)
        self.assertEqual(len(slept), 2)

    def test_conflict_that_never_clears_raises(self):
        w = BranchWorld()
        rec = records(w)
        w.gh.route("PUT", rf"repos/{re.escape(REPO)}/contents/(.+)",
                   lambda m, p, b: ApiError(409, {"message": "conflict"}, m.group(1)))
        with self.assertRaises(ApiError):
            rec.write("runs/8-1.json", {}, "m")

    def test_another_error_is_raised_at_once(self):
        w = BranchWorld()
        rec = records(w)
        w.gh.route("PUT", rf"repos/{re.escape(REPO)}/contents/(.+)",
                   lambda m, p, b: ApiError(403, {"message": "API rate limit exceeded"}, m.group(1)))
        with self.assertRaises(ApiError):
            rec.write("runs/8-1.json", {}, "m")
        self.assertEqual(len(w.gh.writes("PUT")), 1)

    def test_branch_from_env(self):
        from factory.records import branch_from_env
        self.assertEqual(branch_from_env({}), "records")
        self.assertEqual(branch_from_env({"RECORDS_BRANCH": "records-p4"}), "records-p4")

    def test_write_replace_sends_the_snapshots_sha(self):
        w = BranchWorld()
        rec = records(w)
        rec.write_replace("detector/heartbeat.json", {"completed_at": "a"}, "m")
        rec.write_replace("detector/heartbeat.json", {"completed_at": "b"}, "m")
        self.assertEqual(json.loads(w.files["detector/heartbeat.json"]), {"completed_at": "b"})
        # One PUT each; no read of the file first.
        self.assertEqual(len(w.gh.writes("PUT")), 2)
        self.assertNotIn(f"repos/{REPO}/contents/detector/heartbeat.json", w.gets())

    def test_replace_after_another_writer_reads_the_new_sha_once(self):
        w = BranchWorld()
        w.files["detector/heartbeat.json"] = json.dumps({"completed_at": "a"})
        rec = records(w)
        rec.list("")
        w.files["detector/heartbeat.json"] = json.dumps({"completed_at": "other"})   # replaced meanwhile
        rec.write_replace("detector/heartbeat.json", {"completed_at": "b"}, "m")
        self.assertEqual(json.loads(w.files["detector/heartbeat.json"]), {"completed_at": "b"})
        self.assertEqual(len(w.gh.writes("PUT")), 2)                     # 409, then the retry
        self.assertEqual(w.gets().count(f"repos/{REPO}/contents/detector/heartbeat.json"), 1)

    def test_replace_text_writes_the_bytes_as_given(self):
        w = BranchWorld()
        rec = records(w)
        rec.replace_text("scorecard/go-module-bump.md", "# Scorecard\n", "m")
        self.assertEqual(w.files["scorecard/go-module-bump.md"], "# Scorecard\n")

    def test_written_json_is_stable(self):
        w = BranchWorld()
        rec = records(w)
        rec.write("a.json", {"b": 1, "a": [2]}, "m")
        self.assertEqual(w.files["a.json"], '{\n  "a": [\n    2\n  ],\n  "b": 1\n}\n')


if __name__ == "__main__":
    unittest.main()
