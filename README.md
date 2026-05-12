# AgentDir

<p align="center">
  <img src="docs/assets/agentdir-overview.png" alt="AgentDir turns agent work into a saved trail, searchable memory, and proof for engineers." />
</p>

## What is AgentDir

AgentDir is a local-first flight recorder and memory layer for coding agents.

It gives agents a durable place to record what happened during a coding session: the task, tool calls, command results, decisions, blockers, diffs, tests, summaries, and handoffs. Instead of leaving agent work scattered across chat logs and terminal history, AgentDir turns it into a structured, inspectable project record.

### What It Provides

- **Automatic session memory**
  Agents can create or reuse a session in the background and keep a timeline of the work.
- **Evidence-backed coding sessions**
  Test runs, build checks, release steps, and command outputs can be captured as proof, not just claimed in a final message.
- **Replayable agent work**
  Engineers can inspect what the agent did, when it did it, and what evidence it used.
- **Local-first storage**
  Records live in a repo-local `.agentdir` folder by default. No server, daemon, Redis, vector database, or external service is required.
- **Maildir-inspired durability**
  Each event is written as an immutable file, so raw records remain recoverable even if indexes are deleted or rebuilt.
- **Searchable agent memory**
  AgentDir builds a local SQLite sidecar index with exact search, hybrid passage retrieval, and built-in vector-like memory, so agents can find similar prior work.
- **Auditable context packs**
  Agents can emit retrieved context as source manifests, then record which sources were consumed and cited.
- **Federated local memory**
  Explicitly registered AgentDir roots can be searched together without moving the canonical records out of their project stores.
- **Warm indexing and optional semantic extras**
  An opt-in local memory daemon can keep indexes warm, while optional embedding,
  vector, and team backends stay derived from the envelope store.
- **Better handoffs**
  Agents can leave reviewable summaries, evidence, blockers, and task context for engineers or other agents.
- **Agent-owned workflow**
  The user should not have to manually start sessions or run evidence commands. Once installed, the coding agent handles AgentDir in the background.

## Core Idea

Agents produce discrete records:

- user requests
- model responses
- tool calls and outputs
- diffs and patches
- screenshots and artifacts
- review comments
- approvals and blockers
- verification evidence
- summaries and handoffs

AgentDir stores those records as immutable message envelopes in a Maildir-like directory structure. A sidecar SQLite index makes them exactly queryable and vector searchable, but the raw envelopes remain the recoverable source of truth.

## Project Status

AgentDir is now an agent-first utility for recording, replaying, and reviewing coding-agent work with minimal human ceremony.

- [PRD](docs/PRD.md)
- [Technical Brief](docs/TECH_BRIEF.md)
- [Install Guide](docs/INSTALL.md)
- [Agentic Coding Guide](docs/AGENTIC_CODING.md)
- [Release Guide](docs/RELEASING.md)
- [Commit Plan](tasks/COMMIT_PLAN.md)
- [Task Backlog](tasks/BACKLOG.md)
- [Open Questions](tasks/OPEN_QUESTIONS.md)
- [Architecture Decision Records](decisions/0001-agentdir-mailstore.md)

## Install From GitHub Release

AgentDir is distributed through GitHub Releases. For the private `jstxn/agentdir` repo, authenticate GitHub CLI first:

```bash
gh auth login
```

Install the latest release with one command:

```bash
gh api -H "Accept: application/vnd.github.raw" \
  'repos/jstxn/agentdir/contents/scripts/install.sh?ref=v0.5.2' | bash
```

The installer uses `pipx` when available. Otherwise it creates a self-contained virtual environment under `~/.local/share/agentdir` and links `agentdir` into `~/.local/bin`.

To update an existing machine install and refresh the current repo adoption:

```bash
agentdir --upgrade
```

Rollback is also one command and does not rely on the installed `agentdir`
binary. To return to the previous stable release:

```bash
gh api -H "Accept: application/vnd.github.raw" \
  'repos/jstxn/agentdir/contents/scripts/rollback.sh?ref=v0.5.2' | bash
```

## Agent-First Setup

Run this once from a git repository:

```bash
agentdir adopt
```

That creates the repo-local `.agentdir` store, installs AgentDir-managed Git hook shims, and installs the Codex skill into the user skill directory. The default is intentionally hands-off for coding agents. Use `agentdir setup --codex-skill store` if you want the generated skill artifact to stay inside `.agentdir` instead of the user profile.

After setup, the human workflow is just normal coding-agent work. The installed Codex skill tells the agent to use AgentDir in the background.

What the agent handles:

- starts inside AgentDir with `agentdir work start "<task>" --emit-context`
- checks session, evidence, memory, registered roots, and doctor state with `agentdir status`
- emits and cites context packs when retrieval informs a plan, tool use, answer, or handoff
- wraps evidence-bearing commands with `agentdir run`
- leaves routine exploration and file reads as plain shell commands
- records important blockers, decisions, and handoffs
- finishes with `agentdir work finish`, which emits a final report and closes the session

The CLI remains available for inspection and debugging, but daily users should not have to start sessions or run evidence commands by hand.

## Secret Hygiene

AgentDir does not promise complete secret detection, but it should not treat
persisted secret-like bodies as healthy. New emitted message bodies are redacted
for common token, key, password, and private-key patterns before persistence.
For older stores or accidental captures:

```bash
agentdir secrets scan
agentdir secrets redact
agentdir secrets redact --apply
agentdir doctor
```

`secrets scan` prints only affected paths and pattern labels. `secrets redact`
is a dry run by default; `--apply` rewrites affected bodies with redaction
markers and rebuilds the derived SQLite index. `doctor` fails while persisted
secret-like envelope bodies remain.

## Store Shape

```text
.agentdir/
  sessions/<session-id>/Maildir/{tmp,new,cur}
  actors/<actor-id>/inbox/Maildir/{tmp,new,cur}
  actors/<actor-id>/outbox/Maildir/{tmp,new,cur}
  artifacts/blobs/sha256/<prefix>/<hash>
  archives/sessions/<session-id>/Maildir/{tmp,new,cur}
  indexes/agentdir.sqlite3
  state/current-session.json
  state/last-session.json
  state/registered-roots.json
  integrations/
```

AgentDir is designed around five production behaviors:

1. Writes are crash-safe: partial records stay hidden in `tmp`.
2. Agent work is replayable: deleting the index does not destroy the session.
3. Events are inspectable: a human can read records without a service.
4. Handoffs are concrete: humans and agents can exchange work through inboxes.
5. Memory is built in: the SQLite sidecar contains exact indexes plus vector memory documents.

Vector memory is not a separate service. `agentdir index rebuild` derives message memory, session-summary memory, passage chunks, and term shortlists from the same immutable envelopes. `agentdir memory search`, `agentdir memory explain`, and `agentdir context build` rebuild by default so agents can retrieve similar prior work and produce task-ready context without a separate setup step.

Context packs can be emitted as immutable events. `agentdir context build --emit`
stores a JSON source manifest as a content-addressed artifact, while
`agentdir context consume`, `agentdir context cite`, and
`agentdir audit context` record retrieved, consumed, cited, and evidence-backed
source lineage. This audit is advisory: it records cooperative agent behavior,
not proof that a model paid attention to the retrieved context.

Federated memory is explicit. `agentdir roots register <root-or-repo>` adds a
child AgentDir root to the current root's registry, `agentdir roots list`
shows availability, and `agentdir memory search --federated <query>` searches
registered roots with root-qualified source IDs. Child roots remain canonical;
federated results copy metadata, scores, excerpts, and source IDs only.
`agentdir roots suggest` discovers nearby AgentDir roots without registering
them, `agentdir roots doctor` reports root freshness, and root groups scope
repeated cross-repo work:

```bash
agentdir roots group create agent-tools --member root-abc123def456
agentdir memory search --group agent-tools "release evidence"
agentdir context build --group agent-tools "release evidence" --emit
```

Optional vector extras are extension points, not required infrastructure.
`agentdir memory backend list` reports the active local hybrid backend, optional
`sqlite-vec`, local embeddings through `fastembed`, and team backends such as
Qdrant or LanceDB. Use `agentdir memory embeddings configure fastembed` and
`agentdir memory backend configure sqlite-vec` only when those optional extras
are installed. Semantic results remain retrieval hints.

Warm indexing is opt-in:

```bash
agentdir memory daemon start
agentdir memory daemon status
agentdir memory daemon stop
```

The daemon is an accelerator only. Normal commands still rebuild or refresh
derived indexes when needed.

Retention is explicit only. `agentdir archive` moves selected inactive sessions out of the active session store, and `agentdir prune` deletes selected archived sessions. Both commands are dry-runs unless the user passes `--apply`; AgentDir does not run retention automatically.

## Quick Start

Run from a checkout without installing:

```bash
PYTHONPATH=src python3 -m agentdir --help
```

Create the repo-local `.agentdir` store and capture a command:

```bash
PYTHONPATH=src python3 -m agentdir adopt --install-skill store
PYTHONPATH=src python3 -m agentdir work start "demo session" --emit-context
PYTHONPATH=src python3 -m agentdir run -- python3 -c "print('hello from an agent session')"
PYTHONPATH=src python3 -m agentdir status
PYTHONPATH=src python3 -m agentdir memory search "python agent session"
PYTHONPATH=src python3 -m agentdir memory backend list
PYTHONPATH=src python3 -m agentdir memory daemon run --once
PYTHONPATH=src python3 -m agentdir context build "python agent session"
PYTHONPATH=src python3 -m agentdir context build "python agent session" --emit
PYTHONPATH=src python3 -m agentdir report final
PYTHONPATH=src python3 -m agentdir work finish
```

After another repo has its own AgentDir store, register it explicitly before
using federated search:

```bash
PYTHONPATH=src python3 -m agentdir roots register /path/to/other-repo --name other-repo
PYTHONPATH=src python3 -m agentdir roots doctor
PYTHONPATH=src python3 -m agentdir memory search --federated "python agent session"
```

Run the dogfood demo:

```bash
bash examples/dogfood-session.sh
```

Agentic coding recipes are in [docs/AGENTIC_CODING.md](docs/AGENTIC_CODING.md).

AgentDir also works without `--root`. By default it writes to the nearest repo's project store:

```text
<repo>/.agentdir/
```

Use `--scope user`, `--scope global`, or `--scope machine` when a session should live outside the current repo. User and machine scopes use platform application-data locations when available, while existing legacy roots continue to be honored.

## Verification

```bash
python3 -m compileall src tests
uv run --with pytest pytest -q
bash -n examples/dogfood-session.sh
KEEP_WORKDIR=1 bash examples/dogfood-session.sh
```

## Non-Goals

- Replacing SQLite as the source of indexed query state.
- Replacing raw envelopes with vector state as the source of truth.
- Replacing Redis, SQS, Kafka, or RabbitMQ for high-throughput distributed queueing.
- Building a full email client.
- Depending on Dovecot internals.
- Encoding critical workflow metadata in opaque filenames.
