---
name: agentdir
description: Use AgentDir as a local-first flight recorder for non-trivial coding-agent work in software repositories. Load at the start of coding tasks, before tests/lint/build/typecheck, and when asked about AgentDir evidence, audits, session replay, memory, context packs, adoption, or handoffs.
license: MIT
compatibility: Requires the agentdir CLI on PATH; works best inside a git repository.
---

# AgentDir for Pi

AgentDir is local-first memory and evidence capture for agentic engineering. Use it as agent-owned background instrumentation: the engineer should not have to start sessions, wrap checks, collect evidence, or maintain memory by hand during normal coding work.

This Pi package teaches Pi how to operate AgentDir. It does not install the `agentdir` CLI. If `agentdir` is not on `PATH`, tell the user AgentDir needs to be installed and continue without recording unless they ask you to set it up.

## Safety Rules

- Do not record secrets, private keys, raw environment dumps, or credential-bearing command output.
- If `agentdir doctor` reports secret-like persisted bodies, do not print those bodies. Use `agentdir secrets scan` for path-only triage and `agentdir secrets redact --apply` only when cleanup is approved.
- Use Pi's normal file tools for reading and editing files. `agentdir run --` wraps shell commands only; it does not wrap Pi `read`, `edit`, or `write` tool calls.

## Start Work

At the start of a non-trivial coding task in a software repository:

1. Probe with `agentdir root --require --quiet`. Exit 0 means AgentDir found an initialized local or shared linked-worktree store. Do not decide whether AgentDir is set up from the presence of `.agentdir` in this checkout.
2. If the probe exits 3 with `Not an AgentDir root`, run `agentdir adopt --if-needed --gitignore user`. If hook installation is blocked in a restricted linked worktree, rerun it with `--no-hooks`.
3. Start the session yourself with `agentdir work start "<short task>"`; it records a bounded context briefing by default.
   Retrieval is automatic: configured FastEmbed stores use fused semantic and
   lexical matching, while other stores keep the built-in hybrid path. Use the
   default invocation without a retrieval flag during normal work.
4. Read its compact context briefing. If the original output is unavailable, re-open the persisted briefing with `agentdir work context --show` before deciding.
5. Treat excerpts as previews. Prefer the printed `agentdir work context --pack <pack-id> --expand <number>` command before marking a source used when implementation details, prior patterns, or exact evidence matter. Continue long sources with `--page <number>`; expansion itself is not a decision.
6. After reading and any useful expansion, record useful numbered sources with `agentdir work context --use <number> --reason "<how it helps>"`, or record `agentdir work context --none-relevant --reason "<why>"` when none help. Use `--skip --reason "<why>"` only when review is impossible. No decision is needed when no sources are presented.

Use `agentdir status` when you need one compact view of the active session, evidence, memory, context, registered roots, and doctor health.

## Evidence-Bearing Commands

Run evidence-bearing shell commands through AgentDir:

```bash
agentdir run -- pytest -q
agentdir run -- npm test
agentdir run -- npm run lint
agentdir run -- npm run typecheck
agentdir run -- npm run build
```

Evidence-bearing commands include tests, lint, typecheck, build, release checks, reproduced failures, doctor checks, and diagnostics used to support final claims.

Do not wrap routine exploration commands such as `rg`, `sed`, `nl`, `cat`, `ls`, `find`, or quick read-only `git status` checks. Use plain shell commands for reading files, mapping code, and gathering low-level context.

Use `agentdir evidence --brief` and `agentdir timeline` to skim what has been recorded.

## Context And Memory

- `agentdir work start "<task>"` is the normal entry point.
- `agentdir work context` is the normal read-and-decide path. Every non-empty briefing must end in a used, no-relevant, or skipped decision before `work finish`.
- `agentdir work context --expand <number>` is the normal deep-read path for a promising preview. Prefer expanding before use when the task depends on more than the preview; integrity and read-before-use metrics remain visible in the handoff.
- Use `agentdir context build "<task>" --emit` when retrieved context should become an auditable context pack.
- Use `agentdir context consume --pack <pack-id> --source <source-id> --purpose plan|tool|answer|handoff` only for lower-level explicit pack handling.
- Cite only sources actually used. Use `agentdir context cite --pack <pack-id>` or `agentdir audit context --pack <pack-id>` when reporting source lineage.
- Use `agentdir memory search "<task, error, or subsystem>"` and `agentdir memory explain "<same query>"` when prior local work may help.
- `memory explain` follows the same automatic retrieval mode as search and
  reports the actual mode and component scores. Force `--retrieval` only when
  comparing or diagnosing backends.
- Use `agentdir roots suggest` and `agentdir roots doctor` to inspect cross-repo memory. Register roots only when cross-repo memory is explicitly requested or clearly part of the task.

Emit important plans, blockers, diffs, review decisions, and final handoffs as immutable events when they matter for future replay or audit.

## Exit Codes And Structured Errors

- 0 success; `agentdir run` passes through the wrapped command's exit code (124 = `--timeout` kill).
- 2 user error, 3 state error (no root / no active session), 4 missing dependency, 5 configuration error.
- State errors name the recovery command in the message (`agentdir adopt`, `agentdir work start`).
- Add `--json` to get a `{"success": false, "exit_code": ..., "error_code": ...}` envelope on failure; add `--quiet` for exit-code-only checks.
- `agentdir run --session require` fails fast instead of auto-creating a session; `--session create` forces a fresh one.

## Record Claims

After running a check you will rely on in the final response, record what it showed:

```bash
agentdir claim test --passed
agentdir claim build --failed --note "linker error in release profile"
agentdir claim list
```

Families: `test`, `lint`, `typecheck`, `build`, `doctor`, `release`. Re-recording a family replaces the earlier claim, so record again after a fix. Withdraw a claim you should not have made with `agentdir claim <family> --retract`.

`agentdir audit claims` (no `--text`) compares recorded claims against evidence, with no wording involved. A claim that overstates a result is reported `contradicted`; failed evidence with no claim about it is `unreviewed`. Claiming a failure honestly is `acknowledged` and does not count against the audit, so record failures rather than omitting them.

## Finish Work

Before the final response, run `agentdir work finish --json` when practical. Read the `agent_handoff` object before making final verification claims.

If you need to preview the final handoff without ending the active session, run `agentdir report final --format json` instead.

Before claiming tests, builds, lint, typecheck, hooks, release checks, or doctor checks passed, verify the latest recorded evidence with `agentdir evidence --brief`.

`agentdir audit claims` matches claim keywords, so vague phrasing ("everything works", "verified locally") is not checkable. When evidence failed and the text claims nothing about it, the family is reported `unreviewed` and the audit is not `ok`. Name the check and its result plainly so the claim can be verified against evidence.

Reporting a failure plainly ("tests failed", "two tests still fail") is recorded as `acknowledged` and does not count against the audit. Report failing checks rather than writing around them.
