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
  AgentDir builds a local SQLite sidecar index with exact search and built-in vector-like memory documents, so agents can find similar prior work.
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
  'repos/jstxn/agentdir/contents/scripts/install.sh?ref=v0.4.0' | bash
```

The installer uses `pipx` when available. Otherwise it creates a self-contained virtual environment under `~/.local/share/agentdir` and links `agentdir` into `~/.local/bin`.

## Agent-First Setup

Run this once from a git repository:

```bash
agentdir setup
```

That creates the repo-local `.agentdir` store, installs AgentDir-managed Git hook shims, and installs the Codex skill into the user skill directory. The default is intentionally hands-off for coding agents. Use `agentdir setup --codex-skill store` if you want the generated skill artifact to stay inside `.agentdir` instead of the user profile.

After setup, the human workflow is just normal coding-agent work. The installed Codex skill tells the agent to use AgentDir in the background.

What the agent handles:

- creates or reuses a session with `agentdir session ensure`
- builds task context from prior AgentDir memory when useful
- wraps evidence-bearing commands with `agentdir run`
- leaves routine exploration and file reads as plain shell commands
- records important blockers, decisions, and handoffs
- checks `summarize`, `evidence`, and `doctor` before making claims

The CLI remains available for inspection and debugging, but daily users should not have to start sessions or run evidence commands by hand.

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
  integrations/
```

AgentDir is designed around five production behaviors:

1. Writes are crash-safe: partial records stay hidden in `tmp`.
2. Agent work is replayable: deleting the index does not destroy the session.
3. Events are inspectable: a human can read records without a service.
4. Handoffs are concrete: humans and agents can exchange work through inboxes.
5. Memory is built in: the SQLite sidecar contains exact indexes plus vector memory documents.

Vector memory is not a separate service. `agentdir index rebuild` derives message memory and session-summary memory from the same immutable envelopes. `agentdir memory search`, `agentdir memory explain`, and `agentdir context build` rebuild by default so agents can retrieve similar prior work and produce task-ready context without a separate setup step.

Retention is explicit only. `agentdir archive` moves selected inactive sessions out of the active session store, and `agentdir prune` deletes selected archived sessions. Both commands are dry-runs unless the user passes `--apply`; AgentDir does not run retention automatically.

## Quick Start

Run from a checkout without installing:

```bash
PYTHONPATH=src python3 -m agentdir --help
```

Create the repo-local `.agentdir` store and capture a command:

```bash
PYTHONPATH=src python3 -m agentdir setup --codex-skill store
PYTHONPATH=src python3 -m agentdir session ensure --title "demo session"
PYTHONPATH=src python3 -m agentdir run -- python3 -c "print('hello from an agent session')"
PYTHONPATH=src python3 -m agentdir memory search "python agent session"
PYTHONPATH=src python3 -m agentdir context build "python agent session"
PYTHONPATH=src python3 -m agentdir summarize
PYTHONPATH=src python3 -m agentdir evidence
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

Use `--scope user`, `--scope global`, or `--scope machine` when a session should live outside the current repo.

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
