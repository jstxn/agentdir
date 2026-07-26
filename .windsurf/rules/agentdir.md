---
trigger: always_on
---

<!-- agentdir-managed-windsurf:start -->
# AgentDir for Windsurf

AgentDir is agent-owned background instrumentation for this repository.
The engineer installs or adopts it once; agents operate it during normal work.

- Start non-trivial coding work with `agentdir work start "<short task>" --emit-context`.
- If `.agentdir` is missing, run `agentdir adopt --gitignore user` once so the local store stays out of Git without changing the repository's `.gitignore`.
- Wrap evidence-bearing commands with `agentdir run -- <command>`.
- Evidence includes tests, lint, typecheck, builds, doctor checks, release checks, reproduced failures, and diagnostics used in final claims.
- Do not wrap routine exploration such as `rg`, `sed`, `nl`, `cat`, `ls`, `find`, or quick read-only `git status`.
- Use `agentdir evidence --brief` and `agentdir timeline` to skim what happened.
- Record each verification you will rely on with `agentdir claim <family> --passed|--failed`, e.g. `agentdir claim test --passed`. Families: test, lint, typecheck, build, doctor, release.
- Claim what the evidence shows, including failures. `agentdir audit claims` compares recorded claims against evidence, so a claim that overstates a result is reported as contradicted.
- Re-recording a family replaces its earlier claim; withdraw one made in error with `agentdir claim <family> --retract`.
- Use `agentdir report final --format json` or `agentdir work finish --json` for the agent handoff object before final claims when practical.
- Do not record secrets, private keys, raw environment dumps, or credential-bearing command output.
<!-- agentdir-managed-windsurf:end -->
