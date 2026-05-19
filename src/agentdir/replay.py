from __future__ import annotations

import sqlite3
from pathlib import Path

from .index import rebuild_index
from .query import query_messages


def replay_session(root: str | Path, session_id: str) -> list[str]:
    try:
        rows = query_messages(root, session_id=session_id, limit=10_000)
    except sqlite3.DatabaseError as exc:
        if not _looks_like_missing_index(exc):
            raise
        rebuild_index(root)
        rows = query_messages(root, session_id=session_id, limit=10_000)
    lines: list[str] = []
    for row in rows:
        date = row.get("date_header") or row.get("indexed_at") or "unknown-date"
        event_type = row.get("event_type") or "unknown"
        subject = row.get("subject") or ""
        body = (row.get("body_text") or "").strip().replace("\n", "\\n")
        file_path = row.get("file_path") or ""
        lines.append(f"{date} {event_type} {subject} {body} [{file_path}]")
    return lines


def _looks_like_missing_index(exc: sqlite3.DatabaseError) -> bool:
    message = str(exc).lower()
    return "no such table" in message or "file is not a database" in message or "database disk image is malformed" in message
