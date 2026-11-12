"""svc-hello and the controls repo as the scorecard reads them.

Extends the core's `fakes.World` with the reads only the scorecard makes:
reviews, pull request commits, check runs with their times, class-scope jobs
and verdict artifacts, the controls run's jobs, the ruleset records in
platform-factory/.github, commit lists, and the git refs and contents writes
of the promotion record. Every response is hand-written in the REST shape,
with only the fields the code reads.
"""

from __future__ import annotations

import base64
import io
import json
import re
import zipfile
from datetime import datetime, timedelta, timezone

from factory import state as st
from factory.gh import ApiError
from tests import fakes
from tests.fakes import AGENT_BOT, CONTROLS, HUMAN, SVC, World, ev

SYNC, TEXT, PGX = "golang.org/x/sync", "golang.org/x/text", "github.com/jackc/pgx/v5"
T0 = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)
CS_STEPS = ("decide", "upload verdict", "verdict-inconclusive", "verdict-fail")


def at(minutes: float) -> str:
    return st.iso(T0 + timedelta(minutes=minutes))


def form(minutes=3, sections="queue, target, go.sum, verdict", decision="approve", reason="summary agrees"):
    return f"minutes: {minutes}; sections: {sections}; decision: {decision}; reason: {reason}"


class ScoreWorld(World):
    def __init__(self, cf=None):
        super().__init__(cf)
        self.reviews: dict[int, list] = {}
        self.pr_commits: dict[int, list] = {}
        self.artifacts: dict[int, dict] = {}
        self.annotations: dict[int, dict] = {}
        self.controls_jobs: dict[tuple[int, int], list] = {}
        self.ruleset_records: list[str] | None = None
        self.svc_commits: list[dict] = []
        self.controls_commits: list[dict] = []
        self.refs: dict[str, str] = {"main": "cmain"}
        self.branch_files: dict[tuple[str, str], str] = {}
        self._job = 5000
        self._wire_scorecard()

    # -- builders -----------------------------------------------------------

    def item(self, n, position, module, version, opened, *, merged=None, closed=None, closed_by=HUMAN,
             usage=(1000, 200, 90), model="claude-sonnet-5-5", skill_tag="r1"):
        """A queue-item pull request with a run record carrying usage and pins."""
        if merged is not None:
            self.queue_pr(n, position, module, version, at(opened), state="closed", merged_at=at(merged),
                          merge_sha=f"m{n}")
        elif closed is not None:
            self.queue_pr(n, position, module, version, at(opened), state="closed", closed_at=at(closed))
            self.events[n] = [ev("closed", at(closed), closed_by)]
        else:
            self.queue_pr(n, position, module, version, at(opened))
        rec = self.records.files[f"runs/{1000 + n}-1.json"]
        rec.update({"model": model, "harness": {"name": "claude-code", "version": "2.1.322"},
                    "skill": {"tag": skill_tag, "commit": "a" * 40, "content_sha256": "b" * 64},
                    "usage": {"input_tokens": usage[0], "output_tokens": usage[1], "agent_seconds": usage[2]}})
        self.controls_jobs[(1000 + n, 1)] = [
            {"id": 1, "name": "agent", "status": "completed", "conclusion": "success",
             "started_at": at(opened - 4), "completed_at": at(opened - 2), "steps": []},
            {"id": 2, "name": "open", "status": "completed", "conclusion": "success",
             "started_at": at(opened - 2), "completed_at": at(opened - 1), "steps": []}]
        return self.prs[n]

    def cs(self, head, verdict, start, end, codes=(), verifier="1.0.0+sha256.x"):
        """One class-scope run on `head`: its check run, job steps and artifact."""
        self._job += 10
        job_id = self._job
        steps = {"pass": ["success"] * 4,
                 "inconclusive": ["success", "success", "failure", "skipped"],
                 "fail": ["success", "success", "success", "failure"],
                 "noverdict": ["failure", "skipped", "skipped", "skipped"]}[verdict]
        concl = "success" if verdict == "pass" else "failure"
        self.check_runs.setdefault(head, []).append(
            {"id": job_id, "name": "class-scope", "app": {"id": 15368}, "head_sha": head,
             "status": "completed", "conclusion": concl, "started_at": at(start), "completed_at": at(end),
             "details_url": f"https://github.com/{SVC}/actions/runs/{job_id // 10}/job/{job_id}"})
        self.jobs_by_id[job_id] = {"id": job_id, "run_id": job_id // 10, "name": "class-scope",
                                   "status": "completed", "conclusion": concl,
                                   "steps": [{"name": n, "conclusion": c} for n, c in zip(CS_STEPS, steps)]}
        if verdict != "noverdict":
            # What class-scope's decide step writes as a check-run annotation
            # (public), and the same fields in the attempt's artifact.
            self.annotations[job_id] = {
                "schema": 1, "verdict": verdict, "reason_class": ("clearable" if verdict == "inconclusive" else None),
                "reason_codes": list(codes), "pass_kind": "class" if verdict == "pass" else None,
                "verifier_version": verifier, "head_sha": head}
            self.artifacts[job_id // 10] = {
                "schema": 1, "verdict": verdict, "reason_class": ("clearable" if verdict == "inconclusive" else None),
                "reason_codes": list(codes), "pass_kind": "class" if verdict == "pass" else None,
                "verifier_version": verifier, "head_sha": head}
        return job_id

    def gt(self, head, conclusion, start, end):
        self._job += 10
        self.check_runs.setdefault(head, []).append(
            {"id": self._job, "name": "go-test", "app": {"id": 15368}, "head_sha": head, "status": "completed",
             "conclusion": conclusion, "started_at": at(start), "completed_at": at(end),
             "details_url": f"https://github.com/{SVC}/actions/runs/{self._job // 10}/job/{self._job}"})

    def green(self, n, start):
        """Both required checks pass on the pull request's head."""
        head = self.prs[n]["head"]["sha"]
        self.cs(head, "pass", start, start + 1)
        self.gt(head, "success", start, start + 3)

    def review(self, n, minute, state="APPROVED", body=None, user=HUMAN, commit=None, rid=None):
        self.reviews.setdefault(n, []).append(
            {"id": rid or (n * 100 + len(self.reviews.get(n, []))), "user": {"login": user}, "state": state,
             "submitted_at": at(minute), "commit_id": commit or self.prs[n]["head"]["sha"],
             "body": body if body is not None else form()})

    def label(self, n, name, minute, actor=HUMAN, app=None, kind="labeled"):
        self.events.setdefault(n, []).append(ev(kind, at(minute), actor, label=name, app=app,
                                                eid=n * 1000 + len(self.events.get(n, []))))

    def reviewed_merge(self, n, position, module, version, opened, *, review_after=3, label_after=1):
        """A clean L1 item: green checks, reviewing label, approval in the form, merged."""
        self.item(n, position, module, version, opened, merged=opened + review_after + 2)
        self.green(n, opened + 0.5)
        self.label(n, "reviewing", opened + label_after)
        self.review(n, opened + review_after)

    def push_run(self, run_id, head_sha, attempts, failed_step=None):
        super().push_run(run_id, head_sha, attempts)
        if failed_step:
            j = self.jobs[(run_id, 1)][0]
            j["steps"] = [{"name": "Set up job", "conclusion": "success"},
                          {"name": "go test", "conclusion": "success"},
                          {"name": failed_step, "conclusion": "failure"}]

    # -- routes -------------------------------------------------------------

    def _wire_scorecard(self):
        g, s, c = self.gh, re.escape(SVC), re.escape(CONTROLS)
        g.route("GET", rf"repos/{s}/pulls/(\d+)/reviews", lambda m, p, b: self.reviews.get(int(m.group(1)), []))
        g.route("GET", rf"repos/{s}/pulls/(\d+)/commits", lambda m, p, b: [
            {"sha": x} for x in self.pr_commits.get(int(m.group(1)), [self.prs[int(m.group(1))]["head"]["sha"]])])
        g.route("GET", rf"repos/{s}/actions/runs/(\d+)/artifacts", lambda m, p, b: {
            "artifacts": [{"id": int(m.group(1)) * 7, "name": "class-scope-verdict"}]
            if int(m.group(1)) in self.artifacts else []})

        g.route("GET", rf"repos/{s}/check-runs/(\d+)/annotations", lambda m, p, b: [
            {"title": "class-scope-verdict", "annotation_level": "notice",
             "message": json.dumps(self.annotations[int(m.group(1))], sort_keys=True)}]
            if int(m.group(1)) in self.annotations else [])

        def art_zip(m, p, b):
            rid = int(m.group(1)) // 7
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("verdict.json", json.dumps(self.artifacts[rid]))
            return buf.getvalue()

        g.route("GET", rf"repos/{s}/actions/artifacts/(\d+)/zip", art_zip)
        g.route("GET", rf"repos/{c}/actions/runs/(\d+)/attempts/(\d+)/jobs", lambda m, p, b: {
            "jobs": self.controls_jobs.get((int(m.group(1)), int(m.group(2))), [])})

        def dotgithub(m, p, b):
            if self.ruleset_records is None:
                return ApiError(404, {"message": "Not Found"}, m.group(0))
            return [{"name": n, "type": "file", "path": f"rulesets/svc-hello/{n}"} for n in self.ruleset_records]

        g.route("GET", r"repos/platform-factory/\.github/contents/rulesets/svc-hello", dotgithub)
        g.route("GET", rf"repos/{s}/commits", lambda m, p, b: [{"sha": x["sha"]} for x in self.svc_commits])

        def svc_commit(m, p, b):
            sha = m.group(1)
            full = next((x for x in self.svc_commits if x["sha"] == sha), None)
            out = {"sha": sha, "parents": [{"sha": self.parents.get(sha, "unknown-parent")}]}
            if full:
                out.update({"commit": {"message": full["title"], "committer": {"date": full["date"]}},
                            "files": [{"filename": f} for f in full["files"]]})
            return out

        g.route("GET", rf"repos/{s}/commits/([^/]+)", svc_commit)

        def controls_commits(m, p, b):
            if "path" in p:
                if p["path"] == "classes/go-module-bump.yaml":
                    return [{"sha": "classfile-commit"}]
                return [{"sha": "c-" + p["path"].split("/")[-1]}]
            return [{"sha": x["sha"]} for x in self.controls_commits]

        g.route("GET", rf"repos/{c}/commits", controls_commits)
        g.route("GET", rf"repos/{c}/commits/([0-9a-z]+)", lambda m, p, b: next(
            {"sha": x["sha"], "commit": {"message": x["title"], "committer": {"date": x["date"]}},
             "files": [{"filename": f} for f in x["files"]]} for x in self.controls_commits if x["sha"] == m.group(1)))

        def get_ref(m, p, b):
            name = m.group(1)
            if name not in self.refs:
                return ApiError(404, {"message": "Not Found"}, m.group(0))
            return {"ref": f"refs/heads/{name}", "object": {"sha": self.refs[name]}}

        g.route("GET", rf"repos/{c}/git/ref/heads/(.+)", get_ref)

        def post_ref(m, p, b):
            self.refs[b["ref"][len("refs/heads/"):]] = b["sha"]
            return {"ref": b["ref"]}

        g.route("POST", rf"repos/{c}/git/refs", post_ref)

        def get_content(m, p, b):
            path, ref = "classes/records/" + m.group(1), p.get("ref")
            if ref and ref != "main":
                if (ref, path) not in self.branch_files:
                    return ApiError(404, {"message": "Not Found"}, m.group(0))
                return {"sha": "x", "content": base64.b64encode(self.branch_files[(ref, path)].encode()).decode()}
            name = path.split("/")[-1]
            r = next((x for x in self.class_records if x["name"] == name), None)
            if r is None:
                return ApiError(404, {"message": "Not Found"}, m.group(0))
            text = f"schema: 1\nkind: {r['kind']}\nclass: go-module-bump\n"
            return {"content": base64.b64encode(text.encode()).decode()}

        g.route("GET", rf"repos/{c}/contents/classes/records/(.+)", get_content)

        def put_content(m, p, b):
            self.branch_files[(b["branch"], m.group(1))] = base64.b64decode(b["content"]).decode()
            return {"content": {"path": m.group(1)}}

        g.route("PUT", rf"repos/{c}/contents/(.+)", put_content)


def clean_streak(w: ScoreWorld, count: int, start_pr: int = 1, start_min: float = 0, every: float = 30,
                 first_position: int = 1) -> float:
    """`count` clean reviewed merges, one queue item each, from start_min."""
    q = w.cf["queue"]
    t = start_min
    for i in range(count):
        pos = first_position + i
        item = q[pos - 1]
        w.reviewed_merge(start_pr + i, pos, item["module"], item["version"], t)
        t += every
    return t
