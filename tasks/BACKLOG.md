# AgentDir Backlog

## Legend

- `P0`: required for core proof
- `P1`: important after core proof
- `P2`: useful but deferrable
- `Blocked`: requires an open decision

## Epic 1: Product And Protocol Definition

### AD-001: Finalize envelope schema

Priority: `P0`

Tasks:

- Define required headers.
- Define optional session, tool, task, review, and artifact headers.
- Define valid event type names.
- Define how duplicate `Message-ID`s are handled.
- Add schema examples to docs.

Acceptance criteria:

- Every MVP command can produce envelopes with the schema.
- Parser can reject or warn on missing required headers.

### AD-002: Define root directory contract

Priority: `P0`

Tasks:

- Define root `VERSION`.
- Define `config.json` minimal fields.
- Define sessions, actors, artifacts, and indexes paths.
- Document which paths are canonical versus cache.

Acceptance criteria:

- `agentdir init` can be implemented directly from the contract.
- `agentdir doctor` has clear root validation rules.

## Epic 2: CLI Foundation

### AD-010: Create Python package scaffold

Priority: `P0`

Tasks:

- Add `pyproject.toml`.
- Add `src/agentdir`.
- Add `python -m agentdir` entrypoint.
- Add argparse command routing.
- Add initial tests.

Acceptance criteria:

- CLI help works.
- Tests run without external dependencies.

### AD-011: Implement structured command errors

Priority: `P1`

Tasks:

- Define error codes.
- Print actionable messages.
- Support `--json` error output for automation.

Acceptance criteria:

- Invalid root, missing body, and malformed envelope produce clear errors.

## Epic 3: Store And Mailbox Primitives

### AD-020: Implement root initialization

Priority: `P0`

Tasks:

- Create root directories.
- Write version metadata.
- Validate idempotent re-run.
- Refuse to initialize over incompatible metadata.

Acceptance criteria:

- `agentdir init` creates a valid project-scoped root at `.agentdir` by default.
- Re-running init is safe.

### AD-021: Implement mailbox helper

Priority: `P0`

Tasks:

- Create `tmp`, `new`, and `cur`.
- Validate mailbox shape.
- List delivered records from `new` and `cur`.
- Ignore `tmp`.

Acceptance criteria:

- Tests prove `tmp` records are invisible to indexing helpers.

### AD-022: Implement unique basename generation

Priority: `P0`

Tasks:

- Include UTC timestamp.
- Include pid.
- Include hostname-safe component.
- Include random bytes.
- Include a per-process counter as a collision backstop.
- Reject names with slash, colon, or leading dot.

Acceptance criteria:

- Stress test produces no collisions.
- Generated names are treated as opaque by readers.

## Epic 4: Envelopes And Emission

### AD-030: Build envelope writer

Priority: `P0`

Tasks:

- Build RFC 5322-compatible messages.
- Add required AgentDir headers.
- Support text body input.
- Support parent message headers.
- Support subject generation.

Acceptance criteria:

- Python email parser can parse emitted envelopes.

### AD-031: Implement atomic publish

Priority: `P0`

Tasks:

- Write to `tmp`.
- Flush and fsync file.
- Rename into `new`.
- Best effort fsync directories.
- Cleanly handle create collision.

Acceptance criteria:

- Interrupted tmp file is not indexed.
- Completed file appears in `new`.

### AD-032: Implement `agentdir emit`

Priority: `P0`

Tasks:

- Route session events to session mailbox.
- Accept `--type`, `--session`, `--body`, `--subject`.
- Add `--from` and `--to` defaults.
- Add `--header` repeat option later if needed.

Acceptance criteria:

- CLI can emit a visible session event.

## Epic 5: Index

### AD-040: Create SQLite schema

Priority: `P0`

Tasks:

- Create messages table.
- Create headers table.
- Create artifacts table.
- Create indexes.
- Add schema version tracking.

Acceptance criteria:

- Empty index can be created and versioned.

### AD-041: Implement full rebuild

Priority: `P0`

Tasks:

- Scan known mailboxes.
- Parse envelopes.
- Insert messages and headers.
- Detect duplicate message IDs.
- Swap rebuilt index atomically.

Acceptance criteria:

- Index can be deleted and rebuilt from raw envelopes.

### AD-042: Implement incremental update

Priority: `P1`

Tasks:

- Track indexed file path, size, mtime, and hash.
- Insert new records.
- Detect moved `new -> cur` state.
- Avoid trusting a single scan as complete.

Acceptance criteria:

- Incremental update picks up new emitted events.

### AD-043: Add built-in vector memory

Priority: `P0`

Tasks:

- Store derived vector memory documents in the SQLite sidecar during normal index rebuilds.
- Add a first-class `agentdir memory search` command for agents.
- Add `agentdir memory explain` so agents and engineers can inspect why retrieval matched.
- Add `agentdir context build` so agents can gather memory, evidence, and recent summaries before work.
- Add semantic search through `agentdir query --semantic`.
- Keep vector memory rebuildable from raw envelopes without extra services or dependencies.

Acceptance criteria:

- `agentdir index rebuild` creates memory rows for indexed records.
- `agentdir memory search <text>` returns ranked prior records out of the box.
- `agentdir memory explain <text>` reports score, source, overlap terms, and excerpt.
- `agentdir context build <task>` returns an agent-ready context pack.
- Existing query filters still apply during semantic search.

### AD-044: Add FTS fallback strategy

Priority: `P1`

Tasks:

- Detect FTS5 availability.
- Use FTS5 when available.
- Fall back to LIKE search.
- Document behavior.

Acceptance criteria:

- Query works even if FTS5 is unavailable.

## Epic 6: Query And Replay

### AD-050: Implement query filters

Priority: `P0`

Tasks:

- Filter by session.
- Filter by event type.
- Filter by actor.
- Filter by task ID.
- Filter by text.
- Support JSON output.

Acceptance criteria:

- Query returns stable, parseable output.

### AD-051: Implement session replay

Priority: `P0`

Tasks:

- Load session events.
- Sort by date, `X-AgentDir-Created-Ns`, then deterministic file path fallback.
- Render concise timeline.
- Include file path references for raw envelopes.

Acceptance criteria:

- Replay works after index rebuild.

## Epic 7: Actors And Handoff

### AD-060: Create actor mailboxes

Priority: `P1`

Tasks:

- Add `actor create`.
- Create inbox and outbox.
- Validate actor IDs.

Acceptance criteria:

- Actors have valid mailboxes.

### AD-061: Send actor message

Priority: `P1`

Tasks:

- Add `send`.
- Deliver to recipient inbox.
- Optionally copy to sender outbox.
- Index sender and recipient headers.

Acceptance criteria:

- Human-agent handoff works locally.

## Epic 8: Artifacts

### AD-070: Store artifact by hash

Priority: `P1`

Tasks:

- Hash file with SHA-256.
- Store under sharded path.
- Avoid duplicate writes.
- Record bytes and mime hint.

Acceptance criteria:

- Same file maps to same artifact path.

### AD-071: Reference artifacts from envelopes

Priority: `P1`

Tasks:

- Add artifact headers.
- Index message artifact references.
- Report missing blob in doctor.

Acceptance criteria:

- Query can show artifact references for a message.

## Epic 9: Diagnostics And Dogfood

### AD-080: Implement doctor command

Priority: `P1`

Tasks:

- Validate root shape.
- Validate mailboxes.
- Find duplicate message IDs.
- Find malformed envelopes.
- Find missing blobs.

Acceptance criteria:

- Doctor reports actionable diagnostics.

### AD-081: Build dogfood demo

Priority: `P1`

Tasks:

- Emit a toy session.
- Emit tool call and result events.
- Emit a diff event.
- Rebuild index.
- Replay timeline.

Acceptance criteria:

- Demo proves the core product thesis locally.

## Epic 10: Future Work

### AD-090: notmuch adapter

Priority: `P2`

Tasks:

- Detect notmuch.
- Initialize notmuch DB against AgentDir root.
- Sync tags for common state.
- Document tradeoffs.

### AD-091: Signed envelopes

Priority: `P2`

Tasks:

- Define signing envelope.
- Pick signing library after explicit dependency approval.
- Verify signatures in doctor.

### AD-092: Redaction hooks

Priority: `P2`

Tasks:

- Define hook interface.
- Add default denylist examples.
- Test secret-like output handling.

### AD-093: Viewer

Priority: `P2`

Tasks:

- Explore terminal UI.
- Explore static HTML export.
- Keep raw envelope links visible.
