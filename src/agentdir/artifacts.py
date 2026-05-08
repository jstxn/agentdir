from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .mailbox import fsync_directory
from .store import require_root


@dataclass(frozen=True)
class Artifact:
    sha256: str
    path: Path
    bytes: int
    mime_type: str


def artifact_path(root: str | Path, sha256: str) -> Path:
    paths = require_root(root)
    return paths.artifacts / "blobs" / "sha256" / sha256[:2] / sha256[2:4] / sha256


def add_artifact(root: str | Path, source: str | Path) -> Artifact:
    source_path = Path(source).expanduser().resolve()
    digest = hashlib.sha256()
    size = 0
    with source_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    sha = digest.hexdigest()
    target = artifact_path(root, sha)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        with source_path.open("rb") as src, temp.open("wb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(temp, target)
        fsync_directory(target.parent)
    mime_type = mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
    return Artifact(sha256=sha, path=target, bytes=size, mime_type=mime_type)


def artifact_headers(artifact: Artifact) -> dict[str, str]:
    return {
        "X-AgentDir-Blob-SHA256": artifact.sha256,
        "X-AgentDir-Blob-Bytes": str(artifact.bytes),
        "X-AgentDir-Blob-Mime": artifact.mime_type,
    }

