from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

STORE_VERSION = "0.1"
CONFIG_DIR = ".agentdir"
INDEX_FILE = "agentdir.sqlite3"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$")


class AgentDirError(Exception):
    """Base exception for user-facing AgentDir failures."""


@dataclass(frozen=True)
class RootPaths:
    root: Path
    meta: Path
    sessions: Path
    actors: Path
    queues: Path
    artifacts: Path
    indexes: Path

    @property
    def index_path(self) -> Path:
        return self.indexes / INDEX_FILE


def normalize_root(root: str | Path) -> Path:
    return Path(root).expanduser().resolve()


def paths_for(root: str | Path) -> RootPaths:
    root_path = normalize_root(root)
    return RootPaths(
        root=root_path,
        meta=root_path / CONFIG_DIR,
        sessions=root_path / "sessions",
        actors=root_path / "actors",
        queues=root_path / "queues",
        artifacts=root_path / "artifacts",
        indexes=root_path / "indexes",
    )


def validate_id(value: str, label: str = "id") -> str:
    if not _ID_RE.match(value) or "/" in value or ":" in value or value.startswith("."):
        raise AgentDirError(
            f"Invalid {label} {value!r}; use filesystem-safe letters, numbers, dot, underscore, at, plus, or dash"
        )
    return value


def ensure_mailbox(path: Path) -> Path:
    for name in ("tmp", "new", "cur"):
        (path / name).mkdir(parents=True, exist_ok=True)
    return path


def is_mailbox(path: Path) -> bool:
    return all((path / name).is_dir() for name in ("tmp", "new", "cur"))


def init_root(root: str | Path) -> RootPaths:
    paths = paths_for(root)
    paths.root.mkdir(parents=True, exist_ok=True)
    for directory in (
        paths.meta,
        paths.sessions,
        paths.actors,
        paths.queues,
        paths.artifacts / "blobs" / "sha256",
        paths.indexes,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    version_path = paths.meta / "VERSION"
    if version_path.exists():
        current = version_path.read_text(encoding="utf-8").strip()
        if current != STORE_VERSION:
            raise AgentDirError(
                f"Unsupported AgentDir root version {current!r}; expected {STORE_VERSION}"
            )
    else:
        version_path.write_text(f"{STORE_VERSION}\n", encoding="utf-8")

    config_path = paths.meta / "config.json"
    if not config_path.exists():
        config = {"version": STORE_VERSION, "index": str(paths.index_path.relative_to(paths.root))}
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return paths


def require_root(root: str | Path) -> RootPaths:
    paths = paths_for(root)
    if not (paths.meta / "VERSION").is_file():
        raise AgentDirError(f"Not an AgentDir root: {paths.root}")
    return paths


def session_mailbox(root: str | Path, session_id: str, create: bool = True) -> Path:
    paths = require_root(root)
    validate_id(session_id, "session id")
    mailbox = paths.sessions / session_id / "Maildir"
    if create:
        ensure_mailbox(mailbox)
    return mailbox


def actor_dir(root: str | Path, actor_id: str) -> Path:
    paths = require_root(root)
    validate_id(actor_id, "actor id")
    return paths.actors / actor_id


def actor_mailbox(root: str | Path, actor_id: str, box: str, create: bool = True) -> Path:
    if box not in {"inbox", "outbox"}:
        raise AgentDirError(f"Unknown actor mailbox {box!r}")
    mailbox = actor_dir(root, actor_id) / box / "Maildir"
    if create:
        ensure_mailbox(mailbox)
    return mailbox


def queue_mailbox(root: str | Path, queue_id: str, create: bool = True) -> Path:
    paths = require_root(root)
    validate_id(queue_id, "queue id")
    mailbox = paths.queues / queue_id / "Maildir"
    if create:
        ensure_mailbox(mailbox)
    return mailbox


def discover_mailboxes(root: str | Path) -> list[tuple[str, Path]]:
    paths = require_root(root)
    discovered: list[tuple[str, Path]] = []
    for session in sorted(paths.sessions.glob("*/Maildir")):
        if is_mailbox(session):
            discovered.append(("session", session))
    for actor_box in sorted(paths.actors.glob("*/*/Maildir")):
        if is_mailbox(actor_box):
            discovered.append(("actor", actor_box))
    for queue in sorted(paths.queues.glob("*/Maildir")):
        if is_mailbox(queue):
            discovered.append(("queue", queue))
    return discovered

