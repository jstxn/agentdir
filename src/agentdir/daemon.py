from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - AgentDir targets Unix-like developer machines.
    fcntl = None

from .federation import rebuild_registered_roots
from .fsutil import atomic_write_text
from .index import update_index
from .store import AgentDirError, init_root, paths_for, require_root

DAEMON_STATE_FILE = "memory-daemon.json"
DAEMON_STOP_FILE = "memory-daemon.stop"
DAEMON_LOG_FILE = "memory-daemon.log"
DAEMON_LOCK_FILE = ".memory-daemon.lock"
DAEMON_START_WAIT_SECONDS = 5.0


def start_memory_daemon(
    root: str | Path,
    *,
    interval: float = 2.0,
    group: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    paths = init_root(root)
    current = memory_daemon_status(paths.root)
    if current["running"]:
        if not force:
            return {**current, "started": False}
        stopped = stop_memory_daemon(paths.root)
        if stopped["running"]:
            raise AgentDirError("Existing memory daemon did not stop before restart")
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
    with log_path.open("ab") as log:
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
    if fcntl is not None:
        deadline = time.monotonic() + DAEMON_START_WAIT_SECONDS
        while time.monotonic() < deadline:
            locked, lock_pid = _daemon_lock_state(paths.root)
            if locked and lock_pid == process.pid:
                break
            if process.poll() is not None:
                break
            time.sleep(0.02)
    return memory_daemon_status(paths.root) | {"started": True}


def run_memory_daemon(
    root: str | Path,
    *,
    interval: float = 2.0,
    group: str | None = None,
    once: bool = False,
) -> dict[str, Any]:
    paths = init_root(root)
    with _daemon_lock(paths.root):
        state = _daemon_state(paths.root, pid=os.getpid(), interval=interval, group=group)
        _write_state(paths.root, state)
        _refresh(paths.root, group=group)
        if not once:
            if _watchfiles_available():
                _watch_loop(paths.root, group=group, interval=interval)
            else:
                _poll_loop(paths.root, group=group, interval=interval)
    return memory_daemon_status(paths.root)


def stop_memory_daemon(root: str | Path, *, timeout: float = 5.0) -> dict[str, Any]:
    paths = require_root(root)
    _stop_path(paths.root).parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(_stop_path(paths.root), now_iso() + "\n")
    running, pid = _daemon_liveness(paths.root)
    if running and fcntl is not None and pid is None:
        # Wait briefly if the daemon just acquired the lock before flushing its PID.
        pid_deadline = time.monotonic() + min(timeout, 0.25)
        while time.monotonic() < pid_deadline:
            running, pid = _daemon_lock_state(paths.root)
            if not running or pid is not None:
                break
            time.sleep(0.01)
    if running:
        if pid:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            still_running, _ = _daemon_liveness(paths.root)
            if not still_running:
                break
            time.sleep(0.05)
    return memory_daemon_status(paths.root)


def memory_daemon_status(root: str | Path) -> dict[str, Any]:
    paths = require_root(root)
    state = _read_state(paths.root)
    running, lock_pid = _daemon_liveness(paths.root, state=state)
    if running and lock_pid is not None:
        state = {**state, "pid": lock_pid}
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
        roots = rebuild_registered_roots(root, group=group, stale_only=True)
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
    candidates = [paths.sessions, paths.actors, paths.queues]
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


def _lock_path(root: Path) -> Path:
    return paths_for(root).state / DAEMON_LOCK_FILE


@contextmanager
def _daemon_lock(root: Path):
    lock_path = _lock_path(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        if fcntl is not None:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise AgentDirError("Memory daemon is already running for this root") from exc
                raise
        lock.seek(0)
        lock.truncate()
        lock.write(f"{os.getpid()}\n")
        lock.flush()
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _daemon_lock_state(root: Path) -> tuple[bool, int | None]:
    if fcntl is None:
        return False, None
    lock_path = _lock_path(root)
    if not lock_path.is_file():
        return False, None
    try:
        with lock_path.open("r+", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    lock.seek(0)
                    return True, _parse_lock_pid(lock.read())
                raise
            try:
                return False, None
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except FileNotFoundError:
        return False, None


def _daemon_liveness(
    root: Path,
    *,
    state: dict[str, Any] | None = None,
) -> tuple[bool, int | None]:
    if fcntl is not None:
        return _daemon_lock_state(root)
    current = _read_state(root) if state is None else state
    raw_pid = current.get("pid")
    pid = int(raw_pid) if raw_pid else None
    return (_pid_running(pid) if pid else False), pid


def _parse_lock_pid(raw: str) -> int | None:
    text = raw.strip()
    if not text:
        return None
    try:
        pid = int(text)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.is_file():
        try:
            fields = proc_stat.read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            if fields and fields[0] == "Z":
                return False
        except (OSError, IndexError):
            return False
    return True


def _watchfiles_available() -> bool:
    try:
        import watchfiles  # noqa: F401
    except ImportError:
        return False
    return True
