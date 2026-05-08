from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from .envelope import parse_envelope, validate_required
from .mailbox import iter_records
from .memory import index_memory_document, memory_schema_sql
from .store import AgentDirError, discover_mailboxes, paths_for, require_root

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
    conn.execute("insert or replace into metadata(key, value) values('schema_version', '2')")
    conn.execute("insert or replace into metadata(key, value) values('vector_memory', 'yes')")
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


def rebuild_index(root: str | Path) -> IndexResult:
    paths = require_root(root)
    paths.indexes.mkdir(parents=True, exist_ok=True)
    temp_path = paths.index_path.with_suffix(".sqlite3.tmp")
    if temp_path.exists():
        temp_path.unlink()
    conn = sqlite3.connect(temp_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("pragma foreign_keys = on")
        initialize_schema(conn)
        result = _index_into(conn, root)
        conn.commit()
    finally:
        conn.close()
    os.replace(temp_path, paths.index_path)
    return result


def update_index(root: str | Path) -> IndexResult:
    return rebuild_index(root)


def _index_into(conn: sqlite3.Connection, root: str | Path) -> IndexResult:
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
    session_id = get("X-AgentDir-Session")
    git_head = get("X-AgentDir-Git-Head")
    workspace = get("X-AgentDir-Workspace")
    tool = get("X-AgentDir-Tool")
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
            get("From"),
            get("To"),
            session_id,
            get("X-AgentDir-Task"),
            get("In-Reply-To"),
            get("Date"),
            _date_to_utc_iso(get("Date")),
            _int_or_none(get("X-AgentDir-Created-Ns")),
            git_head,
            workspace,
            tool,
            _int_or_none(get("X-AgentDir-Tool-Exit-Code")),
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
        tool=tool,
        workspace=workspace,
        git_head=git_head,
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
