from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .mailbox import fsync_directory
from .store import AgentDirError, require_root

_SHA256_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Artifact:
    sha256: str
    path: Path
    bytes: int
    mime_type: str


def artifact_path(root: str | Path, sha256: str) -> Path:
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise AgentDirError(f"Invalid artifact sha256 digest: {sha256!r}")
    paths = require_root(root)
    return paths.artifacts / "blobs" / "sha256" / sha256[:2] / sha256[2:4] / sha256


def add_artifact(root: str | Path, source: str | Path) -> Artifact:
    paths = require_root(root)
    source_path = Path(source).expanduser().resolve()
    blobs_root = paths.artifacts / "blobs" / "sha256"
    blobs_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with source_path.open("rb") as src:
        fd, temp_name = tempfile.mkstemp(dir=blobs_root, prefix=".artifact-", suffix=".tmp")
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as dst:
                fd = -1
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    size += len(chunk)
                    digest.update(chunk)
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            sha = digest.hexdigest()
            target = artifact_path(paths.root, sha)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                temp_path.unlink(missing_ok=True)
            else:
                os.replace(temp_path, target)
                fsync_directory(target.parent)
        except Exception:
            if fd != -1:
                os.close(fd)
            temp_path.unlink(missing_ok=True)
            raise
    mime_type = mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
    return Artifact(sha256=sha, path=target, bytes=size, mime_type=mime_type)


def artifact_headers(artifact: Artifact) -> dict[str, str]:
    return {
        "X-AgentDir-Blob-SHA256": artifact.sha256,
        "X-AgentDir-Blob-Bytes": str(artifact.bytes),
        "X-AgentDir-Blob-Mime": artifact.mime_type,
    }

