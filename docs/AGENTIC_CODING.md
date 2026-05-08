# Agentic Coding With AgentDir

AgentDir is built for engineers who want agentic coding sessions to leave durable local evidence. It gives agents a filesystem-native place to record what happened without requiring a server.

## What To Record

Use one immutable envelope per meaningful unit of work:

- original user request
- plan or task split
- tool call
- tool result
- changed-file summary
- patch or diff artifact
- test or lint evidence
- review comment
- approval request
- final handoff

The index is disposable. The envelope store is the recovery source.

## Session Flight Recorder

```bash
session="repo-$(basename "$PWD")-$(date -u +%Y%m%dT%H%M%SZ)"

agentdir init --scope project

printf 'User asked for a narrow bug fix in the current repo.\n' > /tmp/user-message.txt
agentdir emit \
  --session "$session" \
  --type user.message \
  --workspace "$(basename "$PWD")" \
  --git-head "$(git rev-parse --short HEAD 2>/dev/null || true)" \
  --body /tmp/user-message.txt
```

## Capturing Tool Calls And Results

```bash
cat > /tmp/tool-call.txt <<'EOF'
tool=pytest
argv=python -m pytest -q
EOF

agentdir emit --session "$session" --type tool.call --tool pytest --body /tmp/tool-call.txt

python -m pytest -q > /tmp/tool-result.txt 2>&1
status=$?

agentdir emit \
  --session "$session" \
  --type tool.result \
  --tool pytest \
  --tool-exit-code "$status" \
  --body /tmp/tool-result.txt
```

## Capturing Diffs As Artifacts

```bash
git diff > /tmp/agentdir.diff

agentdir emit \
  --session "$session" \
  --type file.diff \
  --artifact /tmp/agentdir.diff \
  --body /tmp/agentdir.diff
```

The artifact is stored by SHA-256 and referenced from the envelope.

## Human And Agent Handoff

```bash
agentdir actor create engineer
agentdir actor create codex

printf 'Please review the failing test evidence before merge.\n' > /tmp/review-request.txt

agentdir send \
  --from codex \
  --to engineer \
  --type approval.requested \
  --session "$session" \
  --body /tmp/review-request.txt
```

## Rebuild And Replay

```bash
agentdir index rebuild
agentdir query --session "$session"
agentdir replay --session "$session"
agentdir doctor
```

If the SQLite index is deleted, rebuild it from the envelopes:

```bash
rm -f "$(agentdir root)/indexes/agentdir.sqlite3"
agentdir index rebuild
agentdir replay --session "$session"
```

## Choosing The Storage Scope

Project scope is the default for coding agents:

```text
<repo>/.agentdir
```

Use it when the evidence belongs with a single repository.

Use user/global scope for personal cross-repo agent memory:

```bash
agentdir init --scope user
agentdir emit --scope user --session daily-agent-log --type user.message --body /tmp/note.txt
```

Use machine scope for shared workstation or CI-runner stores:

```bash
AGENTDIR_MACHINE_ROOT=/opt/agentdir agentdir init --scope machine
```

## Guardrails

- Do not emit secrets. `doctor` warns on common secret-like patterns, but it is not a redaction engine.
- Prefer workspace names over absolute paths when the record may be shared.
- Treat `cur` as local processing state only.
- Emit a new envelope for state changes instead of editing old message bodies.
- Keep large artifacts content-addressed instead of copying them into many records.
