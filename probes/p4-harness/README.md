# P4: the agent's harness

## Why

The model that writes each change holds no GitHub credential (ADR-0024 §7):
its job runs with `permissions: {}`, gets the model credential and nothing
else, and hands three files to a second job that holds the agent App's
token. And its output must be exactly what `class-scope` recomputes, or every
queue item would be a near-miss. Both are read from the docs and the build,
not tried on GitHub. P4 runs the agent's workflow once against a copy of
`svc-hello`'s tree on the throwaway repository with `class-scope` and
`go-test` installed, and reads the result. It also records two facts that
change no decision: whether `createCommitOnBranch` commits show as signed and
attributed to the App, and whether a merge shows `merged_by` as the App.

## What must be in place

- P3 done (its rulesets hold only the `p3-base/*` branches) and P2's ruleset
  removed from `main` by the owner, so `main` on `m4-probe` takes the commits
  below. The agent App installed on `m4-probe` (human step 4).
- On `m4-probe`: the `class-scope` workflow and verifier (from the
  `svc-hello` `m4/class-scope` branch) and `validate.yml` with `go-test` (from
  `m4/go-test`), committed to `main`, and the `go 1.26.0` change, so the tree
  matches what the class will start from.
- In factory-controls: a P4 form of the agent's workflow, `agent-p4.yml`,
  run from `main` (the `agent-app` and `agent-model` environments serve
  `main` only). It is `agent.yml` with these differences and no others:
  - `CLASS_FILE` points to `probe/p4-class.yaml`, a copy of the class file
    template whose `repo` is `platform-factory/m4-probe` and whose listed
    login is filled, and each App token step names `repositories: m4-probe`.
    The copy lives outside `classes/`: the class file itself has no commit
    until its first, approved one, which comes after P4 (readiness note,
    order step 3);
  - a workflow-level `env: {RECORDS_BRANCH: records-p4}`, so the run record
    and every other record go to the branch `records-p4`, never to
    `records` (review finding B1). The run record names
    `repo: platform-factory/m4-probe`, so even on `records` no reader of
    svc-hello's records would count it; the separate branch keeps the real
    one clean, since `records` refuses deletion and force-push;
  - the `pre-merge` and `merge` jobs are removed and `prepare` has no
    `needs` and no `if`: the ratchet App is not installed on `m4-probe`, so
    `pre-merge` could not mint its token, and the merge step's checks
    compare svc-hello's ruleset id and listing, need a detector heartbeat
    and a passing push run of `go-test` on the probe's main, none of which
    exists for `m4-probe` (review finding M1). P4 therefore opens a pull
    request and never merges one; `merged_by` is read from P3 (step 4).
  `agent-p4.yml` and `probe/p4-class.yaml` are added by one pull request and
  removed after P4; both commits are recorded with P4's results. P4 runs
  after the listed login is known (human step 2).
- On `m4-probe`, the `class-scope` workflow's decide step carries
  `RECORDS_BRANCH: records-p4` and `CLASS_FILE_PATH: probe/p4-class.yaml` in
  its `env`, so it reads the P4 run record and the P4 copy of the class file
  (`class_scope.py --records-branch`, default `records`;
  `--class-file-path`, default `classes/go-module-bump.yaml`).
- `agent/summary-recipient.pem` holds the certificate whose key is the
  `agent-app` environment's `SUMMARY_KEY` (runbook, step 5): the model's job
  refuses to run without it.
- The model credential stored (human step 5).

## Steps

1. Dispatch the P4 workflow once, with the C-21 model-job attempts as a
   rehearsal:
   `gh workflow run agent-p4.yml --repo platform-factory/factory-controls -f c21_model_job=true`.
2. `python3 p4.py read-run --execute --run <run id>`: the jobs, the model's
   job log (its `GITHUB_TOKEN Permissions` block, and the `c21 attempts`
   step's one JSON line: no secret present, the write refused, no OIDC
   variables), the artifact (go.mod, go.sum and summary.md.cms only, the last
   8752 bytes for any summary), and `records-p4:fixtures/c21-model-job-*`.
3. `python3 p4.py read-pr --execute --pr <n>`: the pull request's author,
   each commit's verification and attribution, `class-scope` and `go-test` on
   the head, and `class-scope`'s verdict annotation (`pass`, `pass_kind`
   `class`) with the artifact's expected and head hashes equal.
4. `merged_by`: read from P3's `b-open-merge`, where the agent App's merge
   API call merged an unapproved go.mod pull request at rung 0:
   `python3 p4.py read-merge --execute --pr <P3's rung0_pr>`. Close P4's own
   pull request unmerged after the reads.
5. Save the reads as `$EVIDENCE/16-p4-*.jsonl`.

## Expected (the readiness note)

| Question | Expected | Must hold before the L1 switch-on? |
|---|---|---|
| The model's job has `permissions: {}` and no GitHub token | yes: no token in its environment, no checkout credential, no OIDC | yes (else the fallback) |
| Queue item 1 passes `class-scope` byte for byte | verdict `pass`, `pass_kind` `class`, the expected and head hashes equal | yes |
| Commits signed and attributed to the App | recorded | no |
| `merged_by` is the App (read from P3's `b-open-merge`) | recorded | no |

## Result table

| Question | Observed (run id, job id, pull request, values) | As expected? |
|---|---|---|
| Model's job: permissions and token | | |
| Queue item 1 byte for byte | | |
| Commit verification and attribution | | |
| `merged_by` | | |

## Fallback (fixed by the note)

- If the model's job cannot run with `permissions: {}` and no GitHub token
  through the action: **the Claude Code CLI in a plain step** of that job.
- The byte-for-byte pass must hold before the L1 switch-on: if it fails, the
  agent's job or the verifier is fixed and P4 re-run; the step after does not
  start until it passes.
- The last two answers are recorded and change no decision: the pull
  request's author is the unforgeable field.

## Read commands, by hand

```sh
gh run view <run id> --repo platform-factory/factory-controls --json jobs --jq '.jobs[] | {name, conclusion}'
gh run view --repo platform-factory/factory-controls --log --job <agent job id> | sed -n '/GITHUB_TOKEN Permissions/,/Secret source/p'
gh api repos/platform-factory/m4-probe/commits/<head> --jq '{verified: .commit.verification.verified, reason: .commit.verification.reason, author: .author.login, committer: .committer.login}'
gh api repos/platform-factory/m4-probe/pulls/<n> --jq '{merged, merged_by: .merged_by.login}'
```
