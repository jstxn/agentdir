from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .index import rebuild_index
from .query import query_messages
from .sessions import require_current_session


def resolve_review_session(root: str | Path, session_id: str | None) -> str:
    if session_id:
        return session_id
    return require_current_session(root).session_id


def ensure_index(root: str | Path) -> None:
    rebuild_index(root)


def summarize_session(root: str | Path, session_id: str | None = None) -> dict[str, Any]:
    resolved = resolve_review_session(root, session_id)
    ensure_index(root)
    rows = query_messages(root, session_id=resolved, limit=10_000)
    counts = Counter(row.get("event_type") or "unknown" for row in rows)
    tool_results = [row for row in rows if row.get("event_type") == "tool.result"]
    failed_tools = [row for row in tool_results if row.get("tool_exit_code") not in (None, 0)]
    return {
        "session_id": resolved,
        "events": len(rows),
        "event_counts": dict(sorted(counts.items())),
        "tool_results": len(tool_results),
        "failed_tools": len(failed_tools),
        "first_event": rows[0]["date_header"] if rows else None,
        "last_event": rows[-1]["date_header"] if rows else None,
    }


def format_summary(summary: dict[str, Any]) -> str:
    lines = [
        f"session={summary['session_id']}",
        f"events={summary['events']}",
        f"tool_results={summary['tool_results']}",
        f"failed_tools={summary['failed_tools']}",
    ]
    for event_type, count in summary["event_counts"].items():
        lines.append(f"{event_type}={count}")
    return "\n".join(lines)


def evidence_rows(root: str | Path, session_id: str | None = None) -> list[dict[str, Any]]:
    resolved = resolve_review_session(root, session_id)
    ensure_index(root)
    rows = query_messages(root, session_id=resolved, limit=10_000)
    wanted = {"tool.call", "tool.result", "file.diff"}
    return [
        row
        for row in rows
        if row.get("event_type") in wanted or str(row.get("event_type") or "").startswith("git.hook.")
    ]


def format_evidence(rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for row in rows:
        date = row.get("date_header") or row.get("indexed_at") or "unknown-date"
        event_type = row.get("event_type") or "unknown"
        tool = row.get("tool") or ""
        exit_code = row.get("tool_exit_code")
        subject = row.get("subject") or ""
        file_path = row.get("file_path") or ""
        detail = f" exit={exit_code}" if exit_code is not None else ""
        lines.append(f"{date} {event_type} {tool}{detail} {subject} [{file_path}]")
    return "\n".join(lines)
