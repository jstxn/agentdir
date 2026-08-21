# AgentDir

<p align="center">
  <img src="docs/assets/agentdir-agent-trace-demo.gif" alt="AgentDir records observable agent reasoning artifacts, tool calls, evidence, and handoffs across Codex, Claude, Pi, and other coding agents." />
</p>

<p align="center">
  <strong>Local-first memory and evidence for agentic engineering.</strong>
</p>

<p align="center">
  <a href="https://github.com/jstxn/agentdir/actions/workflows/ci.yml"><img src="https://github.com/jstxn/agentdir/actions/workflows/ci.yml/badge.svg" alt="CI status" /></a>
</p>

AgentDir is a flight recorder for coding agents. It lets agents record what
happened during a software task, then gives engineers a clean way to inspect,
replay, search, and audit that work later.

The goal is simple: AgentDir should be nearly invisible while you work.

Engineers should not have to manually start sessions, wrap commands, collect
evidence, or maintain agent memory by hand. Once a repository is adopted, the
agent operates AgentDir in the background and leaves behind a useful trail.

## Why AgentDir Exists

Agentic engineering has a trust gap.

Agents can edit code, run tools, and summarize results, but their work often
ends up scattered across chat history, terminal scrollback, temporary files, and
unverified final claims. That makes it hard to answer basic questions:

- What did the agent actually do?
- Which commands support the final answer?
- Were tests, builds, lint, or release checks really run?
- What context did the agent retrieve and rely on?
- Can a future agent learn from this session?
- Can the index or memory layer be rebuilt if it breaks?

AgentDir gives those answers a local, durable home.

## What You Get

| Capability | What it means |
| --- | --- |
| Invisible agent workflow | Agents run AgentDir commands during normal work, so engineers keep using their coding assistant normally. |
| Evidence-backed claims | Test, build, lint, typecheck, doctor, and release claims can be checked against recorded tool results. |
| Replayable sessions | Inspect the task, decisions, commands, outputs, blockers, summaries, and handoffs after the fact. |
| Local-first storage | Records live in the repo-local `.agentdir` directory by default. No hosted service is required. |
| Rebuildable memory | Raw event files are the source of truth. SQLite search and memory indexes can be rebuilt. |
| Context lineage | Agents review a bounded briefing, then record which sources were used, dismissed, skipped, or cited. |
| Cross-repo memory | Explicitly registered AgentDir roots can be searched together without moving canonical records. |
| Secret-aware persistence | Common secret-like patterns are redacted before storage, with scan and cleanup commands for older records. |

## The Invisible Workflow

AgentDir is designed around a small separation of responsibility.

| Human does | Agent does |
| --- | --- |
| Install AgentDir once. | Start and finish AgentDir sessions. |
| Run `agentdir adopt` once per repo. | Capture evidence-bearing commands with `agentdir run`. |
| Ask the coding agent to do work. | Record decisions, blockers, context, and handoffs. |
| Inspect evidence only when needed. | Audit session quality and final claims before reporting. |

In day-to-day use, the human workflow stays the same:

```text
Ask the agent to do the task.
Review the result.
Use AgentDir only when you want the trail.
```

The agent handles the recording surface:

```bash
agentdir work start "fix checkout failure"
agentdir work context --expand 1
agentdir work context --use 1 --reason "prior checkout pattern informs the plan"
agentdir run -- pytest -q
agentdir audit session
agentdir work finish --json --brief
```

## Install

Install from PyPI (the package is `agentdir-cli`; the command it installs is
`agentdir`):

```bash
uv tool install agentdir-cli
# or
pipx install agentdir-cli
```

Or use the GitHub Release installer:

```bash
curl -fsSL https://raw.githubusercontent.com/jstxn/agentdir/main/scripts/install.sh | bash
```

The installer uses `pipx` when available. Otherwise it creates a self-contained
virtual environment under `~/.local/share/agentdir` and links the CLI into
`~/.local/bin`. It does not edit Git configuration or ignore files; after
installation it prints the explicit adoption choices.

Verify:

```bash
agentdir --version
agentdir --help
```

Update an existing install to the latest release and refresh adoption for the
current repository:

```bash
agentdir update
agentdir update --dry-run
```

The older `agentdir --upgrade` interface remains supported for compatibility.

## Pi Package

Pi users can install this repository as a Pi package so the AgentDir skill is
available automatically during coding tasks:

Package page: [@jstxn/agentdir-pi](https://pi.dev/packages/@jstxn/agentdir-pi)

```bash
pi install npm:@jstxn/agentdir-pi@0.8.0
# or install directly from a local checkout / release tag:
pi install /absolute/path/to/agentdir
pi install git:github.com/jstxn/agentdir@<tag-or-commit>
```

The package exposes `skills/agentdir/SKILL.md`; it teaches Pi to start AgentDir
sessions, wrap evidence-bearing commands, use local memory/context packs, and
produce evidence-aware handoffs. See [AgentDir Pi Package](docs/PI_PACKAGE.md)
for details.

## Adopt A Repository

Run once from a git repository:

```bash
agentdir adopt
```

Coding agents use the non-interactive form below so `.agentdir/` is ignored at
the user level without creating a repository `.gitignore` change:

```bash
agentdir adopt --if-needed --gitignore user
```

This is intentionally boring setup. It prepares the repository once, then agents
operate AgentDir during normal coding work:

- creates the repo-local `.agentdir` store
- installs AgentDir-managed git hook shims
- installs the Codex skill into the user skill directory
- writes managed project guidance for common agent tools
- asks interactive users whether `.agentdir/` should be added to the project or
  user-level Git ignore file
- runs `doctor` to confirm the store is healthy

After that, agents have the guidance they need to use AgentDir without the
engineer manually operating it during normal work.

Preview setup without writing anything:

```bash
agentdir adopt --dry-run --json
```

If you want generated integration files to stay inside the AgentDir store
instead of project instruction files:

```bash
agentdir adopt --install-skill store --install-generic store --integration-target store
```

Adoption adapts to repos where other tools own these files. When
[rulesync](https://github.com/dyoshikawa/rulesync) generates the guidance
files, the managed rule is written to `.rulesync/rules/agentdir.md` instead so
it survives regeneration, and files with generated-file headers are never
edited without `--force`. When lefthook, husky, or pre-commit own the Git
hooks, adopt warns up front, `agentdir doctor` flags hook shims those tools
later overwrite, and `agentdir hooks install` restores them. See
[docs/INSTALL.md](docs/INSTALL.md#coexisting-with-rule-generators-and-hook-managers)
for details.

For non-interactive installs, choose the ignore destination explicitly:

```bash
agentdir adopt --gitignore project  # write <repo>/.gitignore
agentdir adopt --gitignore user     # write the user-level Git excludes file
agentdir adopt --gitignore none     # leave ignore files unchanged
```

Generated AgentDir guidance selects `--gitignore user` by default. The plain
interactive command still prompts, and all three explicit choices remain
available.

Undo managed setup while keeping the `.agentdir` evidence store:

```bash
agentdir unadopt          # dry-run
agentdir unadopt --apply  # remove managed hooks and guidance
```

## What Gets Written

Default adoption writes only local files:

```text
<repo>/.agentdir/                         # evidence, artifacts, indexes, state
<repo>/.agentdir/hooks.json               # installed-hook drift manifest
<active-hooks-directory>/*                # managed hook shims with backups
<repo>/AGENTS.md                          # generic / Codex-readable guidance
<repo>/CLAUDE.md                          # Claude Code guidance
<repo>/.github/copilot-instructions.md    # Copilot guidance
<repo>/.cursor/rules/agentdir.mdc         # Cursor guidance
<repo>/.windsurf/rules/agentdir.md        # Windsurf guidance
<repo>/.rulesync/rules/agentdir.md        # rulesync source, when detected
~/.codex/skills/agentdir/SKILL.md         # Codex skill, by default
```

Managed guidance is wrapped in AgentDir markers. Existing unmanaged content is
preserved where the target format supports managed blocks. The active hooks
directory is `.git/hooks` by default and follows `core.hooksPath` or linked
worktree configuration. In rulesync repos, the source rule replaces the listed
project guidance files as the managed source of truth.

## Git Worktrees

A repository has one store, shared by every `git worktree` checkout. Commands
run inside a linked worktree resolve to the main working tree's `.agentdir`, so
evidence and memory from all worktrees stay searchable together instead of
fragmenting into one store per branch.

Each worktree still keeps its own active session, so parallel agents in
different worktrees do not overwrite each other.

Agents should probe the resolved store instead of checking whether the current
checkout contains a physical `.agentdir`:

```bash
agentdir root --require --quiet
```

A zero exit means the local or shared store is ready. Exit 3 means the agent can
run `agentdir adopt --if-needed --gitignore user`. In a restricted linked
worktree that cannot write the shared Git hooks directory, it can retry with
`--no-hooks`; adoption preflights the hook target before creating the store or
writing guidance files.

Two cases keep a store inside the worktree instead:

```bash
# a store already present in the worktree always wins
AGENTDIR_WORKTREE_STORE=local agentdir adopt   # or opt out explicitly
```

`agentdir doctor` warns when a worktree holds a store separate from the main
one and names the command that joins them for search.

## Inspect A Session

Most users will not need these commands every day, but they are the reason
AgentDir exists.

```bash
agentdir status
agentdir evidence --brief
agentdir timeline
agentdir report final --format json
agentdir replay
agentdir memory search "checkout failure"
```

Without `--session`, the review commands use the active session when one
exists, then fall back to the latest completed session for the current
worktree. `status` keeps `session.current` reserved for active work and reports
the historical projection separately as `session.latest` and
`evidence.session_id`.

For final-answer support:

```bash
agentdir audit session
agentdir audit claims                              # recorded structured claims
agentdir audit claims --text final-response.md     # prose, when not instrumented
```

Audits are advisory by default. Use `--strict` when unsupported or contradicted
claims should fail a check.

## How AgentDir Works

AgentDir has one source of truth and several rebuildable views:

```text
raw envelopes -> SQLite index -> memory/search/audit/report
                    ^
                    |
             rebuilt from envelopes
```

1. **Raw envelopes**
   Each meaningful event is stored as an immutable file in a Maildir-inspired
   directory layout. These files are the source of truth.
2. **Derived indexes**
   SQLite indexes, search tables, memory passages, context packs, and audit
   views are derived from the raw event files and can be rebuilt.

Default project layout:

```text
<repo>/.agentdir/
  sessions/
  actors/
  artifacts/
  archives/
  indexes/agentdir.sqlite3
  state/
  integrations/
```

The important property is recoverability: deleting the derived index does not
destroy the session. AgentDir can rebuild from the envelope store.

The agent-facing report surface is JSON. `agentdir report final --format json`
and `agentdir work finish --json` include the full forensic report. Agents can
use `agentdir work finish --json --brief` to receive just the compact handoff,
Git state, health, and ended-session metadata. Both shapes include an
`agent_handoff` object with verification evidence, failed evidence, claim
support, context lineage, known gaps, and recommended next actions.

## Unique Capabilities

### Claims-To-Evidence Checks

Agents record what a check showed, and AgentDir compares that against the
recorded tool results. It does this deterministically, not with an LLM.

```bash
agentdir claim test --passed
agentdir claim build --failed --note "linker error in release profile"
agentdir claim list
agentdir audit claims           # checks recorded claims against evidence
agentdir audit claims --strict  # exit 1 when a claim is not supported
```

A structured claim names its family and outcome outright, so checking it is a
comparison rather than an interpretation of prose:

| Claim | Evidence | Result |
| --- | --- | --- |
| passed | succeeded | `supported` |
| passed | failed | `contradicted` |
| failed | failed | `acknowledged` |
| failed | succeeded | `contradicted` |
| either | none recorded | `unsupported` |
| none recorded | failed | `unreviewed` |

Recording a family again replaces its earlier claim, so a check re-run after a
fix supersedes rather than accumulates. Claiming a failure honestly is
`acknowledged` and does not count against the audit.

Tool evidence remains append-only. When a newer result in the same evidence
family passes, earlier failures remain available as historical and resolved
evidence, while strict session audit and the agent handoff report only failures
whose family is still failing.

A claim made in error can be withdrawn:

```bash
agentdir claim build --retract
```

Claims are append-only events, so retracting supersedes the earlier claim in the
latest-claim view while leaving both in the record.

Recorded claims also reach the `agent_handoff` object without any final text
being supplied.

Supported claim families:

- test
- lint
- typecheck
- build
- doctor
- release

#### Auditing prose instead

For final responses that were not instrumented, `agentdir audit claims --text`
reads claims out of prose:

```bash
agentdir audit claims --text final-response.md
```

This path has to infer intent from wording, so prefer recorded claims when the
agent can emit them.

Claim detection is keyword-based, so ordinary phrasing such as "everything
works" or "verified locally" matches no family. When recorded evidence failed
and the text makes no checkable claim about it, the audit reports that family as
`unreviewed` and is not `ok`, rather than passing because it found nothing to
check. `claims_detected` reports how many claims were actually parsed, so
"nothing to audit" is never mistaken for "audited and clean".

Text that states the failure instead ("tests failed", "two tests fail") is
reported as `acknowledged` and does not count against the audit, so honest
failure reporting is never flagged like text that hid the failure. Failure
vocabulary asserting success ("no test failures") is treated as a success claim
and checked against evidence like any other.

### Context Packs

`work start` retrieves, persists, and prints a bounded briefing by default, with
numbered sources, excerpts, source classes, and match quality. `--no-context`
skips retrieval but still persists a zero-source opt-out marker, so a new task
cannot inherit an older task's context. The agent must then record one honest
decision:

```bash
# when the preview looks useful but is too short:
agentdir work context --expand 1
agentdir work context --expand 1 --page 2
# then record the decision:
agentdir work context --use 1 --reason "prior failure constrains the repair"
# or, when the briefing does not help:
agentdir work context --none-relevant --reason "only generic build logs matched"
# after a restart or lost output:
agentdir work context --show
```

`work finish` keeps every non-empty briefing in the session open until the agent
records used sources, no relevant context, or an explicit skipped review. A
newer pack cannot hide an older pending pack. Status, audits, and handoffs
report the funnel as retrieved, presented, reviewed, used, dismissed, pending,
and cited, including invalid cite-before-use lineage. Lower-level consumption
of omitted sources is reported separately so `used` never exceeds `reviewed`.
Citation remains optional and separate from use. Printed review commands are
bound to the displayed pack, so reopening an older briefing cannot accidentally
decide a newer one.

Briefing excerpts are labeled previews. `work context --expand <number>` reads
the retained canonical source through the same pack-local number without making
a review decision. Output is redacted and capped to 4096 UTF-8 bytes per page;
plain and JSON output identify integrity (`verified`, `legacy_unverified`,
`changed`, or `unavailable`) and extent (`full`, `bounded`, or
`stored_excerpt`). Digest drift or an unavailable federated root returns only
the immutable, redacted manifest preview rather than presenting changed text as
historical truth.

While the pack-owning session is active, a successful canonical expansion emits
one idempotent, metadata-only receipt. The source body is never copied into the
receipt or searchable memory. Expansion remains optional and never changes the
terminal decision or blocks `work finish`; audits, status, and handoffs report
expanded-before-decision, expanded-after-decision, and used-without-prior-
expansion counts. Retained ended or archived packs can still be read directly,
but historical reads do not append new receipts or re-enter active search.

The lower-level context interface remains available for explicit packs:

```bash
agentdir context build "checkout failure" --emit
agentdir context consume --pack <pack-id> --source <source-id> --purpose plan
agentdir context cite --pack <pack-id>
agentdir audit context --pack <pack-id>
```

Consuming every presented source through the lower-level interface completes a
compatibility review. A partial legacy consume can finish with a visible
`compatibility_partial` warning rather than forcing false consumption. A
terminal `work context` decision cannot be changed by a later lower-level
consume. Session-scoped lifecycle locking makes pack creation linearize before
the finish audit; an emitter that loses the race to an ended session is rejected.
Manifest digests, creation-event identities, decision integrity headers, and
source references are validated before lineage can be certified.

AgentDir cannot prove model attention. It can show what was presented, the
agent's reasoned decision, which sources were actually used, and whether those
used sources were later cited.

### Local Agent Memory

AgentDir builds searchable local memory from prior sessions. Agents can search
similar work, explain why a memory hit matched, and include relevant history in
new context packs.

```bash
agentdir memory search "release evidence"
agentdir memory explain "release evidence"
agentdir context build "release evidence" --emit
```

Retrieval mode defaults to `auto`. Without optional dependencies it keeps the
built-in local hybrid path. When FastEmbed is installed and configured for the
store, the same `work start`, `context build`, `memory search`, and
`memory explain` commands automatically fuse semantic and lexical scores while
preserving both components in JSON output. Explicit `--retrieval` modes remain
available for diagnostics and comparisons.

### Federated Memory

For multi-repo work, AgentDir can search explicitly registered roots:

```bash
agentdir roots register ../other-repo --name other-repo
agentdir memory search --federated "release evidence"
```

Each repository remains the canonical owner of its own `.agentdir` store.

## Safety Model

AgentDir is local-first and advisory by design.

- It records what agents choose to record.
- It does not replace code review or CI.
- It does not send data to a hosted AgentDir service.
- It treats raw envelopes as the source of truth.
- It redacts common secret-like patterns before persistence.
- It provides `secrets scan` and `secrets redact --apply` for cleanup.

Useful commands:

```bash
agentdir doctor
agentdir secrets scan
agentdir secrets redact
agentdir secrets redact --apply
```

## Upgrade And Rollback

Upgrade an existing install and refresh current repo adoption:

```bash
agentdir --upgrade
```

Rollback to the previous stable release:

```bash
curl -fsSL https://raw.githubusercontent.com/jstxn/agentdir/main/scripts/rollback.sh | bash
```

## Learn More

- [Install Guide](docs/INSTALL.md)
- [Agentic Coding Guide](docs/AGENTIC_CODING.md)
- [Technical Brief](docs/TECH_BRIEF.md)
- [Release Guide](docs/RELEASING.md)
- [PRD](docs/PRD.md)

## Project Status

AgentDir is beta software for local-first agentic engineering workflows. The
core model is stable: agents operate the recorder, engineers get the evidence,
and raw local envelopes remain the recoverable source of truth.
