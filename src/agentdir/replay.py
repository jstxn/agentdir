from __future__ import annotations

from pathlib import Path

from .query import query_messages


def replay_session(root: str | Path, session_id: str) -> list[str]:
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

