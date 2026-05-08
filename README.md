# AgentDir

AgentDir is a local-first work mailstore for software agents.

It adapts the strongest parts of Maildir for agentic coding sessions: atomic file delivery, immutable event envelopes, human-inspectable state, offline portability, and rebuildable indexes. The goal is not to replace databases or queues. The goal is to give every important unit of agent work a durable physical form that engineers can inspect, grep, sync, replay, and repair.

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

AgentDir stores those records as immutable message envelopes in a Maildir-like directory structure. A sidecar index makes them searchable and queryable, but the raw envelopes remain the recoverable source of truth.

## Project Status

This directory contains the first installable V1 build plus the product and technical planning package.

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

Install the latest V1 release:

```bash
tmpdir="$(mktemp -d)"
gh release download v0.1.0 \
  --repo jstxn/agentdir \
  --pattern install-agentdir.sh \
  --dir "$tmpdir"
bash "$tmpdir/install-agentdir.sh"
```

The installer uses `pipx` when available. Otherwise it creates a self-contained virtual environment under `~/.local/share/agentdir` and links `agentdir` into `~/.local/bin`.

## Initial V1 Shape

```text
agentdir-root/
  sessions/<session-id>/Maildir/{tmp,new,cur}
  actors/<actor-id>/inbox/Maildir/{tmp,new,cur}
  actors/<actor-id>/outbox/Maildir/{tmp,new,cur}
  artifacts/blobs/sha256/<prefix>/<hash>
  indexes/agentdir.sqlite3
```

V1 should prove five things:

1. Writes are crash-safe: partial records stay hidden in `tmp`.
2. Agent work is replayable: deleting the index does not destroy the session.
3. Events are inspectable: a human can read records without a service.
4. Handoffs are concrete: humans and agents can exchange work through inboxes.
5. The system composes: SQLite, notmuch, or other indexers can sit beside the envelope store.

## Quick Start

Run from a checkout without installing:

```bash
PYTHONPATH=src python3 -m agentdir --help
```

Create a local AgentDir root and emit a session event:

```bash
ROOT="$(mktemp -d)/agentdir-root"
printf 'hello from an agent session\n' > /tmp/agentdir-body.txt

PYTHONPATH=src python3 -m agentdir init "$ROOT"
PYTHONPATH=src python3 -m agentdir emit \
  --root "$ROOT" \
  --session demo-session \
  --type agent.message \
  --body /tmp/agentdir-body.txt
PYTHONPATH=src python3 -m agentdir index rebuild --root "$ROOT"
PYTHONPATH=src python3 -m agentdir replay --root "$ROOT" --session demo-session
PYTHONPATH=src python3 -m agentdir doctor --root "$ROOT"
```

Run the dogfood demo:

```bash
bash examples/dogfood-session.sh
```

Agentic coding recipes are in [docs/AGENTIC_CODING.md](docs/AGENTIC_CODING.md).

## Verification

```bash
python3 -m compileall src tests
uv run --with pytest pytest -q
bash -n examples/dogfood-session.sh
KEEP_WORKDIR=1 bash examples/dogfood-session.sh
```

## Non-Goals

- Replacing SQLite as the source of indexed query state.
- Replacing Redis, SQS, Kafka, or RabbitMQ for high-throughput distributed queueing.
- Building a full email client.
- Depending on Dovecot internals.
- Encoding critical workflow metadata in opaque filenames.
