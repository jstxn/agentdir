<!-- agentdir-managed-generic:start -->
# AgentDir

Use AgentDir as the local flight recorder for coding-agent work in this repository.

The engineer should not have to run AgentDir commands during normal coding work.
The agent owns the background recording flow:

- Before starting work in an unfamiliar checkout, run
  `agentdir root --require --quiet`. Exit 0 means an initialized local or shared
  worktree store is ready; do not decide whether AgentDir is set up from the
  presence of `.agentdir` in this checkout.
- If the probe exits 3, run `agentdir adopt --if-needed --gitignore user`.
  If hook installation is blocked in a restricted linked worktree, rerun it
  with `--no-hooks`.
- Then start non-trivial coding work yourself with
  `agentdir work start "<subsystem: distinctive behavior or constraint>"`.
  Keep the title concise but retrieval-specific: name the subsystem plus a
  distinctive behavior or constraint, preserving request terms likely to match
  prior work.
- Retrieval is automatic. Configured FastEmbed stores use fused semantic and
  lexical matching; other stores keep the built-in hybrid path. Use the default
  invocation without a retrieval flag during normal work.
- Read the printed context briefing, re-opening it with
  `agentdir work context --show` when the original output is unavailable. Treat briefing excerpts as
  previews. Prefer the printed
  `agentdir work context --pack <pack-id> --expand <number>` command before marking a source
  used when implementation details, prior patterns, or exact evidence matter.
  Expansion is optional and does not replace the terminal review decision.
- After reading and any useful expansion, close the review once. Repeat `--use`
  in that command for every useful numbered source:
  `agentdir work context --use <number> [--use <number> ...] --reason "<how they help>"`, or record a
  reasoned `agentdir work context --none-relevant --reason "<why>"` decision.
  Use `--skip --reason "<why>"` only when review is impossible; no decision is
  needed when the briefing presents no sources.
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
- Before the final response, run `agentdir work finish --json --brief` when practical.
  Use `agentdir report final --format json` to preview the same agent handoff
  without ending the session.
- Read the `agent_handoff` object before making final verification claims.
- Do not record secrets, private keys, raw environment dumps, or credential-bearing
  command output.
<!-- agentdir-managed-generic:end -->
