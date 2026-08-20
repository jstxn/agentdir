from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - unavailable on Windows
    fcntl = None

from .store import paths_for


_LOCK_STATE = threading.local()


@contextmanager
def lifecycle_lock(root: str | Path, key: str):
    """Serialize one lifecycle transition, including re-entrant callers."""
    paths = paths_for(root)
    paths.state.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    identity = (str(paths.root.resolve()), digest)
    held = getattr(_LOCK_STATE, "held", None)
    if held is None:
        held = {}
        _LOCK_STATE.held = held
    if identity in held:
        held[identity] += 1
        try:
            yield
        finally:
            held[identity] -= 1
        return

    lock_path = paths.state / f".lifecycle-{digest}.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        held[identity] = 1
        try:
            yield
        finally:
            held.pop(identity, None)
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
