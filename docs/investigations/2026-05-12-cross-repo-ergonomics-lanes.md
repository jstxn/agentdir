# AgentDir Cross-Repo And Ergonomics Investigation

Date: 2026-05-12

Guiding product test:

> Make AgentDir feel much less like a toolkit and much more like infrastructure
> agents naturally live inside.

## Scope

This investigation targets the two weaker scoring lanes from the current
AgentDir utility assessment:

| Lane | Current score | Target score |
| --- | ---: | ---: |
| Cross-repo agent value | 7.5/10 | 9.0/10 |
| Engineer ergonomics | 7.5/10 | 9.0/10 |

The current branch already moved AgentDir beyond a local session recorder:

- Context Retrieval V1 exists through hybrid memory search.
- Context Pack V1 exists through build, emit, consume, cite, and audit flows.
- Memory Federation V1 exists through explicit root registration and federated
  search.
- Optional vector backend discovery exists, but real vector backends are not
  installed by default.

The question is no longer whether AgentDir can support RAG-like context. It can.
The harder question is how to make this feel like durable infrastructure instead
of asking every agent and engineer to remember a pile of commands.

## Executive Recommendation

Build the next lane around an AgentDir control plane:

```text
agentdir status
agentdir adopt
agentdir work start "<task>"
agentdir work finish
agentdir report final
agentdir roots suggest
agentdir roots doctor
agentdir roots group ...
```

Keep the existing low-level commands. They are valuable primitives. The next
improvement should hide routine orchestration behind high-level, agent-owned
flows that check health, build context, record evidence, and produce a final
report without user micromanagement.

For cross-repo value, evolve the current federation into Memory Plane V2:

1. Root discovery and suggestions, without automatic registration.
2. Root groups for workspaces, organizations, products, and personal research.
3. Root health and freshness checks.
4. Federated derived indexes that can be warmed in the background.
5. Optional semantic extras using local embeddings and embedded vector search.
6. Provenance and visibility controls on every cross-root hit.

For engineer ergonomics, evolve the CLI into an AgentDir Workbench:

1. One command to adopt a repo.
2. One command to show whether AgentDir is healthy and useful right now.
3. One command to start work that ensures a session and prepares context.
4. One command to finish work that summarizes, records evidence, runs doctor,
   and prints a final report.
5. One copy-pasteable report for PRs, handoffs, and local confidence.

This is the shortest path from "useful utility" to "agent runtime substrate."

## Current Gaps

### Cross-Repo Agent Value

The current federation implementation is intentionally safe:

- Roots are registered explicitly.
- Missing roots are skipped or reported.
- Search is read-only and root-qualified.
- Context packs can include federated sources.

That is the right V1. It is also still a V1. The user has to know which roots
exist, register them, rebuild them, and ask for federated context. There is no
concept of a working set, no freshness dashboard, no suggestion system, and no
background warming.

The practical regression risk is low because federation is explicit. The product
risk is that it remains a hidden expert feature.

### Engineer Ergonomics

AgentDir has the pieces an agent needs:

- `session ensure`
- `context build`
- `run`
- `summarize`
- `evidence`
- `doctor`
- retention commands
- hooks
- skill install

The problem is that those pieces still require agent discipline. A good agent can
use them. A tired, rushed, or unfamiliar agent can miss them.

The next ergonomic win is not more documentation. It is fewer decisions in the
hot path.

## Product Principle

AgentDir should treat agents as first-class runtime participants:

- The agent should not have to remember to start a session.
- The agent should not have to remember which context command matters.
- The agent should not have to manually assemble evidence and summaries.
- The engineer should not have to inspect five commands to know whether AgentDir
  is working.
- Cross-repo memory should be discoverable, scoped, fresh, and explainable.

The control plane should make the best path the normal path.

## Cross-Repo Lane: Memory Plane V2

### Goal

Turn federation from a registered-root search feature into a local memory plane
that agents can depend on across repos.

### Proposed Commands

```text
agentdir roots suggest [--near <path>] [--json]
agentdir roots doctor [--json]
agentdir roots group create <name> --root <root-id>...
agentdir roots group list [--json]
agentdir roots group add <name> <root-id>
agentdir roots group remove <name> <root-id>
agentdir roots rebuild [--stale] [--group <name>] [--json]
agentdir memory search --group <name> "<query>"
agentdir context build --group <name> "<task>" --emit
agentdir memory plane status [--json]
```

The exact naming can be tightened later. The important shift is that agents work
against a named memory plane, not a loose list of paths.

### Root Discovery

`roots suggest` should scan likely local development folders and identify
AgentDir roots without registering them:

- Current repo and parent worktrees.
- Sibling directories under the current repo family.
- User-configured search roots.
- Existing `~/.agentdir` registry entries.

It should output:

- Path.
- Suggested name.
- Whether `.agentdir/VERSION` exists.
- Existing registration state.
- Git remote if available.
- Last indexed time if available.
- Risk label, such as `private`, `external`, or `unknown`.

It should never auto-register. Discovery is a hint, not consent.

### Root Groups

Groups make cross-repo context usable:

```text
upwage
agent-tools
research
client-g2i
personal-machine
```

Each group stores only root IDs and metadata. Roots remain canonical in their own
stores. Groups should support visibility labels and future deny lists.

Root groups make these workflows natural:

- "Search all Upwage agent work."
- "Build context from all AgentDir product repos."
- "Exclude client repos from personal research memory."
- "Run a freshness check before a cross-repo answer."

### Federated Derived Index

The current federated search loops over roots and searches each local index. That
is good enough for V1, but it will not feel like infrastructure at scale.

Memory Plane V2 should add a derived controller index:

```text
federated_sources
federated_passages
federated_terms
federated_index_runs
federated_root_health
federated_groups
```

The controller index should copy only:

- Metadata.
- Provenance.
- Short excerpts.
- Hashes.
- Ranking fields.
- Optional embedded vectors when explicitly enabled.

It should not copy full message bodies by default. Full content remains in the
source root and can be read on demand only when the agent has that root.

### Freshness Model

Every federated hit should show freshness:

- `source_root_id`
- `source_root_name`
- `source_root_path`
- `source_root_visibility`
- `source_indexed_at`
- `source_git_head`
- `source_text_sha256`
- `controller_indexed_at`
- `stale: true|false`

Cross-repo context should be allowed to include stale hits only when the pack
marks them as stale. This preserves usefulness while making risk visible.

### Warm Indexer

Add an optional warm indexer after the control plane exists:

```text
agentdir memory daemon start
agentdir memory daemon status
agentdir memory daemon stop
agentdir roots watch --group <name>
```

The daemon should be opt-in. It should never be required for correctness.
Without it, commands rebuild or refresh synchronously.

### Acceptance Criteria

Cross-repo value reaches 8.4/10 when:

- `roots suggest` finds nearby AgentDir roots without registering them.
- `roots doctor` shows availability, freshness, and stale indexes.
- Groups can scope federated search and context builds.
- Federated hits always include root provenance and freshness.
- Context packs can be emitted against a group.

Cross-repo value reaches 9.0/10 when:

- A warm indexer keeps registered roots fresh without becoming mandatory.
- Optional semantic extras improve retrieval quality across repo vocabulary.
- The control plane can answer "which memory plane did this result come from?"
- Privacy defaults remain explicit and inspectable.

## Engineer Ergonomics Lane: AgentDir Workbench

### Goal

Make the daily agent path feel automatic:

```text
adopt repo -> start work -> run evidence-bearing commands -> finish work
```

The engineer should be able to judge AgentDir health from one command, and the
agent should have one obvious finish command.

### Proposed Commands

```text
agentdir adopt [--scope project|user|global|machine] [--install-skill] [--hooks]
agentdir status [--json]
agentdir work start "<task>" [--emit-context] [--group <name>]
agentdir work finish [--report] [--doctor] [--json]
agentdir report final [--format md|json]
agentdir explain [--session <id>]
```

These commands should compose existing primitives. They should not replace them.

### `agentdir adopt`

One command should handle initial setup:

1. Initialize the root if missing.
2. Install or update the local Codex skill when requested.
3. Install hooks when requested.
4. Run `doctor`.
5. Print the active root, scope, hooks state, skill state, and next command.

This directly addresses the prior install feedback that adoption was too
cumbersome for engineers.

### `agentdir status`

`status` should be the main engineer-facing dashboard:

```text
Root
  path: .agentdir
  scope: project
  version: 0.x

Session
  current: repo-agentdir-...
  started: 2026-05-12T...
  events: 37

Context
  latest pack: ctx-...
  consumed sources: 4
  uncited hints: 2

Evidence
  captured commands: 5
  latest evidence: tests passed

Memory
  index freshness: fresh
  passages: 1284
  federated roots: 3
  stale roots: 1

Health
  doctor: ok
```

JSON output is required so agents and tests can consume it. Rich output can make
the human surface better, but the underlying contract must be structured.

### `agentdir work start`

`work start` should:

1. Ensure an active session.
2. Emit a task-start event.
3. Build a context pack.
4. Optionally emit the context pack.
5. Print a compact briefing.

This makes context retrieval ambient. Agents stop treating `context build` as an
extra step and start inside the AgentDir substrate.

### `agentdir work finish`

`work finish` should:

1. Summarize the session.
2. Collect evidence rows.
3. Run `doctor`.
4. Run diff hygiene checks when inside a git repo.
5. Report unconsumed context hints and uncited sources.
6. Emit a final report event.
7. End the session only when requested or when the skill invokes the final flow.

It should be honest about gaps:

- `Not-tested`
- `No evidence captured`
- `Context pack emitted but not consumed`
- `Federated roots stale`
- `Doctor warnings`

This avoids product-forward overclaiming while making the correct finish path
routine.

### Final Report Shape

The report should be paste-ready:

```text
AgentDir Final Report

Task:
  <task>

Session:
  <session-id>

Changes:
  <summary>

Evidence:
  <captured commands and outcomes>

Context:
  <pack id, consumed sources, cited sources>

Health:
  <doctor result, stale roots, warnings>

Known gaps:
  <not tested or not verified>
```

This report is how AgentDir becomes useful to engineers who do not care about
the internal command set.

### Acceptance Criteria

Engineer ergonomics reaches 8.5/10 when:

- A new repo can be adopted with one command.
- `status` gives a useful human and JSON health view.
- `work start` hides session setup and context build.
- `work finish` produces a final report with evidence and health.
- The Codex skill uses high-level commands for routine flows.

Engineer ergonomics reaches 9.0/10 when:

- A typical agent never needs to know low-level commands.
- The final report is reliable enough to paste into a PR or handoff.
- The status view identifies stale memory, missing hooks, missing context
  consumption, and verification gaps.
- Installation and adoption are boring on a disposable machine.

## Dependency Assessment

The dependency rule should be:

> Add dependencies when they make AgentDir feel more infrastructural without
> weakening local-first ownership, rebuildability, or install confidence.

### Recommended Core Dependencies

#### `platformdirs`

Use for user, global, and machine data paths.

Why it is worth it:

- It reduces platform-specific path edge cases.
- It makes non-project scopes less surprising.
- It is small and mature.

Reference: <https://platformdirs.readthedocs.io/en/latest/>

#### `rich`

Use for `status`, `doctor`, `roots doctor`, and final report rendering.

Why it is worth it:

- It materially improves the daily engineer surface.
- Tables, panels, colors, and progress indicators are exactly the right fit for
  health and report commands.
- JSON output can remain dependency-independent and testable.

Reference: <https://rich.readthedocs.io/en/stable/>

### Recommended Optional Dependencies

#### `watchfiles`

Use in an optional `watch` extra for the warm indexer.

Why it is worth it:

- It avoids hand-rolled cross-platform file watching.
- It lets AgentDir feel ambient without requiring a daemon.

Reference: <https://watchfiles.helpmanual.io/>

#### `fastembed`

Use in an optional `semantic` extra for local embeddings.

Why it is worth it:

- It keeps embeddings local.
- It avoids a hosted API dependency.
- It is aligned with optional semantic retrieval rather than default storage.

Reference: <https://qdrant.github.io/fastembed/>

#### `sqlite-vec`

Use in the optional `semantic` extra as the first embedded vector search target.

Why it is worth it:

- It preserves the SQLite sidecar architecture.
- It avoids introducing a separate vector service.
- It supports the local-first product story.

Risk:

- It is still young. Keep it optional and derived.

References:

- <https://github.com/asg017/sqlite-vec>
- <https://alexgarcia.xyz/sqlite-vec/>

### Dependencies To Defer

#### `Typer` or `Click`

Defer for now. The current `argparse` surface is workable, and a CLI framework
rewrite does not directly create infrastructure value. Revisit if command
composition or shell completion becomes painful.

References:

- <https://typer.tiangolo.com/>
- <https://click.palletsprojects.com/>

#### `Textual`

Defer. A terminal app could be compelling later, but `status` and `report` should
first work as fast CLI commands. A TUI is a second interface, not the foundation.

Reference: <https://textual.textualize.io/>

#### `Tantivy`

Defer. It is a serious full-text engine, but it adds another index stack. SQLite
FTS5 and existing passage tables should be exhausted first.

Reference: <https://github.com/quickwit-oss/tantivy-py>

#### `APSW`

Defer unless `sqlite-vec` loading, backups, or concurrency require it. The
standard SQLite module is simpler until proven insufficient.

Reference: <https://rogerbinns.github.io/apsw/>

#### LanceDB

Keep as an experiment for larger local corpora. It has a good embedded story but
would introduce a second product store.

Reference: <https://lancedb.github.io/lancedb/>

#### Qdrant

Keep as a team or large-store option. It is strong infrastructure, but it changes
the default operational model from local sidecar to managed service or local
server.

Reference: <https://qdrant.tech/documentation/>

#### OpenTelemetry

Defer for core AgentDir. It can become useful for exporting AgentDir events into
larger observability stacks, but it is not needed to make local agent work
better right now.

Reference: <https://opentelemetry.io/docs/languages/python/>

## Rollout Plan

### Phase 1: Control Plane Without New Dependencies

Build:

- `agentdir status --json`
- `agentdir work start`
- `agentdir work finish`
- `agentdir report final`
- `agentdir roots suggest --json`
- `agentdir roots doctor --json`
- root groups

This phase should be dependency-free so behavior and contracts stabilize first.

Tests:

- Status includes root, session, context, evidence, memory, federation, and
  doctor sections.
- Work start creates or reuses a session and builds context.
- Work finish summarizes, gathers evidence, runs doctor, and emits a report.
- Root suggestions never mutate the registry.
- Root groups constrain federated search.

### Phase 2: Human Surface Upgrade

Add:

- `rich` for human tables and reports.
- `platformdirs` for non-project scope paths.

Keep:

- JSON outputs stable.
- Plain fallback acceptable if Rich is unavailable during development.

Tests:

- Snapshot or structural tests for JSON.
- Smoke tests for human output without asserting color codes too tightly.
- Disposable install test verifies adoption flow.

### Phase 3: Warm Indexing

Add:

- optional `watchfiles` extra.
- `memory daemon start/status/stop`.
- stale-root detection.

Controls:

- Daemon is opt-in.
- Commands remain correct without it.
- Status always shows whether the daemon is active.

Tests:

- Index freshness changes after source root mutation.
- Daemon can be stopped cleanly.
- Missing roots are reported, not fatal.

### Phase 4: Semantic Extras

Add:

- optional `fastembed`.
- optional `sqlite-vec`.
- backend configuration and health checks.
- hybrid fusion explanation.

Controls:

- Raw envelopes stay canonical.
- Vector rows stay rebuildable.
- Context packs identify retrieval mode.
- Semantic results remain hints, not proof.

Tests:

- Semantic extras unavailable gives a clear status.
- Installing extras improves recall on vocabulary-mismatch fixtures.
- Rebuilding from envelopes reproduces derived rows.
- Archive and prune exclusions still apply.

### Phase 5: Shared Or Team Memory

Only after local memory plane is strong, evaluate:

- Qdrant for shared vector stores.
- LanceDB for large embedded stores.
- OpenTelemetry export for team observability.

This phase should not be required for the solo engineer or local agent workflow.

## Regression Risks And Controls

| Risk | Control |
| --- | --- |
| Cross-repo memory leaks private work | Explicit registration, groups, visibility labels, no full-body replication by default |
| Root suggestions feel like surveillance | Suggestions are local-only, read-only, and never auto-register |
| Daemon creates hidden behavior | Daemon is optional, visible in status, and never required for correctness |
| AgentDir overclaims enforcement | Keep context enforcement mode advisory |
| Reports become false confidence | Report known gaps, stale roots, uncited context, and missing evidence |
| Dependencies hurt install trust | Add only high-value core deps, keep heavy pieces in extras |
| CLI grows too wide | Put routine flows behind `adopt`, `status`, `work`, and `report` |
| Federated search becomes slow | Add controller derived index and optional warm refresh |
| Semantic ranking hides exact evidence | Keep score explanations and evidence class labels |

## What Would Be Unique

The unique part is not "RAG for coding agents." That category exists.

The unique part is a local-first agent infrastructure layer where:

- Maildir-style envelopes remain the source of truth.
- Evidence and retrieval are separate concepts.
- Context packs have an audit trail.
- Cross-repo memory is federated rather than centralized by default.
- Agents get ambient start and finish flows.
- Engineers get a status and final report surface they can trust.

That is materially more interesting than adding a vector DB. It is a local agent
memory plane with provenance, health, and workflow semantics.

## Recommended Next Implementation Slice

Implement Phase 1 in this order:

1. `agentdir status --json`
2. `agentdir report final --format json|md`
3. `agentdir work start`
4. `agentdir work finish`
5. `agentdir roots doctor --json`
6. `agentdir roots suggest --json`
7. root groups
8. Codex skill update to prefer `work start` and `work finish`

This sequence improves both lanes before adding dependencies:

- Cross-repo value improves because federation becomes visible, scoped, and
  diagnosable.
- Engineer ergonomics improves because routine use collapses into status,
  start, finish, and report.

After Phase 1 is stable, add `rich` and `platformdirs` as the first dependency
step. Add `watchfiles`, `fastembed`, and `sqlite-vec` only as explicit extras.

## Final Score Projection

| Milestone | Cross-repo value | Engineer ergonomics |
| --- | ---: | ---: |
| Current branch | 7.5 | 7.5 |
| Phase 1 control plane | 8.4 | 8.6 |
| Phase 2 human surface | 8.5 | 8.9 |
| Phase 3 warm indexing | 8.8 | 9.0 |
| Phase 4 semantic extras | 9.1 | 9.0 |

The important point is that the flashy part should not be a demo-only vector
search. The flashy part should be that agents start inside AgentDir, finish with
evidence, and can safely use memory across repos without turning the machine
into a black box.
