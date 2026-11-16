# Scorecard: go-module-bump on platform-factory/svc-hello

Inputs SHA-256 `64f46d15fdf72b10981da125150cdc8cfb54e73ad2c044f88226197398272504`; class file commit `de36a84ce36e50fbb197e41fa24bc1794fc81b5c`; main `cb6e4affd687258391311880afc54e7229151ee2`; latest event 2026-11-16T03:14:14Z.

Computed from GitHub's record, the records branch and the class file. Rows for pull requests still open are withheld until they close or merge.

## Rung and streak

- Rung: live rung entry at -1 (unknown); by the records: L1; L2 accepted: False.
- N = 20. Streak 0 (since None), longest 0, resets 0. Promotion due: False.

## Changes

- Queue-item pull requests 0, merged 0, near-misses 0, seeds 0, open (withheld) 0.
- First agent pull request None; window closes None.

## Escapes and attribution

- Type (a): 0; type (b): 0; type (c): not run in M4 (ADR-0024 §2).
- Precision None (0 of 0); recall None; delay max None min (pass line 60); unknowns 0.

## Seeds, reviews, shadow verdicts

- Seeds caught [], missed [], void [], undecided [], not run None (identified by seed records).
- Review median None min measured, None entered, None upper bound, over 0 (pass line 5, falsifier 15).
- Shadow verdict agrees on 0, disagrees on [].

## Inconclusives by reason

- none

## Pull requests (closed or merged)

| PR | kind | item | outcome | rung | class-scope | go-test | shadow | decision | minutes (label) | minutes (entered) | tokens in/out | agent min | CI min |
|---:|---|---|---|---|---|---|---|---|---:|---:|---|---:|---:|

## Lifts and other changes after the switch-on

- Switch-on: None

## Pins

- Model claude-sonnet-5-5; harness []; skill []; verifier [].

## Human-entered fields (declared before the run)

- `reviewing`: the listed human's `reviewing` labeled event (measurement; C-16's review minutes start at it)
- `entered_minutes`: `minutes:` in the review form (measurement; C-16 asks for the reviewer's own minutes beside the measured ones)
- `sections`: `sections:` in the review form (measurement; C-16 asks which packet sections were used)
- `close`: the listed human's close of a queue-item pull request (human classification the design intends; the review rule's close is a near-miss)
- `escape:revert`: the listed human's `escape:revert` labeled event (human classification the design intends; escape type (b) rests on a person's label)
- `human-cleared`: the listed human's `human-cleared` labeled event (human classification the design intends; a clearable inconclusive is cleared by a person)
- `decision` repeats the review's own state and is checked against it; `reason` is free text the scorecard copies and does not read.
