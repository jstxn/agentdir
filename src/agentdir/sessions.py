from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from .events import emit_event
from .fsutil import atomic_write_text
from .git import git_common_root, git_head, git_output, git_root, workspace_name
from .locking import lifecycle_lock
from .store import (
    CONFIG_DIR,
    AgentDirStateError,
    find_project_base,
    init_root,
    machine_root,
    paths_for,
    user_root,
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
    tail = f"-{stamp}-{suffix}"
    prefix = safe_session_id(f"repo-{workspace_name(cwd)}")[: 128 - len(tail)].rstrip(".-")
    return validate_id(f"{prefix or 'repo'}{tail}", "session id")


def workspace_slug(cwd: str | Path | None = None) -> str:
    return safe_session_id(workspace_name(cwd))


def session_workspace_cwd(
    root: str | Path,
    cwd: str | Path | None = None,
) -> Path:
    """Resolve the checkout a new session belongs to.

    Normal and linked worktrees own their shared store. An explicit project
    root invoked elsewhere belongs to the main checkout, while user/global
    stores stay scoped to the caller.
    """
    caller = Path(cwd or Path.cwd()).expanduser().resolve()
    if _cwd_owns_store(root, caller):
        return caller
    owner = _project_store_owner(root)
    return owner or caller


def _project_store_owner(root: str | Path) -> Path | None:
    store = paths_for(root).root
    if store in {user_root(), machine_root()}:
        return None
    if store.name != CONFIG_DIR:
        return None
    candidate = store.parent.resolve()
    if git_root(candidate) != candidate:
        return None
    return candidate


def _git_checkout_slug(cwd: str | Path | None = None) -> str | None:
    checkout = git_root(cwd)
    main = git_common_root(cwd)
    if checkout is None or main is None:
        return None
    if checkout == main:
        return safe_session_id(f"checkout-main-{workspace_name(checkout)}")
    git_dir = git_output(["rev-parse", "--absolute-git-dir"], checkout)
    if not git_dir:
        return None
    worktree_id = Path(git_dir).name
    digest = sha256(worktree_id.encode("utf-8")).hexdigest()[:10]
    return safe_session_id(f"checkout-worktree-{digest}-{worktree_id}")


def _canonical_workspace_slug(
    root: str | Path,
    cwd: str | Path | None = None,
) -> str:
    checkout_slug = _git_checkout_slug(cwd)
    if _project_store_owner(root) is not None and _cwd_owns_store(root, cwd):
        return checkout_slug or workspace_slug(cwd)
    identity = git_common_root(cwd) or git_root(cwd) or Path(cwd or Path.cwd()).resolve()
    digest = sha256(str(identity).encode("utf-8")).hexdigest()[:10]
    label = (checkout_slug or f"checkout-path-{workspace_name(cwd)}").removeprefix("checkout-")
    return safe_session_id(f"checkout-scope-{digest}-{label}")


def _workspace_state_dir(root: str | Path, cwd: str | Path | None = None) -> Path:
    return paths_for(root).state / "workspaces" / _canonical_workspace_slug(root, cwd)


def _legacy_workspace_state_dir(root: str | Path, cwd: str | Path | None = None) -> Path:
    return paths_for(root).state / "workspaces" / workspace_slug(cwd)


def _legacy_current_session_path(root: str | Path) -> Path:
    return paths_for(root).state / "current-session.json"


def scoped_current_session_path(root: str | Path, cwd: str | Path | None = None) -> Path:
    return _workspace_state_dir(root, cwd) / "current-session.json"


def _legacy_scoped_current_session_path(
    root: str | Path,
    cwd: str | Path | None = None,
) -> Path:
    return _legacy_workspace_state_dir(root, cwd) / "current-session.json"


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
    caller_scoped = _cwd_owns_store(root, cwd) or _project_store_owner(root) is None
    if caller_scoped:
        compatibility = _legacy_scoped_current_session_path(root, cwd)
        if compatibility.is_file() and _legacy_workspace_is_unambiguous(root, cwd):
            return compatibility
        if not _active_scoped_session_paths(root) and legacy.is_file():
            return legacy
        return scoped

    active = _active_scoped_session_paths(root)
    if len(active) == 1:
        return next(iter(active.values()))
    if len(active) > 1:
        raise AgentDirStateError(
            "Multiple active sessions share this AgentDir store; run the command "
            "from the owning checkout instead of selecting the store with --root"
        )
    if legacy.is_file():
        return legacy
    return scoped


def _scoped_session_paths(root: str | Path) -> list[Path]:
    return sorted((paths_for(root).state / "workspaces").glob("*/current-session.json"))


def _active_scoped_session_paths(root: str | Path) -> dict[str, Path]:
    active: dict[str, Path] = {}
    for path in _scoped_session_paths(root):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        session_id = payload.get("session_id")
        if session_id and payload.get("status", "active") == "active":
            current = active.get(session_id)
            if current is None or path.parent.name.startswith("checkout-"):
                active[session_id] = path
    return active


def _cwd_owns_store(root: str | Path, cwd: str | Path | None = None) -> bool:
    try:
        base = find_project_base(cwd)
    except OSError:
        return False
    return (base / CONFIG_DIR).resolve() == paths_for(root).root


def _worktree_paths(root: str | Path) -> list[Path]:
    owner = _project_store_owner(root)
    if owner is None:
        return []
    output = git_output(["worktree", "list", "--porcelain"], owner)
    if not output:
        return []
    return [
        Path(line.removeprefix("worktree ")).resolve()
        for line in output.splitlines()
        if line.startswith("worktree ")
    ]


def _legacy_workspace_is_unambiguous(
    root: str | Path,
    cwd: str | Path | None = None,
) -> bool:
    name = workspace_name(cwd)
    if _project_store_owner(root) is not None and sum(
        path.name == name for path in _worktree_paths(root)
    ) > 1:
        return False
    canonical = _canonical_workspace_slug(root, cwd)
    for path in [*_scoped_session_paths(root), *_scoped_last_session_paths(root)]:
        if path.parent.name == canonical or not path.parent.name.startswith("checkout-"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("workspace") == name:
            return False
    return True


def _checkout_for_pointer(root: str | Path, path: Path) -> Path | None:
    slug = path.parent.name
    if not slug.startswith("checkout-"):
        return None
    for checkout in _worktree_paths(root):
        if _git_checkout_slug(checkout) != slug:
            continue
        if _cwd_owns_store(root, checkout):
            return checkout
    caller = git_root() or Path.cwd().resolve()
    if _canonical_workspace_slug(root, caller) == slug:
        return caller
    return None


def session_git_cwd(root: str | Path, state: SessionState) -> Path | None:
    """Return a verified checkout for the session, never an unrelated caller."""
    matching = []
    for current in [*_scoped_session_paths(root), *_scoped_last_session_paths(root)]:
        try:
            payload = json.loads(current.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("session_id") == state.session_id and current.parent.name.startswith(
            "checkout-"
        ):
            matching.append(current)
    if matching:
        return _checkout_for_pointer(root, matching[0])
    if state.status == "unknown":
        return None
    if _cwd_owns_store(root):
        return git_root() or Path.cwd().resolve()
    candidate = _project_store_owner(root)
    if candidate is None or workspace_name(candidate) != state.workspace:
        return None
    return candidate


def last_session_path(root: str | Path, cwd: str | Path | None = None) -> Path:
    return _workspace_state_dir(root, cwd) / "last-session.json"


def _legacy_scoped_last_session_path(
    root: str | Path,
    cwd: str | Path | None = None,
) -> Path:
    return _legacy_workspace_state_dir(root, cwd) / "last-session.json"


def _legacy_last_session_path(root: str | Path) -> Path:
    return paths_for(root).state / "last-session.json"


def _scoped_last_session_paths(root: str | Path) -> list[Path]:
    return sorted((paths_for(root).state / "workspaces").glob("*/last-session.json"))


def read_last_session(
    root: str | Path,
    cwd: str | Path | None = None,
) -> SessionState | None:
    """Return the latest completed session visible to the calling workspace.

    Normal commands stay scoped to their worktree. Commands that explicitly
    target a store from outside one of its worktrees may inspect the most
    recently completed session across the store, matching the read-only
    fallback behavior of the active-session pointer.
    """
    scoped = last_session_path(root, cwd)
    legacy = _legacy_last_session_path(root)
    if scoped.is_file():
        candidates = [scoped]
    elif _cwd_owns_store(root, cwd) or _project_store_owner(root) is None:
        compatibility = _legacy_scoped_last_session_path(root, cwd)
        candidates = (
            [compatibility]
            if compatibility.is_file() and _legacy_workspace_is_unambiguous(root, cwd)
            else [scoped]
        )
        if not _scoped_last_session_paths(root):
            candidates.append(legacy)
    else:
        candidates = [*_scoped_last_session_paths(root), legacy]

    states: list[SessionState] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        states.append(SessionState(**payload))
    if not states:
        return None
    return max(states, key=lambda state: state.ended_at or state.started_at)


def write_session_state(
    root: str | Path,
    state: SessionState,
    *,
    cwd: str | Path | None = None,
) -> None:
    with session_pointer_lock(root):
        init_root(root)
        payload = json.dumps(asdict(state), indent=2) + "\n"
        session_cwd = cwd or session_git_cwd(root, state) or session_workspace_cwd(root)
        paths = {
            scoped_current_session_path(root, session_cwd),
            _legacy_scoped_current_session_path(root, session_cwd),
        }
        for path in paths:
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


def read_session_state(root: str | Path, session_id: str) -> SessionState | None:
    candidates = [
        *_scoped_session_paths(root),
        *_scoped_last_session_paths(root),
        _legacy_current_session_path(root),
        _legacy_last_session_path(root),
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("session_id") == session_id:
            return SessionState(**payload)
    return None


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
                cwd=session_workspace_cwd(root),
                emit_started=emit_started,
            )
        current = read_current_session(root)
        if current and current.status == "active":
            return current
        return start_session(
            root,
            title=title or "AgentDir auto session",
            actor=actor,
            cwd=session_workspace_cwd(root),
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
    session_cwd = session_workspace_cwd(root, cwd)
    resolved_id = safe_session_id(session_id) if session_id else default_session_id(session_cwd)
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
            workspace=workspace_name(session_cwd),
            git_head=git_head(session_cwd),
            started_at=now_iso(),
        )
        write_session_state(root, state, cwd=session_cwd)
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
    git_cwd = session_git_cwd(root, state)
    ending_git_head = (git_head(git_cwd) if git_cwd else None) or state.git_head
    state.git_head = ending_git_head
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
        git_head=ending_git_head,
    )
    paths = init_root(root)
    payload = json.dumps(asdict(state), indent=2) + "\n"
    last_paths = {_legacy_last_session_path(root)}
    if git_cwd is not None:
        last_paths.update(
            {
                last_session_path(root, git_cwd),
                _legacy_scoped_last_session_path(root, git_cwd),
            }
        )
    for current in _scoped_session_paths(root):
        try:
            current_payload = json.loads(current.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if current_payload.get("session_id") == state.session_id:
            last_paths.add(current.with_name("last-session.json"))
    for last in last_paths:
        last.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(last, payload)
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
