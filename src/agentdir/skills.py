from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .git import git_root
from .store import AgentDirError, init_root, paths_for

MANAGED_SKILL_MARKER = "<!-- agentdir-managed-skill -->"

CODEX_SKILL = f"""---
name: agentdir
description: Automatically record coding-agent sessions with AgentDir when working in a software repository.
---
{MANAGED_SKILL_MARKER}

# AgentDir

Use AgentDir as the local flight recorder for coding-agent work.

The user should not have to run AgentDir commands during normal coding work.
Treat AgentDir as agent-owned background instrumentation once it is installed.
Run the commands yourself when they are useful, and only mention AgentDir to the
user when reporting evidence, blockers, or setup problems.

## Start

- At the start of a coding task in a repository, run `agentdir work start "<short task>" --emit-context`.
- If `.agentdir` is missing and the task is non-trivial, run `agentdir adopt` once for the repository before work begins.
- Prefer the default project store. It writes to the nearest repo `.agentdir`.
- Do not record secrets, private keys, raw environment dumps, or credential-bearing command output.
- If `doctor` reports secret-like persisted bodies, do not print the bodies.
  Use `agentdir secrets scan` for path-only triage and
  `agentdir secrets redact --apply` when cleanup is approved.
- Use `agentdir status` when you need one health view for the active session,
  evidence, context pack, memory index, doctor result, and registered roots.

## Tool Calls

- Run evidence-bearing commands through `agentdir run -- <command>`.
- Evidence-bearing commands include tests, lint, typecheck, build, release checks, reproduced failures, and diagnostics that support a final claim.
- Do not wrap routine exploration commands such as `rg`, `sed`, `nl`, `cat`, `ls`, `find`, or quick read-only `git status` checks.
- Use plain shell commands while reading files, mapping code, or gathering low-level context.
- `agentdir run` records both the tool call and the tool result, while still streaming command output to the terminal.
- Do not wrap command chains unless the whole chain is evidence worth preserving.
- If a command must not be wrapped, emit the evidence afterward with `agentdir emit`.

## During Work

- `agentdir work start "<task>" --emit-context` is the normal entry point. Use
  lower-level context commands only when you need finer control.
- Use `agentdir context build "<task>" --emit` when the retrieved context should become an auditable context pack.
- Use `agentdir context consume --pack <pack-id> --source <source-id> --purpose plan|tool|answer|handoff` when you rely on retrieved context.
- Use `agentdir context cite --pack <pack-id>` or `agentdir audit context --pack <pack-id>` when reporting source lineage.
- Use `agentdir memory search "<task, error, or subsystem>"` and `agentdir memory explain "<same query>"` when you need to inspect retrieval.
- Use `agentdir roots suggest` and `agentdir roots doctor` to inspect available
  cross-repo memory without mutating registrations.
- Use `agentdir roots register <root-or-repo>` only when cross-repo memory has
  been explicitly requested or is clearly part of the task.
- Prefer root groups for repeated cross-repo work, then use
  `agentdir memory search --group <name> "<query>"` or
  `agentdir work start "<task>" --group <name> --emit-context`.
- Emit important plans, blockers, diffs, review decisions, and final handoffs as immutable events.
- Use `agentdir memory daemon status` to inspect warm indexing when repeated
  large-store or cross-repo work depends on fresh memory.
- Use `agentdir memory embeddings configure fastembed` or
  `agentdir memory backend configure sqlite-vec` only when optional semantic
  extras are intentionally installed for this environment.
- Use `agentdir index rebuild` if query, replay, memory, summarize, or evidence output looks stale.
- Use `agentdir evidence` before claiming tests or checks passed.

## Finish

- Before the final response, run `agentdir work finish` when practical. It emits
  a final report, checks evidence and context lineage, runs doctor, and ends the
  active session.
- Use `agentdir report final` to preview the same report without ending the session.
- Use `agentdir evidence` when the final response claims tests, builds, hooks, or release checks passed.
- Use lower-level `agentdir summarize`, `agentdir evidence`, `agentdir doctor`,
  and `agentdir session end` only when the workbench command is not appropriate.
"""


@dataclass(frozen=True)
class InstalledSkill:
    path: Path
    target: str
    updated: bool = False
    backup_path: Path | None = None


def install_codex_skill(
    root: str | Path,
    *,
    target: str = "user",
    force: bool = False,
    cwd: str | Path | None = None,
) -> InstalledSkill:
    destination = codex_skill_path(root, target=target, cwd=cwd)
    updated = False
    backup_path: Path | None = None
    if destination.exists() and not force:
        existing = destination.read_text(encoding="utf-8", errors="ignore")
        if existing != CODEX_SKILL:
            if not is_agentdir_managed_skill(existing):
                raise AgentDirError(f"Refusing to overwrite existing Codex skill: {destination}")
            backup_path = destination.with_suffix(destination.suffix + ".bak")
            backup_path.write_text(existing, encoding="utf-8")
            updated = True
        else:
            return InstalledSkill(path=destination, target=target)
    elif destination.exists() and force:
        existing = destination.read_text(encoding="utf-8", errors="ignore")
        updated = existing != CODEX_SKILL
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(CODEX_SKILL, encoding="utf-8")
    return InstalledSkill(path=destination, target=target, updated=updated, backup_path=backup_path)


def is_agentdir_managed_skill(text: str) -> bool:
    if MANAGED_SKILL_MARKER in text:
        return True
    return "name: agentdir" in text and "# AgentDir" in text


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
