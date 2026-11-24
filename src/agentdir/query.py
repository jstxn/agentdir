from __future__ import annotations

import contextlib
import re
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .index import connect_index, update_index
from .store import require_root

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize_timestamp(value: str, *, end_of_day: bool = False) -> str:
    raw = value.strip()
    if _DATE_ONLY_RE.fullmatch(raw):
        try:
            parsed_date = date.fromisoformat(raw)
        except ValueError:
            return raw
        suffix = "T23:59:59.999999+00:00" if end_of_day else "T00:00:00+00:00"
        return f"{parsed_date.isoformat()}{suffix}"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        if raw.endswith(("Z", "z")):
            return f"{raw[:-1]}+00:00"
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _index_ready(index_path: Path) -> bool:
    if not index_path.is_file() or index_path.stat().st_size == 0:
        return False
    try:
        with contextlib.closing(sqlite3.connect(index_path)) as conn:
            row = conn.execute(
                "select 1 from sqlite_master where type = 'table' and name = 'messages'"
            ).fetchone()
            return row is not None
    except sqlite3.DatabaseError:
        return False


def query_messages(
    root: str | Path,
    *,
    session_id: str | None = None,
    event_type: str | None = None,
    actor: str | None = None,
    task_id: str | None = None,
    tool: str | None = None,
    git_head: str | None = None,
    workspace: str | None = None,
    text: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = 100,
) -> list[dict[str, Any]]:
    paths = require_root(root)
    if not _index_ready(paths.index_path):
        update_index(paths.root)

    clauses: list[str] = []
    params: list[Any] = []
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if actor:
        if "@" in actor:
            clauses.append("(from_actor = ? or to_actor = ?)")
            params.extend([actor, actor])
        else:
            escaped_actor = _escape_like(actor)
            prefix = f"{escaped_actor}@%"
            clauses.append(
                "(from_actor = ? or to_actor = ? or from_actor like ? escape '\\' or to_actor like ? escape '\\')"
            )
            params.extend([actor, actor, prefix, prefix])
    if task_id:
        clauses.append("task_id = ?")
        params.append(task_id)
    if tool:
        clauses.append("tool = ?")
        params.append(tool)
    if git_head:
        clauses.append("git_head = ?")
        params.append(git_head)
    if workspace:
        clauses.append("workspace = ?")
        params.append(workspace)
    if since:
        clauses.append("coalesce(date_utc, indexed_at) >= ?")
        params.append(_normalize_timestamp(since, end_of_day=False))
    if until:
        clauses.append("coalesce(date_utc, indexed_at) <= ?")
        params.append(_normalize_timestamp(until, end_of_day=True))
    if text:
        escaped_text = _escape_like(text)
        clauses.append(
            "(subject like ? escape '\\' or body_text like ? escape '\\' or message_id like ? escape '\\')"
        )
        like = f"%{escaped_text}%"
        params.extend([like, like, like])

    sql = "select * from messages"
    if clauses:
        sql += " where " + " and ".join(clauses)
    sql += " order by coalesce(date_utc, indexed_at), coalesce(created_ns, 0), file_path, id"
    if limit is not None:
        sql += " limit ?"
        params.append(limit)

    with contextlib.closing(connect_index(paths.root)) as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
