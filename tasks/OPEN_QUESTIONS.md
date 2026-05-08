# AgentDir Open Questions

## Product

1. Should the first target be a standalone CLI, a library for agent runtimes, or both?
2. Should V1 optimize for personal local usage or multi-agent team usage?
3. Should review threads be in V1 or deferred until the session recorder is proven?
4. Should AgentDir integrate with existing email tooling early, or keep the protocol email-compatible but tool-independent?

## Protocol

1. Should bodies default to `text/plain`, `application/json`, or MIME multipart?
2. Should `X-AgentDir-Sequence` be required per session?
3. Should actor IDs use email-like addresses or simple filesystem-safe names?
4. Should task state be fully append-only, or can local claim state use `cur` moves?
5. Should session summaries use `X-Supersedes`, a custom header, or a distinct `summary.compacted` event with references?

## Implementation

1. Should V1 use Python's `mailbox.Maildir` or a minimal custom writer?
2. Is SQLite FTS5 available enough to depend on for local installs?
3. Should the package be installable as `agentdir` or `agent-maildir`?
4. Should the root contain `.agentdir/config.json`, TOML, or no config until needed?
5. Should body hashes cover the raw body, decoded body, or entire message bytes?

## Security

1. What should be the default redaction policy for tool outputs?
2. Should secret-looking values cause warnings or hard failures?
3. What provenance should be signed first: headers, body, artifacts, or all of them?
4. Should actors have trust levels in V1?

## Operations

1. Should the index live inside the AgentDir root or in an external cache path?
2. Should `agentdir doctor` automatically repair anything or remain read-only?
3. Should old `tmp` files be cleaned automatically?
4. Should `cur` records ever be pruned, or is retention explicit only?
