# The scorecard

`factory/scorecard.py`, run by `.github/workflows/scorecard.yml` once a day,
by hand, and after each merge. It writes three files to the records branch:

| File | What it is |
|---|---|
| `scorecard/go-module-bump.json` | the scorecard (below) |
| `scorecard/go-module-bump.md` | the same numbers for a reader |
| `scorecard/go-module-bump.inputs.json` | every read it was computed from |

## Why it is built this way

C-17's pass line is that two runs over the same inputs give the same JSON,
no output is edited by hand, and every field traces to an API read or a
declared human-entered field. So the job reads once (`gather`), and the JSON
is a pure function of those reads, the class file and the committed rulesets
(`compute`): no clock, no network, every list in one order. The inputs file
is published beside it, and

```sh
python3 -m factory.scorecard --snapshot scorecard/go-module-bump.inputs.json --out /tmp/sc --allow-placeholders
```

reproduces the JSON byte for byte; its `inputs_sha256` is the hash of the
inputs without their `meta` (read time and run id), which do not enter the
result.

Per-pull-request rows are withheld until the pull request is closed or
merged (ADR-0024 §11). The job does not read the events, reviews or checks of
an open pull request, and `compute` drops them if a snapshot carries them, so
the inputs file says nothing about an open pull request beyond its number
and its close and reopen events (which can make it a near-miss).

The streak, near-misses, seeds and labels in force come from
`factory/state.py`, the code the merge step and the detector use.

## The fields (C-17's list)

| C-17 asks for | JSON |
|---|---|
| class, N | `class`, `n` |
| rung | `rung.live_minimum_approvals` and `rung.live_level` (the ruleset now), `rung.by_records_now` (L2 from a merged promotion or fixture record until the next ledger demotion) |
| streak with resets | `streak.current`, `streak.start`, `streak.max`, `streak.resets` (causes `near-miss`, `escape-a`, `escape-b`, `verifier-change`), `streak.history`, `promotion.due` and `promotion.blockers` |
| changes | `changes` (queue-item pull requests, merged, near-misses, seeds, open, fixtures, first agent pull request, window end) |
| escapes by type | `escapes.a`, `escapes.b`, `escapes.c` (not run), `escapes.counts` |
| near-misses | `near_misses` (with who closed: merge step, listed human, other) |
| seeds caught and missed | `seeds.caught`, `seeds.missed`, `seeds.void`, `seeds.undecided`, `seeds.not_run_positions` (after the reveal: drawn positions not reached, or reached at L2, where no seed opens; the latter also in `seeds.skipped_at_l2_positions`), `seeds.key_matches_hash` |
| shadow verdicts | `shadow_verdicts` and each row's `shadow`: what "merge when the required checks are green" would have done beside the listed human's decision, at L1 |
| inconclusives by reason | `inconclusives.by_reason_runs`, `.by_reason_prs`, `.natural_prs`, `.forced_prs`, `.cleared_prs` (reason codes from the `class-scope-verdict` annotation on each class-scope check run, which is public; the verdict artifact needs `actions: read` on svc-hello, which this job's token lacks; `no-verdict` and `reason-unreadable` when there is no annotation) |
| model, harness, skill release, verifier version | `model`, `harness`, `skill_releases`, `verifier_versions` (as used, from run records and verdicts), `pins` (as pinned) |
| lifts and other changes after the switch-on | `changes_after_switch_on`: ruleset records in `platform-factory/.github` after the `-after-l1-switch-on.json` file (the second of the switch-on's two edits, on `main-protection`; the first, on `class-wall`, is named `-after-l1-switch-on-wall.json`), ruleset-difference ledger entries, `svc-hello` commits touching anything but `go.mod`/`go.sum`, controls commits under `factory/`, `.github/`, `classes/` |
| tokens, agent minutes, CI minutes per pull request | each row's `usage` (tokens and agent seconds from the run record; CI minutes summed over the GitHub Actions check runs on the pull request's commits; the opening run's job minutes) |
| attribution (dated addition) | `attribution.precision`, `.recall`, `.recall_by_type`, `.delay_minutes`, and `unknowns` |
| review minutes (C-16) | `reviews` and each row's `review` |

## How the judgement fields are read

- **Review.** The listed human's first review whose body is in the fixed
  form `minutes: <n>; sections: <list>; decision: <approve|close>; reason:
  <text>` is the decision. Measured minutes run from the latest `reviewing`
  label by the listed human (no App) at or before the review, to the review.
  The upper bound runs from when the pull request was ready (opened, and its
  required checks on the reviewed commit complete) to the review. The median
  covers non-seed queue items reviewed at L1 before the first promotion or
  fixture record or the merge that left three items, whichever is first.
  `decision` is checked against the review's state (Approve ↔ approve,
  Comment ↔ close); `reason` is copied and not read.
- **Streak.** By `factory/state.py`, the rule the merge step and detector
  use, with three inputs only the scorecard's snapshot carries: the push runs
  on each merge commit, the time of the read (`as_of`, part of the inputs),
  and the svc-hello commits after the switch-on. A type (a) escape resets the
  streak at its event time whether or not the detector acted on it (an event
  before the latest record is ignored by the detector, never by the streak).
  Any commit on main after the switch-on that touches
  `.github/workflows/validate.yml`, `.github/workflows/class-scope.yml`,
  `.github/class-scope/**` or a `*_test.go` file resets it at its commit time
  (the note: "A change to what go-test or class-scope decides resets the
  streak"; the rule is mechanical and errs toward a reset). A near-miss
  counts once per pull request, at its first close (or at its base being
  changed away from main).
- **Promotion due.** The streak is at least N on L1, no record merged after
  it began, and each of the N merges it counts (the first N of the streak)
  has a push run of `go-test` whose first attempt succeeded within the escape
  window. A merge whose window is still open, or whose runs were not read,
  holds it back and is listed in `promotion.blockers`. The agent workflow
  calls this job after each merge, and once more on the first run after the
  latest merge's window closes (until a scorecard has read GitHub after that
  close, as `as_of` in this inputs file says), so the record is written once
  the 20th merge's window is clear, without waiting for the daily run.
  A live run first reads how many requests the reading token has left this
  hour and defers, writing nothing, when fewer than 600 are left: the
  detector and the merge step share that limit and come first. A deferred
  run leaves the job due, so the next agent run tries again.
- **Shadow verdict.** From the last head's required checks: `merge` if both
  passed without a clearance, `close` if either failed, `wait` otherwise (a
  pass a clearance made would have waited under checks-green). It is set
  beside the listed human's decision when that decision was made at L1: the
  rung at the review, not at opening.
- **Precision.** Detected escapes are the ledger's type (a) and (b) entries.
  A type (a) entry counts as true when attempt 1 of `go-test` failed at the
  step `injected failure (M4, C-18)` (injected), or when the scorecard's own
  re-reading of the push runs by the detector's rule confirms it (real: a
  failed verification step, a missing run, a cancellation, a late success).
  A type (b) entry counts as true when the listed human's `escape:revert` is
  in force on the reverting pull request by its events.
- **Recall.** Injected (a): merged queue items whose push run's first
  attempt failed at the injection step, found from the runs, not the ledger.
  Injected (b): merged pull requests, after the first class merge, carrying
  the listed human's `escape:revert`. Recall is the share found in the ledger.
- **Delay.** From the trusted event to the ruleset's new version as the
  ratchet read it back, or to the ledger entry when the rung was already 1.
  When the ratchet's write failed, the new version is the one the first
  later retry wrote (a `ratchet-retry` ledger entry; `ratchet_retries` counts
  them). Until a retry raises the rung there is no freeze: `delay_minutes` is
  null, the key is listed in `delay_minutes.not_raised`, and
  `within_pass_line` is false.
  For type (a) the trusted event is the earliest `completed_at` of a failed
  first-attempt `go-test` job on the merge commit's push runs (or the
  window's end when no first attempt succeeded and none failed). The note
  names "the completion of the failed run's first attempt"; a job completes
  before its run attempt does, so this reads an equal or earlier time and
  errs toward a longer delay. For type (b) it is the revert's merge.
- **Unknowns.** Label events on the three human labels that do not count
  (not the listed human, or made through an App), on closed or merged pull
  requests; an App applying or removing `seeded` (other than the settlement
  step's own label, after the pull request's seed record) or removing
  `needs-human`; the detector's latest non-label unknowns; unusable class
  records; and tampers the state finds (a queue-item pull request whose base
  was changed away from main).
- **Approved.** The listed human's latest decision on the head (an
  approving, changes-requested or dismissed review, or a comment review in
  the fixed form saying `decision: close`) must be an approval on the head:
  the merge step's own rule.

## The six human-entered fields (declared before the run)

| Field | Source | Kind |
|---|---|---|
| `reviewing` | the listed human's labeled event | measurement C-16 asks for |
| entered minutes | `minutes:` in the review form | measurement C-16 asks for |
| packet sections used | `sections:` in the review form | measurement C-16 asks for |
| close of a queue-item pull request | the listed human's close | human classification the design intends |
| `escape:revert` | the listed human's labeled event | human classification the design intends |
| `human-cleared` | the listed human's labeled event | human classification the design intends |

## The promotion record

When `promotion.due` is true (the frozen rule's streak of N at L1, with no
record merged since the streak began), the job creates the branch
`promotion/go-module-bump-from-pr<first merge of the streak>` from `main`
and writes `classes/records/promotion-go-module-bump-pr<20th merge>.yaml` to
it, then opens an issue unless one with the same title is open. Later runs
during the same streak find both and write nothing. The delegate opens the
pull request; the listed human approves and merges it (`docs/RUNBOOK.md`,
step 12).
