# AgentDir Open Questions

## Product

1. Should the first target be a standalone CLI, a library for agent runtimes, or both?
2. Should AgentDir optimize for personal local usage or multi-agent team usage first?
3. Should review threads be first-class commands or remain event conventions until the session recorder is proven?
4. Should AgentDir integrate with existing email tooling early, or keep the protocol email-compatible but tool-independent?

## Protocol

1. Should bodies default to `text/plain`, `application/json`, or MIME multipart?
2. What stronger ordering primitive should a future multi-writer runtime use beyond `Date`, `X-AgentDir-Created-Ns`, and explicit parent links?
3. Should actor IDs use email-like addresses or simple filesystem-safe names?
4. Should task state be fully append-only, or can local claim state use `cur` moves?

## Implementation

1. Should AgentDir continue with the minimal custom writer or adopt Python's `mailbox.Maildir` for specific operations?
2. Is SQLite FTS5 available enough to depend on for local installs?
3. Should the package be installable as `agentdir` or `agent-maildir`?
4. Should future config remain JSON, move to TOML, or support both?
5. Should body hashes cover the raw body, decoded body, or entire message bytes?

## Security

1. What should be the default redaction policy for tool outputs?
2. Should secret-looking values remain warnings, or should future release commands hard fail?
3. What provenance should be signed first: headers, body, artifacts, or all of them?
4. Should actors have trust levels in the current local trust model?

## Operations

1. Should the index live inside the AgentDir root or in an external cache path?
2. Should a future `agentdir repair` command exist, separate from read-only `doctor`?
3. Should old `tmp` files be cleaned automatically?
4. What higher-level retention presets should sit on top of explicit `archive` and `prune` commands?
