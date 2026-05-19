from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .git import git_root
from .store import AgentDirError, init_root, paths_for

MANAGED_SKILL_MARKER = "<!-- agentdir-managed-skill -->"
GENERIC_GUIDANCE_START = "<!-- agentdir-managed-generic:start -->"
GENERIC_GUIDANCE_END = "<!-- agentdir-managed-generic:end -->"
INTEGRATION_NAMES = ("generic", "codex", "claude", "copilot", "cursor", "windsurf")
BROAD_PROJECT_INTEGRATIONS = ("generic", "claude", "copilot", "cursor", "windsurf")

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
- Use `agentdir evidence --brief` and `agentdir timeline` when you need a
  compact skim of what has been recorded.

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
- Use `agentdir evidence --brief` before claiming tests or checks passed.

## Finish

- Before the final response, run `agentdir work finish --json` when practical.
  It emits a final report, checks evidence and context lineage, runs doctor, and
  ends the active session.
- Use `agentdir report final --format json` to preview the same agent handoff
  without ending the session.
- Read the `agent_handoff` object before making final verification claims.
- Use `agentdir evidence --brief` when the final response claims tests, builds,
  hooks, or release checks passed.
- Use lower-level `agentdir summarize`, `agentdir evidence`, `agentdir doctor`,
  and `agentdir session end` only when the workbench command is not appropriate.
"""

GENERIC_GUIDANCE_BLOCK = f"""{GENERIC_GUIDANCE_START}
# AgentDir

Use AgentDir as the local flight recorder for coding-agent work in this repository.

The engineer should not have to run AgentDir commands during normal coding work.
The agent owns the background recording flow:

- Start non-trivial coding work with `agentdir work start "<short task>" --emit-context`.
- If `.agentdir` is missing, run `agentdir adopt` once before work begins.
- Run evidence-bearing commands through `agentdir run -- <command>`.
- Evidence-bearing commands include tests, lint, typecheck, build, release checks,
  reproduced failures, and diagnostics that support final claims.
- Do not wrap routine exploration commands such as `rg`, `sed`, `nl`, `cat`, `ls`,
  `find`, or quick read-only `git status` checks.
- Use `agentdir status` for a single view of session, evidence, memory, context,
  registered roots, and doctor health.
- Use `agentdir evidence --brief` and `agentdir timeline` to skim recorded work.
- Use `agentdir audit session` and `agentdir audit claims --text <path|->` before
  final claims when evidence support matters.
- Before the final response, run `agentdir work finish --json` when practical.
  Use `agentdir report final --format json` to preview the same agent handoff
  without ending the session.
- Read the `agent_handoff` object before making final verification claims.
- Do not record secrets, private keys, raw environment dumps, or credential-bearing
  command output.
{GENERIC_GUIDANCE_END}
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


def install_generic_guidance(
    root: str | Path,
    *,
    target: str = "store",
    force: bool = False,
    cwd: str | Path | None = None,
) -> InstalledSkill:
    destination = generic_guidance_path(root, target=target, cwd=cwd)
    updated = False
    backup_path: Path | None = None
    if target == "project":
        existing = destination.read_text(encoding="utf-8") if destination.exists() else ""
        updated_text = merge_generic_guidance(existing)
        updated = existing != updated_text
        if destination.exists() and updated and force:
            backup_path = destination.with_suffix(destination.suffix + ".bak")
            backup_path.write_text(existing, encoding="utf-8")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(updated_text, encoding="utf-8")
        return InstalledSkill(path=destination, target=target, updated=updated, backup_path=backup_path)

    if target == "store":
        existing = destination.read_text(encoding="utf-8", errors="ignore") if destination.exists() else ""
        if destination.exists() and existing != GENERIC_GUIDANCE_BLOCK:
            if not force and not is_agentdir_managed_generic(existing):
                raise AgentDirError(f"Refusing to overwrite existing generic guidance: {destination}")
            updated = True
            if force:
                backup_path = destination.with_suffix(destination.suffix + ".bak")
                backup_path.write_text(existing, encoding="utf-8")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(GENERIC_GUIDANCE_BLOCK, encoding="utf-8")
        return InstalledSkill(path=destination, target=target, updated=updated, backup_path=backup_path)

    raise AgentDirError("Unknown generic guidance target; expected project or store")


def is_agentdir_managed_skill(text: str) -> bool:
    if MANAGED_SKILL_MARKER in text:
        return True
    return "name: agentdir" in text and "# AgentDir" in text


def is_agentdir_managed_generic(text: str) -> bool:
    return GENERIC_GUIDANCE_START in text and GENERIC_GUIDANCE_END in text


def merge_generic_guidance(existing: str) -> str:
    block = GENERIC_GUIDANCE_BLOCK.rstrip() + "\n"
    if not existing.strip():
        return block
    start = existing.find(GENERIC_GUIDANCE_START)
    end = existing.find(GENERIC_GUIDANCE_END)
    if start != -1 and end != -1 and end >= start:
        end += len(GENERIC_GUIDANCE_END)
        return existing[:start].rstrip() + "\n\n" + block + existing[end:].lstrip()
    return existing.rstrip() + "\n\n" + block


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


def generic_guidance_path(root: str | Path, *, target: str, cwd: str | Path | None = None) -> Path:
    if target == "store":
        init_root(root)
        return paths_for(root).integrations / "generic" / "AGENTS.md"
    if target == "project":
        project = git_root(cwd)
        if project is None:
            raise AgentDirError("Project generic guidance target requires a git repository")
        return project / "AGENTS.md"
    raise AgentDirError("Unknown generic guidance target; expected project or store")


def install_integrations(
    root: str | Path,
    names: list[str],
    *,
    target: str,
    force: bool = False,
    cwd: str | Path | None = None,
) -> list[dict[str, Any]]:
    installed: list[dict[str, Any]] = []
    for name in expand_integration_names(names):
        if name == "generic":
            generic = install_generic_guidance(root, target=target, force=force, cwd=cwd)
            installed.append(_skill_result(name, generic))
            continue
        if name == "codex":
            codex_target = "store" if target == "store" else "project"
            skill = install_codex_skill(root, target=codex_target, force=force, cwd=cwd)
            installed.append(_skill_result(name, skill))
            continue
        installed.append(_install_guidance_integration(root, name, target=target, force=force, cwd=cwd))
    return installed


def integration_plan(
    root: str | Path,
    names: list[str],
    *,
    target: str,
    force: bool = False,
    cwd: str | Path | None = None,
) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    for name in expand_integration_names(names):
        destination = integration_path(root, name, target=target, cwd=cwd, create=False)
        expected = integration_expected_text(name)
        exists = destination.exists()
        existing = destination.read_text(encoding="utf-8", errors="ignore") if exists else ""
        state = integration_state(root, name, target=target, cwd=cwd)
        action = "none"
        if not exists:
            action = "create"
        elif state["state"] == "installed":
            action = "none"
        elif state["state"] == "stale":
            action = "update"
        elif state["state"] == "missing" and _can_merge_guidance(name, target):
            action = "update"
        elif state["state"] == "conflict":
            action = "overwrite" if force else "refuse"
        backup = None
        if exists and action in {"update", "overwrite"} and (force or not _can_merge_guidance(name, target)):
            backup = str(destination.with_suffix(destination.suffix + ".bak"))
        plan.append(
            {
                "name": name,
                "target": target,
                "path": str(destination),
                "action": action,
                "state": state["state"],
                "exists": exists,
                "managed": state.get("managed", False),
                "backup_path": backup,
                "would_write": action in {"create", "update", "overwrite"},
                "would_refuse": action == "refuse",
                "expected_bytes": len(expected.encode("utf-8")),
                "existing_bytes": len(existing.encode("utf-8")) if exists else 0,
            }
        )
    return plan


def integration_doctor(
    root: str | Path,
    names: list[str] | None = None,
    *,
    target: str = "project",
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    selected = expand_integration_names(names or ["all"])
    checks = [integration_state(root, name, target=target, cwd=cwd) for name in selected]
    return {
        "target": target,
        "ok": all(item["state"] in {"installed", "missing"} for item in checks),
        "checks": checks,
    }


def uninstall_integrations(
    root: str | Path,
    names: list[str] | None = None,
    *,
    target: str = "project",
    apply: bool = False,
    cwd: str | Path | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for name in expand_integration_names(names or ["all"]):
        destination = integration_path(root, name, target=target, cwd=cwd, create=False)
        exists = destination.exists()
        existing = destination.read_text(encoding="utf-8", errors="ignore") if exists else ""
        managed = _integration_managed(name, existing)
        removed = False
        action = "none"
        if exists and managed:
            action = "remove"
            if apply:
                if _can_merge_guidance(name, target):
                    updated = remove_managed_block(existing, *_integration_markers(name)).strip()
                    if updated:
                        destination.write_text(updated + "\n", encoding="utf-8")
                    else:
                        destination.unlink()
                    removed = True
                else:
                    destination.unlink()
                    removed = True
        elif exists:
            action = "preserve-unmanaged"
        results.append(
            {
                "name": name,
                "target": target,
                "path": str(destination),
                "action": action,
                "managed": managed,
                "removed": removed,
                "applied": apply,
            }
        )
    return results


def integration_state(
    root: str | Path,
    name: str,
    *,
    target: str = "project",
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    destination = integration_path(root, name, target=target, cwd=cwd, create=False)
    expected = integration_expected_text(name)
    exists = destination.exists()
    existing = destination.read_text(encoding="utf-8", errors="ignore") if exists else ""
    managed = _integration_managed(name, existing)
    if not exists:
        state = "missing"
    elif name == "codex":
        state = "installed" if existing == CODEX_SKILL else "stale" if managed else "conflict"
    elif _can_merge_guidance(name, target):
        current = extract_managed_block(existing, *_integration_markers(name))
        state = "installed" if current == expected else "stale" if managed else "missing"
    else:
        state = "installed" if existing == expected else "stale" if managed else "conflict"
    return {
        "name": name,
        "target": target,
        "path": str(destination),
        "state": state,
        "installed": state == "installed",
        "managed": managed,
    }


def integration_path(
    root: str | Path,
    name: str,
    *,
    target: str,
    cwd: str | Path | None = None,
    create: bool = True,
) -> Path:
    name = normalize_integration_name(name)
    if target not in {"project", "store"}:
        raise AgentDirError("Unknown integration target; expected project or store")
    if name == "generic":
        return generic_guidance_path_no_create(root, target=target, cwd=cwd, create=create)
    if name == "codex":
        return codex_skill_path_no_create(root, target="store" if target == "store" else "project", cwd=cwd, create=create)
    if target == "store":
        if create:
            init_root(root)
        filename = _project_integration_relative_path(name).name
        return paths_for(root).integrations / name / filename
    project = git_root(cwd)
    if project is None:
        raise AgentDirError("Project integration target requires a git repository")
    return project / _project_integration_relative_path(name)


def integration_expected_text(name: str) -> str:
    name = normalize_integration_name(name)
    if name == "generic":
        return GENERIC_GUIDANCE_BLOCK.rstrip() + "\n"
    if name == "codex":
        return CODEX_SKILL
    return _guidance_block(name)


def expand_integration_names(names: list[str]) -> list[str]:
    expanded: list[str] = []
    for name in names:
        if name == "all":
            expanded.extend(INTEGRATION_NAMES)
        else:
            expanded.append(normalize_integration_name(name))
    deduped: list[str] = []
    for name in expanded:
        if name not in deduped:
            deduped.append(name)
    return deduped


def normalize_integration_name(name: str) -> str:
    if name not in (*INTEGRATION_NAMES, "all"):
        raise AgentDirError(f"Unknown integration {name!r}; expected one of {', '.join(INTEGRATION_NAMES)} or all")
    return name


def merge_managed_block(existing: str, block: str, start: str, end: str) -> str:
    block = block.rstrip() + "\n"
    if not existing.strip():
        return block
    start_index = existing.find(start)
    end_index = existing.find(end)
    if start_index != -1 and end_index != -1 and end_index >= start_index:
        end_index += len(end)
        return existing[:start_index].rstrip() + "\n\n" + block + existing[end_index:].lstrip()
    return existing.rstrip() + "\n\n" + block


def remove_managed_block(existing: str, start: str, end: str) -> str:
    start_index = existing.find(start)
    end_index = existing.find(end)
    if start_index == -1 or end_index == -1 or end_index < start_index:
        return existing
    end_index += len(end)
    return existing[:start_index].rstrip() + "\n\n" + existing[end_index:].lstrip()


def extract_managed_block(existing: str, start: str, end: str) -> str | None:
    start_index = existing.find(start)
    end_index = existing.find(end)
    if start_index == -1 or end_index == -1 or end_index < start_index:
        return None
    end_index += len(end)
    return existing[start_index:end_index].rstrip() + "\n"


def codex_skill_path_no_create(
    root: str | Path,
    *,
    target: str,
    cwd: str | Path | None = None,
    create: bool = True,
) -> Path:
    if create:
        return codex_skill_path(root, target=target, cwd=cwd)
    if target == "user":
        return Path.home().expanduser() / ".codex" / "skills" / "agentdir" / "SKILL.md"
    if target == "store":
        return paths_for(root).integrations / "codex" / "skills" / "agentdir" / "SKILL.md"
    if target == "project":
        project = git_root(cwd)
        if project is None:
            raise AgentDirError("Project skill target requires a git repository")
        return project / ".agents" / "skills" / "agentdir" / "SKILL.md"
    raise AgentDirError("Unknown Codex skill target; expected user, project, or store")


def generic_guidance_path_no_create(
    root: str | Path,
    *,
    target: str,
    cwd: str | Path | None = None,
    create: bool = True,
) -> Path:
    if create:
        return generic_guidance_path(root, target=target, cwd=cwd)
    if target == "store":
        return paths_for(root).integrations / "generic" / "AGENTS.md"
    if target == "project":
        project = git_root(cwd)
        if project is None:
            raise AgentDirError("Project generic guidance target requires a git repository")
        return project / "AGENTS.md"
    raise AgentDirError("Unknown generic guidance target; expected project or store")


def _install_guidance_integration(
    root: str | Path,
    name: str,
    *,
    target: str,
    force: bool,
    cwd: str | Path | None,
) -> dict[str, Any]:
    destination = integration_path(root, name, target=target, cwd=cwd)
    expected = integration_expected_text(name)
    existing = destination.read_text(encoding="utf-8", errors="ignore") if destination.exists() else ""
    updated = existing != expected
    backup_path: Path | None = None
    if _can_merge_guidance(name, target):
        merged = merge_managed_block(existing, expected, *_integration_markers(name))
        updated = existing != merged
        if destination.exists() and force and updated:
            backup_path = destination.with_suffix(destination.suffix + ".bak")
            backup_path.write_text(existing, encoding="utf-8")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(merged, encoding="utf-8")
        return {
            "name": name,
            "target": target,
            "path": str(destination),
            "updated": updated,
            "backup_path": str(backup_path) if backup_path else None,
        }
    if destination.exists() and existing != expected:
        if not _integration_managed(name, existing) and not force:
            raise AgentDirError(f"Refusing to overwrite existing {name} integration: {destination}")
        backup_path = destination.with_suffix(destination.suffix + ".bak")
        backup_path.write_text(existing, encoding="utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(expected, encoding="utf-8")
    return {
        "name": name,
        "target": target,
        "path": str(destination),
        "updated": updated,
        "backup_path": str(backup_path) if backup_path else None,
    }


def _skill_result(name: str, installed: InstalledSkill) -> dict[str, Any]:
    return {
        "name": name,
        "target": installed.target,
        "path": str(installed.path),
        "updated": installed.updated,
        "backup_path": str(installed.backup_path) if installed.backup_path else None,
    }


def _project_integration_relative_path(name: str) -> Path:
    if name == "claude":
        return Path("CLAUDE.md")
    if name == "copilot":
        return Path(".github") / "copilot-instructions.md"
    if name == "cursor":
        return Path(".cursor") / "rules" / "agentdir.mdc"
    if name == "windsurf":
        return Path(".windsurf") / "rules" / "agentdir.md"
    raise AgentDirError(f"Integration {name!r} does not have a project guidance path")


def _can_merge_guidance(name: str, target: str) -> bool:
    if target == "store":
        return False
    return name in {"generic", "claude", "copilot"}


def _integration_managed(name: str, text: str) -> bool:
    if not text:
        return False
    if name == "generic":
        return is_agentdir_managed_generic(text)
    if name == "codex":
        return is_agentdir_managed_skill(text)
    start, end = _integration_markers(name)
    return start in text and end in text


def _integration_markers(name: str) -> tuple[str, str]:
    if name == "generic":
        return GENERIC_GUIDANCE_START, GENERIC_GUIDANCE_END
    return f"<!-- agentdir-managed-{name}:start -->", f"<!-- agentdir-managed-{name}:end -->"


def _guidance_block(name: str) -> str:
    start, end = _integration_markers(name)
    title = {
        "claude": "Claude Code",
        "copilot": "GitHub Copilot",
        "cursor": "Cursor",
        "windsurf": "Windsurf",
    }[name]
    frontmatter = ""
    if name == "cursor":
        frontmatter = "---\ndescription: Use AgentDir as invisible evidence capture for agentic engineering.\nalwaysApply: true\n---\n\n"
    elif name == "windsurf":
        frontmatter = "---\ntrigger: always_on\n---\n\n"
    return f"""{frontmatter}{start}
# AgentDir for {title}

AgentDir is agent-owned background instrumentation for this repository.
The engineer installs or adopts it once; agents operate it during normal work.

- Start non-trivial coding work with `agentdir work start "<short task>" --emit-context`.
- If `.agentdir` is missing, run `agentdir adopt` once before starting.
- Wrap evidence-bearing commands with `agentdir run -- <command>`.
- Evidence includes tests, lint, typecheck, builds, doctor checks, release checks, reproduced failures, and diagnostics used in final claims.
- Do not wrap routine exploration such as `rg`, `sed`, `nl`, `cat`, `ls`, `find`, or quick read-only `git status`.
- Use `agentdir evidence --brief` and `agentdir timeline` to skim what happened.
- Use `agentdir report final --format json` or `agentdir work finish --json` for the agent handoff object before final claims when practical.
- Do not record secrets, private keys, raw environment dumps, or credential-bearing command output.
{end}
"""
