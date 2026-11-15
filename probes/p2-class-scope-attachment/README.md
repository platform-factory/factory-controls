# P2: `class-scope`'s attachment

## Why

Lock 2 is a required check the pull request cannot edit: `class-scope`, on
`pull_request_target`, which runs the workflow from the default branch
(ADR-0024 §5). Read from the docs, not tried: which commit its check run
attaches to (GitHub's docs give `GITHUB_SHA` as the default branch's tip,
while required checks are looked up by the pull request's head); whether a
red one blocks the merge; whether a push or a label re-runs it; whether a
later green run on the same head replaces a red one; and, because required
checks are looked up by head and name, not by pull request, which runs exist
when two pull requests share one head, one into `main` and one into another
branch. P2 needs no App.

## Mental model

The stub (`class-scope-stub.yml`) has `class-scope`'s exact trigger, filter,
types and job name, and a verdict a label flips: it fails unless the pull
request carries `probe-green`. A ruleset requires `class-scope` from GitHub
Actions (integration 15368), strict, as L1 will. Then one head is made red,
pushed, labelled green, and shared with a second pull request.

## Steps (on `m4-probe`, after P1's cleanup)

Preconditions: human step 3 done (an Actions event policy allows
`pull_request_target` on `m4-probe`). P1's rulesets stay: each holds only its
own `p1-base/*` branch, none holds `main`.

1. `python3 p2.py stub --execute` (the delegate's token needs the `workflow`
   scope to write under `.github/workflows/`).
2. `python3 p2.py ruleset --execute`.
3. `python3 p2.py pr --execute`, wait for the run, then `python3 p2.py read --execute`.
4. `python3 p2.py push --execute` (reads follow).
5. `python3 p2.py green --execute` (reads follow).
6. `python3 p2.py two-prs --execute` (reads follow).
6a. Before step 4's push, apply the label `probe-remove` to the pull request
   (the delegate, in the sidebar or `gh pr edit <n> --add-label probe-remove`).
   The stub removes it on `synchronize` with its own token. Read the removal's
   issue event and record `actor.login` and `performed_via_github_app`
   (review finding m5): class-scope and `factory/state.py` treat a removal by
   `github-actions[bot]` (or `performed_via_github_app.slug`
   `github-actions`) as class-scope's own, which ends a head-bound label.
   `gh api repos/platform-factory/m4-probe/issues/<n>/events --jq '.[] | select(.event=="unlabeled") | {actor: .actor.login, app: .performed_via_github_app.slug}'`
7. Save every read as `$EVIDENCE/06-p2-*.jsonl`; `python3 p2.py cleanup --execute`
   (the owner, for the ruleset deletion).

## Expected (the readiness note)

| Question | Expected |
|---|---|
| Does the check run carry the pull request's head commit? | yes: `head_sha` equals the head, `app.id` 15368 |
| Does a red one block the merge? | yes: `mergeStateStatus` BLOCKED |
| Does a push re-run it? | yes: a new run on the new head |
| Does a label re-run it? | yes: a new run on the same head |
| Does a later green run on the same head replace a red one? | yes: no longer BLOCKED by it |
| Two pull requests, one head, into `main` and into another branch: which runs exist? | runs only for the pull request into `main`; none for the other (`branches: [main]`) |

## Result table

| Question | Observed (run ids, `head_sha`, conclusion, `mergeStateStatus`) | As expected? |
|---|---|---|
| Head attachment | check run 132972011127: `head_sha` = the head `890d5ba8`, `app.id` 15368; `GITHUB_SHA` in the job = main's tip `b7fbcbbc` | yes |
| Red blocks | failure; BLOCKED | yes |
| Push re-runs | run 43766388894 on the new head `f79b62fd`, failure; BLOCKED | yes |
| Label re-runs | run 43766358704 on the same head (red), run 43766428735 on the same head (green) | yes |
| Green replaces red | success on `f79b62fd`; UNSTABLE while an unrequired check ran, then CLEAN; the old failed check run stays in the rollup and does not block | yes |
| Two pull requests, one head | no `pull_request_target` run for the pull request into `p2-other` | yes |
| Removal with the workflow's token: actor, `performed_via_github_app` | `github-actions[bot]`; `performed_via_github_app` null (the slug was expected) | actor yes, slug no; the code matches the login |

Run 2026-11-15 on `m4-probe`, pull requests #25 and #26. No fallback
triggered. A run's `pull_requests` array lists every pull request with that
head, whichever one triggered it. The stub's own `unlabeled` event started no
run. The ruleset (27332913) is left for the owner to remove.

## Fallback (fixed by the note)

- If the `pull_request_target` form does not attach to the head or does not
  block: **`class-scope` as a `pull_request` job in its own workflow**, with
  the same filter and types (`class-scope-stub-pull-request.yml` is that
  form; re-run steps 1 to 6 with it), and C-15 records that Lock 2 then rests
  on the agent App's missing `workflows` permission and on the wall entry
  over `.github/**`. In that form the real `class-scope.yml` must check out
  the verifier with `ref: ${{ github.event.repository.default_branch }}`
  (sparse, `.github/class-scope` only), because on `pull_request` a checkout
  with no `ref` takes the pull request's merge ref and the pull request's own
  `class_scope.py` would judge it. The missing `workflows` permission covers
  `.github/workflows/` only, so C-15 also records that `.github/class-scope/`
  rests on the wall entry alone.
- If no trusted check attaches and blocks at all: C-15 runs its off-path
  cases on Lock 1 alone and is graded on them, PARTIAL at most, with the
  missing check recorded as an enforcement gap; C-16 and C-17 run at L1, with
  the packet written by an untrusted job and labelled so; no promotion is
  made; C-18, C-19 and C-20 are NOT REACHED, precondition named: a trusted
  check a pull request cannot edit.

## Read commands, by hand

```sh
gh api "repos/platform-factory/m4-probe/commits/<head>/check-runs?check_name=class-scope&filter=all" \
  --jq '.check_runs[] | {id, head_sha, app: .app.id, status, conclusion, started_at}'
gh api "repos/platform-factory/m4-probe/actions/runs?event=pull_request_target" \
  --jq '.workflow_runs[] | {id, head_sha, head_branch, conclusion, prs: [.pull_requests[].number]}'
gh pr view <n> --repo platform-factory/m4-probe --json mergeStateStatus,reviewDecision,statusCheckRollup
```
