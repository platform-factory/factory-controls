# P3: approvals and labels

## Why

Four behaviours ADR-0024 rests on were read from the docs and not tried:

- (a) the listed human's approval on an entry is dismissed, and the merge
  refused, when the agent App pushes a content change, pushes an empty
  commit, or updates the branch (`dismiss_stale_reviews_on_push` is
  diff-based; whether it applies to an entry's approvals, and to an empty
  commit, is not stated);
- (b) with the rung entry at 0, an unapproved agent App pull request on
  `go.mod` merges (that is what L2 is);
- (c) an approval by the agent App satisfies neither entry (the entries count
  members of a team with write; an App is not a team member);
- (d) the agent App's label events name it: `performed_via_github_app` is
  set, which is what `class-scope`, the detector and the scorecard read.

This kit adds (e): whether the Apps' installation tokens can make the reads
the detector and the merge step make (workflow runs and jobs, check runs,
file contents, compares, artifacts) on a public repository without
`actions`, `checks` or `contents` read. GitHub's docs grant implicit public
reads to user access tokens, not installation tokens; the answer decides
whether the manifests need read-only permissions (factory-controls
`apps/README.md`).

## Mental model

The delegate sets up and reads; the agent App acts through a workflow in
factory-controls (its key exists nowhere else); the listed human approves in
the browser (human step 6). Nobody loosens or deletes a ruleset, and `main`
is not touched: each case has a base branch of its own, as P1's had.

| Base branch | Rulesets (one entry each, P1's fallback) | Cases |
|---|---|---|
| `p3-base/l1` | `p3-l1-rung` (rung entry at 1), `p3-l1-wall` (wall entry at 1) | a, c, d, e |
| `p3-base/l2` | `p3-l2-rung` (rung entry at 0, created so), `p3-l2-wall` | b |
| `p3-base/f` | none | f |

A ruleset created with the rung entry at 0 is the state L2 is; ADR-0024's
promotion reaches the same state by one edit, which P1's ratchet rehearsal
covered in the other direction. Each act is followed by the same reads:
`reviewDecision`, `mergeStateStatus`, the review states and their commits,
and, for (a) and (b), a merge attempt by the App (a probe read is never a
merge attempt by the delegate).

## Steps (on `m4-probe`)

Preconditions: human step 4 done (both Apps registered; the agent App
installed on `m4-probe` only); this kit copied into factory-controls as
`probes/` and `p3-app-acts.yml` into its `.github/workflows/`, merged to
`main` by a pull request (removed again by another after P3). App steps are
dispatched:

```sh
gh workflow run p3-app-acts.yml --repo platform-factory/factory-controls -f step=<step> [-f pr=<n>] [-f pr2=<m>]
gh run view --repo platform-factory/factory-controls --log <run id>    # the calls and GitHub's answers
```

1. Delegate: `python3 p3.py bases --execute` (the three base branches, and
   one commit on `p3-base/l1`), `python3 p3.py ruleset --execute` (four
   rulesets, all creations) and `python3 p3.py human-prs --execute` (the
   README.md and go.mod pull requests for (c), into `p3-base/l1`).
2. App: `a-open` (a go.mod pull request one commit behind `p3-base/l1`; the
   commit it lacks is a text file, so update-branch needs no `workflows`
   permission).
3. Listed human: approve it. App: `a-read` (reads only: expect APPROVED and
   the pull request mergeable; no merge is attempted, because it would
   succeed and end case (a)).
4. App: `a-push-content`. Expect the approval DISMISSED, `reviewDecision`
   REVIEW_REQUIRED, the merge 405.
   Each push step waits until the pull request shows the new head and a
   computed mergeability before it reads and attempts the merge (the first
   run did not, and its reads were stale). `a-merge` makes the merge attempt
   alone, on the settled head.
5. Listed human approves again. App: `a-push-empty`. Same expectation.
6. Listed human approves again. App: `a-update-branch`. Same expectation.
7. App: `c-approve` with `pr` = the delegate's README.md pull request and
   `pr2` = its go.mod pull request. Expect both still BLOCKED, `platform`
   still requested.
8. App: `d-labels` on any open pull request; then read the events (the step
   prints them). Expect both events with `actor.login`
   `platform-factory-agent[bot]` and `performed_via_github_app.slug`
   `platform-factory-agent`. (P2 showed the workflow's own token leaves
   `performed_via_github_app` null, so this is the case that matters.)
9. App: `e-reads` with any pull request. Record each status.
10. App: `b-open-merge` (an unapproved go.mod pull request into
    `p3-base/l2`). Expect it to merge.
11. Save all reads as `$EVIDENCE/15-p3-*.jsonl`; close the remaining pull
    requests. The rulesets stay; none holds `main`.
12. Update-branch behind a workflow change (review finding M5), on
    `p3-base/f`, which no ruleset holds: App `f-open` (a go.mod pull request
    at the base's tip); delegate `python3 p3.py f-workflow-base --execute` (a
    harmless workflow file committed to `p3-base/f`); App `f-update-branch`
    with `pr` = that pull request, and record the status and message; then
    App `f-remake` with the same `pr` (the merge step's fallback: the same
    go.mod and go.sum committed on the base's tip through
    `createCommitOnBranch`, the branch moved to it with `PATCH git/refs`
    force, the temporary branch deleted). Record each answer; close the pull
    request after. The result goes in M4's entry before the class file's
    first commit: if both are refused, the merge step cannot bring a pull
    request up to a main that gained a workflow change, and the readiness
    note's "updated by the merge step" needs a dated correction (a person
    makes the change again).

## Expected (the readiness note) and the result table

| Case | Expected | Observed | As expected? |
|---|---|---|---|
| (a) content push after approval | approval dismissed; merge refused | | |
| (a) empty commit after approval | approval dismissed; merge refused | | |
| (a) update-branch after approval | approval dismissed; merge refused | | |
| (b) rung 0, no approval | merges | | |
| (c) App approves a person's README.md pull request | not counted; still BLOCKED | | |
| (c) App approves a person's go.mod pull request at rung 1 | not counted; still BLOCKED | | |
| (d) App applies and removes a label | events name the App (`performed_via_github_app`) | | |
| (e) App token: actions runs, check runs, jobs, contents, compare, artifacts | not stated by the docs | | |
| (f) update-branch after the base gained a workflow file | not stated by the docs; refused for `workflows` is likely [I] | | |
| (f) remake: `createCommitOnBranch` on the base's tip, then `PATCH git/refs` force | the commit accepted; the force-move not stated by the docs | | |

## Fallbacks (fixed by the note)

- (a): the merge step refuses at L1 unless the listed human's latest approval
  is on the head commit, and C-15 and C-21 record that this lock is code, not
  GitHub.
- (b): L2 removes the rung entry instead, still one recorded change;
  CODEOWNERS still requests `platform`.
- (c): none; C-15 records the gap, and the boundary rests on the model
  holding no token (ADR-0024 §7).
- (d): `class-scope` accepts only events whose actor is the listed login,
  whatever `performed_via_github_app` says.
- (e), not in the note: a refused read means a read-only permission added to
  the manifest before the class file's first commit, by a dated correction in
  the claims register, with C-21's attempt list noting it. The ratchet App is
  not installed on `m4-probe`, so its reads are first tried by the detector's
  `--dry-run` with its token right after human step 8, before the first agent
  pull request.

## Read commands, by hand

```sh
gh api repos/platform-factory/m4-probe/pulls/<n>/reviews --jq '.[] | {user: .user.login, state, commit_id}'
gh pr view <n> --repo platform-factory/m4-probe --json reviewDecision,mergeStateStatus,reviewRequests
gh api repos/platform-factory/m4-probe/issues/<n>/events \
  --jq '.[] | select(.event=="labeled" or .event=="unlabeled") | {event, actor: .actor.login, app: .performed_via_github_app.slug}'
```
