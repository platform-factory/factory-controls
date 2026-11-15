# P1: patterns (the wall and rung entries)

## Why

The L1 ruleset holds the class with two required-reviewers entries for team
`platform` in one ruleset (ADR-0024 §4): the **rung entry** on `/go.mod`,
`/go.sum`, and the **wall entry** on `*`, `.github/**`, `!/go.mod`,
`!/go.sum`, in that order, at one approval on every rung. Read from the docs,
not tried: whether one ruleset accepts two entries for one team; whether
patterns anchored with a leading `/` are honoured (the rules page says
gitignore format, the REST description says fnmatch); whether `!` un-matches
the earlier patterns; whether `*` matches nested paths and dotfiles. P1
answers each before any of them is relied on. It needs no App.

## Mental model

A pull request that needs no approval reads `mergeStateStatus: CLEAN` (or
`UNSTABLE` while a check that is not required runs or fails); one
that needs an approval from `platform` reads `BLOCKED` with `platform`
requested. With both entries at one approval every file reads BLOCKED and
the entries cannot be told apart, so each variant runs twice:

- **config W**: wall at 1, rung at 0. BLOCKED means the wall matched.
- **config R**: wall at 0, rung at 1. BLOCKED means the rung matched.

A probe read is `reviewDecision`, `mergeStateStatus`, the review requests and
rule insights; nothing is merged and the delegate attempts no merge.

## Steps (on `platform-factory/m4-probe`)

Preconditions: human steps 1 to 3 done (the `claude` App narrowed, the listed
human the only member of `platform`, `pull_request_target` allowed);
`m4-probe` does not exist yet.

1. `python3 p1.py create --execute`: the repository (public), `platform`
   given write, the `claude` App's absence read; then seed it with
   `svc-hello`'s tree at `312b18d` by the two shell lines it prints.
2. For each variant (`anchored`, then `unanchored`; `dotfiles` only if the
   anchored reads show `*` missing nested paths or dotfiles) and each config
   (`R`, then `W`):

   ```sh
   python3 p1.py prs     --execute --variant anchored --config R
   python3 p1.py ruleset --execute --variant anchored --config R --mode evaluate
   python3 p1.py read    --execute --variant anchored --config R > "$EVIDENCE/05-p1-anchored-R-evaluate.jsonl"
   python3 p1.py ruleset --execute --variant anchored --config R --mode active
   python3 p1.py read    --execute --variant anchored --config R > "$EVIDENCE/05-p1-anchored-R-active.jsonl"
   ```

   `prs` makes a base branch `p1-base/<variant>-<config>` at `main`'s tip and
   one pull request per file into it: `go.mod`, `x/go.mod`, `README.md`,
   `.github/workflows/a.yml`, `.gitignore`, `sub/dir/f.txt`. `ruleset` makes
   a ruleset that holds that base branch only.
3. No step lowers or removes anything. Each configuration has its own base
   branch and its own ruleset, created in `evaluate` and then switched to
   `active`, which is a tightening. So the delegate runs every step, and the
   script refuses any other change of mode. In `evaluate` mode nothing is
   enforced and every pull request should read CLEAN; insights are logged
   only on a merge attempt, so expect the rule suites to be empty.
4. **L1 round trip** (review finding M7): with the variant P1 chose written
   into factory-controls' `rulesets/svc-hello/l1.json` and `l2.json` (and
   their hashes), `python3 p1.py l1-roundtrip --execute` creates `l1.json` on
   `m4-probe` with its name and its target branch changed (a branch of its
   own, so `main` is never held), reads it back, and prints
   `factory.ruleset.diff(normalize(l1), normalize(GET))`. Expected: `[]`.
   Anything else is a field GitHub adds, reorders or reshapes that the
   detector would report as a standing difference; it is corrected in
   `l1.json`, `l2.json` and both hashes before the class file's first
   commit, never after. `~DEFAULT_BRANCH`, the one value not tried here,
   already round-trips on `svc-hello`'s live ruleset.
5. **Ratchet rehearsal** (review finding ADV-08): `python3 p1.py
   ratchet-rehearsal --execute` creates a second ruleset equal to `l2.json`
   (rung entry 0) on another branch of its own, runs
   `factory.ratchet.plan_raise` on the live body, prints the plan (expected
   `write`, one field 0 -> 1), sends the ratchet's own PUT body with the
   delegate's token (a tightening), reads it back and prints its diff
   against `l1.json` (expected `[]`). This tries the body the ratchet sends;
   the ratchet App's own `administration` write is first exercised at C-18's
   injection (a), which the runbook states (`docs/RUNBOOK.md`, step 8).
6. `python3 p1.py cleanup --execute` closes P1's pull requests. Its rulesets
   stay: none holds `main`, and they go with the repository at the close.

Every `--execute` line is also printed; run each with `--dry-run` first to
see the calls.

## Expected (the readiness note)

In `active` mode, anchored variant:

| File | config W (wall 1, rung 0) | config R (wall 0, rung 1) | Entry |
|---|---|---|---|
| `go.mod` | CLEAN | BLOCKED | rung only |
| `x/go.mod` | BLOCKED | CLEAN | wall |
| `README.md` | BLOCKED | CLEAN | wall |
| `.github/workflows/a.yml` | BLOCKED | CLEAN | wall |
| `.gitignore` | BLOCKED | CLEAN | wall |
| `sub/dir/f.txt` | BLOCKED | CLEAN | wall |

The unanchored variant is informative: under gitignore rules a bare `go.mod`
matches at any depth, so `x/go.mod` would read CLEAN in W and BLOCKED in R.

## Result table (fill from the reads)

| Variant | Mode | Config | go.mod | x/go.mod | README.md | .github/workflows/a.yml | .gitignore | sub/dir/f.txt | Two entries for one team accepted? |
|---|---|---|---|---|---|---|---|---|---|
| anchored | evaluate | R | | | | | | | |
| anchored | evaluate | W | | | | | | | |
| anchored | active | R | | | | | | | |
| anchored | active | W | | | | | | | |
| unanchored | active | R | | | | | | | |
| unanchored | active | W | | | | | | | |

Each cell: `mergeStateStatus` / `reviewDecision` / teams requested.

| Step | Expected | Observed (diff printed) | As expected? |
|---|---|---|---|
| L1 round trip: `diff(l1, GET)` | `[]` | | |
| Ratchet rehearsal: plan | `write`, rung entry 0 -> 1 only | | |
| Ratchet rehearsal: `diff(l1, GET after the PUT)` | `[]` | | |

## Fallbacks (fixed by the note)

- One ruleset refuses two entries for one team (a 422 on create): **two
  rulesets**, one entry each.
- Anchoring not supported: **the unanchored form**, and C-15 records that
  nested module files are held by `class-scope` alone.
- Negation does not un-match: the wall becomes **one pattern per top-level
  entry of `svc-hello`'s tree** other than `go.mod` and `go.sum` (files by
  name, directories as `dir/**`), with no catch-all, and C-15 records that the
  entry could not say "everything except" and that a new root file is held
  by `class-scope` alone.
- `*` does not match nested paths or dotfiles: the wall adds whichever of
  `**/*`, `.*` and `**/.*` the `dotfiles` variant shows to match, before the
  negations.

A fallback changes `rulesets/svc-hello/l1.json` and `l2.json` before the class
file's first commit: regenerate both, run `python3 scripts/ruleset_hashes.py`
in `factory-controls`, and update `tests/test_ruleset.py`. A result with no
fallback here is settled by a dated correction in the claims register before
the step that rests on it.

## Read commands, by hand

```sh
gh api graphql -f query='query($n:Int!){repository(owner:"platform-factory",name:"m4-probe"){pullRequest(number:$n){reviewDecision mergeStateStatus reviewRequests(first:10){nodes{requestedReviewer{... on Team{slug}}}}}}}' -F n=<pr>
gh api "repos/platform-factory/m4-probe/rulesets/rule-suites?time_period=day"
gh api repos/platform-factory/m4-probe/rulesets
```
