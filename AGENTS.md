<!-- agentdir-managed-generic:start -->
# AgentDir

Use AgentDir as the local flight recorder for coding-agent work in this repository.

The engineer should not have to run AgentDir commands during normal coding work.
The agent owns the background recording flow:

- Start non-trivial coding work with `agentdir work start "<short task>" --emit-context`.
- If `.agentdir` is missing, run `agentdir adopt --gitignore user` once so the
  local store stays out of Git without changing the repository's `.gitignore`.
- Run evidence-bearing commands through `agentdir run -- <command>`.
- Evidence-bearing commands include tests, lint, typecheck, build, release checks,
  reproduced failures, and diagnostics that support final claims.
- Do not wrap routine exploration commands such as `rg`, `sed`, `nl`, `cat`, `ls`,
  `find`, or quick read-only `git status` checks.
- Use `agentdir status` for a single view of session, evidence, memory, context,
  registered roots, and doctor health.
- Use `agentdir evidence --brief` and `agentdir timeline` to skim recorded work.
- Record each verification the final response relies on with
  `agentdir claim <family> --passed|--failed`, e.g. `agentdir claim test --passed`.
  Families: test, lint, typecheck, build, doctor, release.
- Record failures as failures. A claim that overstates a result is reported as
  contradicted; an honest failure claim is acknowledged.
- Re-recording a family replaces its earlier claim; withdraw one made in error
  with `agentdir claim <family> --retract`.
- Use `agentdir audit session` and `agentdir audit claims` before
  final claims when evidence support matters.
- Before the final response, run `agentdir work finish --json` when practical.
  Use `agentdir report final --format json` to preview the same agent handoff
  without ending the session.
- Read the `agent_handoff` object before making final verification claims.
- Do not record secrets, private keys, raw environment dumps, or credential-bearing
  command output.
<!-- agentdir-managed-generic:end -->
