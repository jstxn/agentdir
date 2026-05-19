from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .git import git_output, git_root

GITIGNORE_CHOICES = ("ask", "project", "user", "none")
AGENTDIR_IGNORE_PATTERN = ".agentdir/"
_AGENTDIR_PATTERN_VARIANTS = {".agentdir", ".agentdir/", "/.agentdir", "/.agentdir/"}


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
        return (root / ".gitignore") if root else None
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
