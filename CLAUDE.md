<!-- agentdir-managed-claude:start -->
# AgentDir for Claude Code

AgentDir is agent-owned background instrumentation for this repository.
The engineer installs or adopts it once; agents operate it during normal work.

- Before starting work in an unfamiliar checkout, run `agentdir root --require --quiet`. Exit 0 means an initialized local or shared worktree store is ready; do not decide whether AgentDir is set up from the presence of `.agentdir` in this checkout.
- If the probe exits 3, run `agentdir adopt --if-needed --gitignore user`. If hook installation is blocked in a restricted linked worktree, rerun it with `--no-hooks`.
- Then start non-trivial coding work with `agentdir work start "<short task>"`.
- Read the printed context briefing, treat excerpts as previews, and prefer the printed `agentdir work context --pack <pack-id> --expand <number>` command before using a source when implementation details, prior patterns, or exact evidence matter. Then record either useful numbered sources with `agentdir work context --use <number> --reason "<how it helps>"` or `agentdir work context --none-relevant --reason "<why>"`. Use `--skip --reason "<why>"` only when review is impossible; no decision is needed when no sources are presented. Re-open lost output with `agentdir work context --show`.
- Wrap evidence-bearing commands with `agentdir run -- <command>`.
- Evidence includes tests, lint, typecheck, builds, doctor checks, release checks, reproduced failures, and diagnostics used in final claims.
- Do not wrap routine exploration such as `rg`, `sed`, `nl`, `cat`, `ls`, `find`, or quick read-only `git status`.
- Use `agentdir evidence --brief` and `agentdir timeline` to skim what happened.
- Record each verification you will rely on with `agentdir claim <family> --passed|--failed`, e.g. `agentdir claim test --passed`. Families: test, lint, typecheck, build, doctor, release.
- Claim what the evidence shows, including failures. `agentdir audit claims` compares recorded claims against evidence, so a claim that overstates a result is reported as contradicted.
- Re-recording a family replaces its earlier claim; withdraw one made in error with `agentdir claim <family> --retract`.
- Use `agentdir report final --format json` or `agentdir work finish --json` for the agent handoff object before final claims when practical.
- Do not record secrets, private keys, raw environment dumps, or credential-bearing command output.
<!-- agentdir-managed-claude:end -->
