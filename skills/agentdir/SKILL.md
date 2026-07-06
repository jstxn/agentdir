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

1. If `.agentdir` is missing, run `agentdir adopt` once before work begins.
2. Start the session with `agentdir work start "<short task>" --emit-context`.
3. Keep any returned context pack id/source ids if you plan to cite or consume retrieved context later.

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

- `agentdir work start "<task>" --emit-context` is the normal entry point.
- Use `agentdir context build "<task>" --emit` when retrieved context should become an auditable context pack.
- Use `agentdir context consume --pack <pack-id> --source <source-id> --purpose plan|tool|answer|handoff` when you rely on retrieved context.
- Use `agentdir context cite --pack <pack-id>` or `agentdir audit context --pack <pack-id>` when reporting source lineage.
- Use `agentdir memory search "<task, error, or subsystem>"` and `agentdir memory explain "<same query>"` when prior local work may help.
- Use `agentdir roots suggest` and `agentdir roots doctor` to inspect cross-repo memory. Register roots only when cross-repo memory is explicitly requested or clearly part of the task.

Emit important plans, blockers, diffs, review decisions, and final handoffs as immutable events when they matter for future replay or audit.

## Exit Codes And Structured Errors

- 0 success; `agentdir run` passes through the wrapped command's exit code (124 = `--timeout` kill).
- 2 user error, 3 state error (no root / no active session), 4 missing dependency, 5 configuration error.
- State errors name the recovery command in the message (`agentdir adopt`, `agentdir work start`).
- Add `--json` to get a `{"success": false, "exit_code": ..., "error_code": ...}` envelope on failure; add `--quiet` for exit-code-only checks.
- `agentdir run --session require` fails fast instead of auto-creating a session; `--session create` forces a fresh one.

## Finish Work

Before the final response, run `agentdir work finish --json` when practical. Read the `agent_handoff` object before making final verification claims.

If you need to preview the final handoff without ending the active session, run `agentdir report final --format json` instead.

Before claiming tests, builds, lint, typecheck, hooks, release checks, or doctor checks passed, verify the latest recorded evidence with `agentdir evidence --brief`.
