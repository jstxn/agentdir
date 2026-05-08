#!/usr/bin/env bash
set -euo pipefail

if command -v agentdir >/dev/null 2>&1; then
  AGENTDIR=(agentdir)
else
  export PYTHONPATH="$(pwd)/src${PYTHONPATH:+:$PYTHONPATH}"
  AGENTDIR=(python3 -m agentdir)
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
