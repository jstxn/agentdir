from __future__ import annotations

import os
import secrets
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .fsutil import fsync_directory
from .store import AgentDirError, ensure_mailbox

_SEQUENCE = 0


@dataclass(frozen=True)
class MaildirRecord:
    mailbox: Path
    state: str
    path: Path


def _safe_hostname() -> str:
    hostname = socket.gethostname() or "localhost"
    return hostname.replace("/", "\\057").replace(":", "\\072").lstrip(".") or "localhost"


def unique_basename() -> str:
    global _SEQUENCE
    _SEQUENCE += 1
    basename = f"{time.time_ns()}.P{os.getpid()}Q{_SEQUENCE}R{secrets.token_hex(8)}.{_safe_hostname()}"
    if "/" in basename or ":" in basename or basename.startswith("."):
        raise AgentDirError("Generated unsafe Maildir basename")
    return basename


def atomic_deliver(mailbox: Path, data: bytes, basename: str | None = None) -> Path:
    ensure_mailbox(mailbox)
    for _ in range(20):
        name = basename or unique_basename()
        tmp_path = mailbox / "tmp" / name
        new_path = mailbox / "new" / name
        try:
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            basename = None
            continue
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, new_path)
        fsync_directory(mailbox / "tmp")
        fsync_directory(mailbox / "new")
        return new_path
    raise AgentDirError("Unable to create a unique Maildir filename")


def iter_records(mailbox: Path, states: Iterable[str] = ("new", "cur")) -> list[MaildirRecord]:
    records: list[MaildirRecord] = []
    for state in states:
        if state not in {"new", "cur"}:
            continue
        directory = mailbox / state
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.is_file() and not path.name.startswith("."):
                records.append(MaildirRecord(mailbox=mailbox, state=state, path=path))
    return records

