"""Shared plumbing for the probe scripts: every GitHub call is printed first.

Why: the probes change a throwaway repository's rulesets and pull requests,
and in this phase nothing may be sent to GitHub at all. So every script runs
in one of two modes, chosen explicitly on the command line:

- `--dry-run` prints each call it would make, as one JSON line, and sends
  nothing;
- `--execute` prints the same line, then makes the call with the `gh` CLI
  (the delegate's credential, or `GH_TOKEN` when a workflow passes an App's
  installation token) and prints GitHub's answer.

There is no default: a script refuses to run without one of the two.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Callable

ORG = "platform-factory"
REPO = f"{ORG}/m4-probe"
TEAM_ID = 18810690          # team `platform` (read 2026-11-11; also in factory-controls' l1.json)
ACTIONS_APP_ID = 15368      # GitHub Actions, the integration the required checks are pinned to
AGENT_BOT = "platform-factory-agent[bot]"

PR_STATUS = """query($owner: String!, $name: String!, $n: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $n) {
    number headRefOid reviewDecision mergeStateStatus mergeable
    reviewRequests(first: 20) { nodes { requestedReviewer { ... on Team { slug } ... on User { login } } } }
    reviews(last: 20) { nodes { author { login } state commit { oid } submittedAt } }
  } } }"""


class Api:
    def __init__(self, execute: bool, out=None, runner: Callable | None = None, token: str | None = None):
        self.execute = execute
        self.out = out or sys.stdout
        self.runner = runner or subprocess.run
        self.token = token
        self.calls: list[dict] = []

    def _emit(self, obj: dict) -> None:
        self.out.write(json.dumps(obj, sort_keys=True) + "\n")

    def call(self, method: str, path: str, body: Any = None, *, params: dict | None = None, note: str = "") -> Any:
        if params:
            path = path + "?" + urllib.parse.urlencode(params)
        line = {"call": len(self.calls) + 1, "method": method, "path": path, "note": note}
        if body is not None:
            line["body"] = body
        self.calls.append(line)
        self._emit(line)
        if not self.execute:
            return None
        argv = ["gh", "api", "-X", method, path, "-H", "Accept: application/vnd.github+json"]
        if body is not None:
            argv += ["--input", "-"]
        env = dict(os.environ)
        if self.token:
            env["GH_TOKEN"] = self.token
        r = self.runner(argv, input=json.dumps(body) if body is not None else None, capture_output=True,
                        text=True, env=env)
        try:
            parsed = json.loads(r.stdout) if r.stdout.strip() else None
        except ValueError:
            parsed = r.stdout
        self._emit({"result_of": line["call"], "exit": r.returncode, "body": parsed, "stderr": r.stderr[-2000:]})
        return parsed

    def graphql(self, query: str, variables: dict, note: str = "") -> Any:
        return self.call("POST", "graphql", {"query": query, "variables": variables}, note=note)

    def pr_status(self, n: int | None, note: str = "") -> Any:
        owner, name = REPO.split("/")
        return self.graphql(PR_STATUS, {"owner": owner, "name": name, "n": n or 0},
                            note=note or f"reviewDecision, mergeStateStatus, review requests of #{n}")


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def state_file(probe_dir: Path) -> Path:
    return probe_dir / "state.local.json"   # pull request numbers and ids from an executed run; never committed


def load_state(probe_dir: Path) -> dict:
    p = state_file(probe_dir)
    return json.loads(p.read_text()) if p.exists() else {}


def save_state(probe_dir: Path, st: dict, api: Api) -> None:
    if api.execute:
        state_file(probe_dir).write_text(json.dumps(st, indent=2, sort_keys=True) + "\n")


def number(res: Any, fallback: int | None = None) -> int | None:
    return res.get("number") if isinstance(res, dict) and "number" in res else fallback


def main_sha(api: Api) -> str:
    res = api.call("GET", f"repos/{REPO}/branches/main", note="main's tip")
    return (res or {}).get("commit", {}).get("sha", "<main-sha>") if isinstance(res, dict) else "<main-sha>"


def parser(description: str, steps: list[str]) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("step", choices=steps)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="print the calls; send nothing")
    mode.add_argument("--execute", action="store_true", help="print the calls, then make them with gh")
    return ap
