from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .store import AgentDirError, require_root

DEFAULT_VECTOR_DIM = 256
DEFAULT_MIN_SCORE = 0.05
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_+#.:-]{1,}", re.IGNORECASE)
STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "and",
    "are",
    "because",
    "before",
    "but",
    "can",
    "could",
    "for",
    "from",
    "has",
    "have",
    "into",
    "not",
    "that",
    "the",
    "then",
    "this",
    "was",
    "were",
    "with",
    "would",
}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def memory_schema_sql() -> str:
    return """
    create table if not exists memory_documents (
      message_rowid integer primary key references messages(id) on delete cascade,
      message_id text,
      session_id text,
      event_type text,
      subject text,
      vector_dim integer not null,
      vector_json text not null,
      text_sha256 text not null,
      token_count integer not null,
      indexed_at text not null
    );
    create index if not exists memory_documents_session_idx on memory_documents(session_id);
    create index if not exists memory_documents_event_type_idx on memory_documents(event_type);
    """


def build_memory_text(
    *,
    message_id: str | None = None,
    session_id: str | None = None,
    event_type: str | None = None,
    subject: str | None = None,
    tool: str | None = None,
    workspace: str | None = None,
    git_head: str | None = None,
    body_text: str | None = None,
) -> str:
    parts = [
        _labeled("message", message_id),
        _labeled("session", session_id),
        _labeled("event", event_type),
        _labeled("subject", subject),
        _labeled("tool", tool),
        _labeled("workspace", workspace),
        _labeled("git", git_head),
        body_text or "",
    ]
    return "\n".join(part for part in parts if part.strip())


def index_memory_document(
    conn: sqlite3.Connection,
    *,
    message_rowid: int,
    message_id: str | None,
    session_id: str | None,
    event_type: str | None,
    subject: str | None,
    tool: str | None,
    workspace: str | None,
    git_head: str | None,
    body_text: str | None,
    indexed_at: str | None = None,
) -> None:
    text = build_memory_text(
        message_id=message_id,
        session_id=session_id,
        event_type=event_type,
        subject=subject,
        tool=tool,
        workspace=workspace,
        git_head=git_head,
        body_text=body_text,
    )
    vector, token_count = vectorize(text)
    if not vector:
        return
    conn.execute(
        """
        insert or replace into memory_documents(
          message_rowid, message_id, session_id, event_type, subject,
          vector_dim, vector_json, text_sha256, token_count, indexed_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_rowid,
            message_id,
            session_id,
            event_type,
            subject,
            DEFAULT_VECTOR_DIM,
            serialize_vector(vector),
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
            token_count,
            indexed_at or now_iso(),
        ),
    )


def vectorize(text: str, *, dimensions: int = DEFAULT_VECTOR_DIM) -> tuple[dict[int, float], int]:
    tokens = _tokens(text)
    weights: dict[int, float] = {}
    for token in tokens:
        _add_feature(weights, token, 1.0, dimensions)
        if len(token) >= 5:
            for index in range(len(token) - 2):
                _add_feature(weights, f"tri:{token[index:index + 3]}", 0.25, dimensions)
    for first, second in zip(tokens, tokens[1:]):
        _add_feature(weights, f"bi:{first} {second}", 1.35, dimensions)

    norm = math.sqrt(sum(value * value for value in weights.values()))
    if norm == 0:
        return {}, 0
    return {index: value / norm for index, value in weights.items()}, len(tokens)


def serialize_vector(vector: dict[int, float]) -> str:
    return json.dumps({str(index): value for index, value in sorted(vector.items())}, sort_keys=True)


def deserialize_vector(value: str) -> dict[int, float]:
    raw = json.loads(value)
    return {int(index): float(weight) for index, weight in raw.items()}


def cosine_similarity(left: dict[int, float], right: dict[int, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(weight * right.get(index, 0.0) for index, weight in left.items())


def search_memory(
    root: str | Path,
    query: str,
    *,
    session_id: str | None = None,
    event_type: str | None = None,
    actor: str | None = None,
    task_id: str | None = None,
    tool: str | None = None,
    git_head: str | None = None,
    workspace: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 10,
    min_score: float = DEFAULT_MIN_SCORE,
) -> list[dict[str, Any]]:
    query_vector, token_count = vectorize(query)
    if token_count == 0 or not query_vector:
        raise AgentDirError("Memory search query must contain searchable text")

    clauses: list[str] = []
    params: list[Any] = []
    if session_id:
        clauses.append("m.session_id = ?")
        params.append(session_id)
    if event_type:
        clauses.append("m.event_type = ?")
        params.append(event_type)
    if actor:
        clauses.append("(m.from_actor like ? or m.to_actor like ?)")
        params.extend([f"{actor}%", f"{actor}%"])
    if task_id:
        clauses.append("m.task_id = ?")
        params.append(task_id)
    if tool:
        clauses.append("m.tool = ?")
        params.append(tool)
    if git_head:
        clauses.append("m.git_head = ?")
        params.append(git_head)
    if workspace:
        clauses.append("m.workspace = ?")
        params.append(workspace)
    if since:
        clauses.append("coalesce(m.date_utc, m.indexed_at) >= ?")
        params.append(since)
    if until:
        clauses.append("coalesce(m.date_utc, m.indexed_at) <= ?")
        params.append(until)

    sql = """
        select m.*, md.vector_json, md.token_count
        from memory_documents md
        join messages m on m.id = md.message_rowid
    """
    if clauses:
        sql += " where " + " and ".join(clauses)
    sql += " order by coalesce(m.date_utc, m.indexed_at), coalesce(m.created_ns, 0), m.file_path, m.id"

    paths = require_root(root)
    hits: list[dict[str, Any]] = []
    with sqlite3.connect(paths.index_path) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute(sql, params).fetchall():
            score = cosine_similarity(query_vector, deserialize_vector(row["vector_json"]))
            if score < min_score:
                continue
            payload = dict(row)
            payload.pop("vector_json", None)
            payload["memory_score"] = round(score, 6)
            hits.append(payload)
    hits.sort(key=lambda row: (-float(row["memory_score"]), row.get("date_utc") or row.get("indexed_at") or ""))
    return hits[:limit]


def memory_stats(root: str | Path) -> dict[str, Any]:
    paths = require_root(root)
    with sqlite3.connect(paths.index_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            select
              (select count(*) from messages) as messages,
              count(*) as memory_documents,
              min(vector_dim) as min_vector_dim,
              max(vector_dim) as max_vector_dim,
              min(indexed_at) as first_indexed_at,
              max(indexed_at) as last_indexed_at
            from memory_documents
            """
        ).fetchone()
    messages = int(row["messages"] or 0)
    documents = int(row["memory_documents"] or 0)
    return {
        "messages": messages,
        "memory_documents": documents,
        "coverage": documents / messages if messages else 1.0,
        "vector_dim": row["max_vector_dim"] or DEFAULT_VECTOR_DIM,
        "min_vector_dim": row["min_vector_dim"],
        "max_vector_dim": row["max_vector_dim"],
        "first_indexed_at": row["first_indexed_at"],
        "last_indexed_at": row["last_indexed_at"],
    }


def format_memory_hits(rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for row in rows:
        body = (row.get("body_text") or "").strip().replace("\n", "\\n")
        if len(body) > 240:
            body = body[:237] + "..."
        lines.append(
            f"{row.get('memory_score'):.3f} "
            f"{row.get('event_type') or 'unknown'} "
            f"{row.get('subject') or ''} "
            f"session={row.get('session_id') or ''} "
            f"{body} "
            f"{row.get('file_path')}"
        )
    return "\n".join(lines)


def _tokens(text: str) -> list[str]:
    return [
        token
        for token in TOKEN_RE.findall(text.lower())
        if len(token) > 1 and token not in STOPWORDS
    ]


def _add_feature(weights: dict[int, float], feature: str, weight: float, dimensions: int) -> None:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    index = int.from_bytes(digest[:4], "big") % dimensions
    sign = 1.0 if digest[4] & 1 else -1.0
    weights[index] = weights.get(index, 0.0) + sign * weight


def _labeled(label: str, value: str | None) -> str:
    return f"{label}: {value}" if value else ""
