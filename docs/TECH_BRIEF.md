# AgentDir Technical Brief

## 1. Technical Position

AgentDir is a Maildir-inspired event envelope store for software agents. It should preserve the traits that make Maildir powerful:

- atomic visibility through `tmp -> new`
- one file per delivered record
- opaque unique filenames
- immutable delivered message bodies
- plain filesystem layout
- easy backup and sync

It should add the pieces Maildir does not provide:

- structured agent event schema
- rebuildable index
- built-in vector memory
- task and actor model
- artifact references
- duplicate detection
- replay and query commands

The canonical event store is the envelope directory. The canonical mutable operational state is the SQLite sidecar. Vector memory is part of that sidecar by default, and is derived from envelopes rather than treated as separate truth.

## 2. Source-Informed Constraints

### Maildir Delivery Model

Maildir writes complete records to `tmp`, then publishes by atomic rename into `new`. Consumers can later move records into `cur`. This is the core safety property to preserve.

AgentDir rule: no command should make a partially written event visible.

### Filename Opaqueness

The original Maildir guidance says unique filenames are writer concerns and readers should not parse them.

AgentDir rule: routing and workflow metadata must live in headers or body content, not in basenames.

### Message Immutability

Dovecot treats delivered messages as immutable. If content changes, a new message should be created.

AgentDir rule: semantic updates are new envelopes. Local read or processing state may be represented by `new -> cur`, but truth is append-only.

### Directory Mutation Caveat

Dovecot locks during scanning and modification because `readdir()` can skip entries if a directory changes while being read.

AgentDir rule: use idempotent indexing, duplicate detection, and periodic full rebuilds. Avoid protocols that depend on one scan being complete under concurrent mutation.

## 3. Architecture

```text
                   +------------------+
                   | Agent Runtime    |
                   +---------+--------+
                             |
                             v
                    emit envelope
                             |
                             v
agentdir-root/      +--------+---------+
  sessions/         | Maildir-like     |
  actors/           | envelope store   |
  artifacts/        +--------+---------+
  indexes/                   |
                             v
                    rebuild/update index
                             |
                             v
                   +---------+---------+
                   | SQLite sidecar    |
                   | messages, FTS,    |
                   | vector memory,    |
                   | artifacts, tasks  |
                   +---------+---------+
                             |
                             v
                   query, replay, view
```

## 4. Directory Layout

```text
<root>/
  VERSION
  config.json
  sessions/
    <session-id>/
      Maildir/
        tmp/
        new/
        cur/
  actors/
    <actor-id>/
      inbox/
        Maildir/
          tmp/
          new/
          cur/
      outbox/
        Maildir/
          tmp/
          new/
          cur/
  queues/
    <queue-id>/
      Maildir/
        tmp/
        new/
        cur/
  artifacts/
    blobs/
      sha256/
        ab/
          cd/
            <full-sha256>
  archives/
    sessions/
      <session-id>/
        Maildir/
          tmp/
          new/
          cur/
  indexes/
    agentdir.sqlite3
  state/
    current-session.json
    last-session.json
  hooks/
  integrations/
```

AgentDir creates `sessions`, `actors`, `artifacts`, `archives`, `indexes`, `state`, `hooks`, and `integrations`. The root itself may be a project hidden directory such as `<repo>/.agentdir`, a user store such as `~/.agentdir`, or an explicit custom path.

## 5. Envelope Format

AgentDir uses RFC 5322-compatible message files so existing parsers can read them.

Minimum envelope:

```text
Message-ID: <20260508T154500Z.RANDOM@agentdir.local>
Date: Fri, 08 May 2026 15:45:00 -0000
From: codex@agentdir.local
To: session@agentdir.local
Subject: tool.result pytest
X-AgentDir-Version: 0.1
X-AgentDir-Event-Type: tool.result
X-AgentDir-Session: session-123
X-AgentDir-Created-Ns: 1778256000000000000
Content-Type: text/plain; charset=utf-8

<body>
```

Structured JSON bodies are allowed but not required. The system should parse headers first and treat body parsing as event-type-specific.

## 6. Atomic Write Algorithm

Writer flow:

1. Ensure target mailbox has `tmp`, `new`, and `cur`.
2. Generate a unique basename from UTC timestamp, pid, hostname, sequence, and random bytes.
3. Open `tmp/<basename>` with exclusive create.
4. Write the complete envelope bytes.
5. Flush and fsync the file.
6. Close the file.
7. Rename `tmp/<basename>` to `new/<basename>`.
8. Best effort fsync the containing directories where supported.

Notes:

- Readers ignore `tmp`.
- If exclusive create fails, generate a new name.
- The final filename remains opaque to readers.

## 7. SQLite Schema

Initial schema:

```sql
create table messages (
  id integer primary key,
  message_id text not null,
  mailbox_path text not null,
  file_path text not null,
  state text not null,
  event_type text,
  subject text,
  from_actor text,
  to_actor text,
  session_id text,
  task_id text,
  parent_message_id text,
  date_header text,
  created_ns integer,
  received_at text,
  git_head text,
  workspace text,
  tool text,
  tool_exit_code integer,
  body_sha256 text,
  body_text text,
  indexed_at text not null,
  unique(message_id, file_path)
);

create index messages_session_idx on messages(session_id);
create index messages_event_type_idx on messages(event_type);
create index messages_task_idx on messages(task_id);
create index messages_parent_idx on messages(parent_message_id);

create table headers (
  message_rowid integer not null references messages(id) on delete cascade,
  name text not null,
  value text not null
);

create table artifacts (
  sha256 text primary key,
  path text not null,
  bytes integer not null,
  mime_type text,
  created_at text not null
);

create table message_artifacts (
  message_rowid integer not null references messages(id) on delete cascade,
  sha256 text not null references artifacts(sha256)
);

create table memory_documents (
  id integer primary key,
  source_kind text not null,
  source_id text not null unique,
  message_rowid integer references messages(id) on delete cascade,
  message_id text,
  session_id text,
  event_type text,
  subject text,
  from_actor text,
  to_actor text,
  task_id text,
  git_head text,
  workspace text,
  tool text,
  tool_exit_code integer,
  date_header text,
  date_utc text,
  file_path text,
  body_text text not null,
  vector_dim integer not null,
  vector_json text not null,
  text_sha256 text not null,
  token_count integer not null,
  indexed_at text not null
);
```

FTS can be a second step:

```sql
create virtual table message_fts using fts5(
  message_id,
  subject,
  body,
  content=''
);
```

If FTS5 is unavailable in a given Python SQLite build, AgentDir should degrade to LIKE search and report the limitation.

Vector memory is not optional. The first implementation uses deterministic feature-hashed vectors stored in SQLite so the package keeps its zero-runtime-dependency install shape. It stores message-level documents, derived session-summary documents, passage chunks, and term shortlists. Future embedding backends can improve ranking, but they must remain rebuildable from envelopes and must not replace the raw record.

Hybrid retrieval is the default path:

1. Apply metadata filters to memory documents.
2. Use `memory_terms` to shortlist matching passages.
3. Rerank passages with deterministic vector similarity.
4. Collapse passages back to source-level rows.
5. Penalize derived summaries so direct message evidence wins when both match.
6. Fall back to document-level vector scoring when no passage candidate matches.

## 8. Indexing Model

Index commands:

```text
agentdir index rebuild [--root <root>] [--scope <scope>]
agentdir index update [--root <root>] [--scope <scope>]
```

Rebuild strategy:

- create a new SQLite database in a temp path
- scan `new` and `cur` under known mailboxes
- parse headers with a standard email parser
- hash body or full message bytes
- derive vector memory documents from message metadata, body text, and session summaries
- derive passage and term indexes from those memory documents
- detect duplicate `Message-ID`s
- replace old index by atomic rename after successful build
- treat rebuild as a point-in-time best effort under concurrent writers, then allow a follow-up `index update` pass

Incremental strategy:

- compare file path, mtime, size, and optional message hash
- insert unseen records
- update state if a file moved from `new` to `cur`
- periodically recommend full rebuild

## 9. Replay Model

Replay is a materialized timeline.

Ordering preference:

1. `Date` header when parseable.
2. `X-AgentDir-Created-Ns` when present.
3. file path as a deterministic fallback, labeled non-causal.

AgentDir does not promise global causal ordering. Agent runtimes that need stronger ordering should emit explicit parent links with `In-Reply-To` and `References`.

## 10. Actor Handoff Model

Actors are filesystem identities with mailboxes.

```text
actors/<actor-id>/inbox/Maildir
actors/<actor-id>/outbox/Maildir
```

Sending to an actor writes a new envelope into the recipient inbox. Optionally, the sender outbox gets a copy or hardlink, but hardlinks must be an optimization only.

Duplicate `Message-ID` classes:

- benign replica: the same `Message-ID` and same body hash appear in multiple mailboxes, such as inbox and outbox copies
- duplicate emit: the same `Message-ID` and same body hash appear more than once in the same logical mailbox
- conflicting reuse: the same `Message-ID` appears with different body hashes

`doctor` warns on benign replicas and reports conflicting reuse as an error. Query and replay show stored files as records and do not silently dedupe output.

## 11. Task Spool Model

Task spool semantics are intentionally conservative.

Task state should be append-only:

- `task.created`
- `task.claimed`
- `task.blocked`
- `task.completed`
- `task.failed`

Do not implement distributed leases until a sidecar lease table and expiry policy exist.

## 12. Artifact Model

Artifacts are content-addressed blobs:

```text
artifacts/blobs/sha256/ab/cd/<sha256>
```

Envelope headers reference artifacts:

```text
X-AgentDir-Blob-SHA256: <sha256>
X-AgentDir-Blob-Bytes: <bytes>
X-AgentDir-Blob-Mime: text/x-diff
```

AgentDir supports add and reference. Garbage collection should be postponed until reachability is explicit.

## 13. Retention Model

Retention is explicit only. AgentDir must not archive, prune, compact, or garbage collect records from background workflows, setup, hooks, doctor, summarize, evidence, memory search, or context build.

Session archive moves selected inactive sessions from:

```text
sessions/<session-id>/
```

to:

```text
archives/sessions/<session-id>/
```

Archived sessions are outside the active mailbox discovery path, so an index rebuild drops them from active query, replay, and memory output without destroying the raw envelopes.

Prune deletes archived sessions by default. It can delete live sessions only when the user explicitly passes `--include-live-sessions`. Both commands are dry-runs unless `--apply` is passed, and the current active session is protected.

## 14. CLI Surface

Current CLI:

```text
agentdir adopt [--install-skill user|project|store|none]
agentdir setup [--codex-skill user|project|store|none]
agentdir init [<root>] [--scope <scope>]
agentdir root [--scope <scope>]
agentdir status [--json]
agentdir work start <task> [--emit-context] [--federated] [--group <name>]
agentdir work finish [--keep-session] [--json]
agentdir report final [--format md|json]
agentdir session ensure [--id <id>] [--title <title>]
agentdir session start [--id <id>] [--title <title>]
agentdir session current
agentdir session end [--summary <file-or-text>]
agentdir run [--session <id>] [--name <tool>] -- <command> [args...]
agentdir capsule run --image <image> [--mode copy|readonly|write-through] [--dry-run] -- <command> [args...]
agentdir emit [--root <root>] [--scope <scope>] [--session <id>] --type <type> --body <file>
agentdir actor create [--root <root>] [--scope <scope>] <actor-id>
agentdir send [--root <root>] [--scope <scope>] --from <actor> --to <actor> --type <type> --body <file>
agentdir artifact add [--root <root>] [--scope <scope>] <path>
agentdir archive [--session <id>] [--older-than-days <days>] [--keep-recent <n>] [--apply]
agentdir prune [--session <id>] [--older-than-days <days>] [--keep-recent <n>] [--include-live-sessions] [--apply]
agentdir hooks install|status|uninstall
agentdir skills install codex [--target user|project|store]
agentdir index rebuild [--root <root>] [--scope <scope>]
agentdir query [--root <root>] [--scope <scope>] [--session <id>] [--type <type>] [--actor <actor>] [--tool <tool>] [--git-head <sha>] [--text <query>] [--semantic <query>]
agentdir memory search <query>
agentdir memory search --retrieval semantic <query>
agentdir memory explain <query> [--source <source-id>]
agentdir memory stats
agentdir memory backend list
agentdir memory backend configure sqlite-vec|none
agentdir memory embeddings configure fastembed|none [--model <model>]
agentdir memory team configure qdrant|lancedb|none
agentdir memory daemon start|status|stop
agentdir roots register <root-or-repo> [--name <name>] [--visibility private|team|machine]
agentdir roots list
agentdir roots remove <root-id-or-name>
agentdir roots rebuild [--stale] [--group <name>]
agentdir roots suggest [--near <path>]
agentdir roots doctor [--group <name>]
agentdir roots group create <name> --member <root-id>
agentdir roots group list
agentdir roots group add <name> <root-id>
agentdir roots group remove <name> <root-id>
agentdir memory search --federated <query>
agentdir memory search --group <name> <query>
agentdir context build <task> [--emit]
agentdir context build <task> --federated [--emit]
agentdir context build <task> --group <name> [--emit]
agentdir context consume --pack <pack-id> --source <source-id> --purpose plan|tool|answer|handoff
agentdir context cite --pack <pack-id> [--source <source-id>] [--format md|json]
agentdir audit context --pack <pack-id>
agentdir replay [--root <root>] [--scope <scope>] --session <id>
agentdir summarize [--session <id>]
agentdir evidence [--session <id>]
agentdir doctor [--root <root>] [--scope <scope>]
```

`work start` is the agent-owned path. It ensures a session, emits a work-start
event, builds task context, and can emit an auditable context pack. `status`
gives the engineer and the agent one health view for the active session,
evidence, memory, roots, and doctor state. `work finish` emits a final report,
records a work-finished event, and closes the session by default.

Context pack emission is also agent-owned. `context build --emit` writes a
manifest artifact and emits `context.pack.created`; `context consume`,
`context cite`, and `audit context` preserve advisory source lineage without
claiming hard proof of model attention.

Federation is explicit and derived. `roots register` stores registered root
metadata in `state/registered-roots.json` under the controller root. `roots
suggest` discovers nearby AgentDir roots without registration. `roots doctor`
reports availability and stale indexes. Root groups store named sets of
registered root IDs so repeated cross-repo work can search a scoped memory
plane. Federated search rebuilds each selected child root, returns
root-qualified source IDs, and copies only metadata, scores, excerpts, and
source references into the result surface. The child root remains the canonical
store.

`memory backend list` exposes the active local hybrid backend plus optional
vector-extra lanes. Optional backend configuration records operator intent in
state, checks dependency availability, and never replaces envelopes as the
canonical store. Semantic retrieval requires explicit fastembed configuration
and optional dependencies; otherwise the local hybrid retriever remains active.

The memory daemon is an opt-in warm indexer. It records process state under
`state/memory-daemon.json`, rebuilds the local index and selected registered
roots, and can use `watchfiles` when that extra is installed. Commands remain
correct without the daemon because the index is still rebuildable on demand.

## 14. Implementation Recommendation

The current implementation uses Python.

Reasons:

- standard library includes `mailbox`, `email`, `sqlite3`, `hashlib`, and `argparse`
- fast iteration for protocol design
- readable implementation for early adopters
- no new dependency needed for core behavior

Potential later Rust rewrite or core library:

- better static packaging
- stronger concurrency tests
- easier distribution as a single binary
- lower memory overhead for large mailstores

## 15. Testing Strategy

### Unit Tests

- unique filename generation
- envelope header validation
- atomic write ignores `tmp`
- duplicate `Message-ID` detection
- artifact hashing path
- SQLite schema migrations

### Integration Tests

- initialize root and emit events
- rebuild index from raw envelopes
- replay session after deleting index
- actor send and receive flow
- malformed envelope handling
- concurrent emit stress test

### Failure Tests

- simulate interrupted writer leaving file in `tmp`
- simulate duplicate message IDs
- simulate missing artifact blob
- simulate moved `new -> cur` state

## 16. Verification Gates

Initial docs-only gate:

```text
find agentdir -type f -maxdepth 3 -print
rg -n "\\x{2013}|\\x{2014}" agentdir
```

Initial Python implementation gate:

```text
python -m pytest
python -m compileall src tests
python -m agentdir doctor --root <tmp-root>
```

No new dependency should be added without an explicit decision record.

## 17. Security Model

AgentDir security is intentionally simple and honest:

- local filesystem permissions protect records
- AgentDir does not promise complete secret detection
- `agentdir run` avoids environment capture by default
- `agentdir run` redacts common secret-like patterns in stored command output
- emitted envelope bodies are redacted for common secret-like patterns before persistence
- `doctor` errors on persisted secret-like envelope bodies
- `agentdir secrets scan` reports only affected paths and pattern labels
- `agentdir secrets redact --apply` redacts affected bodies and rebuilds the derived index
- configurable redaction policy is future work
- signing and encryption are future work

Future security features:

- signed envelopes
- encrypted bodies
- secret pattern redaction hooks
- per-actor trust policy
- provenance verification

## 18. Key Risks And Mitigations

| Risk | Mitigation |
| --- | --- |
| Directory scan race | Idempotent indexing, duplicate detection, periodic rebuild. |
| Index drift | Raw envelopes canonical, rebuild command required. |
| Secret leakage | No environment capture by default, bounded command output, stored-output redaction, and `doctor` warnings. |
| Overbuilt queue semantics | Task state is append-only, no distributed lease promise. |
| Large directory performance | Shard by session and actor. |
| Metadata inconsistency | Required header schema plus doctor command. |

## 19. Research References

- Dovecot CE Maildir format: https://doc.dovecot.org/2.4.0/core/config/mailbox/formats/maildir.html
- Original qmail Maildir notes: https://cr.yp.to/proto/maildir.html
- Python `mailbox`: https://docs.python.org/3/library/mailbox.html
- RFC 5322: https://www.rfc-editor.org/rfc/rfc5322
- notmuch: https://notmuchmail.org/doc/latest/command-line.html
- mbsync: https://isync.sourceforge.io/mbsync.html
- Buildbot Maildir precedent: https://docs.buildbot.net/0.8.0/Using-Maildirs.html
