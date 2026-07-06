from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .index import rebuild_index
from .mailbox import fsync_directory
from .sessions import read_current_session
from .store import AgentDirError, init_root, validate_id

SECONDS_PER_DAY = 24 * 60 * 60


@dataclass(frozen=True)
class RetentionCandidate:
    session_id: str
    store: str
    path: str
    event_count: int
    latest_event_at: float | None
    active: bool = False


@dataclass(frozen=True)
class RetentionResult:
    action: str
    dry_run: bool
    selected: list[RetentionCandidate]
    protected: list[RetentionCandidate]
    changed: list[str]
    rebuilt_index: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "dry_run": self.dry_run,
            "selected": [asdict(candidate) for candidate in self.selected],
            "protected": [asdict(candidate) for candidate in self.protected],
            "changed": self.changed,
            "rebuilt_index": self.rebuilt_index,
        }


def archive_sessions(
    root: str | Path,
    *,
    sessions: list[str] | None = None,
    older_than_days: int | None = None,
    keep_recent: int | None = None,
    apply: bool = False,
) -> RetentionResult:
    paths = init_root(root)
    _validate_selectors(
        sessions=sessions,
        older_than_days=older_than_days,
        keep_recent=keep_recent,
        command="archive",
    )
    active_id = _active_session_id(paths.root)
    candidates = _list_sessions(paths.sessions, store="sessions", active_session_id=active_id)
    selected, protected = _select_candidates(
        candidates,
        sessions=sessions,
        older_than_days=older_than_days,
        keep_recent=keep_recent,
    )
    _raise_on_exact_active("archive", sessions, protected)

    changed: list[str] = []
    if apply:
        target_root = paths.archives / "sessions"
        target_root.mkdir(parents=True, exist_ok=True)
        targets: list[tuple[RetentionCandidate, Path]] = []
        for candidate in selected:
            target = target_root / candidate.session_id
            if target.exists():
                raise AgentDirError(f"archive target already exists: {target}")
            targets.append((candidate, target))
        for candidate, target in targets:
            source = Path(candidate.path)
            source.rename(target)
            fsync_directory(paths.sessions)
            fsync_directory(target_root)
            changed.append(f"{candidate.session_id}:sessions->archives")
        rebuilt = bool(changed)
        if rebuilt:
            rebuild_index(paths.root)
    else:
        rebuilt = False

    return RetentionResult(
        action="archive",
        dry_run=not apply,
        selected=selected,
        protected=protected,
        changed=changed,
        rebuilt_index=rebuilt,
    )


def prune_sessions(
    root: str | Path,
    *,
    sessions: list[str] | None = None,
    older_than_days: int | None = None,
    keep_recent: int | None = None,
    include_live_sessions: bool = False,
    apply: bool = False,
) -> RetentionResult:
    paths = init_root(root)
    _validate_selectors(
        sessions=sessions,
        older_than_days=older_than_days,
        keep_recent=keep_recent,
        command="prune",
    )
    active_id = _active_session_id(paths.root)
    candidates = _list_sessions(
        paths.archives / "sessions",
        store="archives",
        active_session_id=active_id,
    )
    if include_live_sessions:
        candidates.extend(
            _list_sessions(paths.sessions, store="sessions", active_session_id=active_id)
        )
    selected, protected = _select_candidates(
        candidates,
        sessions=sessions,
        older_than_days=older_than_days,
        keep_recent=keep_recent,
    )
    _raise_on_exact_active("prune", sessions, protected)

    changed: list[str] = []
    live_changed = False
    if apply:
        for candidate in selected:
            source = Path(candidate.path)
            shutil.rmtree(source)
            fsync_directory(source.parent)
            changed.append(f"{candidate.session_id}:{candidate.store}->deleted")
            live_changed = live_changed or candidate.store == "sessions"
        if live_changed:
            rebuild_index(paths.root)

    return RetentionResult(
        action="prune",
        dry_run=not apply,
        selected=selected,
        protected=protected,
        changed=changed,
        rebuilt_index=apply and live_changed,
    )


def format_retention_result(result: RetentionResult) -> str:
    mode = "dry-run" if result.dry_run else "applied"
    lines = [
        f"action={result.action}",
        f"mode={mode}",
        f"selected={len(result.selected)}",
        f"protected={len(result.protected)}",
        f"changed={len(result.changed)}",
    ]
    for candidate in result.selected:
        lines.append(
            f"selected: {candidate.session_id} store={candidate.store} events={candidate.event_count}"
        )
    for candidate in result.protected:
        lines.append(f"protected: {candidate.session_id} store={candidate.store} active=true")
    for changed in result.changed:
        lines.append(f"changed: {changed}")
    if result.rebuilt_index:
        lines.append("rebuilt_index=true")
    if result.dry_run and result.selected:
        lines.append("rerun with --apply to perform this change")
    return "\n".join(lines)


def _validate_selectors(
    *,
    sessions: list[str] | None,
    older_than_days: int | None,
    keep_recent: int | None,
    command: str,
) -> None:
    if not sessions and older_than_days is None and keep_recent is None:
        raise AgentDirError(
            f"{command} requires --session, --older-than-days, or --keep-recent"
        )
    if sessions and (older_than_days is not None or keep_recent is not None):
        raise AgentDirError("Use --session or age/count filters, not both")
    for session_id in sessions or []:
        validate_id(session_id, "session id")
    if older_than_days is not None and older_than_days < 0:
        raise AgentDirError("--older-than-days must be non-negative")
    if keep_recent is not None and keep_recent < 0:
        raise AgentDirError("--keep-recent must be non-negative")


def _active_session_id(root: Path) -> str | None:
    # This guards retention from deleting the live session, so fail closed:
    # an unreadable state file aborts the retention run instead of silently
    # dropping the protection.
    try:
        state = read_current_session(root)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AgentDirError(
            f"Cannot determine the active session (corrupt state/current-session.json?): {exc}. "
            "Fix or remove the state file before archiving or pruning."
        ) from exc
    if state and state.status == "active":
        return state.session_id
    return None


def _list_sessions(
    directory: Path,
    *,
    store: str,
    active_session_id: str | None,
) -> list[RetentionCandidate]:
    if not directory.is_dir():
        return []
    candidates: list[RetentionCandidate] = []
    for session_dir in sorted(directory.iterdir()):
        if not session_dir.is_dir() or not (session_dir / "Maildir").is_dir():
            continue
        event_paths = [
            path
            for state in ("new", "cur")
            for path in (session_dir / "Maildir" / state).glob("*")
            if path.is_file() and not path.name.startswith(".")
        ]
        latest_event_at = max((path.stat().st_mtime for path in event_paths), default=None)
        if latest_event_at is None:
            latest_event_at = session_dir.stat().st_mtime
        candidates.append(
            RetentionCandidate(
                session_id=session_dir.name,
                store=store,
                path=str(session_dir),
                event_count=len(event_paths),
                latest_event_at=latest_event_at,
                active=session_dir.name == active_session_id and store == "sessions",
            )
        )
    return candidates


def _select_candidates(
    candidates: list[RetentionCandidate],
    *,
    sessions: list[str] | None,
    older_than_days: int | None,
    keep_recent: int | None,
) -> tuple[list[RetentionCandidate], list[RetentionCandidate]]:
    selected = _select_unprotected_candidates(
        candidates,
        sessions=sessions,
        older_than_days=older_than_days,
        keep_recent=keep_recent,
    )
    protected = [candidate for candidate in selected if candidate.active]
    return [candidate for candidate in selected if not candidate.active], protected


def _select_unprotected_candidates(
    candidates: list[RetentionCandidate],
    *,
    sessions: list[str] | None,
    older_than_days: int | None,
    keep_recent: int | None,
) -> list[RetentionCandidate]:
    if sessions:
        requested = set(sessions)
        selected = [candidate for candidate in candidates if candidate.session_id in requested]
        found = {candidate.session_id for candidate in selected}
        missing = sorted(requested - found)
        if missing:
            raise AgentDirError(f"unknown session id(s): {', '.join(missing)}")
        return selected

    selected = list(candidates)
    if older_than_days is not None:
        cutoff = time.time() - (older_than_days * SECONDS_PER_DAY)
        selected = [
            candidate
            for candidate in selected
            if (candidate.latest_event_at or 0) <= cutoff
        ]
    if keep_recent is not None:
        ordered = sorted(
            candidates,
            key=lambda candidate: (
                candidate.latest_event_at or 0,
                candidate.store,
                candidate.session_id,
            ),
            reverse=True,
        )
        prune_keys = {
            (candidate.store, candidate.session_id)
            for candidate in ordered[keep_recent:]
        }
        selected = [
            candidate
            for candidate in selected
            if (candidate.store, candidate.session_id) in prune_keys
        ]
    return selected


def _raise_on_exact_active(
    command: str,
    sessions: list[str] | None,
    protected: list[RetentionCandidate],
) -> None:
    if not sessions or not protected:
        return
    ids = ", ".join(candidate.session_id for candidate in protected)
    raise AgentDirError(f"refusing to {command} active session(s): {ids}")
