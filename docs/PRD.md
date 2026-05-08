# AgentDir PRD

## 1. Product Summary

AgentDir is a local-first durable mailstore for agent work. It gives software agents, engineers, and agent orchestration tools a shared filesystem-native substrate for recording, routing, replaying, and auditing agentic coding sessions.

The product borrows the proven Maildir lifecycle:

```text
tmp -> new -> cur
```

but specializes the envelope schema, index, task states, and tooling for agents instead of human email.

## 2. Problem Statement

Agentic coding sessions are often fragile and opaque:

- Session histories live inside a vendor UI or process memory.
- Tool calls and outputs are hard to reconstruct after interruption.
- Artifacts are scattered across temp directories, chat transcripts, and git diffs.
- Human approvals and review comments are not durable local objects.
- Multi-agent handoffs rely on ad hoc text, not a common protocol.
- Indexes and summaries become trusted even when the raw evidence is unavailable.

Engineers need agent work to be inspectable, replayable, portable, and recoverable. Agents need a simple substrate for durable event emission and handoff without requiring a server.

## 3. Product Thesis

The powerful primitive is not email. It is an atomic, immutable envelope per unit of agent work.

AgentDir should make agent activity feel like a local mailstore:

- every meaningful event is delivered as a complete file
- incomplete writes are hidden
- inboxes and outboxes are plain directories
- session timelines can be rebuilt
- engineers can inspect records with normal tools
- indexes are useful but disposable

## 4. Target Users

### Primary Users

- Engineers using coding agents locally.
- Agent orchestration tools that need durable session records.
- Reviewers who need audit trails for agent-generated changes.
- Teams running multiple agents across a shared workspace.

### Secondary Users

- Researchers studying agent behavior.
- CI systems that want replayable agent traces.
- Tool builders who need a portable interchange format for agent events.

## 5. Personas

### Engineer

Wants to know what the agent did, why it did it, what files it touched, what tests ran, and whether the evidence can be trusted.

### Agent Runtime

Needs a low-friction way to emit durable events, attach artifacts, receive tasks, and recover after crashes.

### Supervisor Agent

Needs to coordinate work, distribute tasks, collect verification evidence, and preserve an auditable record.

### Reviewer

Needs to inspect a change history, review unresolved threads, and verify that tests and tool outputs correspond to the current diff.

## 6. Goals

### V1 Product Goals

- Provide a local envelope store for immutable agent events.
- Provide a CLI for initialization, event emission, indexing, querying, and replay.
- Provide a SQLite sidecar index that can be rebuilt from envelopes.
- Support session flight recording for coding sessions.
- Support actor inboxes and outboxes for human-agent handoff.
- Support content-addressed artifact references.
- Provide clear docs and examples for agent runtime integration.

### V1 Technical Goals

- Use only standard-library implementation primitives at first where practical.
- Keep the raw envelope store readable without AgentDir-specific tooling.
- Use stable `Message-ID` and threading headers.
- Avoid parsing Maildir basenames for metadata.
- Make all semantic state changes append-only.
- Make index rebuild a first-class operation.

## 7. Non-Goals

- Full email client functionality.
- IMAP server implementation.
- High-throughput distributed queue replacement.
- Primary mutable database replacement.
- Cloud hosted service.
- Multi-tenant authorization model in V1.
- Real-time collaboration UI.
- Dependency on Dovecot metadata files.

## 8. Core Concepts

### Envelope

An immutable RFC 5322-style message file. It contains headers, a body, and optional MIME parts or artifact references.

### Mailbox

A directory with `tmp`, `new`, and `cur`. `tmp` holds incomplete writes, `new` holds visible unprocessed records, and `cur` holds locally processed records.

### Session

A logical agentic work session. Every turn, tool call, tool result, diff, and summary can be emitted into a session mailbox.

### Actor

A human, agent, supervisor, CI runner, verifier, or tool process with an inbox and outbox.

### Artifact

A large or reusable blob stored by content hash and referenced from envelopes.

### Index

A rebuildable SQLite database that materializes messages, headers, sessions, actors, artifacts, task state, and full-text search.

## 9. User Stories

### Session Recovery

As an engineer, I can delete the AgentDir index and rebuild it from raw envelopes so that I can trust the raw record over derived state.

Acceptance criteria:

- Given a populated session mailbox, `agentdir index rebuild` recreates the message table.
- Rebuilt session timeline preserves event order according to envelope metadata.
- Missing or malformed envelopes are reported without stopping unrelated records from indexing.

### Crash-Safe Event Emission

As an agent runtime, I can emit an event such that interrupted writes do not appear as complete records.

Acceptance criteria:

- Events are written to `tmp` first.
- Complete events are atomically renamed into `new`.
- Files left in `tmp` are ignored by index and replay commands.

### Human-Agent Handoff

As an engineer, I can place a task or approval into an agent inbox and see the agent's response in my inbox.

Acceptance criteria:

- `agentdir actor create <id>` creates inbox and outbox mailboxes.
- `agentdir send --to <actor> ...` emits a message into the recipient inbox.
- Message headers include sender, recipient, task ID, and event type.

### Tool Output Trace

As a reviewer, I can inspect the exact command output an agent used as evidence.

Acceptance criteria:

- Tool outputs are emitted as `tool.result` envelopes.
- Large outputs may be stored as artifact blobs with envelope references.
- Query output can filter by tool name, session, exit status, and git HEAD.

### Review Thread

As a reviewer, I can see an agent-generated review thread as immutable comments and resolution messages.

Acceptance criteria:

- Comments use `In-Reply-To` and `References`.
- Resolution is represented by a new message, not a mutation of the original comment.
- Query output can show unresolved review threads once indexed.

### Offline Portability

As an engineer, I can move a session directory to another machine and replay it.

Acceptance criteria:

- Raw session mailboxes are self-contained except for optional referenced blobs.
- Missing blobs are reported as missing references.
- Index rebuild works in a copied directory.

## 10. Functional Requirements

### FR1: Initialize Store

The CLI must create the root directory layout and any required metadata.

V1 command:

```text
agentdir init <root>
```

### FR2: Emit Event

The CLI must emit an immutable event envelope into a target mailbox.

V1 command:

```text
agentdir emit --root <root> --session <id> --type <type> --body <file>
```

### FR3: Manage Actors

The CLI must create actor inboxes and outboxes.

V1 command:

```text
agentdir actor create <actor-id>
```

### FR4: Send Handoff Message

The CLI must deliver an envelope to an actor inbox.

V1 command:

```text
agentdir send --from <actor> --to <actor> --type <type> --body <file>
```

### FR5: Index Envelopes

The CLI must scan visible records and write a rebuildable index.

V1 commands:

```text
agentdir index rebuild --root <root>
agentdir index update --root <root>
```

### FR6: Query Records

The CLI must query indexed records by session, type, actor, tool, task ID, git HEAD, text, and time range.

V1 command:

```text
agentdir query --root <root> --session <id>
```

### FR7: Replay Session

The CLI must render a session timeline from the index or raw envelopes.

V1 command:

```text
agentdir replay --root <root> --session <id>
```

### FR8: Store Artifacts

The CLI must store artifact blobs by SHA-256 and emit references from envelopes.

V1 command:

```text
agentdir artifact add --root <root> <path>
```

## 11. Non-Functional Requirements

### Reliability

- Partial writes must not appear in query or replay output.
- Index rebuild must tolerate malformed records.
- Duplicate `Message-ID`s must be detected.
- All semantic state changes must be append-only in V1.

### Inspectability

- Envelopes must be readable as plain text when the body is text.
- Binary or large artifacts must be stored by hash and referenced.
- Directory layout must be understandable without a running service.

### Portability

- V1 must run locally on macOS and Linux.
- Avoid platform-specific file assumptions where possible.
- Support a configurable info delimiter later for Windows-like filesystems, but do not target Windows in V1.

### Performance

- V1 should support thousands of envelopes per session.
- Incremental index updates should avoid full rescans where possible.
- Large artifacts should not be duplicated into every envelope.

### Security

- V1 relies on filesystem permissions.
- Secrets must not be emitted by default from adapters.
- Redaction hooks are a future extension.
- Signatures and encryption are planned after the envelope protocol stabilizes.

## 12. Envelope Schema

Required headers:

```text
Message-ID: <unique-id@agentdir.local>
Date: <RFC 5322 date>
From: <actor-id@agentdir.local>
To: <actor-id@agentdir.local>
Subject: <short event summary>
X-AgentDir-Version: 0.1
X-AgentDir-Event-Type: <event-type>
```

Session headers:

```text
X-AgentDir-Session: <session-id>
X-AgentDir-Workspace: <absolute-or-logical-path>
X-AgentDir-Git-Head: <sha>
```

Tool headers:

```text
X-AgentDir-Tool: <tool-name>
X-AgentDir-Tool-Exit-Code: <integer>
X-AgentDir-Tool-Duration-Ms: <integer>
```

Threading headers:

```text
In-Reply-To: <parent-message-id>
References: <ancestor-message-ids>
```

Artifact headers:

```text
X-AgentDir-Blob-SHA256: <sha256>
X-AgentDir-Blob-Bytes: <bytes>
X-AgentDir-Blob-Mime: <mime-type>
```

## 13. Event Types

Initial event taxonomy:

- `session.started`
- `session.ended`
- `user.message`
- `agent.message`
- `tool.call`
- `tool.result`
- `file.diff`
- `artifact.added`
- `task.created`
- `task.claimed`
- `task.blocked`
- `task.completed`
- `review.comment`
- `review.resolved`
- `approval.requested`
- `approval.granted`
- `approval.denied`
- `summary.compacted`

## 14. MVP Scope

### Must Have

- Project package scaffold.
- `init`, `emit`, `index rebuild`, `query`, and `replay`.
- Session mailbox layout.
- SQLite sidecar index.
- Plain-text RFC 5322 envelopes.
- Collision-resistant filename generation.
- Basic tests for atomic write, indexing, duplicate detection, and replay.

### Should Have

- Actor inbox and outbox commands.
- Artifact blob storage.
- JSON output mode for CLI commands.
- Example integration script that records a toy coding session.

### Could Have

- notmuch adapter.
- filesystem watcher.
- HTML or TUI viewer.
- signed envelopes.
- compacted session summaries.

### Won't Have In V1

- IMAP server.
- network sync daemon.
- multi-tenant permissions.
- distributed queue guarantees.
- UI.

## 15. Milestones

### Milestone 0: Planning Package

Deliver this PRD, technical brief, task backlog, and commit plan.

### Milestone 1: CLI Skeleton And Store Initialization

Create package structure, CLI entrypoint, root layout, and mailbox creation.

### Milestone 2: Envelope Emission

Implement RFC 5322 envelope builder and crash-safe `tmp -> new` write path.

### Milestone 3: Rebuildable Index

Implement SQLite schema, full rebuild, incremental update, and duplicate detection.

### Milestone 4: Replay And Query

Implement timeline rendering, filters, and JSON output.

### Milestone 5: Actors And Handoff

Implement actor inbox/outbox creation and directed delivery.

### Milestone 6: Artifacts

Implement content-addressed artifact storage and envelope references.

### Milestone 7: Dogfood Demo

Record an agentic coding session and prove replay after index deletion.

## 16. Success Metrics

- A session can be reconstructed after deleting `indexes/agentdir.sqlite3`.
- Concurrent emitters do not collide over 10,000 test events.
- Interrupted writes leave no visible indexed records.
- A human can inspect an event with `less` and understand it.
- A copied session directory can be indexed on another machine.
- The first dogfood run captures prompts, tool calls, outputs, diffs, and verification evidence.

## 17. Risks

### Directory Scan Races

Maildir-like stores can have scan races under concurrent mutation. V1 mitigates by making semantic truth append-only and using rescans plus idempotent message IDs.

### Index Drift

The SQLite index may drift from raw envelopes. V1 mitigates by making rebuild a core command and treating raw envelopes as canonical for immutable events.

### Metadata Sprawl

Too many custom headers can become hard to reason about. V1 mitigates with a minimal required header set and event-type-specific optional headers.

### Secret Leakage

Agent tool outputs may include secrets. V1 must document this clearly and avoid automatic capture adapters until redaction policy exists.

### Overbuilding Queue Semantics

Maildir is a spool, not a full queue. V1 should record work and handoffs, but avoid promising distributed lease semantics.

## 18. Open Decisions

- Python versus Rust for first implementation.
- Whether to use `mailbox.Maildir` directly or implement a minimal writer.
- SQLite FTS5 availability strategy.
- Whether event bodies should default to text, JSON, or MIME multipart.
- Exact command naming: `agentdir emit` versus `agentdir record`.
