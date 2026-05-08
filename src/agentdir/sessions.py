from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .events import emit_event
from .git import git_head, workspace_name
from .store import AgentDirError, init_root, paths_for, validate_id


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


def current_session_path(root: str | Path) -> Path:
    return paths_for(root).state / "current-session.json"


def last_session_path(root: str | Path) -> Path:
    return paths_for(root).state / "last-session.json"


def write_session_state(root: str | Path, state: SessionState) -> None:
    paths = init_root(root)
    paths.state.mkdir(parents=True, exist_ok=True)
    current_session_path(root).write_text(json.dumps(asdict(state), indent=2) + "\n", encoding="utf-8")


def read_current_session(root: str | Path) -> SessionState | None:
    path = current_session_path(root)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return SessionState(**payload)


def require_current_session(root: str | Path) -> SessionState:
    state = read_current_session(root)
    if state is None or state.status != "active":
        raise AgentDirError("No active AgentDir session; run: agentdir session start")
    return state


def ensure_session(root: str | Path, session_id: str | None = None, *, title: str | None = None) -> SessionState:
    if session_id:
        current = read_current_session(root)
        if current and current.session_id == session_id and current.status == "active":
            return current
        return start_session(root, session_id=session_id, title=title, emit_started=False)
    current = read_current_session(root)
    if current and current.status == "active":
        return current
    return start_session(root, title=title or "AgentDir auto session")


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
    resolved_id = safe_session_id(session_id) if session_id else default_session_id(cwd)
    state = SessionState(
        session_id=resolved_id,
        title=title or resolved_id,
        actor=actor,
        workspace=workspace_name(cwd),
        git_head=git_head(cwd),
        started_at=now_iso(),
    )
    write_session_state(root, state)
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
) -> SessionState:
    state = require_current_session(root)
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
    last_session_path(root).write_text(json.dumps(asdict(state), indent=2) + "\n", encoding="utf-8")
    current = current_session_path(root)
    if current.exists():
        current.unlink()
    paths.state.mkdir(parents=True, exist_ok=True)
    return state
