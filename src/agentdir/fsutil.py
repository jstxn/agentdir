from __future__ import annotations

import os
import tempfile
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
    mode = target.stat().st_mode if target.exists() else None
    # A unique temp name per writer, so concurrent writers of one file (the
    # memory daemon and the CLI that starts it) cannot unlink or rename each
    # other's temp file.
    fd, temp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".agentdir-tmp")
    temp = Path(temp_name)
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
