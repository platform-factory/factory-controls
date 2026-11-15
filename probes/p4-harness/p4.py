"""P4: a dry run of the agent's harness against the throwaway repository.

Four questions (the readiness note, P4): does the model's job run with
`permissions: {}` and no GitHub token; does its queue item 1 pass
`class-scope` byte for byte; are the commits signed and attributed to the
App; does a merge show `merged_by` as the App. The run itself is one dispatch
of the agent's workflow in its P4 form (README), which opens and never
merges; `merged_by` is read from P3's `b-open-merge` pull request, which the
agent App's merge call merged. This script makes the reads that answer each
question. It writes nothing.

    python3 p4.py read-run   --dry-run --run RUN_ID     # the controls run: the model's job, its token, its log
    python3 p4.py read-pr    --dry-run --pr N           # the pull request: class-scope's verdict, commit signatures
    python3 p4.py read-merge --dry-run --pr N           # merged_by on P3's b-open-merge pull request
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
import probe  # noqa: E402

CONTROLS = f"{probe.ORG}/factory-controls"


def main(argv=None) -> int:
    ap = probe.parser("P4: the agent's harness", ["read-run", "read-pr", "read-merge"])
    ap.add_argument("--run", type=int)
    ap.add_argument("--pr", type=int)
    a = ap.parse_args(argv)
    api = probe.Api(a.execute)
    if a.step == "read-run":
        jobs = api.call("GET", f"repos/{CONTROLS}/actions/runs/{a.run}/jobs",
                        note="the P4 form's jobs: prepare, agent, open, c21-record (no pre-merge or merge)")
        agent_id = next((j["id"] for j in (jobs or {}).get("jobs", []) if j.get("name") == "agent"), "<agent job id>") \
            if isinstance(jobs, dict) else "<agent job id>"
        api.call("GET", f"repos/{CONTROLS}/actions/jobs/{agent_id}/logs",
                 note="the model's job log: its 'GITHUB_TOKEN Permissions' block must be empty (metadata only), "
                      "no checkout credential, and the `c21 attempts` line (no other secret, no OIDC)")
        api.call("GET", f"repos/{CONTROLS}/actions/runs/{a.run}/artifacts",
                 note="the model's artifact agent-output (deleted by open), holding go.mod, go.sum, summary.md.cms")
    elif a.step == "read-pr":
        pr = api.call("GET", f"repos/{probe.REPO}/pulls/{a.pr}", note="author (the App's bot), head")
        head = pr["head"]["sha"] if isinstance(pr, dict) else "<head>"
        api.call("GET", f"repos/{probe.REPO}/pulls/{a.pr}/commits",
                 note="each commit's verification (verified, reason) and author/committer")
        api.call("GET", f"repos/{probe.REPO}/commits/{head}",
                 note="commit.verification.verified and .reason; author.login and committer.login")
        api.call("GET", f"repos/{probe.REPO}/commits/{head}/check-runs", params={"filter": "all"},
                 note="class-scope and go-test, each from app 15368, on this head")
        api.call("GET", f"repos/{probe.REPO}/actions/runs", params={"event": "pull_request_target", "per_page": 5},
                 note="class-scope's run; its check run's class-scope-verdict annotation (verdict pass, pass_kind "
                      "class) and its artifact class-scope-verdict-attempt-1: expected_sha256 equal to head_sha256")
    elif a.step == "read-merge":
        api.call("GET", f"repos/{probe.REPO}/pulls/{a.pr}",
                 note="P3's b-open-merge pull request: merged, merged_by.login (expect "
                      "platform-factory-agent[bot]), merge_commit_sha")
    return 0


if __name__ == "__main__":
    sys.exit(main())
