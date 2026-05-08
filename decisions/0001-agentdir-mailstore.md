# ADR 0001: Build AgentDir As A Maildir-Inspired Agent Work Mailstore

Date: 2026-05-08

## Status

Proposed

## Context

Agentic coding sessions need durable, inspectable, replayable records. Existing chat transcripts and process-local histories are not enough for engineering workflows where tool outputs, diffs, approvals, and review evidence matter.

Maildir has useful properties for this problem:

- complete records are written before becoming visible
- each message is one file
- delivery does not require a daemon
- the store is easy to inspect with normal filesystem tools
- offline sync and backup are straightforward
- existing mail parsers and search tools can be reused

Maildir also has limits:

- weak native search
- weak mutable state semantics
- no multi-record transactions
- no built-in lease or retry model
- no authorization or encryption
- directory scans can race with concurrent mutation

## Decision

AgentDir will use a Maildir-inspired envelope store as the canonical immutable event layer and a SQLite sidecar as the canonical mutable index and operational state layer.

The project will not treat raw Maildir as a full database or queue. It will use Maildir-like delivery for durable records and use explicit indexes and append-only state messages for higher-level behavior.

## Consequences

### Positive

- Agent work becomes inspectable and recoverable.
- Indexes can be deleted and rebuilt.
- Engineers can use shell tools to inspect records.
- Agent runtimes can emit events without a server.
- Existing email parsing and search tools remain available.

### Negative

- The system has two layers to keep consistent.
- Rich query behavior requires indexing.
- Queue semantics require additional protocol work.
- Security must be handled above the filesystem in future versions.

## Guardrails

- Do not parse basenames as metadata.
- Do not mutate delivered message bodies.
- Do not promise exact-once task queues in V1.
- Do not hand-edit Dovecot control files.
- Do not add external dependencies without an explicit decision.

## Initial Direction

Start with Python and the standard library:

- `email` for envelope parsing and generation
- `sqlite3` for the sidecar index
- `hashlib` for artifact hashing
- `argparse` for CLI
- direct filesystem operations for atomic write behavior

Rust or another compiled implementation can be considered after the protocol is proven.
