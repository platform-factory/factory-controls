#!/bin/sh
# Run the model once, headless, on the workspace the prepare job built.
#
# Usage: run-model.sh INPUT_DIR OUTPUT_DIR META_DIR
#
# Why it looks like this (ADR-0024 §7): the model's job holds one secret, the
# model credential, and no GitHub credential. Its workspace is svc-hello's
# go.mod, go.sum and .go files at main's tip; its instructions are the pinned
# skill (appended to the system prompt) and the prompt the prepare job wrote
# from the queue item. Its tools are Read, Write and Bash, and a PreToolUse
# hook (agenttool) narrows them to reading the workspace, writing summary.md,
# and running `go get <module>@<version>` and `go mod tidy`. It runs in the
# pinned builder image with GOTOOLCHAIN=local (set by the workflow), so the
# go.mod and go.sum it makes are made the way class-scope recomputes them.
#
# OUTPUT_DIR receives exactly go.mod, go.sum and summary.md. META_DIR
# receives the token and time counts (usage.txt) and the permission denials,
# which are not part of the artifact.
#
# The model's own final message is never printed: the log shows only counts,
# denials and the exit status, the same lines for every queue item.
set -eu

# Absolute paths: the script changes into the workspace before it writes.
mkdir -p "$2" "$3"
IN=$(cd "$1" && pwd)
OUT=$(cd "$2" && pwd)
META=$(cd "$3" && pwd)
CLAUDE=${CLAUDE_BIN:-claude}
AGENTTOOL=${AGENTTOOL:-/usr/local/bin/agenttool}
WORK=${AGENT_WORK:-/work}
WS="$WORK/svc"

rm -rf "$WORK"
mkdir -p "$WS" "$WORK/home"
cp -R "$IN/svc/." "$WS/"

# Claude Code reads settings and memory from $HOME; an empty home means only
# the settings file below applies.
export HOME="$WORK/home"
export AGENT_WORKSPACE="$WS"
export DISABLE_AUTOUPDATER=1 DISABLE_UPDATES=1 DISABLE_TELEMETRY=1
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
export CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 CLAUDE_CODE_SKIP_PROMPT_HISTORY=1
# The model credential never sits in an environment a tool can inherit:
# Claude Code reads it through apiKeyHelper from a file outside the
# workspace (the hook refuses reads outside it), and the variable is unset
# before Claude Code starts, so `go`, the hook and anything else Claude Code
# runs never see it. (CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1 would do the same,
# but Claude Code 2.1.322 then refuses to start without bubblewrap, which
# cannot create namespaces in an unprivileged container job.)
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "::error::ANTHROPIC_API_KEY is not set"
  exit 1
fi
mkdir -p "$WORK/secret"
chmod 0700 "$WORK/secret"
trap 'rm -rf "$WORK/secret"' EXIT
(umask 077 && printf '%s' "$ANTHROPIC_API_KEY" > "$WORK/secret/key")
unset ANTHROPIC_API_KEY
# Alpine: use the image's ripgrep, as Claude Code's setup docs say for musl.
export USE_BUILTIN_RIPGREP=0
# The Bash tool runs commands in bash or zsh only; the install step adds bash.
export CLAUDE_CODE_SHELL=/bin/bash SHELL=/bin/bash
if [ ! -x /bin/bash ]; then
  echo "::error::/bin/bash is missing: the Bash tool cannot run go get or go mod tidy"
  exit 1
fi

cat > "$WORK/settings.json" <<EOF
{
  "permissions": {
    "allow": ["Bash(go get *)", "Bash(go mod tidy)", "Write"],
    "deny": ["WebFetch", "WebSearch", "Edit", "NotebookEdit"],
    "defaultMode": "dontAsk"
  },
  "hooks": {
    "PreToolUse": [
      {"matcher": "*", "hooks": [{"type": "command", "command": "$AGENTTOOL hook"}]}
    ]
  },
  "apiKeyHelper": "cat $WORK/secret/key",
  "env": {"USE_BUILTIN_RIPGREP": "0"}
}
EOF

MODEL=$(cat "$IN/model.txt")
PROMPT=$(cat "$IN/prompt.md")

cd "$WS"
set +e
"$CLAUDE" -p "$PROMPT" \
  --model "$MODEL" \
  --tools "Read,Write,Bash" \
  --allowedTools "Bash(go get *),Bash(go mod tidy),Write" \
  --disallowedTools "WebFetch,WebSearch,mcp__*" \
  --permission-mode dontAsk \
  --settings "$WORK/settings.json" \
  --strict-mcp-config \
  --no-session-persistence \
  --append-system-prompt-file "$IN/skill/SKILL.md" \
  --output-format json \
  --max-turns 40 \
  > "$WORK/result.json" 2> "$WORK/stderr.log"
rc=$?
set -e
echo "claude exit status: $rc"
tail -n 20 "$WORK/stderr.log" | sed 's/^/claude stderr: /' || true

if [ -s "$WORK/result.json" ]; then
  "$AGENTTOOL" usage "$WORK/result.json" > "$META/usage.txt" || true
  "$AGENTTOOL" denials "$WORK/result.json" > "$META/denials.txt" || true
  cat "$META/usage.txt" "$META/denials.txt"
fi
if [ "$rc" -ne 0 ]; then
  echo "::error::the model run failed (exit $rc)"
  exit 1
fi
if grep -q '^is_error=true' "$META/usage.txt" 2>/dev/null; then
  echo "::error::the model run ended in an error result"
  exit 1
fi

rm -rf "$WORK/secret"

# Nothing but go.mod, go.sum and summary.md may differ from the input: the
# hook allows no other write, and this checks that it held.
(cd "$IN/svc" && find . -type f ! -name go.mod ! -name go.sum) | while read -r f; do
  if ! cmp -s "$IN/svc/$f" "$WS/$f"; then
    echo "::error::the workspace file $f changed during the model run"
    exit 1
  fi
done

if cmp -s "$IN/svc/go.mod" "$WS/go.mod"; then
  echo "::error::go.mod is unchanged: the bump did not happen"
  exit 1
fi

for f in go.mod go.sum summary.md; do
  if [ ! -f "$WS/$f" ] || [ -L "$WS/$f" ]; then
    echo "::error::the model run left no regular file $f"
    exit 1
  fi
  cp "$WS/$f" "$OUT/$f"
done
echo "output: $(cd "$OUT" && find . -type f | sort | tr '\n' ' ')"
