# The agent's workflow

How the Go-module bump class gets written: `.github/workflows/agent.yml`,
`factory/prepare.py`, `agent/`, `factory/open_pr.py`, `factory/seeds.py`
and `factory/reveal.py`.

## Why it is built this way

The class's writer is an LLM agent (Claude Code, run headless), and the
ladder must not depend on it behaving well. ADR-0024 §7 therefore splits
the work so that the model holds nothing worth taking except its own
credential:

- the model's job has `permissions: {}`, no GitHub token, no checkout
  credential and one secret, the model credential;
- everything it needs is fetched before it runs, by a job with no secret,
  from public repositories, and handed over as an artifact;
- what it produces is exactly three files, taken by another job that
  refuses any other name and is the only place the agent App's key writes.

Every number the workflow uses (the queue, the release age, the model, the
builder image, the Claude Code version and checksum, the skill pin) comes
from the class file, which the agent cannot reach.

## The mental model

One run, at minute 23 or 53, does one of three things: it acts on the open
agent pull request (merge it, close it, update it, or wait), or, when none
is open, it opens the next queue item, or it does nothing because the next
item is too young or the queue is done. Runs never overlap.

| job | environment | holds | does |
|---|---|---|---|
| `pre-merge` | `ratchet` | ratchet App key | detector checks; demotes first; ok or not |
| `merge` | `agent-app` | agent App key, SEED_KEY | merge, close, update or wait; then settles a decided seed |
| `prepare` | none | read-only `GITHUB_TOKEN` | next item, release age, the model's workspace |
| `agent` | `agent-model` | model credential only | Claude Code in the builder image; out come go.mod, go.sum and summary.md, the summary padded and encrypted |
| `open` | `agent-app` | agent App key, SEED_KEY, SUMMARY_KEY | intake (decrypts the summary), commit, pull request, run record |
| `c21-record` | none | `GITHUB_TOKEN` (contents, issues) | only on a dispatch with `c21_model_job`: the record of C-21's model-job attempts |
| `scorecard` | none (reusable `scorecard.yml`) | `GITHUB_TOKEN` | after a merge, and on the first run after the latest merge's escape window closes until a scorecard has read GitHub since that close |

## The model's job, in detail

**Image and toolchain.** The job is a container job in the class file's
builder image (`golang:1.27.1-alpine3.24`, pinned by digest), the image
`class-scope` recomputes in and `go-test` runs in, with `GOTOOLCHAIN=local`,
`GOFLAGS` empty and the public proxy and sum database. The local dry run
showed its output equal to `class-scope`'s recompute byte for byte.

**Claude Code** is downloaded by version from Anthropic's release bucket
and checked against the SHA-256 in the class file (`agent.harness`). That
checksum is the one the release's `manifest.json` lists for `linux-x64-musl`;
the manifest's detached GPG signature was checked against the release
signing key (fingerprint `31DD DE24 DDFA B679 F42D 7BD2 BAA9 29FF 1A7E CACE`,
as Claude Code's setup page gives it) when the pin was written. Alpine needs
`libgcc`, `libstdc++` and `ripgrep` (setup page), and `bash`: the Bash tool
runs commands only in bash or zsh (`CLAUDE_CODE_SHELL`), and the image has
busybox `sh` alone.

**The command line** (`agent/run-model.sh`), each flag checked against
Claude Code's CLI reference and headless page on 2026-11-11:

```
claude -p "$PROMPT" --model "$MODEL" \
  --tools "Read,Write,Bash" \
  --allowedTools "Bash(go get *),Bash(go mod tidy),Write" \
  --disallowedTools "WebFetch,WebSearch,mcp__*" \
  --permission-mode dontAsk --settings "$WORK/settings.json" \
  --strict-mcp-config --no-session-persistence \
  --append-system-prompt-file "$IN/skill/SKILL.md" \
  --output-format json --max-turns 40
```

- `--tools` limits the built-in tools Claude has; `--allowedTools` only
  pre-approves calls. `dontAsk` denies every call that would otherwise
  prompt.
- The documentation says a built-in set of read-only Bash commands (`cat`,
  `ls`, `grep`, `find`, ...) runs in every permission mode without an allow
  rule. So a PreToolUse hook (`agent/agenttool`, Go, standard library)
  sees every tool call and blocks, by exiting 2, anything but a Read under
  the workspace, a Write of `summary.md`, and `go get <module>@<version>` or
  `go mod tidy` with no shell syntax. In the dry run it refused `cat`, a
  compound `go get ... && env`, `curl` and a write to `main.go`, and Claude
  Code itself refused a read of `/proc/self/environ`.
- The skill is appended to the system prompt; the prompt (`agent/prompt.md`,
  filled by `prepare`) names the queue item and quotes the upstream data in
  `<upstream-data>` blocks the data cannot close.
- The model credential is never in an environment a tool can inherit:
  `run-model.sh` writes it to a file outside the workspace, Claude Code
  reads it through `apiKeyHelper`, and `ANTHROPIC_API_KEY` is unset before
  Claude Code starts. `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` was the first
  choice; Claude Code 2.1.322 then refuses to start without bubblewrap, and
  bubblewrap cannot create namespaces in an unprivileged container job.
- `HOME` is an empty directory, so no user settings, memory or hooks other
  than the settings file apply; auto-update, telemetry and other
  non-essential traffic are off.

**Token usage** is captured from the `--output-format json` result: input,
output, cache-read and cache-creation tokens summed over `modelUsage`
(whole run, subagents included; the result's top-level `usage` covers only
the main loop), `num_turns`, `duration_ms` as `agent_seconds`, and
`total_cost_usd`, which is Claude Code's client-side estimate. They become
the agent job's outputs and the run record's `usage`. The model's final
message is never printed or kept.

## Seeds without a tell

A seed is the first queue-item pull request opened after the k-th merge,
for four drawn k (`factory/seeds.py`, the class file's `seeds.rule`), at L1
only: a drawn position reached while the live rung entry is 0 has no seed
(the run record's `rung_at_open`; the scorecard lists it under
`skipped_at_l2_positions`). Until
the listed human decides on it, nothing public may tell it from a real
item. So:

- the model runs on the seed exactly as on any item; only the opening job
  knows, and it replaces the summary with the drawn misreport after
  computing every misreport for every item;
- the run record has the same fields and sizes for both; the agent's own
  words are only in `sealed_summary`;
- the model's own summary is never public: after the model exits, the job
  pads summary.md to one fixed size (`agenttool pad`) and encrypts it with
  `openssl cms` (RSA-OAEP to `agent/summary-recipient.pem`, AES-256) before
  the artifact is uploaded, and only the `open` job holds the private key
  (`SUMMARY_KEY`). The artifact is public while it exists and its upload
  step logs its size and digest; a fixed-size ciphertext says nothing, so it
  cannot be compared with a seed's pull request body (ADV-09). The artifact
  is still deleted before the pull request opens. Checked locally: two
  summaries of 5 and 8000 bytes both seal to 8752 bytes with OpenSSL 3.5 in
  the builder image, and `factory/open_pr.py` opens both;
- the merge step and the settlement make the same reads for every open
  queue-item pull request, and a failed read ends the step with one message
  whichever pull request it was for, so neither a traceback nor a step's
  duration shows the seed-only branch (ADV-11);
- the merge step reads, compares, updates and waits on a seed exactly as on
  an item. While a seed can be open (the rung is L1 and no more than 18
  items have merged), nothing goes to the merge API without the listed
  human's approval on the head, seed or not: the latest decision is an
  approval, on the head commit, and submitted no earlier than
  `class-scope`'s first run on that commit. The time is there because
  GitHub keeps an approval across an empty commit or an update from main
  and re-points it to the new head (probe P3), so the commit alone does not
  show the approval was made on it. Once the listed human has
  decided, the merge step reports `wait-seed`, and the settlement step
  writes `seeds/<pr>.json`, labels the pull request `seeded` and closes it.

A consequence: C-15's L1 case "an on-class agent pull request with no
approval (refused by the merge API)" is not produced by the merge step
before the 19th merge. The fixtures workflow can make it on queue item 1,
which is never a seed (the lowest position is 2).

At the close, `reveal.yml` (dispatch only, typed confirmation, agent
workflow disabled) publishes the key to `close/seed-key.json` and checks
every pull request body against its sealed summary.

## Running it locally

```
python3 -m unittest discover -s tests            # offline
python3 -m factory.prepare --out /tmp/ws --skill-git-url file://<clone with the tag> \
  --allow-placeholders    # with CONTROLS_TOKEN, SKILL_TAG, SKILL_COMMIT; reads public state only
python3 -m factory.open_pr --artifact-dir <dir> --dry-run --allow-placeholders --plain-summary
python3 -m factory.seeds --print-key-hash        # with SEED_KEY; prints only its SHA-256
```

Before the class file's first commit there is no `classes/go-module-bump.yaml`;
`--allow-placeholders` dry runs read `templates/go-module-bump.yaml` instead,
and every other run does nothing (one notice line, exit 0).

**Probe P4** runs this workflow's P4 form, `agent-p4.yml`, against
`m4-probe`, with `RECORDS_BRANCH=records-p4` and
`CLASS_FILE=probe/p4-class.yaml`: a copy of the class file whose `repo` is
`platform-factory/m4-probe`. The copy is outside `classes/`, so it is not a
class file and the `classes/**` approval does not apply to it; it is added
with `agent-p4.yml` by one pull request and removed after P4. A
`CLASS_FILE` that names a missing file is an error, not an absent class
file, so a P4 run without its copy fails rather than doing nothing.

`go test` for the hook runs inside the builder image (the workflow runs it
before every model run).
