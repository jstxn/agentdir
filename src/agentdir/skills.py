from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .git import git_root
from .store import AgentDirError, init_root, paths_for

CODEX_SKILL = """---
name: agentdir
description: Automatically record coding-agent sessions with AgentDir when working in a software repository.
---

# AgentDir

Use AgentDir as the local flight recorder for coding-agent work.

The user should not have to run AgentDir commands during normal coding work.
Treat AgentDir as agent-owned background instrumentation once it is installed.
Run the commands yourself when they are useful, and only mention AgentDir to the
user when reporting evidence, blockers, or setup problems.

## Start

- At the start of a coding task in a repository, run `agentdir session ensure --title "<short task>"`.
- If `.agentdir` is missing and the task is non-trivial, run `agentdir setup` once for the repository before work begins.
- Prefer the default project store. It writes to the nearest repo `.agentdir`.
- Do not record secrets, private keys, raw environment dumps, or credential-bearing command output.

## Tool Calls

- Run evidence-bearing commands through `agentdir run -- <command>`.
- Evidence-bearing commands include tests, lint, typecheck, build, release checks, reproduced failures, and diagnostics that support a final claim.
- Do not wrap routine exploration commands such as `rg`, `sed`, `nl`, `cat`, `ls`, `find`, or quick read-only `git status` checks.
- Use plain shell commands while reading files, mapping code, or gathering low-level context.
- `agentdir run` records both the tool call and the tool result, while still streaming command output to the terminal.
- Do not wrap command chains unless the whole chain is evidence worth preserving.
- If a command must not be wrapped, emit the evidence afterward with `agentdir emit`.

## During Work

- Before non-trivial work, run `agentdir context build "<task, error, or subsystem>"` to gather relevant memory, evidence, and recent session summaries.
- Use `agentdir memory search "<task, error, or subsystem>"` and `agentdir memory explain "<same query>"` when you need to inspect retrieval.
- Emit important plans, blockers, diffs, review decisions, and final handoffs as immutable events.
- Use `agentdir index rebuild` if query, replay, memory, summarize, or evidence output looks stale.
- Use `agentdir evidence` before claiming tests or checks passed.

## Finish

- Before the final response, run `agentdir summarize` and `agentdir doctor` when practical.
- Use `agentdir evidence` when the final response claims tests, builds, hooks, or release checks passed.
- End the active session with `agentdir session end --summary "<what changed and what was verified>"` when the task is complete.
"""


@dataclass(frozen=True)
class InstalledSkill:
    path: Path
    target: str


def install_codex_skill(
    root: str | Path,
    *,
    target: str = "user",
    force: bool = False,
    cwd: str | Path | None = None,
) -> InstalledSkill:
    destination = codex_skill_path(root, target=target, cwd=cwd)
    if destination.exists() and not force:
        existing = destination.read_text(encoding="utf-8", errors="ignore")
        if existing != CODEX_SKILL:
            raise AgentDirError(f"Refusing to overwrite existing Codex skill: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(CODEX_SKILL, encoding="utf-8")
    return InstalledSkill(path=destination, target=target)


def codex_skill_path(root: str | Path, *, target: str, cwd: str | Path | None = None) -> Path:
    if target == "user":
        return Path.home().expanduser() / ".codex" / "skills" / "agentdir" / "SKILL.md"
    if target == "store":
        init_root(root)
        return paths_for(root).integrations / "codex" / "skills" / "agentdir" / "SKILL.md"
    if target == "project":
        project = git_root(cwd)
        if project is None:
            raise AgentDirError("Project skill target requires a git repository")
        return project / ".agents" / "skills" / "agentdir" / "SKILL.md"
    raise AgentDirError("Unknown Codex skill target; expected user, project, or store")
