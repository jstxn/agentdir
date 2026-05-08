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
- task and actor model
- artifact references
- duplicate detection
- replay and query commands

The canonical event store is the envelope directory. The canonical mutable operational state is the SQLite sidecar.

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
                   | artifacts, tasks  |
                   +---------+---------+
                             |
                             v
                   query, replay, view
```

## 4. Directory Layout

```text
<root>/
  .agentdir/
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
  indexes/
    agentdir.sqlite3
```

V1 can create `sessions`, `actors`, `artifacts`, and `indexes`. `queues` can be reserved until task spool semantics are explicit.

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
Content-Type: text/plain; charset=utf-8

<body>
```

Structured JSON bodies are allowed but not required. The system should parse headers first and treat body parsing as event-type-specific.

## 6. Atomic Write Algorithm

V1 writer flow:

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
  received_at text,
  git_head text,
  workspace text,
  body_sha256 text,
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

If FTS5 is unavailable in a given Python SQLite build, V1 should degrade to LIKE search and report the limitation.

## 8. Indexing Model

Index commands:

```text
agentdir index rebuild --root <root>
agentdir index update --root <root>
```

Rebuild strategy:

- create a new SQLite database in a temp path
- scan `new` and `cur` under known mailboxes
- parse headers with a standard email parser
- hash body or full message bytes
- detect duplicate `Message-ID`s
- replace old index by atomic rename after successful build

Incremental strategy:

- compare file path, mtime, size, and optional message hash
- insert unseen records
- update state if a file moved from `new` to `cur`
- periodically recommend full rebuild

## 9. Replay Model

Replay is a materialized timeline.

Ordering preference:

1. `Date` header when parseable.
2. `X-AgentDir-Sequence` if available.
3. filesystem discovery order as a fallback, labeled unstable.

V1 should add `X-AgentDir-Sequence` per writer when possible, but the protocol must remain correct without global ordering.

## 10. Actor Handoff Model

Actors are filesystem identities with mailboxes.

```text
actors/<actor-id>/inbox/Maildir
actors/<actor-id>/outbox/Maildir
```

Sending to an actor writes a new envelope into the recipient inbox. Optionally, the sender outbox gets a copy or hardlink, but hardlinks must be an optimization only.

## 11. Task Spool Model

Task spool semantics are intentionally conservative in V1.

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

V1 should support add and reference. Garbage collection should be postponed until reachability is explicit.

## 13. CLI Surface

Proposed V1 CLI:

```text
agentdir init <root>
agentdir emit --root <root> --session <id> --type <type> --body <file>
agentdir actor create --root <root> <actor-id>
agentdir send --root <root> --from <actor> --to <actor> --type <type> --body <file>
agentdir artifact add --root <root> <path>
agentdir index rebuild --root <root>
agentdir query --root <root> [--session <id>] [--type <type>] [--text <query>]
agentdir replay --root <root> --session <id>
agentdir doctor --root <root>
```

## 14. Implementation Recommendation

Start with Python for V1.

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

V1 security is intentionally simple and honest:

- local filesystem permissions protect records
- AgentDir does not hide secrets automatically
- adapters must avoid capturing environment variables by default
- redaction policy is future work
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
| Secret leakage | No automatic broad capture in V1, document risk. |
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
