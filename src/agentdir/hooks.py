from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .events import emit_event
from .git import git_branch, git_head, git_output, git_status_short
from .sessions import end_session, read_current_session, start_session
from .store import AgentDirError, init_root

DEFAULT_HOOKS = ("pre-commit", "post-commit", "pre-push", "post-checkout", "post-merge")
MANAGED_MARKER = "# AgentDir managed hook"


@dataclass(frozen=True)
class HookInfo:
    hook: str
    path: str
    installed: bool
    managed: bool
    original: str | None = None


def git_hooks_dir(cwd: str | Path | None = None) -> Path:
    git_dir = git_output(["rev-parse", "--git-dir"], cwd)
    if not git_dir:
        raise AgentDirError("AgentDir hooks require a git repository")
    path = Path(git_dir)
    if not path.is_absolute():
        root = git_output(["rev-parse", "--show-toplevel"], cwd)
        base = Path(root) if root else Path(cwd or Path.cwd())
        path = base / path
    hooks = path.resolve() / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    return hooks


def hook_status(cwd: str | Path | None = None, hooks: list[str] | None = None) -> list[HookInfo]:
    hooks_dir = git_hooks_dir(cwd)
    names = hooks or list(DEFAULT_HOOKS)
    statuses: list[HookInfo] = []
    for name in names:
        path = hooks_dir / name
        original = hooks_dir / f"{name}.agentdir-original"
        managed = path.is_file() and MANAGED_MARKER in path.read_text(encoding="utf-8", errors="ignore")
        statuses.append(
            HookInfo(
                hook=name,
                path=str(path),
                installed=path.exists(),
                managed=managed,
                original=str(original) if original.exists() else None,
            )
        )
    return statuses


def install_hooks(
    root: str | Path,
    *,
    cwd: str | Path | None = None,
    hooks: list[str] | None = None,
    force: bool = False,
) -> list[HookInfo]:
    init_root(root)
    hooks_dir = git_hooks_dir(cwd)
    installed: list[HookInfo] = []
    for name in hooks or list(DEFAULT_HOOKS):
        target = hooks_dir / name
        original = hooks_dir / f"{name}.agentdir-original"
        if target.exists():
            managed = target.is_file() and MANAGED_MARKER in target.read_text(encoding="utf-8", errors="ignore")
            if not managed:
                if original.exists() and not force:
                    raise AgentDirError(f"Refusing to overwrite existing hook and backup: {target}")
                if original.exists() and force:
                    original.unlink()
                target.rename(original)
        target.write_text(_hook_script(name, original), encoding="utf-8")
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        installed.append(
            HookInfo(
                hook=name,
                path=str(target),
                installed=True,
                managed=True,
                original=str(original) if original.exists() else None,
            )
        )
    return installed


def uninstall_hooks(
    *,
    cwd: str | Path | None = None,
    hooks: list[str] | None = None,
) -> list[HookInfo]:
    hooks_dir = git_hooks_dir(cwd)
    removed: list[HookInfo] = []
    for name in hooks or list(DEFAULT_HOOKS):
        target = hooks_dir / name
        original = hooks_dir / f"{name}.agentdir-original"
        managed = target.is_file() and MANAGED_MARKER in target.read_text(encoding="utf-8", errors="ignore")
        if managed:
            target.unlink()
            if original.exists():
                original.rename(target)
        removed.append(
            HookInfo(
                hook=name,
                path=str(target),
                installed=target.exists(),
                managed=target.is_file() and MANAGED_MARKER in target.read_text(encoding="utf-8", errors="ignore"),
                original=str(original) if original.exists() else None,
            )
        )
    return removed


def record_hook_event(
    root: str | Path,
    *,
    hook: str,
    original_exit_code: int,
    stdin_file: str | Path | None = None,
    hook_args: list[str] | None = None,
    cwd: str | Path | None = None,
) -> None:
    session = read_current_session(root)
    created_hook_session = session is None or session.status != "active"
    if created_hook_session:
        session = start_session(
            root,
            title=f"Git hook {hook}",
            actor="git",
            note="AgentDir background git hook session.",
            cwd=cwd,
        )
    stdin_text = ""
    if stdin_file:
        path = Path(stdin_file)
        if path.is_file():
            stdin_text = path.read_text(encoding="utf-8", errors="replace")[:32_000]
    body = "\n".join(
        [
            f"hook={hook}",
            f"original_exit_code={original_exit_code}",
            f"args={json.dumps(hook_args or [])}",
            f"branch={git_branch(cwd) or ''}",
            f"git_head={git_head(cwd) or ''}",
            f"recorded_at={datetime.now(UTC).isoformat()}",
            "",
            "git_status_short:",
            git_status_short(cwd),
            "",
            "stdin:",
            stdin_text,
        ]
    )
    emit_event(
        root,
        session_id=session.session_id,
        event_type=f"git.hook.{hook}",
        subject=f"git hook {hook} exit {original_exit_code}",
        from_actor="git",
        body=body,
        git_head=git_head(cwd),
        tool="git",
        tool_exit_code=original_exit_code,
    )
    if created_hook_session:
        end_session(
            root,
            status="completed",
            summary=f"Recorded git hook {hook} exit {original_exit_code}.",
            actor="git",
        )


def _hook_script(name: str, original: Path) -> str:
    return f"""#!/bin/sh
{MANAGED_MARKER}: {name}
hook_name="{name}"
stdin_file="$(mktemp "${{TMPDIR:-/tmp}}/agentdir-hook.XXXXXX")" || stdin_file=""
if [ -n "$stdin_file" ]; then
  cat > "$stdin_file"
fi

original="{original}"
original_status=0
if [ -x "$original" ]; then
  if [ -n "$stdin_file" ]; then
    "$original" "$@" < "$stdin_file"
  else
    "$original" "$@"
  fi
  original_status=$?
fi

if command -v agentdir >/dev/null 2>&1; then
  if [ -n "$stdin_file" ]; then
    agentdir hooks record --hook "$hook_name" --original-exit-code "$original_status" --stdin-file "$stdin_file" -- "$@" >/dev/null 2>&1 || true
  else
    agentdir hooks record --hook "$hook_name" --original-exit-code "$original_status" -- "$@" >/dev/null 2>&1 || true
  fi
fi

if [ -n "$stdin_file" ]; then
  rm -f "$stdin_file"
fi
exit "$original_status"
"""
