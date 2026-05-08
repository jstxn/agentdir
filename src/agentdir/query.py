from __future__ import annotations

from pathlib import Path
from typing import Any

from .index import connect_index


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
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if actor:
        clauses.append("(from_actor like ? or to_actor like ?)")
        params.extend([f"{actor}%", f"{actor}%"])
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
        params.append(since)
    if until:
        clauses.append("coalesce(date_utc, indexed_at) <= ?")
        params.append(until)
    if text:
        clauses.append("(subject like ? or body_text like ? or message_id like ?)")
        like = f"%{text}%"
        params.extend([like, like, like])

    sql = "select * from messages"
    if clauses:
        sql += " where " + " and ".join(clauses)
    sql += " order by coalesce(date_utc, indexed_at), coalesce(created_ns, 0), file_path, id limit ?"
    params.append(limit)

    with connect_index(root) as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
