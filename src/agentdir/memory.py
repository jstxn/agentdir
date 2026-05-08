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
SOURCE_MESSAGE = "message"
SOURCE_SESSION_SUMMARY = "session_summary"
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
      id integer primary key,
      source_kind text not null,
      source_id text not null unique,
      message_rowid integer references messages(id) on delete cascade,
      message_id text,
      session_id text,
      event_type text,
      subject text,
      from_actor text,
      to_actor text,
      task_id text,
      git_head text,
      workspace text,
      tool text,
      tool_exit_code integer,
      date_header text,
      date_utc text,
      file_path text,
      body_text text not null,
      vector_dim integer not null,
      vector_json text not null,
      text_sha256 text not null,
      token_count integer not null,
      indexed_at text not null
    );
    create index if not exists memory_documents_source_idx on memory_documents(source_kind, source_id);
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
    from_actor: str | None,
    to_actor: str | None,
    task_id: str | None,
    tool: str | None,
    tool_exit_code: int | None,
    workspace: str | None,
    git_head: str | None,
    date_header: str | None,
    date_utc: str | None,
    file_path: str,
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
    _insert_memory_document(
        conn,
        source_kind=SOURCE_MESSAGE,
        source_id=f"message:{file_path}",
        message_rowid=message_rowid,
        message_id=message_id,
        session_id=session_id,
        event_type=event_type,
        subject=subject,
        from_actor=from_actor,
        to_actor=to_actor,
        task_id=task_id,
        git_head=git_head,
        workspace=workspace,
        tool=tool,
        tool_exit_code=tool_exit_code,
        date_header=date_header,
        date_utc=date_utc,
        file_path=file_path,
        body_text=body_text or "",
        memory_text=text,
        indexed_at=indexed_at,
    )


def index_session_summaries(conn: sqlite3.Connection) -> None:
    sessions = conn.execute(
        """
        select session_id
        from messages
        where session_id is not null
        group by session_id
        order by max(coalesce(date_utc, indexed_at)) desc, session_id
        """
    ).fetchall()
    for session in sessions:
        session_id = session["session_id"]
        rows = conn.execute(
            """
            select *
            from messages
            where session_id = ?
            order by coalesce(date_utc, indexed_at), coalesce(created_ns, 0), file_path, id
            """,
            (session_id,),
        ).fetchall()
        if not rows:
            continue
        summary = session_memory_summary(session_id, [dict(row) for row in rows])
        last = rows[-1]
        indexed_at = now_iso()
        _insert_memory_document(
            conn,
            source_kind=SOURCE_SESSION_SUMMARY,
            source_id=f"session:{session_id}:summary",
            message_rowid=None,
            message_id=None,
            session_id=session_id,
            event_type="summary.compacted",
            subject=f"session summary: {session_id}",
            from_actor="agentdir@agentdir.local",
            to_actor=f"{session_id}@agentdir.local",
            task_id=None,
            git_head=last["git_head"],
            workspace=last["workspace"],
            tool=None,
            tool_exit_code=None,
            date_header=last["date_header"],
            date_utc=last["date_utc"],
            file_path=f"sessions/{session_id}/derived-summary",
            body_text=summary,
            memory_text=summary,
            indexed_at=indexed_at,
        )


def session_memory_summary(session_id: str, rows: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    tools: list[str] = []
    failures: list[str] = []
    excerpts: list[str] = []
    for row in rows:
        event_type = row.get("event_type") or "unknown"
        counts[event_type] = counts.get(event_type, 0) + 1
        if event_type == "tool.result":
            tool = row.get("tool") or "tool"
            exit_code = row.get("tool_exit_code")
            tools.append(f"{tool} exit={exit_code}")
            if exit_code not in (None, 0):
                failures.append(f"{tool} exit={exit_code}")
        body = _excerpt(row.get("body_text") or "", 180)
        if body and len(excerpts) < 8:
            excerpts.append(f"- {event_type}: {body}")

    lines = [
        f"Session summary: {session_id}",
        f"Events: {len(rows)}",
        "Event counts: " + ", ".join(f"{key}={counts[key]}" for key in sorted(counts)),
    ]
    if tools:
        lines.append("Tool results: " + "; ".join(tools[:12]))
    if failures:
        lines.append("Failures: " + "; ".join(failures[:12]))
    if excerpts:
        lines.append("Key records:")
        lines.extend(excerpts)
    return "\n".join(lines)


def _insert_memory_document(
    conn: sqlite3.Connection,
    *,
    source_kind: str,
    source_id: str,
    message_rowid: int | None,
    message_id: str | None,
    session_id: str | None,
    event_type: str | None,
    subject: str | None,
    from_actor: str | None,
    to_actor: str | None,
    task_id: str | None,
    git_head: str | None,
    workspace: str | None,
    tool: str | None,
    tool_exit_code: int | None,
    date_header: str | None,
    date_utc: str | None,
    file_path: str,
    body_text: str,
    memory_text: str,
    indexed_at: str | None = None,
) -> None:
    text = memory_text.strip()
    vector, token_count = vectorize(text)
    if not vector:
        return
    conn.execute(
        """
        insert or replace into memory_documents(
          source_kind, source_id, message_rowid, message_id, session_id,
          event_type, subject, from_actor, to_actor, task_id, git_head,
          workspace, tool, tool_exit_code, date_header, date_utc, file_path, body_text,
          vector_dim, vector_json, text_sha256, token_count, indexed_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_kind,
            source_id,
            message_rowid,
            message_id,
            session_id,
            event_type,
            subject,
            from_actor,
            to_actor,
            task_id,
            git_head,
            workspace,
            tool,
            tool_exit_code,
            date_header,
            date_utc,
            file_path,
            body_text,
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
        clauses.append("md.session_id = ?")
        params.append(session_id)
    if event_type:
        clauses.append("md.event_type = ?")
        params.append(event_type)
    if actor:
        clauses.append("(md.from_actor like ? or md.to_actor like ?)")
        params.extend([f"{actor}%", f"{actor}%"])
    if task_id:
        clauses.append("md.task_id = ?")
        params.append(task_id)
    if tool:
        clauses.append("md.tool = ?")
        params.append(tool)
    if git_head:
        clauses.append("md.git_head = ?")
        params.append(git_head)
    if workspace:
        clauses.append("md.workspace = ?")
        params.append(workspace)
    if since:
        clauses.append("coalesce(md.date_utc, md.indexed_at) >= ?")
        params.append(since)
    if until:
        clauses.append("coalesce(md.date_utc, md.indexed_at) <= ?")
        params.append(until)

    sql = """
        select md.*
        from memory_documents md
    """
    if clauses:
        sql += " where " + " and ".join(clauses)
    sql += " order by coalesce(md.date_utc, md.indexed_at), md.source_id"

    paths = require_root(root)
    hits: list[dict[str, Any]] = []
    with sqlite3.connect(paths.index_path) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute(sql, params).fetchall():
            score = cosine_similarity(query_vector, deserialize_vector(row["vector_json"]))
            if score < min_score:
                continue
            payload = _public_memory_row(dict(row))
            payload.pop("vector_json", None)
            payload["memory_score"] = round(score, 6)
            hits.append(payload)
    hits.sort(key=lambda row: (-float(row["memory_score"]), row.get("date_utc") or row.get("indexed_at") or ""))
    return hits[:limit]


def memory_stats(root: str | Path) -> dict[str, Any]:
    paths = require_root(root)
    with sqlite3.connect(paths.index_path) as conn:
        conn.row_factory = sqlite3.Row
        totals = conn.execute(
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
        kinds = {
            row["source_kind"]: int(row["count"])
            for row in conn.execute(
                "select source_kind, count(*) as count from memory_documents group by source_kind"
            ).fetchall()
        }
    messages = int(totals["messages"] or 0)
    documents = int(totals["memory_documents"] or 0)
    message_documents = kinds.get(SOURCE_MESSAGE, 0)
    return {
        "messages": messages,
        "memory_documents": documents,
        "message_documents": message_documents,
        "session_summary_documents": kinds.get(SOURCE_SESSION_SUMMARY, 0),
        "coverage": message_documents / messages if messages else 1.0,
        "vector_dim": totals["max_vector_dim"] or DEFAULT_VECTOR_DIM,
        "min_vector_dim": totals["min_vector_dim"],
        "max_vector_dim": totals["max_vector_dim"],
        "first_indexed_at": totals["first_indexed_at"],
        "last_indexed_at": totals["last_indexed_at"],
    }


def explain_memory_match(
    root: str | Path,
    query: str,
    *,
    source_id: str | None = None,
    min_score: float = 0.0,
) -> dict[str, Any]:
    query_vector, token_count = vectorize(query)
    if token_count == 0 or not query_vector:
        raise AgentDirError("Memory explain query must contain searchable text")

    if source_id:
        row = _memory_document(root, source_id)
        score = cosine_similarity(query_vector, deserialize_vector(row["vector_json"]))
        if score < min_score:
            raise AgentDirError(f"Memory source did not meet score threshold: {source_id}")
        hit = _public_memory_row(row)
        hit["memory_score"] = round(score, 6)
    else:
        hits = search_memory(root, query, limit=1, min_score=min_score)
        if not hits:
            raise AgentDirError("No memory hits to explain")
        hit = hits[0]

    body = hit.get("body_text") or ""
    query_terms = sorted(set(_tokens(query)))
    document_terms = sorted(set(_tokens(body)))
    overlap = sorted(set(query_terms).intersection(document_terms))
    return {
        "query": query,
        "source_id": hit.get("source_id"),
        "source_kind": hit.get("source_kind"),
        "session_id": hit.get("session_id"),
        "event_type": hit.get("event_type"),
        "subject": hit.get("subject"),
        "file_path": hit.get("file_path"),
        "memory_score": hit.get("memory_score"),
        "overlap_terms": overlap,
        "query_terms": query_terms,
        "document_terms_sample": document_terms[:30],
        "excerpt": _excerpt(body, 500),
    }


def format_memory_explanation(explanation: dict[str, Any]) -> str:
    lines = [
        f"query={explanation['query']}",
        f"source_id={explanation['source_id']}",
        f"source_kind={explanation['source_kind']}",
        f"score={explanation['memory_score']}",
        f"session={explanation.get('session_id') or ''}",
        "overlap=" + ", ".join(explanation["overlap_terms"]),
        "excerpt:",
        explanation["excerpt"],
    ]
    return "\n".join(lines)


def recent_session_summaries(root: str | Path, *, limit: int = 5) -> list[dict[str, Any]]:
    paths = require_root(root)
    with sqlite3.connect(paths.index_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select *
            from memory_documents
            where source_kind = ?
            order by coalesce(date_utc, indexed_at) desc, source_id
            limit ?
            """,
            (SOURCE_SESSION_SUMMARY, limit),
        ).fetchall()
    return [_public_memory_row(dict(row)) for row in rows]


def format_memory_hits(rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for row in rows:
        body = (row.get("body_text") or "").strip().replace("\n", "\\n")
        if len(body) > 240:
            body = body[:237] + "..."
        lines.append(
            f"{row.get('memory_score'):.3f} "
            f"{row.get('source_kind') or 'memory'} "
            f"{row.get('event_type') or 'unknown'} "
            f"{row.get('subject') or ''} "
            f"session={row.get('session_id') or ''} "
            f"{body} "
            f"{row.get('file_path')}"
        )
    return "\n".join(lines)


def _memory_document(root: str | Path, source_id: str) -> dict[str, Any]:
    paths = require_root(root)
    with sqlite3.connect(paths.index_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("select * from memory_documents where source_id = ?", (source_id,)).fetchone()
    if row is None:
        raise AgentDirError(f"Unknown memory source: {source_id}")
    return dict(row)


def _public_memory_row(row: dict[str, Any]) -> dict[str, Any]:
    row.pop("vector_json", None)
    return row


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


def _excerpt(text: str, limit: int) -> str:
    collapsed = " ".join(text.strip().split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."
