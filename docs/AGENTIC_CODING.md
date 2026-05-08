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

## One-Time Setup

From a git repository:

```bash
agentdir setup
```

This initializes `.agentdir`, installs managed Git hook shims, and installs the Codex skill in the user skill directory. Use `agentdir setup --codex-skill store` when you want generated integration files to stay under `.agentdir`.

## Session Flight Recorder

```bash
agentdir session start --title "Fix failing checkout flow"
agentdir session current
```

## Capturing Tool Calls And Results

```bash
agentdir run -- pytest -q
agentdir run -- npm test
agentdir run -- git diff --check
```

`agentdir run` streams command output to the terminal and records both the call and the result. Stored output is truncated at a bounded size and common secret-like patterns are redacted in the stored envelope.

## Capturing Diffs As Artifacts

```bash
git diff > /tmp/agentdir.diff

agentdir emit \
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
  --body /tmp/review-request.txt
```

## Review And Replay

```bash
agentdir summarize
agentdir evidence
agentdir replay --session "$(agentdir session current)"
agentdir doctor
```

If the SQLite index is deleted, rebuild it from the envelopes:

```bash
rm -f "$(agentdir root)/indexes/agentdir.sqlite3"
agentdir index rebuild
agentdir replay --session "$(agentdir session current)"
```

End the session when the task is done:

```bash
agentdir session end --summary /tmp/final-summary.txt
```

## Git Hooks

`agentdir setup` installs managed shims for common local Git hooks. The shims preserve existing hooks by moving them to `*.agentdir-original`, run the original hook first, record the result, and return the original exit code.

```bash
agentdir hooks status
agentdir hooks install --hook pre-commit
agentdir hooks uninstall --hook pre-commit
```

Hook records use the active session when one exists. If no session is active, AgentDir starts a small automatic hook session.

## Codex Skill

The generated Codex skill tells coding agents to start sessions, wrap commands with `agentdir run`, emit important evidence, and close with `summarize`, `evidence`, and `doctor`.

```bash
agentdir skills install codex --target user
agentdir skills install codex --target project
agentdir skills install codex --target store
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
