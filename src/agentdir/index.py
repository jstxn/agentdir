from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - AgentDir targets Unix-like developer machines.
    fcntl = None

from .envelope import parse_envelope, validate_required
from .mailbox import iter_records
from .memory import index_memory_document, index_session_summaries, memory_schema_sql
from .store import AgentDirError, RootPaths, discover_mailboxes, paths_for, require_root

SCHEMA = """
pragma foreign_keys = on;
create table if not exists metadata (key text primary key, value text not null);
create table if not exists messages (
  id integer primary key,
  message_id text,
  mailbox_kind text not null,
  mailbox_path text not null,
  file_path text not null unique,
  state text not null,
  event_type text,
  subject text,
  from_actor text,
  to_actor text,
  session_id text,
  task_id text,
  parent_message_id text,
  date_header text,
  date_utc text,
  created_ns integer,
  git_head text,
  workspace text,
  tool text,
  tool_exit_code integer,
  body_sha256 text,
  body_text text,
  indexed_at text not null,
  malformed integer not null default 0,
  errors text not null default '[]'
);
create index if not exists messages_message_id_idx on messages(message_id);
create index if not exists messages_session_idx on messages(session_id);
create index if not exists messages_event_type_idx on messages(event_type);
create index if not exists messages_task_idx on messages(task_id);
create table if not exists headers (
  message_rowid integer not null references messages(id) on delete cascade,
  name text not null,
  value text not null
);
create table if not exists artifacts (
  sha256 text primary key,
  path text not null,
  bytes integer,
  mime_type text,
  created_at text not null
);
create table if not exists message_artifacts (
  message_rowid integer not null references messages(id) on delete cascade,
  sha256 text not null
);
"""


@dataclass
class IndexResult:
    indexed: int = 0
    malformed: int = 0
    duplicates: dict[str, list[str]] = field(default_factory=dict)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def initialize_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.executescript(memory_schema_sql())
    conn.execute("insert or replace into metadata(key, value) values('schema_version', '3')")
    conn.execute("insert or replace into metadata(key, value) values('vector_memory', 'yes')")
    conn.execute("insert or replace into metadata(key, value) values('hybrid_passages', 'yes')")
    conn.execute("insert or replace into metadata(key, value) values('memory_backend', 'local-hybrid')")
    conn.execute("insert or replace into metadata(key, value) values('semantic_embeddings', 'optional')")
    try:
        conn.execute(
            "create virtual table if not exists message_fts using fts5(message_id, subject, body_text)"
        )
        conn.execute("insert or replace into metadata(key, value) values('fts5', 'yes')")
    except sqlite3.DatabaseError:
        conn.execute("insert or replace into metadata(key, value) values('fts5', 'no')")


def connect_index(root: str | Path) -> sqlite3.Connection:
    paths = require_root(root)
    conn = sqlite3.connect(paths.index_path)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    return conn


@contextmanager
def index_rebuild_lock(indexes: Path):
    indexes.mkdir(parents=True, exist_ok=True)
    lock_path = indexes / ".agentdir-index.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def rebuild_index(root: str | Path) -> IndexResult:
    paths = require_root(root)
    with index_rebuild_lock(paths.indexes):
        return _rebuild_index_locked(paths)


def update_index(root: str | Path) -> IndexResult:
    """Incrementally index added and removed envelope files.

    Diffs mailbox contents against indexed file paths, so it only parses new
    envelopes and drops rows for deleted ones. It does not detect in-place
    edits to existing envelopes; flows that mutate stored envelopes (secret
    redaction) must run a full rebuild_index() instead. Falls back to a full
    rebuild when the index is missing or has an unexpected schema.
    """
    paths = require_root(root)
    if not paths.index_path.exists() or not _index_schema_current(paths.index_path):
        return rebuild_index(root)
    with index_rebuild_lock(paths.indexes):
        conn = sqlite3.connect(paths.index_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("pragma foreign_keys = on")
            result = _update_into(conn, root)
            conn.commit()
            return result
        except sqlite3.DatabaseError:
            conn.close()
            conn = None
            return _rebuild_index_locked(paths)
        finally:
            if conn is not None:
                conn.close()


def _index_schema_current(index_path: Path) -> bool:
    try:
        conn = sqlite3.connect(index_path)
        try:
            row = conn.execute("select value from metadata where key = 'schema_version'").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False
    return bool(row) and row[0] == "3"


def _update_into(conn: sqlite3.Connection, root: str | Path) -> IndexResult:
    paths = paths_for(root)
    existing: dict[str, int] = {
        row["file_path"]: row["id"]
        for row in conn.execute("select id, file_path from messages")
    }
    on_disk: dict[str, tuple[str, Path, str, Path]] = {}
    for kind, mailbox in discover_mailboxes(root):
        for record in iter_records(mailbox):
            relative = str(record.path.relative_to(paths.root))
            on_disk[relative] = (kind, record.mailbox, record.state, record.path)

    result = IndexResult()
    affected_sessions: set[str] = set()

    removed_ids = [existing[path] for path in existing.keys() - on_disk.keys()]
    for start in range(0, len(removed_ids), 500):
        chunk = removed_ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(f"select session_id from messages where id in ({marks})", chunk):
            if row["session_id"]:
                affected_sessions.add(row["session_id"])
        try:
            conn.execute(f"delete from message_fts where rowid in ({marks})", chunk)
        except sqlite3.DatabaseError:
            pass
        conn.execute(f"delete from messages where id in ({marks})", chunk)

    for relative in sorted(on_disk.keys() - existing.keys()):
        kind, mailbox, state, path = on_disk[relative]
        rowid, _message_id, malformed = _insert_record(
            conn=conn,
            root=paths.root,
            kind=kind,
            mailbox=mailbox,
            state=state,
            path=path,
        )
        result.indexed += 1
        if malformed:
            result.malformed += 1
        row = conn.execute("select session_id from messages where id = ?", (rowid,)).fetchone()
        if row and row["session_id"]:
            affected_sessions.add(row["session_id"])

    if affected_sessions:
        index_session_summaries(conn, only_sessions=affected_sessions)
        conn.execute(
            """
            delete from memory_documents
            where source_kind = 'session_summary'
              and session_id is not null
              and session_id not in (
                select distinct session_id from messages where session_id is not null
              )
            """
        )

    duplicate_rows = conn.execute(
        """
        select message_id, group_concat(file_path, char(10)) as files
        from messages
        where message_id is not null
        group by message_id
        having count(*) > 1
        """
    ).fetchall()
    result.duplicates = {row["message_id"]: row["files"].split("\n") for row in duplicate_rows}
    return result


def _rebuild_index_locked(paths: RootPaths) -> IndexResult:
    # Caller must hold index_rebuild_lock.
    temp_path = paths.index_path.with_name(
        f"{paths.index_path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    conn = sqlite3.connect(temp_path)
    conn.row_factory = sqlite3.Row
    replaced = False
    try:
        conn.execute("pragma foreign_keys = on")
        initialize_schema(conn)
        result = _index_into(conn, paths.root)
        conn.commit()
        _carry_forward_semantic_embeddings(conn, paths.index_path)
        conn.close()
        os.replace(temp_path, paths.index_path)
        replaced = True
        return result
    finally:
        if not replaced:
            conn.close()
        if not replaced and temp_path.exists():
            temp_path.unlink()


def _carry_forward_semantic_embeddings(conn: sqlite3.Connection, previous_index: Path) -> None:
    # Rebuilds start from an empty temp database, which would discard the
    # semantic embedding cache and force a full re-embed on the next semantic
    # query. Embeddings are content-addressed by (source_id, model,
    # text_sha256), so rows whose document text is unchanged stay valid; the
    # join drops rows for documents that no longer exist.
    if not previous_index.exists():
        return
    try:
        conn.execute("attach database ? as previous", (str(previous_index),))
    except sqlite3.DatabaseError:
        return
    try:
        conn.execute(
            """
            insert or replace into semantic_embeddings(
              source_id, model, text_sha256, dimensions, vector_json, indexed_at
            )
            select prev.source_id, prev.model, prev.text_sha256,
                   prev.dimensions, prev.vector_json, prev.indexed_at
            from previous.semantic_embeddings prev
            join memory_documents md
              on md.source_id = prev.source_id and md.text_sha256 = prev.text_sha256
            """
        )
        conn.commit()
    except sqlite3.DatabaseError:
        conn.rollback()
    finally:
        conn.execute("detach database previous")


def _index_into(conn: sqlite3.Connection, root: str | Path) -> IndexResult:
    conn.execute("delete from memory_terms")
    conn.execute("delete from memory_passages")
    conn.execute("delete from memory_documents")
    conn.execute("delete from message_artifacts")
    conn.execute("delete from headers")
    conn.execute("delete from messages")
    try:
        conn.execute("delete from message_fts")
    except sqlite3.DatabaseError:
        pass

    paths = paths_for(root)
    result = IndexResult()
    seen: dict[str, list[str]] = defaultdict(list)
    for kind, mailbox in discover_mailboxes(root):
        for record in iter_records(mailbox):
            rowid, message_id, malformed = _insert_record(
                conn=conn,
                root=paths.root,
                kind=kind,
                mailbox=record.mailbox,
                state=record.state,
                path=record.path,
            )
            result.indexed += 1
            if malformed:
                result.malformed += 1
            if message_id:
                seen[message_id].append(str(record.path.relative_to(paths.root)))

    index_session_summaries(conn)
    result.duplicates = {mid: files for mid, files in seen.items() if len(files) > 1}
    return result


def _insert_record(
    *,
    conn: sqlite3.Connection,
    root: Path,
    kind: str,
    mailbox: Path,
    state: str,
    path: Path,
) -> tuple[int, str | None, bool]:
    relative_mailbox = str(mailbox.relative_to(root))
    relative_file = str(path.relative_to(root))
    errors: list[str] = []
    try:
        parsed = parse_envelope(path)
        errors.extend(validate_required(parsed))
        msg = parsed.message
        body_text = parsed.body_text
        body_sha = parsed.body_sha256
    except Exception as exc:
        msg = None
        body_text = ""
        body_sha = None
        errors.append(f"parse error: {exc}")

    get = msg.get if msg is not None else lambda _name, _default=None: None
    malformed = bool(errors)
    message_id = get("Message-ID")
    event_type = get("X-AgentDir-Event-Type")
    subject = get("Subject")
    from_actor = get("From")
    to_actor = get("To")
    session_id = get("X-AgentDir-Session")
    task_id = get("X-AgentDir-Task")
    date_header = get("Date")
    date_utc = _date_to_utc_iso(date_header)
    git_head = get("X-AgentDir-Git-Head")
    workspace = get("X-AgentDir-Workspace")
    tool = get("X-AgentDir-Tool")
    tool_exit_code = _int_or_none(get("X-AgentDir-Tool-Exit-Code"))
    indexed_at = now_iso()
    cursor = conn.execute(
        """
        insert into messages(
          message_id, mailbox_kind, mailbox_path, file_path, state, event_type,
          subject, from_actor, to_actor, session_id, task_id, parent_message_id,
          date_header, date_utc, created_ns, git_head, workspace, tool,
          tool_exit_code, body_sha256, body_text, indexed_at,
          malformed, errors
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            kind,
            relative_mailbox,
            relative_file,
            state,
            event_type,
            subject,
            from_actor,
            to_actor,
            session_id,
            task_id,
            get("In-Reply-To"),
            date_header,
            date_utc,
            _int_or_none(get("X-AgentDir-Created-Ns")),
            git_head,
            workspace,
            tool,
            tool_exit_code,
            body_sha,
            body_text,
            indexed_at,
            1 if malformed else 0,
            json.dumps(errors),
        ),
    )
    rowid = int(cursor.lastrowid)
    if msg is not None:
        for name, value in msg.items():
            conn.execute(
                "insert into headers(message_rowid, name, value) values (?, ?, ?)",
                (rowid, name, value),
            )
        for sha in msg.get_all("X-AgentDir-Blob-SHA256", []):
            conn.execute(
                "insert into message_artifacts(message_rowid, sha256) values (?, ?)",
                (rowid, sha),
            )
    try:
        conn.execute(
            "insert into message_fts(rowid, message_id, subject, body_text) values (?, ?, ?, ?)",
            (rowid, message_id, subject, body_text),
        )
    except sqlite3.DatabaseError:
        pass
    index_memory_document(
        conn,
        message_rowid=rowid,
        message_id=message_id,
        session_id=session_id,
        event_type=event_type,
        subject=subject,
        from_actor=from_actor,
        to_actor=to_actor,
        task_id=task_id,
        tool=tool,
        tool_exit_code=tool_exit_code,
        workspace=workspace,
        git_head=git_head,
        date_header=date_header,
        date_utc=date_utc,
        file_path=relative_file,
        body_text=body_text,
        indexed_at=indexed_at,
    )
    return rowid, message_id, malformed


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _date_to_utc_iso(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()
