# factory-controls

The machinery that holds one change class on its rung of the autonomy
ladder, and returns it to the bottom rung when it escapes.

This repository belongs to the reference implementation of the **Platform
Factory** pattern. The design seed — pattern docs, ADRs, and the build plan —
lives at
[https://github.com/platform-factory/platform-factory-concept](https://github.com/platform-factory/platform-factory-concept).

Platform Factory was designed and written by **Ronak Patel**
([thecloudgeek LLC](https://github.com/thecloudgeek)). Licensed Apache-2.0 —
the attribution to keep is in [NOTICE](NOTICE), and
[CITATION.cff](CITATION.cff) says how to cite it.

## Why this repository exists

Milestone M4 of the platform-factory build tests one idea from ADR-0007: a
change class earns merge rights from its own track record and loses them
automatically. M4 runs it on one class, a Go-module bump on `svc-hello`,
written by an LLM agent through a GitHub App, on 25 replayed upstream
releases (ADR-0024).

GitHub can hold most of the boundary itself. A ruleset on `svc-hello` says
how many approvals a change to `go.mod` and `go.sum` needs, and a required
check, `class-scope`, decides from the default branch whether a pull request
is one on-class change. What a ruleset has no field for has to live
somewhere else:

- the frozen rule: how many clean merges in a row earn a promotion (N = 20),
  the queue of releases, what resets the streak;
- the detector that notices an escape after a merge, and the ratchet that
  raises the approval count again before anyone has said why;
- the merge step, which asks GitHub to merge and lets the ruleset decide;
- the records all of that writes.

They live here, in a repository no `svc-hello` code can write and on which
neither App is installed, so nothing the agent produces can edit the rules
it is judged by. Every loosening is a person's act (a promotion is a pull
request here that the listed human approves, then a hand edit by the owner);
every tightening may be a machine's.

## The mental model

- **A rung is one number.** In `svc-hello`'s ruleset, the required-reviewers
  entry for team `platform` on `/go.mod` and `/go.sum` (the *rung entry*)
  has `minimum_approvals` 1 at L1 (a person approves before any merge) and 0
  at L2 (the team is told; the agent may merge). A second entry, the *wall
  entry*, keeps every other path at one approval on every rung.
  `rulesets/svc-hello/l1.json` and `l2.json` are the whole ruleset at each
  rung, and differ in that one field.
- **The class file freezes the rules.** `classes/go-module-bump.yaml` holds
  every number the M4 readiness note fixed, and the hashes of the two
  ruleset files. It changes only by a pull request the listed human
  approves. Its first commit is that pull request, filled: the repository
  is created without it, and until then only its template exists
  (`templates/go-module-bump.yaml`, four placeholders), which the same pull
  request deletes. Nothing runs the class before it lands.
- **State is recomputed, never stored.** The streak, the next queue item and
  the open agent pull request are derived each time from GitHub's record and
  the records branch (`factory/state.py`). A dropped run loses nothing, and a
  reader can recompute any of them.
- **Identity comes from records, never from labels.** A queue-item pull
  request is one in a run record; a seed is computed from a secret key; a
  label counts only from the listed human's own label event.
- **The ratchet only raises.** Its GitHub permission could lower an entry or
  delete the ruleset; its code can do one thing: set the rung entry from 0 to
  1, after checking that this is the only field the write changes.

## How it works

**The detector** (`factory/detector.py`, `.github/workflows/detector.yml`)
runs every ten minutes. It looks for two kinds of escape:

- (a) a merged queue-item pull request whose merge commit has no `push` run
  of `go-test` whose *first* attempt succeeded within 30 minutes of the
  merge. Failed, cancelled and missing runs all count; a re-run never clears
  one, because only attempt 1 is read.
- (b) a merged pull request that reverts a class merge (its `go.mod` and
  `go.sum` equal those at the parent of the latest class merge) and carries
  `escape:revert` from the listed human.

It acts once per event, only on events after the latest merged promotion or
fixture record: the ratchet raises the rung entry to 1, every open agent
pull request gets `needs-human`, and an entry goes to the ledger. It also
compares the live ruleset with `l1.json`, ignoring the ids and timestamps
GitHub assigns. L2 is accepted only after a merged promotion or fixture
record, until the next demotion. Any other difference raises the rung,
opens an issue here, and blocks merges while it stands. Each complete run
writes a heartbeat.

**The merge step** (`factory/merge_step.py`) is the first two jobs of each
agent-workflow run. `pre-merge` runs the detector's checks (demoting first
if an escape is unhandled) and refuses while `main`'s latest push run of
`go-test` has not succeeded, while a ruleset difference stands, or while the
last complete detector run is more than 30 minutes old. Scheduled runs can
be dropped; this is why a merge cannot land between an escape and its
freeze. `merge` acts on the one open agent pull request: it updates one left
behind `main`, closes one whose `class-scope` or `go-test` concluded fail (a
near-miss, read from the check's step names, never from a label), and
otherwise calls the merge API with the head commit `class-scope` verified,
squash, and a fixed title and message. GitHub's answer is recorded; a 405 is
a refusal, not an error. It never merges or closes a seed; until the listed
human has decided on one, it treats the seed exactly as a real item, so its
public output gives nothing away.

**The agent's workflow** (`.github/workflows/agent.yml`, `docs/AGENT.md`)
runs at minutes 23 and 53: the merge step's two jobs, then, only when no
agent pull request is open and the next item's release is at least seven
days old, `prepare` (the workspace, fetched with no secret), `agent` (Claude
Code headless in the builder image, with `permissions: {}` and the model
credential only; Read, Write and Bash narrowed by a hook to the workspace,
`summary.md`, `go get` and `go mod tidy`; the summary leaves the job only
padded and encrypted to the open job's key) and `open` (exactly `go.mod`,
`go.sum` and `summary.md.cms`, decrypted here; a one-hour agent App token;
GraphQL `createCommitOnBranch`; the pull request; the run record), and, after
a merge, the scorecard. At four positions
drawn from a secret key (`factory/seeds.py`) the opening job replaces the
agent's summary with a misreport; after the listed human's decision the
seed is recorded, labelled `seeded` and closed, and `reveal.yml` publishes
the key at the close.

**The records branch** holds one JSON file per fact. Its layout and every
schema are in [docs/RECORDS.md](docs/RECORDS.md).

## Layout

```
classes/go-module-bump.yaml   the class file (frozen numbers); absent until its first, filled commit
templates/go-module-bump.yaml the class file's template, four placeholders; deleted by that commit
classes/records/              promotion and fixture records, merged by the listed human
rulesets/svc-hello/           l1.json and l2.json, the ruleset bodies as PUT
scripts/ruleset_hashes.py     prints the two hashes for the class file (--check compares)
factory/                      Python, standard library only
  gh.py                       REST and GraphQL client: retries, pagination, no global state
  classfile.py                load and validate the class file
  ruleset.py                  normal form and diff of rulesets; the rung entry
  records.py                  the records branch: one snapshot per process, writes by the contents API
  state.py                    the class's state, derived from GitHub and the records
  detector.py                 escapes (a) and (b), the ruleset comparison, the ledger
  ratchet.py                  the only ruleset writer: rung entry 0 -> 1
  merge_step.py               --phase pre-merge | merge
  prepare.py                  next item, release age, the model's workspace
  seeds.py                    the HMAC seed schedule and the six misreports
  open_pr.py                  artifact intake, commit, pull request, run record; seed settlement
  reveal.py                   publishes SEED_KEY at the close and checks every body
  guard.py                    stops a workflow run while the class file is absent or holds a placeholder
agent/                        shipped into the model's job: prompt, run script, the hook (Go)
.github/workflows/detector.yml
.github/workflows/agent.yml   pre-merge, merge, prepare, agent, open
.github/workflows/reveal.yml  dispatch only, at the close
tests/                        offline unit tests over hand-written API JSON
docs/RECORDS.md               the records branch, file by file
docs/AGENT.md                 the agent's workflow, the model's job, seeds without a tell
```

The scorecard and the fixtures are built beside these, against the same
interfaces.

## Running it

```
python3 -m unittest discover -s tests          # offline; no token, no network
python3 scripts/ruleset_hashes.py --check      # class file agrees with l1/l2.json
docker run --rm -v "$PWD":/repo -w /repo rhysd/actionlint:latest -color
```

A read-only run against the live repositories, which sends no write and
prints the writes it would have made:

```
SVC_TOKEN=... CONTROLS_TOKEN=... python3 -m factory.detector --dry-run --allow-placeholders
SVC_TOKEN=... CONTROLS_TOKEN=... python3 -m factory.merge_step --phase pre-merge --dry-run --allow-placeholders
```

Without `--allow-placeholders`, both do nothing while there is no class
file or it holds the `LISTED-HUMAN-LOGIN` placeholder: one notice line,
exit 0, no request. With it, before the class file's first commit, they
read the template. Every CLI a workflow runs behaves the same way, and
every workflow that runs the class (the agent's, the detector's, the
scorecard's, the fixtures' and the reveal's) goes no further than a first
job, `guard`, while the class file is absent or holds any placeholder: it
holds no secret, writes nothing, and ends green with one notice line
(`factory/guard.py`).

## Not yet tried on GitHub

Each of these is read from documentation and marked so until a probe or the
first run settles it:

- Which permissions the two Apps need for their *reads*. The endpoint
  permission data lists workflow runs and jobs under `actions: read`, check
  runs under `checks: read`, and file contents and commit comparison under
  `contents: read`. The ratchet App as ADR-0024 §7 describes it holds
  `administration` and `pull_requests` only, and the agent App `contents`
  and `pull_requests`. Whether an installation token reads those endpoints on
  a public repository without the permission is not settled. If not, the
  ratchet App needs `actions: read` and `contents: read`, and the agent App
  `actions: read` and `checks: read`; none of these can write.
- That the `class-scope` check run, from `pull_request_target`, attaches to
  the pull request's head commit (probe P2); and that a check run's
  `details_url` names its job, which is how the job's steps are found.
- That a label removed by `class-scope` on a new head shows as an
  `unlabeled` event performed by `github-actions`.
- That the model's job runs as built on a hosted runner: a container job
  in the Alpine builder image with `permissions: {}`, where the artifact
  actions use the run's own artifact token rather than `GITHUB_TOKEN`
  (download-artifact's README: same-run downloads need no `github-token`),
  and where JavaScript actions run in an Alpine container (probe P4). The
  local dry run ran the job's scripts verbatim in the builder image on
  arm64 with the arm64 build of the pinned Claude Code release; the amd64
  build's install step ran too, but Go under amd64 emulation crashes on
  the build host, so the full amd64 run is P4's.
- That the run record is on the records branch before `class-scope`'s
  first run reads it for the packet's "opened by" line; it is written right
  after the pull request opens, and class-scope re-runs on later events.
- In a public repository, scheduled workflows stop after 60 days without
  activity, so a quiet stretch would stop the detector; the merge step's
  heartbeat check then refuses every merge.
