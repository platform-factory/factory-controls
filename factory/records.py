"""Read and write records on the controls repo's `records` branch.

Why a branch of one-fact files: the records are evidence the machinery
writes while it runs (run records, the ledger, ratchet before/after JSON,
the detector's heartbeat). One file per fact means two writers never edit
the same file, and `write` refuses to overwrite, so a fact once written
stays as written; the branch's ruleset refuses force-pushes and deletion.
Only the heartbeat and the scorecard are replaced in place.

Why one snapshot per process: the workflow's own GITHUB_TOKEN may make
1,000 REST requests an hour for the whole repository (GitHub's Actions
documentation), and every controls workflow shares that budget. Reading
each record with its own request made the cost of a run grow with the
number of records (about 50 requests per read of the state with 20 run
records, 25 fixture records and 5 ledger entries). So the first read takes
the whole branch at once: one request for the branch's commit and one for
that commit's tarball (the redirect to the archive host is not an API
request). Every later list and read in the process is served from it, and a
write updates it. The commit is read first so the archive is of a fixed
commit, never a cached copy of the branch name. A process that must see
another process's writes calls `refresh`.

Every write goes through the contents API with the workflow's own
GITHUB_TOKEN (contents: write on this repository only). A write needs no
read first: the snapshot says whether a record exists, and its blob SHA is
the `sha` a replacement sends. If another process wrote in the meantime,
GitHub refuses the write (422 or 409) and the path is read once to settle it.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import tarfile
import time
import urllib.parse
from typing import Any

from .gh import ApiError


class RecordExists(Exception):
    """`write` found the path already present. The caller treats the fact as written."""


DEFAULT_BRANCH = "records"
# Attempts and the base delay for a write that loses a race on the branch.
# GitHub documents that parallel contents-API writes to one branch conflict
# (409); the detector, the merge and open jobs, the scorecard and the
# fixtures all write here, so a lost race is expected and retried.
WRITE_ATTEMPTS = 5
WRITE_BACKOFF_SECONDS = 2.0


def branch_from_env(env: dict | None = None) -> str:
    """The records branch: RECORDS_BRANCH, default `records`. Probe P4 runs
    the agent's workflow against the throwaway repository and sets it to
    `records-p4`, so a probe's run record can never land beside the real
    ones (a run record for another repository is ignored by every reader
    anyway; this keeps the real branch clean too)."""
    env = os.environ if env is None else env
    return env.get("RECORDS_BRANCH") or DEFAULT_BRANCH


def dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def blob_sha(data: bytes) -> str:
    """Git's id for a file's bytes: the `sha` the contents API asks for when
    a file is replaced."""
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def read_archive(raw: bytes) -> dict[str, bytes]:
    """{path: bytes} for every regular file in a repository tarball.

    GitHub's tarball puts every path under one top directory named after the
    repository and commit; that directory is dropped. Nothing is extracted
    to disk, and links and directories are skipped."""
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tf:
        for m in tf:
            if not m.isfile():
                continue
            parts = m.name.split("/", 1)
            if len(parts) != 2 or not parts[1]:
                continue
            f = tf.extractfile(m)
            if f is not None:
                out[parts[1]] = f.read()
    return out


class Records:
    def __init__(self, gh, repo: str = "platform-factory/factory-controls", branch: str = DEFAULT_BRANCH,
                 sleep=time.sleep):
        self.gh = gh
        self.repo = repo
        self.branch = branch
        self._sleep = sleep
        self._files: dict[str, bytes] | None = None
        self.commit: str | None = None     # the branch's commit the snapshot was taken at

    # -- the snapshot --------------------------------------------------------

    def refresh(self) -> None:
        """Take the snapshot again: one read of the branch's commit, one
        tarball of that commit. A missing branch is an empty snapshot."""
        try:
            ref = self.gh.get(f"repos/{self.repo}/git/ref/heads/{urllib.parse.quote(self.branch)}")
        except ApiError as e:
            if e.status == 404:
                self._files, self.commit = {}, None
                return
            raise
        sha = ((ref or {}).get("object") or {}).get("sha") if isinstance(ref, dict) else None
        if not sha:
            raise ApiError(200, {"message": "the branch's ref names no commit"}, f"git/ref/heads/{self.branch}")
        self._files = read_archive(self.gh.get_raw(f"repos/{self.repo}/tarball/{sha}"))
        self.commit = sha

    def _snapshot(self) -> dict[str, bytes]:
        if self._files is None:
            self.refresh()
        assert self._files is not None
        return self._files

    def _contents(self, path: str) -> str:
        return f"repos/{self.repo}/contents/{urllib.parse.quote(path.lstrip('/'))}"

    def _get(self, path: str) -> dict | None:
        """One file's metadata and content from the contents API, read only
        to settle a write that GitHub refused."""
        try:
            meta = self.gh.get(self._contents(path), ref=self.branch)
        except ApiError as e:
            if e.status == 404:
                return None
            raise
        return meta if isinstance(meta, dict) else None

    # -- reads, from the snapshot ---------------------------------------------

    def read(self, path: str) -> dict | None:
        data = self._snapshot().get(path.lstrip("/"))
        return None if data is None else json.loads(data.decode())

    def list(self, prefix: str) -> list[str]:
        """Paths of every file under `prefix` on the records branch, sorted."""
        prefix = prefix.lstrip("/")
        return sorted(p for p in self._snapshot() if p.startswith(prefix))

    # -- writes ----------------------------------------------------------------

    def write(self, path: str, obj: Any, message: str) -> None:
        """Create `path`. Raises RecordExists if it is already there.

        A create that races another writer gets 422 when the file now exists
        (no sha was sent) or 409 when the branch moved under it. Either way
        the path is read again: present means another run wrote this fact
        (RecordExists); absent means the write lost a race on the branch and
        is retried with backoff, so a run record or a seed record is not lost
        to a heartbeat written at the same moment."""
        path = path.lstrip("/")
        data = dumps(obj).encode()
        snap = self._snapshot()
        body = {"message": message, "branch": self.branch, "content": base64.b64encode(data).decode()}
        for attempt in range(1, WRITE_ATTEMPTS + 1):
            if path in snap:
                raise RecordExists(path)
            try:
                self.gh.put(self._contents(path), body)
                snap[path] = data
                return
            except ApiError as e:
                if e.status not in (409, 422):
                    raise
                remote = self._get(path)
                if remote is not None:
                    snap[path] = base64.b64decode(remote.get("content", ""))
                    raise RecordExists(path) from e
                if attempt == WRITE_ATTEMPTS:
                    raise
                self._sleep(WRITE_BACKOFF_SECONDS * attempt)

    def write_replace(self, path: str, obj: Any, message: str) -> None:
        """Create or replace `path`. For the heartbeat and the scorecard only."""
        self.replace_text(path, dumps(obj), message)

    def replace_text(self, path: str, text: str, message: str) -> None:
        """Create or replace `path` with exactly `text` (the scorecard's
        Markdown is not JSON). The `sha` sent is the snapshot's; a refusal
        means another writer replaced the file since, so its `sha` is read
        and the write retried."""
        path = path.lstrip("/")
        data = text.encode()
        snap = self._snapshot()
        sha = blob_sha(snap[path]) if path in snap else None
        for attempt in range(WRITE_ATTEMPTS):
            body = {"message": message, "branch": self.branch, "content": base64.b64encode(data).decode()}
            if sha is not None:
                body["sha"] = sha
            try:
                self.gh.put(self._contents(path), body)
                snap[path] = data
                return
            except ApiError as e:
                if e.status in (409, 422) and attempt < WRITE_ATTEMPTS - 1:
                    remote = self._get(path)
                    sha = remote.get("sha") if remote else None
                    self._sleep(WRITE_BACKOFF_SECONDS * (attempt + 1))
                    continue
                raise
