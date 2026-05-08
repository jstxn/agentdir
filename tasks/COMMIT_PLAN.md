# AgentDir Commit Plan

This is the planned sectioned commit sequence. Each section is meant to be independently reviewable and reversible.

## Commit 1: Establish the product contract

Intent:

```text
Define why AgentDir should exist before implementation starts

Capture the product goal, core user stories, and non-goals so implementation work stays tied to a narrow local-first mailstore rather than drifting into a general queue, database, or email client.

Constraint: Correctness-first planning before code changes
Rejected: Start with a CLI prototype immediately | protocol mistakes would become harder to unwind
Confidence: high
Scope-risk: narrow
Tested: Documentation reviewed for internal consistency
Not-tested: Runtime behavior, no implementation in this commit
```

Files:

- `README.md`
- `docs/PRD.md`
- `docs/TECH_BRIEF.md`
- `decisions/0001-agentdir-mailstore.md`

Exit criteria:

- PRD defines MVP, non-goals, user stories, and success metrics.
- Technical brief defines store layout, envelope schema, write algorithm, and index model.

## Commit 2: Add task backlog and execution slices

Intent:

```text
Make AgentDir buildable in reviewable slices

Break the project into small implementation phases with explicit acceptance criteria, verification gates, and open decisions so future commits can stay narrow.

Constraint: Keep diffs small, reviewable, and reversible
Rejected: One large milestone issue | too easy to hide protocol and test gaps
Confidence: high
Scope-risk: narrow
Tested: Backlog cross-checked against PRD milestones
Not-tested: Runtime behavior, no implementation in this commit
```

Files:

- `tasks/BACKLOG.md`
- `tasks/COMMIT_PLAN.md`
- `tasks/OPEN_QUESTIONS.md`

Exit criteria:

- Every V1 milestone has tasks and acceptance criteria.
- Open questions are isolated from committed product requirements.

## Commit 3: Scaffold the Python package

Intent:

```text
Create the smallest runnable AgentDir CLI skeleton

Add packaging, module layout, and a no-op CLI so later behavior can be implemented behind stable command names.

Constraint: No new runtime dependencies for V1 scaffold
Rejected: Rust first implementation | Python standard library is faster for protocol iteration
Confidence: medium
Scope-risk: narrow
Tested: python -m agentdir --help, python -m compileall src
Not-tested: Maildir writes, indexing, replay
```

Files:

- `pyproject.toml`
- `src/agentdir/__init__.py`
- `src/agentdir/__main__.py`
- `src/agentdir/cli.py`
- `tests/test_cli.py`

Exit criteria:

- CLI help works.
- Test command runs.
- No external dependencies added.

## Commit 4: Implement store initialization

Intent:

```text
Make the AgentDir root concrete on disk

Implement root initialization and mailbox directory creation so future event writing has a verified filesystem target.

Constraint: Preserve Maildir-like tmp/new/cur visibility semantics
Rejected: Lazy directory creation everywhere | harder to diagnose malformed roots
Confidence: medium
Scope-risk: narrow
Tested: unit tests for root and mailbox creation
Not-tested: concurrent writers
```

Files:

- `src/agentdir/store.py`
- `src/agentdir/cli.py`
- `tests/test_store.py`

Exit criteria:

- `agentdir init` creates `VERSION`, `config.json`, sessions, actors, artifacts, and indexes directories in the default project store.
- Mailbox helper creates `tmp`, `new`, and `cur`.
- Existing valid roots are handled idempotently.

## Commit 5: Implement envelope creation and atomic emit

Intent:

```text
Guarantee complete agent events appear atomically

Write RFC 5322-style envelopes through tmp before publishing to new so interrupted writes never look like delivered records.

Constraint: Delivered message bodies are immutable
Rejected: JSON files as the canonical format | loses compatibility with existing mail parsers and threading conventions
Confidence: medium
Scope-risk: moderate
Tested: atomic write tests, tmp files ignored, duplicate filename stress test
Not-tested: process kill during fsync on every filesystem
```

Files:

- `src/agentdir/envelope.py`
- `src/agentdir/mailbox.py`
- `src/agentdir/cli.py`
- `tests/test_envelope.py`
- `tests/test_emit.py`

Exit criteria:

- `agentdir emit` writes a parseable message into `new`.
- Files left in `tmp` are ignored by listing helpers.
- Basenames are opaque and collision-resistant.

## Commit 6: Add rebuildable SQLite index

Intent:

```text
Make raw envelopes queryable without making the index authoritative

Create a SQLite sidecar that can be deleted and rebuilt from delivered envelopes so AgentDir stays recoverable.

Constraint: Raw envelopes remain canonical immutable evidence
Rejected: Index-first storage | would weaken recovery and inspectability goals
Confidence: medium
Scope-risk: moderate
Tested: rebuild tests, duplicate Message-ID detection, malformed envelope handling
Not-tested: FTS availability across all target systems
```

Files:

- `src/agentdir/index.py`
- `src/agentdir/schema.sql`
- `src/agentdir/cli.py`
- `tests/test_index.py`

Exit criteria:

- `agentdir index rebuild` creates `indexes/agentdir.sqlite3`.
- Message headers are searchable through the database.
- Index can be deleted and rebuilt.

## Commit 7: Add replay and query

Intent:

```text
Turn stored agent events into useful timelines

Add session replay and basic query filters so engineers can inspect what happened without reading raw files one by one.

Constraint: Replay must label uncertain ordering honestly
Rejected: Hidden UI-first viewer | CLI keeps the recovery surface simple
Confidence: medium
Scope-risk: moderate
Tested: replay ordering tests, query filter tests, JSON output tests
Not-tested: Very large sessions
```

Files:

- `src/agentdir/replay.py`
- `src/agentdir/query.py`
- `src/agentdir/cli.py`
- `tests/test_replay.py`
- `tests/test_query.py`

Exit criteria:

- `agentdir replay --session <id>` renders a timeline.
- `agentdir query` supports session, type, actor, and text filters.
- JSON output is available for downstream tools.

## Commit 8: Add actors and handoff

Intent:

```text
Make human-agent handoff a first-class filesystem object

Create actor inboxes and outboxes so work requests, approvals, blockers, and results can move through durable local mailboxes.

Constraint: Exact-once delivery is not promised in V1
Rejected: Distributed queue leases | too much semantics before the local envelope model is proven
Confidence: medium
Scope-risk: moderate
Tested: actor create, send, inbox query, duplicate send idempotency
Not-tested: Multi-machine sync conflicts
```

Files:

- `src/agentdir/actors.py`
- `src/agentdir/cli.py`
- `tests/test_actors.py`

Exit criteria:

- Actors have inbox and outbox mailboxes.
- `agentdir send` delivers to a recipient inbox.
- Sender and recipient headers are indexed.

## Commit 9: Add artifact blobs

Intent:

```text
Keep large agent evidence deduplicated and referencable

Store large outputs, patches, screenshots, and traces by content hash while envelopes retain searchable metadata and references.

Constraint: Hardlinks are optimization only
Rejected: Inline every artifact in message bodies | bloats session records and weakens dedupe
Confidence: medium
Scope-risk: moderate
Tested: artifact add, hash path, message reference indexing
Not-tested: Garbage collection
```

Files:

- `src/agentdir/artifacts.py`
- `src/agentdir/cli.py`
- `tests/test_artifacts.py`

Exit criteria:

- `agentdir artifact add` stores by SHA-256.
- Envelopes can reference artifacts.
- Missing artifact references are reported by doctor.

## Commit 10: Dogfood demo and doctor

Intent:

```text
Prove AgentDir can survive the failures it is designed for

Add diagnostics and a dogfood demo that records a small coding-session trace, deletes the index, rebuilds it, and replays the timeline.

Constraint: Demonstrate recovery from raw envelopes
Rejected: Demo that only exercises happy-path CLI output | does not prove the core thesis
Confidence: medium
Scope-risk: moderate
Tested: dogfood script, doctor checks, full test suite
Not-tested: Real production agent integration
```

Files:

- `src/agentdir/doctor.py`
- `examples/dogfood-session.sh`
- `tests/test_doctor.py`
- `docs/DEMO.md`

Exit criteria:

- Demo emits session events and tool outputs.
- Index deletion and rebuild is demonstrated.
- Doctor reports malformed roots, missing blobs, and duplicate message IDs.
