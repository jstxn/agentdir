from __future__ import annotations

import json
import os
import shlex
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .environment import identify_hook_owner
from .events import emit_event
from .git import git_branch, git_head, git_output, git_root, git_status_short
from .sessions import end_session, read_current_session, start_session
from .store import AgentDirDependencyError, AgentDirError, init_root, paths_for

DEFAULT_HOOKS = ("pre-commit", "post-commit", "pre-push", "post-checkout", "post-merge")
MANAGED_MARKER = "# AgentDir managed hook"
HOOKS_MANIFEST_NAME = "hooks.json"


@dataclass(frozen=True)
class HookInfo:
    hook: str
    path: str
    installed: bool
    managed: bool
    original: str | None = None
    owner: str | None = None


def resolve_git_hooks_dir(cwd: str | Path | None = None) -> Path:
    """Resolve the hooks directory git actually consults.

    ``rev-parse --git-path hooks`` honours ``core.hooksPath`` and linked
    worktrees (which share the common hooks directory); resolving the git dir
    by hand does neither, so shims would land where git never looks.
    """
    hooks_path = git_output(["rev-parse", "--git-path", "hooks"], cwd)
    if not hooks_path:
        raise AgentDirDependencyError("AgentDir hooks require a git repository")
    path = Path(hooks_path).expanduser()
    if not path.is_absolute():
        path = Path(cwd or Path.cwd()) / path
    return path.resolve()


def git_hooks_dir(cwd: str | Path | None = None) -> Path:
    hooks = resolve_git_hooks_dir(cwd)
    hooks.mkdir(parents=True, exist_ok=True)
    return hooks


def hook_status(cwd: str | Path | None = None, hooks: list[str] | None = None) -> list[HookInfo]:
    hooks_dir = git_hooks_dir(cwd)
    names = hooks or list(DEFAULT_HOOKS)
    statuses: list[HookInfo] = []
    for name in names:
        path = hooks_dir / name
        original = hooks_dir / f"{name}.agentdir-original"
        text = path.read_text(encoding="utf-8", errors="ignore") if path.is_file() else ""
        managed = MANAGED_MARKER in text
        statuses.append(
            HookInfo(
                hook=name,
                path=str(path),
                installed=path.exists(),
                managed=managed,
                original=str(original) if original.exists() else None,
                owner=identify_hook_owner(text) if text and not managed else None,
            )
        )
    return statuses


def preflight_install_hooks(
    *,
    cwd: str | Path | None = None,
    hooks: list[str] | None = None,
    force: bool = False,
) -> Path:
    """Validate the entire hook install before AgentDir mutates repository state."""
    hooks_dir = resolve_git_hooks_dir(cwd)
    if hooks_dir.exists() and not hooks_dir.is_dir():
        raise AgentDirError(
            f"Git hooks path is not a directory: {hooks_dir}. "
            "Use --no-hooks in a restricted worktree, or adopt from a checkout "
            "that can write the Git hooks directory."
        )

    for name in hooks or list(DEFAULT_HOOKS):
        target = hooks_dir / name
        original = hooks_dir / f"{name}.agentdir-original"
        if target.exists() and not target.is_file():
            raise AgentDirError(f"Git hook target is not a file: {target}")
        if original.exists() and not original.is_file():
            raise AgentDirError(f"AgentDir hook backup is not a file: {original}")
        if not target.exists():
            continue
        existing = target.read_text(encoding="utf-8", errors="ignore")
        if MANAGED_MARKER in existing or not original.exists():
            continue
        backup = original.read_text(encoding="utf-8", errors="ignore")
        if not force and not can_refresh_manager_hook_backup(existing, backup):
            raise AgentDirError(f"Refusing to overwrite existing hook and backup: {target}")

    probe_dir = hooks_dir
    while not probe_dir.exists() and probe_dir.parent != probe_dir:
        probe_dir = probe_dir.parent
    if not probe_dir.is_dir():
        raise AgentDirError(
            f"Cannot create Git hooks directory {hooks_dir}: nearest existing path "
            f"is not a directory: {probe_dir}"
        )

    descriptor = -1
    probe_path: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".agentdir-hook-preflight-", dir=probe_dir)
        probe_path = Path(name)
        os.close(descriptor)
        descriptor = -1
        probe_path.unlink()
        probe_path = None
    except OSError as exc:
        raise AgentDirError(
            f"Cannot install AgentDir hooks in {hooks_dir}: {exc}. "
            "Use --no-hooks in a restricted worktree, or adopt from a checkout "
            "that can write the Git hooks directory."
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if probe_path is not None:
            try:
                probe_path.unlink(missing_ok=True)
            except OSError:
                pass
    return hooks_dir


def install_hooks(
    root: str | Path,
    *,
    cwd: str | Path | None = None,
    hooks: list[str] | None = None,
    force: bool = False,
) -> list[HookInfo]:
    hooks_dir = preflight_install_hooks(cwd=cwd, hooks=hooks, force=force)
    init_root(root)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    installed: list[HookInfo] = []
    for name in hooks or list(DEFAULT_HOOKS):
        target = hooks_dir / name
        original = hooks_dir / f"{name}.agentdir-original"
        owner: str | None = None
        if target.exists():
            existing = target.read_text(encoding="utf-8", errors="ignore") if target.is_file() else ""
            managed = MANAGED_MARKER in existing
            if not managed:
                owner = identify_hook_owner(existing)
                if original.exists():
                    backup = original.read_text(encoding="utf-8", errors="ignore")
                    # A hook manager that reinstalled over our shim leaves a
                    # stale manager script as the backup; refreshing it loses
                    # nothing, so heal without --force. Anything else needs an
                    # explicit --force to discard the backup.
                    stale_manager_backup = can_refresh_manager_hook_backup(existing, backup)
                    if not force and not stale_manager_backup:
                        raise AgentDirError(f"Refusing to overwrite existing hook and backup: {target}")
                    original.unlink()
                target.rename(original)
        target.write_text(
            _hook_script(name, original, paths_for(root).root),
            encoding="utf-8",
        )
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        installed.append(
            HookInfo(
                hook=name,
                path=str(target),
                installed=True,
                managed=True,
                original=str(original) if original.exists() else None,
                owner=owner,
            )
        )
    _record_installed_hooks(root, hooks_dir=hooks_dir, names=[info.hook for info in installed], cwd=cwd)
    return installed


def uninstall_hooks(
    *,
    cwd: str | Path | None = None,
    hooks: list[str] | None = None,
    root: str | Path | None = None,
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
    if root is not None:
        _forget_installed_hooks(root, names=[info.hook for info in removed])
    return removed


def hooks_manifest_path(root: str | Path) -> Path:
    return paths_for(root).meta / HOOKS_MANIFEST_NAME


def read_hooks_manifest(root: str | Path) -> dict[str, object] | None:
    path = hooks_manifest_path(root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def hooks_manifest_issues(root: str | Path) -> list[str]:
    """Report installed-hook drift: shims a hook manager overwrote or removed.

    Uses the manifest written at install time so drift is visible even when
    doctor runs from a different working directory than the hooked repo.
    """
    manifest = read_hooks_manifest(root)
    if not manifest:
        return []
    hooks_dir_value = manifest.get("hooks_dir")
    names = manifest.get("hooks")
    if not isinstance(hooks_dir_value, str) or not isinstance(names, list):
        return []
    hooks_dir = Path(hooks_dir_value)
    issues: list[str] = []
    repo = manifest.get("repo")
    if isinstance(repo, str) and repo:
        try:
            current_hooks_dir = resolve_git_hooks_dir(repo)
        except AgentDirDependencyError:
            current_hooks_dir = None
        if current_hooks_dir is not None and current_hooks_dir != hooks_dir.resolve():
            configured = git_output(["config", "--get", "core.hooksPath"], repo)
            source = f"git core.hooksPath={configured}" if configured else "git's default hooks path"
            issues.append(
                f"{source} resolves to {current_hooks_dir} and bypasses the AgentDir hook shims in "
                f"{hooks_dir}; git activity is not being recorded. Re-run 'agentdir hooks "
                "install' to install into the active hooks directory."
            )
    for name in names:
        if not isinstance(name, str):
            continue
        path = hooks_dir / name
        if not path.is_file():
            issues.append(
                f"AgentDir git hook {name} is missing from {hooks_dir}; git activity is not "
                "being recorded. Run 'agentdir hooks install' to restore it."
            )
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if MANAGED_MARKER in text:
            continue
        owner = identify_hook_owner(text)
        owner_label = owner or "another tool"
        issues.append(
            f"AgentDir git hook {name} was overwritten by {owner_label}; git activity is not "
            f"being recorded. Run 'agentdir hooks install' to restore it (the shim chains "
            f"{owner_label}), and see docs/INSTALL.md for hook-manager coexistence."
        )
    return issues


def _record_installed_hooks(
    root: str | Path,
    *,
    hooks_dir: Path,
    names: list[str],
    cwd: str | Path | None = None,
) -> None:
    manifest = read_hooks_manifest(root) or {}
    recorded = manifest.get("hooks")
    merged = set(recorded) if isinstance(recorded, list) else set()
    merged.update(names)
    repo = git_root(cwd)
    payload = {
        "version": 1,
        "repo": str(repo) if repo else manifest.get("repo"),
        "hooks_dir": str(hooks_dir),
        "hooks": sorted(merged),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    hooks_manifest_path(root).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _forget_installed_hooks(root: str | Path, *, names: list[str]) -> None:
    manifest = read_hooks_manifest(root)
    if not manifest:
        return
    recorded = manifest.get("hooks")
    if not isinstance(recorded, list):
        return
    remaining = sorted(set(recorded) - set(names))
    path = hooks_manifest_path(root)
    if not remaining:
        path.unlink(missing_ok=True)
        return
    manifest["hooks"] = remaining
    manifest["updated_at"] = datetime.now(UTC).isoformat()
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def can_refresh_manager_hook_backup(existing: str, backup: str) -> bool:
    """Return whether replacing a stale hook-manager backup is non-destructive."""
    return bool(identify_hook_owner(existing) and identify_hook_owner(backup))


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


def _hook_script(name: str, original: Path, root: Path) -> str:
    fallback_root = shlex.quote(str(root.resolve()))
    return f"""#!/bin/sh
{MANAGED_MARKER}: {name}
hook_name="{name}"
agentdir_root={fallback_root}
worktree_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -n "$worktree_root" ]; then
  agentdir_root=""
  if [ "${{AGENTDIR_WORKTREE_STORE:-shared}}" = "local" ]; then
    if [ -f "$worktree_root/.agentdir/VERSION" ]; then
      agentdir_root="$worktree_root/.agentdir"
    fi
  elif [ -f "$worktree_root/.agentdir/VERSION" ]; then
    agentdir_root="$worktree_root/.agentdir"
  else
    common_dir="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || git rev-parse --git-common-dir 2>/dev/null || true)"
    case "$common_dir" in
      /*) ;;
      *) common_dir="$worktree_root/$common_dir" ;;
    esac
    common_root="$(dirname "$common_dir")"
    if [ -f "$common_root/.agentdir/VERSION" ]; then
      agentdir_root="$common_root/.agentdir"
    fi
  fi
fi
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

if [ -n "$agentdir_root" ] && command -v agentdir >/dev/null 2>&1; then
  if [ -n "$stdin_file" ]; then
    agentdir hooks record --root "$agentdir_root" --hook "$hook_name" --original-exit-code "$original_status" --stdin-file "$stdin_file" -- "$@" >/dev/null 2>&1 || true
  else
    agentdir hooks record --root "$agentdir_root" --hook "$hook_name" --original-exit-code "$original_status" -- "$@" >/dev/null 2>&1 || true
  fi
fi

if [ -n "$stdin_file" ]; then
  rm -f "$stdin_file"
fi
exit "$original_status"
"""
