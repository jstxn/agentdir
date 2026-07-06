from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any

from .federation import rebuild_registered_roots
from .fsutil import atomic_write_text
from .index import update_index
from .store import AgentDirError, init_root, paths_for, require_root

DAEMON_STATE_FILE = "memory-daemon.json"
DAEMON_STOP_FILE = "memory-daemon.stop"
DAEMON_LOG_FILE = "memory-daemon.log"


def start_memory_daemon(
    root: str | Path,
    *,
    interval: float = 2.0,
    group: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    paths = init_root(root)
    current = memory_daemon_status(paths.root)
    if current["running"] and not force:
        return {**current, "started": False}
    _stop_path(paths.root).unlink(missing_ok=True)
    log_path = paths.state / DAEMON_LOG_FILE
    command = [
        sys.executable,
        "-m",
        "agentdir",
        "memory",
        "daemon",
        "run",
        "--root",
        str(paths.root),
        "--interval",
        str(interval),
    ]
    if group:
        command.extend(["--group", group])
    log = log_path.open("ab")
    process = subprocess.Popen(
        command,
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=Path.cwd(),
        env=os.environ.copy(),
        start_new_session=True,
    )
    state = _daemon_state(paths.root, pid=process.pid, interval=interval, group=group)
    state.update({"started": True, "log_path": str(log_path)})
    _write_state(paths.root, state)
    return memory_daemon_status(paths.root) | {"started": True}


def run_memory_daemon(
    root: str | Path,
    *,
    interval: float = 2.0,
    group: str | None = None,
    once: bool = False,
) -> dict[str, Any]:
    paths = init_root(root)
    state = _daemon_state(paths.root, pid=os.getpid(), interval=interval, group=group)
    _write_state(paths.root, state)
    _refresh(paths.root, group=group)
    if once:
        return memory_daemon_status(paths.root)
    if _watchfiles_available():
        _watch_loop(paths.root, group=group, interval=interval)
    else:
        _poll_loop(paths.root, group=group, interval=interval)
    return memory_daemon_status(paths.root)


def stop_memory_daemon(root: str | Path, *, timeout: float = 5.0) -> dict[str, Any]:
    paths = require_root(root)
    _stop_path(paths.root).write_text(now_iso() + "\n", encoding="utf-8")
    status = memory_daemon_status(paths.root)
    pid = status.get("pid")
    if pid and status["running"]:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _pid_running(int(pid)):
                break
            time.sleep(0.05)
    return memory_daemon_status(paths.root)


def memory_daemon_status(root: str | Path) -> dict[str, Any]:
    paths = require_root(root)
    state = _read_state(paths.root)
    pid = state.get("pid")
    running = _pid_running(int(pid)) if pid else False
    return {
        **state,
        "running": running,
        "stop_requested": _stop_path(paths.root).is_file(),
        "watch_backend": "watchfiles" if _watchfiles_available() else "poll",
        "state_path": str(_state_path(paths.root)),
        "log_path": str(paths.state / DAEMON_LOG_FILE),
    }


def format_memory_daemon_status(status: dict[str, Any]) -> str:
    lines = [
        f"running={str(status['running']).lower()}",
        f"pid={status.get('pid') or ''}",
        f"backend={status['watch_backend']}",
        f"interval={status.get('interval') or ''}",
        f"group={status.get('group') or ''}",
        f"last_refresh_at={status.get('last_refresh_at') or ''}",
        f"last_refresh_ok={str(status.get('last_refresh_ok', False)).lower()}",
    ]
    if status.get("last_error"):
        lines.append(f"last_error={status['last_error']}")
    return "\n".join(lines) + "\n"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _poll_loop(root: Path, *, group: str | None, interval: float) -> None:
    while not _stop_path(root).is_file():
        time.sleep(max(interval, 0.1))
        if _stop_path(root).is_file():
            break
        _refresh(root, group=group)


def _watch_loop(root: Path, *, group: str | None, interval: float) -> None:
    try:
        from watchfiles import watch
    except ImportError:
        _poll_loop(root, group=group, interval=interval)
        return
    stop = Event()
    watch_paths = _watch_paths(root)
    timeout_ms = max(int(interval * 1000), 100)
    for changes in watch(
        *watch_paths,
        stop_event=stop,
        yield_on_timeout=True,
        rust_timeout=timeout_ms,
        recursive=True,
    ):
        if _stop_path(root).is_file():
            stop.set()
            break
        if changes:
            _refresh(root, group=group)


def _refresh(root: Path, *, group: str | None) -> None:
    state = _read_state(root)
    try:
        result = update_index(root)
        roots = rebuild_registered_roots(root, group=group)
    except Exception as exc:
        state.update(
            {
                "last_refresh_at": now_iso(),
                "last_refresh_ok": False,
                "last_error": str(exc),
            }
        )
    else:
        state.update(
            {
                "last_refresh_at": now_iso(),
                "last_refresh_ok": True,
                "last_error": None,
                "indexed": result.indexed,
                "malformed": result.malformed,
                "registered_roots": len(roots),
            }
        )
    _write_state(root, state)


def _watch_paths(root: Path) -> list[str]:
    paths = paths_for(root)
    candidates = [paths.sessions, paths.actors, paths.queues, paths.state]
    return [str(path) for path in candidates if path.exists()]


def _daemon_state(root: Path, *, pid: int, interval: float, group: str | None) -> dict[str, Any]:
    return {
        "pid": pid,
        "root": str(root),
        "interval": interval,
        "group": group,
        "started_at": now_iso(),
        "last_refresh_at": None,
        "last_refresh_ok": False,
        "last_error": None,
    }


def _read_state(root: Path) -> dict[str, Any]:
    path = _state_path(root)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AgentDirError(f"Invalid daemon state file: {exc}") from exc


def _write_state(root: Path, state: dict[str, Any]) -> None:
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(state, indent=2, sort_keys=True) + "\n")


def _state_path(root: Path) -> Path:
    return paths_for(root).state / DAEMON_STATE_FILE


def _stop_path(root: Path) -> Path:
    return paths_for(root).state / DAEMON_STOP_FILE


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _watchfiles_available() -> bool:
    try:
        import watchfiles  # noqa: F401
    except ImportError:
        return False
    return True
