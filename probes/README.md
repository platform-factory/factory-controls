# Probe kit (P1 to P4)

M4's readiness note names four probes that settle, on a throwaway
repository, the GitHub behaviours ADR-0024 relies on but read only from the
docs. Each probe's result is recorded in M4's build-log entry before the
step that rests on it, and each has a fallback fixed in advance. A result
with no fallback is settled by a dated correction in the claims register
before that step, never after the first agent pull request.

| Probe | Question | Needs | Before |
|---|---|---|---|
| [P1](p1-patterns/) | which required-reviewers entry applies to which file: anchors, negation, `*`, two entries for one team | no App | the class file's first commit (the L1 JSON) |
| [P2](p2-class-scope-attachment/) | where a `pull_request_target` check run attaches, and whether it blocks | no App; `pull_request_target` allowed (human step 3) | building `class-scope` on it |
| [P3](p3-approvals-labels/) | stale dismissal on entries, rung 0 merges, App approvals, App label events, App token reads | the agent App on `m4-probe`; the listed human's approvals | the class file's first commit |
| [P4](p4-harness/) | the model's job holds no token; queue item 1 passes `class-scope` byte for byte; signing; `merged_by` | P3 done, the agent's workflow in its P4 form | the L1 switch-on |

The throwaway repository is `platform-factory/m4-probe`, public, with team
`platform` given write; P1's `create` step makes it.

## How the scripts work

Each probe directory has a `README.md` (why, the steps, the expected result
from the note, the read commands, a result table to fill, the fallback) and
one script. Every script needs `--dry-run` or `--execute`; there is no
default. `--dry-run` prints each call it would make as one JSON line and
sends nothing. `--execute` prints the same line, makes the call with the `gh`
CLI, and prints GitHub's answer. The pull request numbers and ruleset ids an
executed step creates are kept in `state.local.json` beside the script
(ignored by git). `lib/probe.py` is the shared plumbing.

Who runs what: the delegate runs the set-up and the reads; a step that lowers
an approval count or deletes a ruleset is a loosening, which the owner runs
(as run, only one remains: removing P2's ruleset from `main`; P1 and P3 use
base branches of their own and loosen nothing); the listed human makes P3's
approvals in the browser; the agent App's acts run from a workflow in
factory-controls, because its key exists nowhere else.

P1, P2 and P3 have been run (2026-11-12 and 2026-11-15); their READMEs hold
the results. P4 has not. `python3 -m unittest discover -s tests`
runs every step of every script in `--dry-run` and checks that no command is
executed.
