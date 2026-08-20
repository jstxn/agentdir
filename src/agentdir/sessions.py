from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .events import emit_event
from .fsutil import atomic_write_text
from .git import git_head, workspace_name
from .locking import lifecycle_lock
from .store import (
    CONFIG_DIR,
    AgentDirStateError,
    find_project_base,
    init_root,
    paths_for,
    validate_id,
)


@dataclass
class SessionState:
    session_id: str
    title: str
    actor: str
    workspace: str
    git_head: str | None
    started_at: str
    status: str = "active"
    ended_at: str | None = None


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def safe_session_id(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._@+-" else "-" for ch in value.strip())
    safe = safe.strip(".-") or "agent-session"
    return validate_id(safe[:128], "session id")


def default_session_id(cwd: str | Path | None = None) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = f"{time.time_ns() % 1_000_000_000:09d}"
    return safe_session_id(f"repo-{workspace_name(cwd)}-{stamp}-{suffix}")


def workspace_slug(cwd: str | Path | None = None) -> str:
    return safe_session_id(workspace_name(cwd))


def _workspace_state_dir(root: str | Path, cwd: str | Path | None = None) -> Path:
    return paths_for(root).state / "workspaces" / workspace_slug(cwd)


def _legacy_current_session_path(root: str | Path) -> Path:
    return paths_for(root).state / "current-session.json"


def scoped_current_session_path(root: str | Path, cwd: str | Path | None = None) -> Path:
    return _workspace_state_dir(root, cwd) / "current-session.json"


def current_session_path(root: str | Path, cwd: str | Path | None = None) -> Path:
    """Active-session pointer for the calling workspace.

    Linked worktrees share the main tree's store, so a single pointer would let
    parallel worktree sessions overwrite each other. Reads still fall back to
    the pre-workspace location so existing stores keep working.
    """
    scoped = scoped_current_session_path(root, cwd)
    if scoped.is_file():
        return scoped
    legacy = _legacy_current_session_path(root)
    others = _scoped_session_paths(root)
    if _cwd_owns_store(root, cwd):
        # The caller is inside a working tree that owns this store, so its slug
        # is authoritative and a sibling worktree's pointer must not be
        # borrowed. The legacy pointer is only this workspace's when nothing has
        # written a scoped one, i.e. a store from before workspace scoping.
        if not others and legacy.is_file():
            return legacy
        return scoped
    # Otherwise the command targeted the store from an unrelated directory
    # (e.g. an explicit --root) and the slug is meaningless. A single active
    # pointer is unambiguous; with several, refuse to guess between worktrees.
    if legacy.is_file():
        return legacy
    if len(others) == 1:
        return others[0]
    return scoped


def _scoped_session_paths(root: str | Path) -> list[Path]:
    return sorted((paths_for(root).state / "workspaces").glob("*/current-session.json"))


def _cwd_owns_store(root: str | Path, cwd: str | Path | None = None) -> bool:
    try:
        base = find_project_base(cwd)
    except OSError:
        return False
    return (base / CONFIG_DIR).resolve() == paths_for(root).root


def last_session_path(root: str | Path, cwd: str | Path | None = None) -> Path:
    return _workspace_state_dir(root, cwd) / "last-session.json"


def write_session_state(
    root: str | Path,
    state: SessionState,
    *,
    cwd: str | Path | None = None,
) -> None:
    with session_pointer_lock(root):
        init_root(root)
        payload = json.dumps(asdict(state), indent=2) + "\n"
        path = scoped_current_session_path(root, cwd)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, payload)
        # Mirror to the unscoped pointer so a downgrade still finds the session.
        # The scoped path wins on read, so this never shadows the real one; the
        # project ships scripts/rollback.sh, which makes downgrade a supported path.
        atomic_write_text(_legacy_current_session_path(root), payload)


def session_pointer_lock(root: str | Path):
    """Serialize mutations of the active-session pointers for this store."""
    return lifecycle_lock(root, "active-session-pointer")


def read_current_session(root: str | Path) -> SessionState | None:
    path = current_session_path(root)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return SessionState(**payload)


def require_current_session(root: str | Path) -> SessionState:
    state = read_current_session(root)
    if state is None or state.status != "active":
        raise AgentDirStateError(
            "No active AgentDir session. "
            "Run 'agentdir work start \"<task>\"' to begin one, "
            "or 'agentdir session ensure' for a bare session."
        )
    return state


def ensure_session(
    root: str | Path,
    session_id: str | None = None,
    *,
    title: str | None = None,
    actor: str = "agent",
    emit_started: bool = True,
) -> SessionState:
    with session_pointer_lock(root):
        if session_id:
            current = read_current_session(root)
            if current and current.session_id == session_id and current.status == "active":
                return current
            return start_session(
                root,
                session_id=session_id,
                title=title,
                actor=actor,
                emit_started=emit_started,
            )
        current = read_current_session(root)
        if current and current.status == "active":
            return current
        return start_session(
            root,
            title=title or "AgentDir auto session",
            actor=actor,
            emit_started=emit_started,
        )


def start_session(
    root: str | Path,
    *,
    session_id: str | None = None,
    title: str | None = None,
    actor: str = "agent",
    note: str | None = None,
    cwd: str | Path | None = None,
    emit_started: bool = True,
) -> SessionState:
    with session_pointer_lock(root):
        return _start_session_locked(
            root,
            session_id=session_id,
            title=title,
            actor=actor,
            note=note,
            cwd=cwd,
            emit_started=emit_started,
        )


def _start_session_locked(
    root: str | Path,
    *,
    session_id: str | None,
    title: str | None,
    actor: str,
    note: str | None,
    cwd: str | Path | None,
    emit_started: bool,
) -> SessionState:
    resolved_id = safe_session_id(session_id) if session_id else default_session_id(cwd)
    with lifecycle_lock(root, f"session:{resolved_id}"):
        paths = init_root(root)
        live_session = paths.sessions / resolved_id
        archived_session = paths.archives / "sessions" / resolved_id
        if archived_session.exists():
            raise AgentDirStateError(
                f"Session id {resolved_id} already exists and cannot be reused; choose a new id"
            )
        try:
            # Reserving the directory is the durable identity boundary even when
            # callers intentionally suppress the session.started envelope.
            live_session.mkdir()
        except FileExistsError as exc:
            raise AgentDirStateError(
                f"Session id {resolved_id} already exists and cannot be reused; choose a new id"
            ) from exc
        state = SessionState(
            session_id=resolved_id,
            title=title or resolved_id,
            actor=actor,
            workspace=workspace_name(cwd),
            git_head=git_head(cwd),
            started_at=now_iso(),
        )
        write_session_state(root, state, cwd=cwd)
        if emit_started:
            body = "\n".join(
                [
                    f"session_id={state.session_id}",
                    f"title={state.title}",
                    f"actor={state.actor}",
                    f"workspace={state.workspace}",
                    f"git_head={state.git_head or ''}",
                    f"started_at={state.started_at}",
                    "",
                    note or "AgentDir session started.",
                ]
            )
            emit_event(
                root,
                session_id=state.session_id,
                event_type="session.started",
                body=body,
                subject=state.title,
                from_actor=actor,
                workspace=state.workspace,
                git_head=state.git_head,
            )
        return state


def end_session(
    root: str | Path,
    *,
    status: str = "completed",
    summary: str | None = None,
    actor: str = "agent",
    expected_session_id: str | None = None,
) -> SessionState:
    with session_pointer_lock(root):
        target = require_current_session(root)
        if expected_session_id and target.session_id != expected_session_id:
            raise AgentDirStateError(
                f"Session {expected_session_id} is not the active session and cannot be ended"
            )
        with lifecycle_lock(root, f"session:{target.session_id}"):
            state = require_current_session(root)
            if state.session_id != target.session_id:
                raise AgentDirStateError(
                    f"Active session changed from {target.session_id} to {state.session_id} before it could end"
                )
            return _end_session_locked(
                root,
                state=state,
                status=status,
                summary=summary,
                actor=actor,
            )


def _end_session_locked(
    root: str | Path,
    *,
    state: SessionState,
    status: str,
    summary: str | None,
    actor: str,
) -> SessionState:
    state.status = status
    state.ended_at = now_iso()
    body = "\n".join(
        [
            f"session_id={state.session_id}",
            f"status={state.status}",
            f"ended_at={state.ended_at}",
            "",
            summary or "AgentDir session ended.",
        ]
    )
    emit_event(
        root,
        session_id=state.session_id,
        event_type="session.ended",
        body=body,
        subject=f"session {status}",
        from_actor=actor,
        workspace=state.workspace,
        git_head=git_head(),
    )
    paths = init_root(root)
    last = last_session_path(root)
    last.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(last, json.dumps(asdict(state), indent=2) + "\n")
    _remove_session_pointers(root, state.session_id)
    paths.state.mkdir(parents=True, exist_ok=True)
    return state


def _remove_session_pointers(root: str | Path, session_id: str) -> None:
    candidates = [_legacy_current_session_path(root), *_scoped_session_paths(root)]
    for current in candidates:
        if not current.is_file():
            continue
        try:
            payload = json.loads(current.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("session_id") == session_id:
            current.unlink(missing_ok=True)
