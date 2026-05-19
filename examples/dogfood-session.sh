#!/usr/bin/env bash
set -euo pipefail

AGENTDIR_MIN_PYTHON_MAJOR=3
AGENTDIR_MIN_PYTHON_MINOR=11

python_is_supported() {
  local python_cmd="$1"
  "$python_cmd" - "$AGENTDIR_MIN_PYTHON_MAJOR" "$AGENTDIR_MIN_PYTHON_MINOR" <<'PY'
import sys

required = (int(sys.argv[1]), int(sys.argv[2]))
raise SystemExit(0 if sys.version_info[:2] >= required else 1)
PY
}

find_python() {
  if [ -n "${AGENTDIR_PYTHON:-}" ]; then
    if ! command -v "$AGENTDIR_PYTHON" >/dev/null 2>&1; then
      printf 'agentdir dogfood error: AGENTDIR_PYTHON not found: %s\n' "$AGENTDIR_PYTHON" >&2
      exit 1
    fi
    if ! python_is_supported "$AGENTDIR_PYTHON"; then
      printf 'agentdir dogfood error: AGENTDIR_PYTHON must be Python 3.11 or newer\n' >&2
      exit 1
    fi
    command -v "$AGENTDIR_PYTHON"
    return
  fi

  local candidate
  for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && python_is_supported "$candidate"; then
      command -v "$candidate"
      return
    fi
  done

  printf 'agentdir dogfood error: missing Python 3.11 or newer; set AGENTDIR_PYTHON=/path/to/python3.11+\n' >&2
  exit 1
}

if command -v agentdir >/dev/null 2>&1; then
  AGENTDIR=(agentdir)
else
  PYTHON_CMD="$(find_python)"
  export PYTHONPATH="$(pwd)/src${PYTHONPATH:+:$PYTHONPATH}"
  AGENTDIR=("$PYTHON_CMD" -m agentdir)
fi

KEEP_WORKDIR="${KEEP_WORKDIR:-0}"
SESSION_ID="${SESSION_ID:-dogfood-$(date -u +%Y%m%dT%H%M%SZ)}"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/agentdir-demo.XXXXXX")"
ROOT="${1:-$WORKDIR/root}"
BODY_DIR="$WORKDIR/bodies"

cleanup() {
  if [ "$KEEP_WORKDIR" = "1" ]; then
    return
  fi

  if [ -d "$WORKDIR" ]; then
    rm -rf "$WORKDIR"
  fi
}

log() {
  printf '\n[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"
}

run() {
  log "$*"
  "$@"
}

trap cleanup EXIT

mkdir -p "$(dirname "$ROOT")" "$BODY_DIR"

cat <<'EOF' >"$BODY_DIR/session-started.txt"
AgentDir dogfood session started.
Workspace: ./demo-workspace
Goal: prove that raw envelopes can outlive the index.
EOF

cat <<'EOF' >"$BODY_DIR/user-message.txt"
Please inspect the failing test, update the docs, and show the exact evidence used.
EOF

cat <<'EOF' >"$BODY_DIR/tool-call.txt"
tool=pytest
argv=python -m pytest tests/test_cli.py -q
cwd=/workspace/demo-workspace
EOF

cat <<'EOF' >"$BODY_DIR/tool-result.txt"
exit_code=0
duration_ms=842
stdout:
1 passed in 0.84s
EOF

cat <<'EOF' >"$BODY_DIR/file-diff.txt"
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@
-Old sentence
+New sentence with clarified recovery guidance
EOF

cat <<'EOF' >"$BODY_DIR/agent-message.txt"
I updated the docs, kept the diff narrow, and verified the test command before replying.
EOF

cat <<'EOF' >"$BODY_DIR/session-ended.txt"
AgentDir dogfood session ended cleanly.
Verification:
- query succeeded
- replay succeeded after index rebuild
- doctor reported a healthy root
EOF

run "${AGENTDIR[@]}" init "$ROOT"

run "${AGENTDIR[@]}" emit --root "$ROOT" --session "$SESSION_ID" --type session.started --body "$BODY_DIR/session-started.txt"
run "${AGENTDIR[@]}" emit --root "$ROOT" --session "$SESSION_ID" --type user.message --body "$BODY_DIR/user-message.txt"
run "${AGENTDIR[@]}" emit --root "$ROOT" --session "$SESSION_ID" --type tool.call --body "$BODY_DIR/tool-call.txt"
run "${AGENTDIR[@]}" emit --root "$ROOT" --session "$SESSION_ID" --type tool.result --body "$BODY_DIR/tool-result.txt"
run "${AGENTDIR[@]}" emit --root "$ROOT" --session "$SESSION_ID" --type file.diff --body "$BODY_DIR/file-diff.txt"
run "${AGENTDIR[@]}" emit --root "$ROOT" --session "$SESSION_ID" --type agent.message --body "$BODY_DIR/agent-message.txt"
run "${AGENTDIR[@]}" emit --root "$ROOT" --session "$SESSION_ID" --type session.ended --body "$BODY_DIR/session-ended.txt"

run "${AGENTDIR[@]}" index rebuild --root "$ROOT"
run "${AGENTDIR[@]}" query --root "$ROOT" --session "$SESSION_ID"

run rm -f "$ROOT/indexes/agentdir.sqlite3"
run "${AGENTDIR[@]}" index rebuild --root "$ROOT"
run "${AGENTDIR[@]}" replay --root "$ROOT" --session "$SESSION_ID"
run "${AGENTDIR[@]}" doctor --root "$ROOT"

log "Demo session id: $SESSION_ID"
log "AgentDir root: $ROOT"

if [ "$KEEP_WORKDIR" != "1" ]; then
  log "Set KEEP_WORKDIR=1 to preserve the generated root for manual inspection."
fi
