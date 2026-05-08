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

## Daily Workflow

The user should not have to operate AgentDir during a normal coding session. Once the CLI and skill are installed, the coding agent owns the recording workflow.

Agent responsibilities:

- run `agentdir session ensure --title "<task>"` when coding work begins
- run `agentdir setup` once if the repository has not been prepared yet
- build context from prior memory when the task is non-trivial
- wrap evidence-bearing commands with `agentdir run`
- use plain shell commands for routine exploration and file reads
- record important blockers, decisions, and handoffs
- use `agentdir summarize`, `agentdir evidence`, and `agentdir doctor` before making evidence-backed claims

Human responsibilities:

- install AgentDir
- ask the coding agent to do the work
- inspect AgentDir output only when they want evidence, replay, or debugging details

## Built-In Agent Memory

AgentDir builds vector memory inside the normal SQLite sidecar whenever the index is rebuilt. Agents do not need a vector database daemon, embedding service, or separate install step.

Agents search similar prior work before and during a task:

```bash
agentdir context build "checkout failure tests"
agentdir memory search "checkout failure tests"
agentdir memory explain "checkout failure tests"
agentdir query --semantic "sqlite index failure" --type tool.result
agentdir memory stats
```

`agentdir memory search` rebuilds the index by default, then ranks matching envelopes and derived session summaries by vector similarity. `agentdir memory explain` shows why a hit matched. `agentdir context build` combines memory, current-session evidence, and recent session summaries into an agent-ready context pack. The raw Maildir envelopes remain the source of truth, so the memory layer can always be deleted and rebuilt.

## Capturing Tool Calls And Results

```bash
agentdir run -- pytest -q
agentdir run -- npm test
agentdir run -- git diff --check
```

`agentdir run` streams command output to the terminal and records both the call and the result. Stored output is truncated at a bounded size and common secret-like patterns are redacted in the stored envelope. Agents should use this automatically for commands they run as evidence.

Do not wrap routine exploration commands such as `rg`, `sed`, `nl`, `cat`, `ls`, `find`, or quick read-only `git status` checks. Use plain shell commands while reading files, mapping code, or gathering low-level context. The evidence trail should capture verification, reproduced failures, important diagnostics, and final support for claims, not every glance at a file.

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

These commands are mainly for agents and for humans who want to inspect the record:

```bash
agentdir summarize
agentdir evidence
agentdir memory search "similar failing verification"
agentdir context build "similar failing verification"
agentdir replay --session "$(agentdir session current)"
agentdir doctor
```

If the SQLite index is deleted, rebuild it from the envelopes:

```bash
rm -f "$(agentdir root)/indexes/agentdir.sqlite3"
agentdir index rebuild
agentdir replay --session "$(agentdir session current)"
```

Agents should end the session when the task is done:

```bash
agentdir session end --summary /tmp/final-summary.txt
```

## Store Hygiene

Retention commands are explicit user operations, not background maintenance. They default to dry-run and require `--apply` before moving or deleting session records.

```bash
agentdir archive --keep-recent 20
agentdir archive --older-than-days 30 --apply
agentdir prune --session old-session-id
agentdir prune --session old-session-id --apply
```

`agentdir archive` moves inactive sessions from `sessions/` to `archives/sessions/`, then rebuilds the active index when applied. `agentdir prune` deletes archived sessions by default. It only considers live `sessions/` when the user also passes `--include-live-sessions`. The current active session is protected.

## Git Hooks

`agentdir setup` installs managed shims for common local Git hooks. The shims preserve existing hooks by moving them to `*.agentdir-original`, run the original hook first, record the result, and return the original exit code.

```bash
agentdir hooks status
agentdir hooks install --hook pre-commit
agentdir hooks uninstall --hook pre-commit
```

Hook records use the active session when one exists. If no session is active, AgentDir starts a small automatic hook session.

## Codex Skill

The generated Codex skill tells coding agents that AgentDir is their responsibility, not a user checklist. It instructs agents to ensure sessions, build context, search and explain memory, wrap commands with `agentdir run`, emit important evidence, and close with `summarize`, `evidence`, and `doctor`.

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
