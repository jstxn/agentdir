# Agentic Coding With AgentDir

AgentDir is built for engineers who want agentic coding sessions to leave durable local evidence. It gives agents a filesystem-native place to record what happened without requiring a server.

## What To Record

Use one immutable envelope per meaningful unit of work:

- original user request
- plan or task split
- tool call
- tool result
- changed-file summary
- patch or diff artifact
- test or lint evidence
- review comment
- approval request
- final handoff

The index is disposable. The envelope store is the recovery source.

## One-Time Setup

From a git repository:

```bash
agentdir adopt
```

For agent-driven, non-interactive setup, make the ignore destination explicit:

```bash
agentdir adopt --if-needed --gitignore user
```

This keeps `.agentdir/` out of Git across repositories without changing a
project's tracked `.gitignore`.

This initializes `.agentdir`, installs managed Git hook shims, installs the
Codex skill in the user skill directory, writes broad project guidance for
common agent tools, asks interactive users where to ignore `.agentdir/`, runs
doctor, and prints the next command.

Preview or undo setup safely:

```bash
agentdir adopt --dry-run --json
agentdir setup --dry-run --json
agentdir unadopt
agentdir unadopt --apply
```

Use `agentdir adopt --install-skill store --install-generic store --integration-target store`
when you want generated integration files to stay under `.agentdir`.
Use `agentdir adopt --gitignore project`, `--gitignore user`, or
`--gitignore none` to make the ignore destination explicit in non-interactive
setup.

## Daily Workflow

The user should not have to operate AgentDir during a normal coding session. Once the CLI and skill are installed, the coding agent owns the recording workflow.

Agent responsibilities:

- probe with `agentdir root --require --quiet`; a zero exit means an initialized
  local or shared linked-worktree store is ready, even if this checkout has no
  physical `.agentdir`
- if the probe exits 3, run `agentdir adopt --if-needed --gitignore user`; add
  `--no-hooks` if a restricted linked worktree cannot write the shared Git hooks
  directory
- run `agentdir work start "<task>"` when coding work begins; it persists a bounded context briefing by default
- read its bounded context briefing and record either useful numbered sources
  or a reasoned no-relevant decision with `agentdir work context`
- treat briefing excerpts as previews; prefer `agentdir work context --expand
  <number>` before marking a source used when implementation details, patterns,
  or exact evidence matter
- use `agentdir status` when a single health view is needed
- use `--skip --reason` only when the context briefing cannot be reviewed, so
  the final handoff keeps the gap visible
- cite only context sources actually used when reporting lineage
- wrap evidence-bearing commands with `agentdir run`
- use plain shell commands for routine exploration and file reads
- record important blockers, decisions, and handoffs
- use `agentdir evidence --brief` and `agentdir timeline` to skim the trail
- use `agentdir report final --format json` to preview the agent handoff object
- use `agentdir work finish --json --brief` before final claims when practical

Human responsibilities:

- install AgentDir
- ask the coding agent to do the work
- inspect AgentDir output only when they want evidence, replay, or debugging details

## Built-In Agent Memory

AgentDir builds vector memory inside the normal SQLite sidecar whenever the index is rebuilt. Agents do not need a vector database daemon, embedding service, or separate install step. The default retriever is hybrid: it uses passage chunks and term shortlists first, then reranks with deterministic vector similarity.

Agents search similar prior work before and during a task:

```bash
agentdir context build "checkout failure tests"
agentdir memory search "checkout failure tests"
agentdir memory explain "checkout failure tests"
agentdir query --semantic "sqlite index failure" --type tool.result
agentdir memory stats
agentdir memory backend list
agentdir memory daemon status
```

`agentdir memory search` incrementally updates the index by default (new and deleted envelopes only; pass `--no-rebuild` to skip even that), then ranks matching envelopes and derived session summaries by vector similarity. `agentdir memory explain` shows why a hit matched. `work start` removes generic task words and the workspace name from its retrieval query, diversifies candidates, and prints at most five sources with match quality. Full IDs, scores, and match reasons remain in JSON. `agentdir context build` combines memory, current-session evidence, and recent session summaries into an agent-ready context pack. The raw Maildir envelopes remain the source of truth, so the memory layer can always be deleted and rebuilt.

The normal read-and-decide flow is:

```bash
agentdir work start "checkout failure tests"
agentdir work context --expand 1
# continue a long source: agentdir work context --expand 1 --page 2
agentdir work context --use 1 --reason "prior checkout failure constrains the plan"
# or: agentdir work context --none-relevant --reason "only generic matches"
# after a restart or lost output: agentdir work context --show
```

The briefing source numbers are pack-local selectors; full source IDs stay in
JSON, and retrieved-but-omitted IDs stay on the lower-level interface. A used decision reviews
the entire bounded briefing, records selected sources as used, and marks the
other presented sources dismissed. A no-relevant decision reviews and dismisses
the entire briefing without manufacturing consumption or citation. A non-empty
briefing remains pending until one of those decisions, or an explicit skipped
review, is recorded. The terminal decision is serialized and idempotent, so
concurrent identical declarations produce one record and conflicting later
declarations are rejected.

Expansion is a nonterminal read, not a disposition. It maps the exact displayed
ordinal back to the persisted manifest, resolves local messages, derived
summaries, or currently registered federated roots, checks captured hashes,
redacts the delivered representation, and returns at most 4096 UTF-8 bytes per
page. A changed or unavailable source falls back to the stored manifest preview
with an explicit integrity state instead of silently showing current content as
captured history.

Canonical reads made while the pack's session is active emit deterministic,
metadata-only `context.sources.expanded` receipts. Repeating a page is
idempotent; ended and archived pack reads remain read-only. Receipt bodies never
contain source content, are excluded from vector memory and summary excerpts,
and cannot change review status, decision IDs, lineage validity, or finish
eligibility. The audit funnel adds expansion counts before and after the
decision plus used sources that had no prior expansion.

For lower-level explicit pack handling:

```bash
agentdir context build "checkout failure tests" --emit --json
agentdir context consume --pack <pack-id> --source <source-id> --purpose plan
agentdir context cite --pack <pack-id> --source <source-id>
agentdir audit context --pack <pack-id>
```

Consuming every presented source through this lower-level path satisfies the
compatibility review. Partial legacy consumption can finish with a visible
`compatibility_partial` warning instead of requiring false use. New review-aware
packs reject cite-before-use; legacy v1 packs keep their historical citation
behavior without turning old sessions into new strict-audit failures.

The emitted pack stores a JSON source manifest and its bounded presentation as a
content-addressed artifact. Review, consumption, and citation events are
append-only records. Audits, status, and handoffs expose retrieved, presented,
reviewed, used, dismissed, pending, and cited counts; consumption outside the
bounded briefing is reported separately. Citation defaults only to
used sources and remains optional. This is an advisory audit trail for
cooperative agents; evidence claims still need evidence rows and fresh
verification.

For cross-repo memory, register roots explicitly:

```bash
agentdir roots suggest --near ..
agentdir roots register ../other-repo --name other-repo
agentdir roots list
agentdir roots doctor
agentdir memory search --federated "checkout failure tests"
agentdir context build --federated "checkout failure tests" --emit
```

Federated search does not copy full stores into one canonical database. It
searches registered child roots, returns root-qualified source IDs, and keeps
the child roots as the source of truth.

For repeated cross-repo work, create a group and use it as the memory plane:

```bash
agentdir roots group create product-work --member root-abc123def456
agentdir memory search --group product-work "checkout failure tests"
agentdir work start "checkout failure tests" --group product-work
```

Warm indexing is opt-in. Use it when repeated cross-repo or large-store work
would otherwise spend too much time rebuilding:

```bash
agentdir memory daemon start --group product-work
agentdir memory daemon status
agentdir memory daemon stop
```

Optional semantic retrieval is configuration-gated:

```bash
agentdir memory embeddings configure fastembed --model BAAI/bge-small-en-v1.5
agentdir memory backend configure sqlite-vec
agentdir memory search "checkout failure tests"
```

After configuration, automatic `work start`, context building, memory search,
and memory explanation fuse semantic and lexical signals. Use
`--retrieval semantic`, `--retrieval hybrid`, or `--retrieval document` only to
force a diagnostic comparison. If optional dependencies are missing, automatic
mode keeps the local hybrid retriever active; an explicitly forced semantic
request still reports the missing dependency clearly.

## Capturing Tool Calls And Results

```bash
agentdir run -- pytest -q
agentdir run -- npm test
agentdir run -- git diff --check
```

`agentdir run` streams command output to the terminal and records both the call and the result. Stored output is truncated at a bounded size and common secret-like patterns are redacted in the stored envelope. AgentDir also redacts common secret-like patterns from emitted message bodies before persistence. Agents should use this automatically for commands they run as evidence.

When captured output is truncated, `agentdir run` prints a warning to stderr and the stored `tool.result` carries an `X-AgentDir-Truncated` header. Downstream surfaces treat the evidence as partial: `evidence` marks the row `truncated=true`, `audit session` reports a `truncated_evidence` warning, and `audit claims` downgrades claims resting on truncated successful evidence from `supported` to `partial` (which fails `--strict`). Raise the limit with `--max-capture-bytes` when full output matters for a claim.

`agentdir run` supports `--timeout <seconds>` (kills the command and records exit 124), `--session require|create|auto|<id>` to control session placement, and `--json` for a machine-readable run summary after the streamed output.

Exit codes are structured for agents: 2 = user error, 3 = state error (no root or no active session — the error message names the recovery command), 4 = missing dependency, 5 = configuration error. With `--json`, failures also print `{"success": false, "exit_code": ..., "error": ..., "error_code": ...}` to stdout. Every command accepts `--quiet` for exit-code-only checks.

Do not wrap routine exploration commands such as `rg`, `sed`, `nl`, `cat`, `ls`, `find`, or quick read-only `git status` checks. Use plain shell commands while reading files, mapping code, or gathering low-level context. The evidence trail should capture verification, reproduced failures, important diagnostics, and final support for claims, not every glance at a file.

## Capturing Diffs As Artifacts

```bash
git diff > /tmp/agentdir.diff

agentdir emit \
  --type file.diff \
  --artifact /tmp/agentdir.diff \
  --body /tmp/agentdir.diff
```

The artifact is stored by SHA-256 and referenced from the envelope.

## Human And Agent Handoff

```bash
agentdir actor create engineer
agentdir actor create codex

printf 'Please review the failing test evidence before merge.\n' > /tmp/review-request.txt

agentdir send \
  --from codex \
  --to engineer \
  --type approval.requested \
  --body /tmp/review-request.txt
```

## Review And Replay

These commands are mainly for agents and for humans who want to inspect the record:

```bash
agentdir status
agentdir report final --format json
agentdir summarize
agentdir evidence --brief
agentdir timeline
agentdir memory search "similar failing verification"
agentdir memory search --federated "similar failing verification"
agentdir memory search --group product-work "similar failing verification"
agentdir context build "similar failing verification"
agentdir audit context --pack <pack-id>
agentdir replay --session "$(agentdir session current)"
agentdir doctor
```

If the SQLite index is deleted, rebuild it from the envelopes:

```bash
rm -f "$(agentdir root)/indexes/agentdir.sqlite3"
agentdir index rebuild
agentdir replay --session "$(agentdir session current)"
```

Agents should end the session when the task is done:

```bash
agentdir work finish --json --brief
```

## Store Hygiene

Retention commands are explicit user operations, not background maintenance. They default to dry-run and require `--apply` before moving or deleting session records.

```bash
agentdir archive --keep-recent 20
agentdir archive --older-than-days 30 --apply
agentdir prune --session old-session-id
agentdir prune --session old-session-id --apply
```

`agentdir archive` moves inactive sessions from `sessions/` to `archives/sessions/`, then rebuilds the active index when applied. `agentdir prune` deletes archived sessions by default. It only considers live `sessions/` when the user also passes `--include-live-sessions`. The current active session is protected.

## Git Hooks

`agentdir setup` installs managed shims for common local Git hooks. The shims preserve existing hooks by moving them to `*.agentdir-original`, run the original hook first, record the result, and return the original exit code.

```bash
agentdir hooks status
agentdir hooks install --hook pre-commit
agentdir hooks uninstall --hook pre-commit
```

Hook records use the active session when one exists. If no session is active, AgentDir starts a small automatic hook session.

## Agent Guidance

The generated Codex skill and project guidance tell coding agents that AgentDir is their responsibility, not a user checklist. They instruct agents to probe the resolved store instead of checking for a physical `.agentdir`, adopt only when needed, start with `work start`, read and decide on the bounded briefing, use `status`, search and explain memory, wrap commands with `agentdir run`, emit important evidence, audit session quality and final claims when useful, and close with `work finish` when practical.

```bash
agentdir integrations install all --target project
agentdir integrations doctor --json
agentdir skills install codex --target user
agentdir skills install codex --target project
agentdir skills install codex --target store
agentdir skills install generic --target project
agentdir skills install generic --target store
```

## Choosing The Storage Scope

Project scope is the default for coding agents:

```text
<repo>/.agentdir
```

Use it when the evidence belongs with a single repository.

Use user/global scope for personal cross-repo agent memory:

```bash
agentdir init --scope user
agentdir emit --scope user --session daily-agent-log --type user.message --body /tmp/note.txt
```

Use machine scope for shared workstation or CI-runner stores:

```bash
AGENTDIR_MACHINE_ROOT=/opt/agentdir agentdir init --scope machine
```

## Secret Hygiene

```bash
agentdir secrets scan
agentdir secrets redact
agentdir secrets redact --apply
```

`doctor` treats persisted secret-like envelope bodies as errors. `secrets scan`
prints only paths and pattern labels, not body content. `secrets redact` is a
dry run by default; `--apply` rewrites affected bodies with redaction markers
and rebuilds the derived SQLite index so old body copies are removed from
messages, FTS, and memory tables.

## Guardrails

- Do not emit secrets. Redaction catches common patterns only and is not a complete data-loss prevention system.
- Prefer workspace names over absolute paths when the record may be shared.
- Treat `cur` as local processing state only.
- Emit a new envelope for state changes instead of editing old message bodies.
- Keep large artifacts content-addressed instead of copying them into many records.
