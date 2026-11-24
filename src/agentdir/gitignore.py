from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .git import git_output, git_root
from .store import AgentDirError

GITIGNORE_CHOICES = ("ask", "project", "user", "none")
AGENTDIR_IGNORE_PATTERN = ".agentdir/"
_AGENTDIR_PATTERN_VARIANTS = {".agentdir", ".agentdir/", "/.agentdir", "/.agentdir/"}


def _ensure_within_project(project: Path, destination: Path) -> None:
    resolved_project = project.resolve(strict=False)
    try:
        relative = destination.relative_to(project)
        base = project
    except ValueError:
        try:
            relative = destination.relative_to(resolved_project)
            base = resolved_project
        except ValueError as exc:
            raise AgentDirError(
                f"Refusing to access path outside project root {resolved_project}: {destination}"
            ) from exc

    current = base
    if current.is_symlink():
        raise AgentDirError(f"Refusing to follow symlink at project root: {current}")
    for part in relative.parts:
        if part == "..":
            raise AgentDirError(
                f"Refusing to traverse parent directory outside project root {resolved_project}: {destination}"
            )
        current = current / part
        if current.is_symlink():
            raise AgentDirError(f"Refusing to follow symlink inside project: {current}")

    resolved_destination = destination.resolve(strict=False)
    if not resolved_destination.is_relative_to(resolved_project):
        raise AgentDirError(
            f"Refusing to access path outside project root {resolved_project}: {destination}"
        )


def gitignore_plan(
    *,
    target: str,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    if target == "ask":
        return {
            "target": "ask",
            "action": "prompt",
            "path": None,
            "pattern": AGENTDIR_IGNORE_PATTERN,
            "would_write": None,
        }
    if target == "none":
        return _skip("none", "selected_none")

    path = _ignore_path(target, cwd)
    if path is None:
        return _skip(target, "not_in_git_repository")
    exists = path.is_file()
    return {
        "target": target,
        "action": "exists" if _has_agentdir_pattern(path) else "update" if exists else "create",
        "path": str(path),
        "pattern": AGENTDIR_IGNORE_PATTERN,
        "exists": exists,
        "would_write": not _has_agentdir_pattern(path),
    }


def ensure_agentdir_ignored(
    *,
    target: str,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    plan = gitignore_plan(target=target, cwd=cwd)
    if not plan.get("would_write"):
        return {**plan, "changed": False}

    path = Path(str(plan["path"]))
    if target == "project":
        root = git_root(cwd)
        if root is not None:
            _ensure_within_project(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _append_pattern(path)
    return {**plan, "changed": True}


def user_gitignore_path() -> Path:
    configured = git_output(["config", "--global", "--get", "core.excludesFile"])
    if configured:
        return Path(configured).expanduser().resolve()
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config).expanduser() if xdg_config else Path.home() / ".config"
    return (base / "git" / "ignore").resolve()


def _ignore_path(target: str, cwd: str | Path | None) -> Path | None:
    if target == "project":
        root = git_root(cwd)
        if root is None:
            return None
        destination = root / ".gitignore"
        _ensure_within_project(root, destination)
        return destination
    if target == "user":
        return user_gitignore_path()
    return None


def _has_agentdir_pattern(path: Path) -> bool:
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        if candidate in _AGENTDIR_PATTERN_VARIANTS:
            return True
    return False


def _append_pattern(path: Path) -> None:
    if path.exists():
        text = path.read_text(encoding="utf-8")
        separator = "" if text.endswith(("\n", "\r")) or not text else "\n"
        path.write_text(f"{text}{separator}{AGENTDIR_IGNORE_PATTERN}\n", encoding="utf-8")
        return
    path.write_text(f"{AGENTDIR_IGNORE_PATTERN}\n", encoding="utf-8")


def _skip(target: str, reason: str) -> dict[str, Any]:
    return {
        "target": target,
        "action": "skip",
        "path": None,
        "pattern": AGENTDIR_IGNORE_PATTERN,
        "reason": reason,
        "would_write": False,
    }
