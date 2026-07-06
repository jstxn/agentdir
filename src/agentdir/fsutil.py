from __future__ import annotations

import os
from pathlib import Path


def fsync_directory(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    """Write data durably: temp file in the same directory, fsync, rename.

    Preserves the existing file mode when the target already exists. Either
    the old content or the new content survives a crash, never a partial
    write.
    """
    target = Path(path)
    temp = target.with_name(f".{target.name}.agentdir-tmp")
    if temp.exists():
        temp.unlink()
    mode = target.stat().st_mode if target.exists() else None
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp, mode)
        os.replace(temp, target)
        fsync_directory(target.parent)
    except Exception:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(path: str | Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))
