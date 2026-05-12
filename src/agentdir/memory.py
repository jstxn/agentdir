from __future__ import annotations

import hashlib
import importlib.util
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
DEFAULT_PASSAGE_TOKEN_LIMIT = 160
DEFAULT_PASSAGE_TOKEN_OVERLAP = 32
SOURCE_MESSAGE = "message"
SOURCE_SESSION_SUMMARY = "session_summary"
RETRIEVAL_HYBRID = "hybrid"
RETRIEVAL_DOCUMENT = "document"
RETRIEVAL_SEMANTIC = "semantic"
RETRIEVAL_MODES = (RETRIEVAL_HYBRID, RETRIEVAL_DOCUMENT, RETRIEVAL_SEMANTIC)
MEMORY_CONFIG_FILE = "memory-backends.json"
DEFAULT_FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"
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
    create table if not exists memory_passages (
      id integer primary key,
      memory_document_id integer not null references memory_documents(id) on delete cascade,
      source_kind text not null,
      source_id text not null,
      message_rowid integer references messages(id) on delete cascade,
      session_id text,
      event_type text,
      tool text,
      git_head text,
      workspace text,
      date_utc text,
      ordinal integer not null,
      body_text text not null,
      token_count integer not null,
      vector_dim integer not null,
      vector_json text not null,
      text_sha256 text not null
    );
    create index if not exists memory_passages_document_idx on memory_passages(memory_document_id);
    create index if not exists memory_passages_source_idx on memory_passages(source_kind, source_id);
    create table if not exists memory_terms (
      term text not null,
      passage_id integer not null references memory_passages(id) on delete cascade,
      tf integer not null,
      field_mask integer not null,
      primary key(term, passage_id)
    );
    create index if not exists memory_terms_passage_idx on memory_terms(passage_id);
    create table if not exists semantic_embeddings (
      source_id text not null,
      model text not null,
      text_sha256 text not null,
      dimensions integer not null,
      vector_json text not null,
      indexed_at text not null,
      primary key(source_id, model)
    );
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
    cursor = conn.execute(
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
    document_id = int(cursor.lastrowid)
    _index_memory_passages(
        conn,
        memory_document_id=document_id,
        source_kind=source_kind,
        source_id=source_id,
        message_rowid=message_rowid,
        session_id=session_id,
        event_type=event_type,
        tool=tool,
        git_head=git_head,
        workspace=workspace,
        date_utc=date_utc,
        memory_text=text,
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
    retrieval_mode: str = RETRIEVAL_HYBRID,
) -> list[dict[str, Any]]:
    if retrieval_mode not in RETRIEVAL_MODES:
        raise AgentDirError(
            f"Unknown retrieval mode {retrieval_mode!r}; expected one of {', '.join(RETRIEVAL_MODES)}"
        )
    query_vector, token_count = vectorize(query)
    if token_count == 0 or not query_vector:
        raise AgentDirError("Memory search query must contain searchable text")

    if retrieval_mode == RETRIEVAL_HYBRID:
        hits = _search_memory_hybrid(
            root,
            query,
            query_vector=query_vector,
            session_id=session_id,
            event_type=event_type,
            actor=actor,
            task_id=task_id,
            tool=tool,
            git_head=git_head,
            workspace=workspace,
            since=since,
            until=until,
            limit=limit,
            min_score=min_score,
        )
        if hits:
            return hits

    if retrieval_mode == RETRIEVAL_SEMANTIC:
        return _search_memory_semantic(
            root,
            query,
            session_id=session_id,
            event_type=event_type,
            actor=actor,
            task_id=task_id,
            tool=tool,
            git_head=git_head,
            workspace=workspace,
            since=since,
            until=until,
            limit=limit,
            min_score=min_score,
        )

    return _search_memory_documents(
        root,
        query_vector=query_vector,
        session_id=session_id,
        event_type=event_type,
        actor=actor,
        task_id=task_id,
        tool=tool,
        git_head=git_head,
        workspace=workspace,
        since=since,
        until=until,
        limit=limit,
        min_score=min_score,
    )


def _search_memory_documents(
    root: str | Path,
    *,
    query_vector: dict[int, float],
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
    clauses: list[str] = []
    params: list[Any] = []
    _append_memory_filters(
        clauses,
        params,
        session_id=session_id,
        event_type=event_type,
        actor=actor,
        task_id=task_id,
        tool=tool,
        git_head=git_head,
        workspace=workspace,
        since=since,
        until=until,
    )

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
    hits.sort(key=_hit_sort_key)
    return hits[:limit]


def _search_memory_semantic(
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
    config = read_memory_config(root)
    embeddings = config.get("embeddings") or {}
    if embeddings.get("provider") != "fastembed":
        raise AgentDirError("Semantic retrieval requires: agentdir memory embeddings configure fastembed")
    if not _module_available("fastembed"):
        raise AgentDirError("Semantic retrieval requires the optional semantic extra: fastembed")

    clauses: list[str] = []
    params: list[Any] = []
    _append_memory_filters(
        clauses,
        params,
        session_id=session_id,
        event_type=event_type,
        actor=actor,
        task_id=task_id,
        tool=tool,
        git_head=git_head,
        workspace=workspace,
        since=since,
        until=until,
    )
    sql = "select md.* from memory_documents md"
    if clauses:
        sql += " where " + " and ".join(clauses)
    sql += " order by coalesce(md.date_utc, md.indexed_at), md.source_id"
    model_name = embeddings.get("model") or DEFAULT_FASTEMBED_MODEL
    paths = require_root(root)
    with sqlite3.connect(paths.index_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
        if not rows:
            return []
        query_vector = _fastembed_vectors(model_name, [query])[0]
        document_vectors = _semantic_vectors_for_rows(conn, model_name, rows)

    hits: list[dict[str, Any]] = []
    for row, vector in zip(rows, document_vectors, strict=True):
        score = dense_cosine_similarity(query_vector, vector)
        if row.get("source_kind") == SOURCE_SESSION_SUMMARY:
            score *= 0.75
        if score < min_score:
            continue
        payload = _public_memory_row(row)
        payload["memory_score"] = round(score, 6)
        payload["retrieval_mode"] = RETRIEVAL_SEMANTIC
        hits.append(payload)
    hits.sort(key=_hit_sort_key)
    return hits[:limit]


def _search_memory_hybrid(
    root: str | Path,
    query: str,
    *,
    query_vector: dict[int, float],
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
    query_terms = sorted(set(_tokens(query)))
    if not query_terms:
        return []

    clauses = ["mt.term in (" + ", ".join("?" for _ in query_terms) + ")"]
    params: list[Any] = [*query_terms]
    _append_memory_filters(
        clauses,
        params,
        session_id=session_id,
        event_type=event_type,
        actor=actor,
        task_id=task_id,
        tool=tool,
        git_head=git_head,
        workspace=workspace,
        since=since,
        until=until,
    )
    candidate_limit = max(limit * 24, 64)
    sql = f"""
        select
          md.*,
          mp.id as passage_id,
          mp.ordinal as passage_ordinal,
          mp.body_text as passage_body_text,
          mp.vector_json as passage_vector_json,
          mp.text_sha256 as passage_text_sha256,
          mp.token_count as passage_token_count,
          sum(mt.tf) as lexical_score
        from memory_terms mt
        join memory_passages mp on mp.id = mt.passage_id
        join memory_documents md on md.id = mp.memory_document_id
        where {' and '.join(clauses)}
        group by mp.id
        order by sum(mt.tf) desc, coalesce(md.date_utc, md.indexed_at) desc, md.source_id
        limit ?
    """
    params.append(candidate_limit)

    paths = require_root(root)
    best_by_source: dict[str, dict[str, Any]] = {}
    with sqlite3.connect(paths.index_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        for row in rows:
            vector_score = cosine_similarity(query_vector, deserialize_vector(row["passage_vector_json"]))
            lexical_score = float(row["lexical_score"] or 0)
            lexical_ratio = min(1.0, lexical_score / max(len(query_terms), 1))
            score = (0.82 * max(vector_score, 0.0)) + (0.18 * lexical_ratio)
            if row["source_kind"] == SOURCE_SESSION_SUMMARY:
                score *= 0.75
            if score < min_score:
                continue
            payload = _public_memory_row(dict(row))
            payload.pop("passage_vector_json", None)
            payload["memory_score"] = round(score, 6)
            payload["passage_score"] = round(vector_score, 6)
            payload["lexical_score"] = round(lexical_score, 6)
            payload["retrieval_mode"] = RETRIEVAL_HYBRID
            source_id = str(payload["source_id"])
            previous = best_by_source.get(source_id)
            if previous is None or _hit_sort_key(payload) < _hit_sort_key(previous):
                best_by_source[source_id] = payload

    hits = sorted(best_by_source.values(), key=_hit_sort_key)
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
        passage_totals = conn.execute(
            """
            select
              count(*) as passages,
              coalesce(sum(token_count), 0) as passage_tokens,
              (select count(*) from memory_terms) as terms
            from memory_passages
            """
        ).fetchone()
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
        "passages": int(passage_totals["passages"] or 0),
        "passage_tokens": int(passage_totals["passage_tokens"] or 0),
        "terms": int(passage_totals["terms"] or 0),
        "retrieval_backend": "local-hybrid",
    }


def memory_backend_status(root: str | Path) -> dict[str, Any]:
    stats = memory_stats(root)
    config = read_memory_config(root)
    fastembed_available = _module_available("fastembed")
    sqlite_vec_available = _module_available("sqlite_vec")
    embeddings = config.get("embeddings", {})
    configured_embedding_provider = embeddings.get("provider")
    semantic_enabled = configured_embedding_provider == "fastembed" and fastembed_available
    active = "semantic-local" if semantic_enabled else "local-hybrid"
    return {
        "active": active,
        "source_of_truth": "immutable envelopes",
        "config": config,
        "backends": [
            {
                "name": "local-hybrid",
                "kind": "built-in",
                "enabled": True,
                "dependencies": [],
                "tables": ["memory_documents", "memory_passages", "memory_terms"],
                "documents": stats["memory_documents"],
                "passages": stats["passages"],
                "terms": stats["terms"],
            },
            {
                "name": "sqlite-vec",
                "kind": "optional-extra",
                "enabled": bool(config.get("vector_backend") == "sqlite-vec" and sqlite_vec_available),
                "dependencies": ["sqlite-vec"],
                "available": sqlite_vec_available,
                "configured": config.get("vector_backend") == "sqlite-vec",
                "status": _optional_status(
                    configured=config.get("vector_backend") == "sqlite-vec",
                    available=sqlite_vec_available,
                ),
            },
            {
                "name": "local-embeddings",
                "kind": "optional-extra",
                "enabled": semantic_enabled,
                "dependencies": ["fastembed"],
                "available": fastembed_available,
                "configured": configured_embedding_provider == "fastembed",
                "provider": configured_embedding_provider,
                "model": embeddings.get("model"),
                "status": _optional_status(
                    configured=configured_embedding_provider == "fastembed",
                    available=fastembed_available,
                ),
            },
            {
                "name": "qdrant",
                "kind": "optional-extra",
                "enabled": bool(config.get("team_backend") == "qdrant" and _module_available("qdrant_client")),
                "dependencies": ["qdrant client or service"],
                "available": _module_available("qdrant_client"),
                "configured": config.get("team_backend") == "qdrant",
                "status": _optional_status(
                    configured=config.get("team_backend") == "qdrant",
                    available=_module_available("qdrant_client"),
                ),
            },
            {
                "name": "lancedb",
                "kind": "optional-extra",
                "enabled": bool(config.get("team_backend") == "lancedb" and _module_available("lancedb")),
                "dependencies": ["lancedb"],
                "available": _module_available("lancedb"),
                "configured": config.get("team_backend") == "lancedb",
                "status": _optional_status(
                    configured=config.get("team_backend") == "lancedb",
                    available=_module_available("lancedb"),
                ),
            },
        ],
    }


def read_memory_config(root: str | Path) -> dict[str, Any]:
    path = _memory_config_path(root)
    if not path.is_file():
        return {"version": 1, "vector_backend": None, "embeddings": {}, "team_backend": None}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise AgentDirError(f"Unsupported memory config version: {data.get('version')}")
    data.setdefault("vector_backend", None)
    data.setdefault("embeddings", {})
    data.setdefault("team_backend", None)
    return data


def configure_vector_backend(root: str | Path, backend: str) -> dict[str, Any]:
    if backend not in {"sqlite-vec", "none"}:
        raise AgentDirError("Unknown vector backend; expected sqlite-vec or none")
    config = read_memory_config(root)
    config["vector_backend"] = None if backend == "none" else backend
    _write_memory_config(root, config)
    return memory_backend_status(root)


def configure_embeddings(root: str | Path, provider: str, *, model: str | None = None) -> dict[str, Any]:
    if provider not in {"fastembed", "none"}:
        raise AgentDirError("Unknown embedding provider; expected fastembed or none")
    config = read_memory_config(root)
    config["embeddings"] = {} if provider == "none" else {
        "provider": provider,
        "model": model or DEFAULT_FASTEMBED_MODEL,
    }
    _write_memory_config(root, config)
    return memory_backend_status(root)


def configure_team_backend(root: str | Path, backend: str) -> dict[str, Any]:
    if backend not in {"qdrant", "lancedb", "none"}:
        raise AgentDirError("Unknown team backend; expected qdrant, lancedb, or none")
    config = read_memory_config(root)
    config["team_backend"] = None if backend == "none" else backend
    _write_memory_config(root, config)
    return memory_backend_status(root)


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
        hit.update(_best_passage_for_document(root, int(row["id"]), query_vector))
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
        "passage_id": hit.get("passage_id"),
        "passage_ordinal": hit.get("passage_ordinal"),
        "passage_score": hit.get("passage_score"),
        "lexical_score": hit.get("lexical_score"),
        "passage_excerpt": _excerpt(hit.get("passage_body_text") or body, 500),
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
        f"passage_id={explanation.get('passage_id') or ''}",
        f"passage_score={explanation.get('passage_score') or ''}",
        f"lexical_score={explanation.get('lexical_score') or ''}",
        "passage_excerpt:",
        explanation["passage_excerpt"],
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
            f"passage={row.get('passage_ordinal') if row.get('passage_ordinal') is not None else ''} "
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


def _semantic_vectors_for_rows(
    conn: sqlite3.Connection,
    model_name: str,
    rows: list[dict[str, Any]],
) -> list[list[float]]:
    vectors: list[list[float] | None] = []
    missing: list[dict[str, Any]] = []
    for row in rows:
        cached = conn.execute(
            """
            select vector_json
            from semantic_embeddings
            where source_id = ? and model = ? and text_sha256 = ?
            """,
            (row["source_id"], model_name, row["text_sha256"]),
        ).fetchone()
        if cached:
            vectors.append(json.loads(cached["vector_json"]))
        else:
            vectors.append(None)
            missing.append(row)
    if missing:
        embedded = _fastembed_vectors(model_name, [row["body_text"] for row in missing])
        now = now_iso()
        for row, vector in zip(missing, embedded, strict=True):
            conn.execute(
                """
                insert or replace into semantic_embeddings(
                  source_id, model, text_sha256, dimensions, vector_json, indexed_at
                ) values (?, ?, ?, ?, ?, ?)
                """,
                (
                    row["source_id"],
                    model_name,
                    row["text_sha256"],
                    len(vector),
                    json.dumps(vector),
                    now,
                ),
            )
        conn.commit()
        iterator = iter(embedded)
        vectors = [next(iterator) if vector is None else vector for vector in vectors]
    return [vector for vector in vectors if vector is not None]


def _fastembed_vectors(model_name: str, texts: list[str]) -> list[list[float]]:
    from fastembed import TextEmbedding

    model = TextEmbedding(model_name=model_name)
    return [[float(value) for value in vector] for vector in model.embed(texts)]


def dense_cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _index_memory_passages(
    conn: sqlite3.Connection,
    *,
    memory_document_id: int,
    source_kind: str,
    source_id: str,
    message_rowid: int | None,
    session_id: str | None,
    event_type: str | None,
    tool: str | None,
    git_head: str | None,
    workspace: str | None,
    date_utc: str | None,
    memory_text: str,
) -> None:
    for ordinal, passage_text in enumerate(_passage_texts(memory_text)):
        vector, token_count = vectorize(passage_text)
        if not vector:
            continue
        cursor = conn.execute(
            """
            insert into memory_passages(
              memory_document_id, source_kind, source_id, message_rowid, session_id,
              event_type, tool, git_head, workspace, date_utc, ordinal, body_text,
              token_count, vector_dim, vector_json, text_sha256
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory_document_id,
                source_kind,
                source_id,
                message_rowid,
                session_id,
                event_type,
                tool,
                git_head,
                workspace,
                date_utc,
                ordinal,
                passage_text,
                token_count,
                DEFAULT_VECTOR_DIM,
                serialize_vector(vector),
                hashlib.sha256(passage_text.encode("utf-8")).hexdigest(),
            ),
        )
        passage_id = int(cursor.lastrowid)
        for term, tf in _term_counts(passage_text).items():
            conn.execute(
                "insert into memory_terms(term, passage_id, tf, field_mask) values (?, ?, ?, ?)",
                (term, passage_id, tf, 1),
            )


def _passage_texts(text: str) -> list[str]:
    tokens = _tokens(text)
    if len(tokens) <= DEFAULT_PASSAGE_TOKEN_LIMIT:
        return [text.strip()] if text.strip() else []

    passages: list[str] = []
    step = DEFAULT_PASSAGE_TOKEN_LIMIT - DEFAULT_PASSAGE_TOKEN_OVERLAP
    for start in range(0, len(tokens), step):
        window = tokens[start : start + DEFAULT_PASSAGE_TOKEN_LIMIT]
        if not window:
            break
        passages.append(" ".join(window))
        if start + DEFAULT_PASSAGE_TOKEN_LIMIT >= len(tokens):
            break
    return passages


def _term_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for token in _tokens(text):
        counts[token] = counts.get(token, 0) + 1
    return counts


def _append_memory_filters(
    clauses: list[str],
    params: list[Any],
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
) -> None:
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


def _best_passage_for_document(
    root: str | Path,
    document_id: int,
    query_vector: dict[int, float],
) -> dict[str, Any]:
    paths = require_root(root)
    with sqlite3.connect(paths.index_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select id, ordinal, body_text, vector_json, text_sha256, token_count
            from memory_passages
            where memory_document_id = ?
            order by ordinal
            """,
            (document_id,),
        ).fetchall()
    best: dict[str, Any] = {}
    best_score = -1.0
    for row in rows:
        score = cosine_similarity(query_vector, deserialize_vector(row["vector_json"]))
        if score > best_score:
            best_score = score
            best = {
                "passage_id": row["id"],
                "passage_ordinal": row["ordinal"],
                "passage_body_text": row["body_text"],
                "passage_text_sha256": row["text_sha256"],
                "passage_token_count": row["token_count"],
                "passage_score": round(score, 6),
            }
    return best


def _hit_sort_key(row: dict[str, Any]) -> tuple[float, str, str]:
    score = float(row["memory_score"])
    if row.get("source_kind") == SOURCE_SESSION_SUMMARY:
        score -= 0.12
    return (
        -score,
        row.get("date_utc") or row.get("indexed_at") or "",
        row.get("source_id") or "",
    )


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


def _memory_config_path(root: str | Path) -> Path:
    return require_root(root).state / MEMORY_CONFIG_FILE


def _write_memory_config(root: str | Path, config: dict[str, Any]) -> None:
    path = _memory_config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _module_available(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def _optional_status(*, configured: bool, available: bool) -> str:
    if configured and available:
        return "ready"
    if configured:
        return "configured but dependency missing"
    if available:
        return "available but not configured"
    return "not configured"
