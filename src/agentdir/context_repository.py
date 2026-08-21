from __future__ import annotations

import hashlib
import json
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .artifacts import artifact_path
from .envelope import parse_envelope
from .index import connect_index, update_index
from .query import query_messages
from .store import AgentDirError, paths_for, validate_id

CONTEXT_PROTOCOL = "agentdir.context-pack.v1"
CONTEXT_BRIEFING_PROTOCOL = "agentdir.context-briefing.v1"
CONTEXT_ENFORCEMENT_MODE = "advisory"

EVENT_CONTEXT_PACK_CREATED = "context.pack.created"
EVENT_CONTEXT_PACK_CONSUMED = "context.pack.consumed"
EVENT_CONTEXT_PACK_REVIEWED = "context.pack.reviewed"
EVENT_CONTEXT_SOURCES_CITED = "context.sources.cited"
EVENT_CONTEXT_SOURCES_EXPANDED = "context.sources.expanded"

HEADER_PROTOCOL = "X-AgentDir-Protocol"
HEADER_PACK_ID = "X-AgentDir-Pack-Id"
HEADER_CONTEXT_QUERY = "X-AgentDir-Context-Query"
HEADER_CONTEXT_SCOPE = "X-AgentDir-Context-Scope"
HEADER_SOURCE_ID = "X-AgentDir-Source-Id"
HEADER_REVIEWED_SOURCE_ID = "X-AgentDir-Reviewed-Source-Id"
HEADER_USED_SOURCE_ID = "X-AgentDir-Used-Source-Id"
HEADER_DISMISSED_SOURCE_ID = "X-AgentDir-Dismissed-Source-Id"
HEADER_CONTEXT_DISPOSITION = "X-AgentDir-Context-Disposition"
HEADER_CONTEXT_DECISION_ID = "X-AgentDir-Context-Decision-Id"
HEADER_CONTEXT_DECISION_REVISION = "X-AgentDir-Context-Decision-Revision"
HEADER_CONSUMPTION_PURPOSE = "X-AgentDir-Consumption-Purpose"
HEADER_ENFORCEMENT_MODE = "X-AgentDir-Enforcement-Mode"

CONSUMPTION_PURPOSES = ("plan", "tool", "answer", "handoff")
CONTEXT_DISPOSITIONS = ("used", "no_relevant", "skipped")


def read_context_manifest(
    root: str | Path,
    pack_id: str,
    *,
    rebuild: bool = True,
) -> dict[str, Any]:
    """Load and validate the immutable manifest for exactly one context pack."""
    creation_events = context_events(
        root,
        pack_id,
        event_type=EVENT_CONTEXT_PACK_CREATED,
        rebuild=rebuild,
    )
    if not creation_events:
        raise AgentDirError(f"Unknown context pack: {pack_id}")
    if len(creation_events) != 1:
        raise AgentDirError(f"Context pack has multiple creation events: {pack_id}")
    event = creation_events[0]
    identity_error = context_pack_identity_error(
        event.get("pack_ids") or [],
        context_pack_body_ids(event.get("body_text") or ""),
        expected_pack_id=pack_id,
    )
    if identity_error:
        raise AgentDirError(f"Context pack creation identity is invalid: {identity_error}")
    sha = event["headers"].get("X-AgentDir-Blob-SHA256")
    if not sha:
        raise AgentDirError(f"Context pack has no manifest artifact: {pack_id}")
    manifest_file = artifact_path(root, sha)
    if not manifest_file.is_file():
        raise AgentDirError(f"Context pack manifest artifact is missing: {sha}")
    try:
        manifest_bytes = manifest_file.read_bytes()
    except OSError as exc:
        raise AgentDirError(f"Context pack manifest artifact is unreadable: {sha}") from exc
    actual_sha = hashlib.sha256(manifest_bytes).hexdigest()
    if actual_sha != sha:
        raise AgentDirError(
            f"Context pack manifest artifact digest does not match its address: {sha}"
        )
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise AgentDirError(f"Context pack manifest artifact is not UTF-8: {sha}") from exc
    except json.JSONDecodeError as exc:
        raise AgentDirError(f"Context pack manifest artifact is invalid JSON: {sha}") from exc
    if not isinstance(manifest, dict):
        raise AgentDirError(f"Context pack manifest must be a JSON object: {sha}")
    if manifest.get("protocol") != CONTEXT_PROTOCOL:
        raise AgentDirError(f"Unsupported context pack protocol: {manifest.get('protocol')}")
    if manifest.get("pack_id") != pack_id:
        raise AgentDirError(f"Context pack manifest id does not match {pack_id}")
    manifest_session_id = manifest.get("session_id")
    event_session_id = event.get("session_id")
    if not isinstance(manifest_session_id, str) or not manifest_session_id:
        raise AgentDirError(f"Context pack manifest has no valid session id: {pack_id}")
    if not isinstance(event_session_id, str) or manifest_session_id != event_session_id:
        raise AgentDirError(
            f"Context pack manifest session does not match its creation event: {pack_id}"
        )
    sources = manifest.get("sources")
    if not isinstance(sources, list):
        raise AgentDirError(f"Context pack manifest sources must be a list: {pack_id}")
    source_ids: list[str] = []
    for index, source in enumerate(sources):
        if not isinstance(source, dict) or not isinstance(source.get("source_id"), str):
            raise AgentDirError(
                f"Context pack manifest source {index + 1} has no valid source id: {pack_id}"
            )
        source_ids.append(source["source_id"])
    if len(source_ids) != len(set(source_ids)):
        raise AgentDirError(f"Context pack manifest contains duplicate source ids: {pack_id}")
    briefing = manifest.get("briefing")
    if briefing is not None:
        _validate_briefing(pack_id, briefing, source_ids)
    return manifest


def context_events(
    root: str | Path,
    pack_id: str,
    *,
    event_type: str | None = None,
    rebuild: bool = True,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return the ordered live and archived events that claim one pack."""
    if rebuild:
        update_index(root)
    if event_type == EVENT_CONTEXT_PACK_CREATED:
        sql = """
            select m.*
            from messages m
            where m.event_type = ?
            order by coalesce(m.date_utc, m.indexed_at), coalesce(m.created_ns, 0), m.file_path, m.id
        """
        params: list[Any] = [EVENT_CONTEXT_PACK_CREATED]
    else:
        clauses = ["hp.name = ?", "hp.value = ?", "m.event_type != ?"]
        params = [HEADER_PACK_ID, pack_id, EVENT_CONTEXT_SOURCES_EXPANDED]
        if event_type:
            clauses.append("m.event_type = ?")
            params.append(event_type)
        sql = f"""
            select distinct m.*
            from messages m
            join headers hp on hp.message_rowid = m.id
            where {' and '.join(clauses)}
            order by coalesce(m.date_utc, m.indexed_at), coalesce(m.created_ns, 0), m.file_path, m.id
        """
    with connect_index(root) as conn:
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
        for row in rows:
            headers = conn.execute(
                "select name, value from headers where message_rowid = ? order by rowid",
                (row["id"],),
            ).fetchall()
            _hydrate_context_event(row, headers)
    if event_type == EVENT_CONTEXT_PACK_CREATED:
        rows = [
            row
            for row in rows
            if pack_id in set(row["pack_ids"])
            or pack_id in set(context_pack_body_ids(row.get("body_text") or ""))
        ]
        archive_sessions = {
            str(row.get("session_id"))
            for row in rows
            if row.get("session_id")
        }
        if archive_sessions:
            for archived_session_id in sorted(archive_sessions):
                rows.extend(
                    _archived_context_events(
                        root,
                        pack_id,
                        event_type=event_type,
                        session_id=archived_session_id,
                    )
                )
        else:
            rows.extend(
                _archived_context_events(
                    root,
                    pack_id,
                    event_type=event_type,
                    session_id=None,
                )
            )
    else:
        rows.extend(
            _archived_context_events(
                root,
                pack_id,
                event_type=event_type,
                session_id=session_id,
            )
        )
    rows.sort(key=_context_event_order)
    return rows


def list_context_packs(
    root: str | Path,
    session_id: str | None = None,
    *,
    fallback_any: bool = False,
    rebuild: bool = True,
) -> list[dict[str, Any]]:
    """Discover creation records and fail-closed action claims for a session."""
    if rebuild:
        update_index(root)
    rows = query_messages(
        root,
        session_id=session_id,
        event_type=EVENT_CONTEXT_PACK_CREATED,
        limit=10_000,
    )
    if not rows and session_id and fallback_any:
        rows = query_messages(root, event_type=EVENT_CONTEXT_PACK_CREATED, limit=10_000)
    packs: list[dict[str, Any]] = []
    for row in rows:
        event_path = _event_path(root, row)
        header_ids: list[str] = []
        body_ids: list[str] = []
        identity_error: str | None = None
        try:
            parsed = parse_envelope(event_path)
            header_ids = parsed.headers(HEADER_PACK_ID)
            body_ids = context_pack_body_ids(parsed.body_text)
            identity_error = context_pack_identity_error(header_ids, body_ids)
        except (AgentDirError, OSError) as exc:
            identity_error = f"context pack creation event cannot be read: {exc}"
        pack_id = (
            header_ids[0]
            if header_ids
            else body_ids[0]
            if body_ids
            else f"event-{row.get('id') or 'unknown'}"
        )
        pack = _pack_row(row, pack_id)
        if identity_error:
            pack["identity_error"] = identity_error
        packs.append(pack)

    counts: dict[str, int] = {}
    for pack in packs:
        counts[pack["pack_id"]] = counts.get(pack["pack_id"], 0) + 1
    for pack in packs:
        if counts[pack["pack_id"]] > 1:
            duplicate_error = f"context pack has multiple creation events: {pack['pack_id']}"
            existing = pack.get("identity_error")
            pack["identity_error"] = (
                f"{existing}; {duplicate_error}" if existing else duplicate_error
            )

    if session_id:
        _append_orphan_action_claims(root, session_id, packs)
    return packs


def context_pack_body_ids(body: str) -> list[str]:
    return [
        line.split("=", 1)[1].strip()
        for line in body.splitlines()
        if line.startswith("pack_id=") and line.split("=", 1)[1].strip()
    ]


def context_pack_identity_error(
    header_pack_ids: list[str],
    body_pack_ids: list[str],
    *,
    expected_pack_id: str | None = None,
) -> str | None:
    errors: list[str] = []
    if len(header_pack_ids) != 1:
        errors.append(
            "expected exactly one X-AgentDir-Pack-Id header, "
            f"found {len(header_pack_ids)}"
        )
    if len(body_pack_ids) != 1:
        errors.append(f"expected exactly one pack_id body field, found {len(body_pack_ids)}")
    if len(header_pack_ids) == 1 and len(body_pack_ids) == 1:
        if header_pack_ids[0] != body_pack_ids[0]:
            errors.append(
                f"header pack id {header_pack_ids[0]} does not match body pack id {body_pack_ids[0]}"
            )
    if expected_pack_id:
        if len(header_pack_ids) == 1 and header_pack_ids[0] != expected_pack_id:
            errors.append(f"header pack id does not match requested pack {expected_pack_id}")
        if len(body_pack_ids) == 1 and body_pack_ids[0] != expected_pack_id:
            errors.append(f"body pack id does not match requested pack {expected_pack_id}")
    return "; ".join(errors) if errors else None


def _validate_briefing(pack_id: str, briefing: Any, source_ids: list[str]) -> None:
    if not isinstance(briefing, dict):
        raise AgentDirError(f"Context pack manifest briefing must be an object: {pack_id}")
    if briefing.get("protocol") != CONTEXT_BRIEFING_PROTOCOL:
        raise AgentDirError(f"Unsupported context briefing protocol: {briefing.get('protocol')}")
    briefing_ids = briefing.get("source_ids")
    if not isinstance(briefing_ids, list) or not all(
        isinstance(source_id, str) for source_id in briefing_ids
    ):
        raise AgentDirError(
            f"Context pack manifest briefing source ids must be a list: {pack_id}"
        )
    if len(briefing_ids) != len(set(briefing_ids)):
        raise AgentDirError(
            f"Context pack manifest briefing contains duplicate source ids: {pack_id}"
        )
    unknown = [source_id for source_id in briefing_ids if source_id not in set(source_ids)]
    if unknown:
        raise AgentDirError(
            "Context pack manifest briefing references unknown sources: " + ", ".join(unknown)
        )
    if bool(briefing_ids) != bool(source_ids):
        raise AgentDirError(
            f"Context pack manifest briefing does not present its available sources: {pack_id}"
        )
    review_required = briefing.get("review_required")
    if not isinstance(review_required, bool) or review_required != bool(briefing_ids):
        raise AgentDirError(
            f"Context pack manifest briefing review requirement is inconsistent: {pack_id}"
        )
    presented_count = briefing.get("presented_count")
    omitted_count = briefing.get("omitted_count")
    if (
        not isinstance(presented_count, int)
        or isinstance(presented_count, bool)
        or presented_count != len(briefing_ids)
    ):
        raise AgentDirError(
            f"Context pack manifest briefing presented count is inconsistent: {pack_id}"
        )
    if (
        not isinstance(omitted_count, int)
        or isinstance(omitted_count, bool)
        or omitted_count != len(source_ids) - len(briefing_ids)
    ):
        raise AgentDirError(
            f"Context pack manifest briefing omitted count is inconsistent: {pack_id}"
        )


def _append_orphan_action_claims(
    root: str | Path,
    session_id: str,
    packs: list[dict[str, Any]],
) -> None:
    created_in_session = {
        pack["pack_id"] for pack in packs if pack.get("session_id") == session_id
    }
    orphan_claims: set[str] = set()
    for event_type in (
        EVENT_CONTEXT_PACK_CONSUMED,
        EVENT_CONTEXT_PACK_REVIEWED,
        EVENT_CONTEXT_SOURCES_CITED,
    ):
        action_rows = query_messages(
            root,
            session_id=session_id,
            event_type=event_type,
            limit=10_000,
        )
        for row in action_rows:
            event_path = _event_path(root, row)
            try:
                header_ids = parse_envelope(event_path).headers(HEADER_PACK_ID)
            except (AgentDirError, OSError) as exc:
                header_ids = []
                identity_error = f"context action event cannot be read: {exc}"
            else:
                identity_error = (
                    "context action event must have exactly one "
                    f"X-AgentDir-Pack-Id header; found {len(header_ids)}"
                    if len(header_ids) != 1
                    else None
                )
            claimed_pack_id = (
                header_ids[0]
                if len(header_ids) == 1
                else f"event-{row.get('id') or 'unknown'}"
            )
            if identity_error:
                pack = _pack_row(row, claimed_pack_id)
                pack["identity_error"] = identity_error
                packs.append(pack)
                continue
            if claimed_pack_id in created_in_session or claimed_pack_id in orphan_claims:
                continue
            try:
                claimed_manifest = read_context_manifest(root, claimed_pack_id, rebuild=False)
            except AgentDirError:
                claimed_manifest = None
            if claimed_manifest is not None and "briefing" not in claimed_manifest:
                continue
            orphan_claims.add(claimed_pack_id)
            pack = _pack_row(row, claimed_pack_id)
            pack["identity_error"] = (
                f"context action in session {session_id} references pack "
                f"{claimed_pack_id} without a creation event in that session"
            )
            packs.append(pack)


def _event_path(root: str | Path, row: dict[str, Any]) -> Path:
    event_path = Path(str(row.get("file_path") or ""))
    if not event_path.is_absolute():
        event_path = paths_for(root).root / event_path
    return event_path


def _pack_row(row: dict[str, Any], pack_id: str) -> dict[str, Any]:
    return {
        "pack_id": pack_id,
        "session_id": row.get("session_id"),
        "event_path": row.get("file_path"),
        "subject": row.get("subject"),
        "date_utc": row.get("date_utc"),
    }


def _hydrate_context_event(row: dict[str, Any], headers: list[Any]) -> None:
    row["headers"] = _header_map(headers)
    row["pack_ids"] = [
        header["value"] for header in headers if header["name"] == HEADER_PACK_ID
    ]
    row["source_ids"] = [
        header["value"] for header in headers if header["name"] == HEADER_SOURCE_ID
    ]
    row["reviewed_source_ids"] = [
        header["value"] for header in headers if header["name"] == HEADER_REVIEWED_SOURCE_ID
    ]
    explicit_used = [
        header["value"] for header in headers if header["name"] == HEADER_USED_SOURCE_ID
    ]
    row["used_source_ids"] = (
        explicit_used
        if explicit_used
        else row["source_ids"] if row["event_type"] == EVENT_CONTEXT_PACK_CONSUMED else []
    )
    row["dismissed_source_ids"] = [
        header["value"] for header in headers if header["name"] == HEADER_DISMISSED_SOURCE_ID
    ]


def _archived_context_events(
    root: str | Path,
    pack_id: str,
    *,
    event_type: str | None,
    session_id: str | None,
) -> list[dict[str, Any]]:
    paths = paths_for(root)
    rows: list[dict[str, Any]] = []
    if session_id:
        try:
            validate_id(session_id, "session id")
        except AgentDirError:
            return []
        mailboxes = [paths.archives / "sessions" / session_id / "Maildir"]
    else:
        mailboxes = sorted((paths.archives / "sessions").glob("*/Maildir"))
    for mailbox in mailboxes:
        for state in ("new", "cur"):
            directory = mailbox / state
            if not directory.is_dir():
                continue
            for path in sorted(directory.iterdir()):
                if not path.is_file() or path.name.startswith("."):
                    continue
                try:
                    contained = path.resolve(strict=True)
                    contained.relative_to(paths.root.resolve())
                except (OSError, ValueError):
                    continue
                try:
                    parsed = parse_envelope(contained)
                except (AgentDirError, OSError):
                    continue
                parsed_event_type = parsed.header("X-AgentDir-Event-Type")
                if event_type and parsed_event_type != event_type:
                    continue
                if not event_type and parsed_event_type not in {
                    EVENT_CONTEXT_PACK_CREATED,
                    EVENT_CONTEXT_PACK_CONSUMED,
                    EVENT_CONTEXT_PACK_REVIEWED,
                    EVENT_CONTEXT_SOURCES_CITED,
                }:
                    continue
                pack_ids = parsed.headers(HEADER_PACK_ID)
                body_ids = context_pack_body_ids(parsed.body_text)
                if pack_id not in set([*pack_ids, *body_ids]):
                    continue
                row = {
                    "id": 0,
                    "message_id": parsed.message_id,
                    "event_type": parsed_event_type,
                    "session_id": parsed.header("X-AgentDir-Session"),
                    "date_utc": _context_date_utc(parsed.header("Date")),
                    "created_ns": _context_int(parsed.header("X-AgentDir-Created-Ns")),
                    "file_path": str(contained.relative_to(paths.root.resolve())),
                    "body_text": parsed.body_text,
                    "indexed_at": None,
                }
                headers = [
                    {"name": name, "value": " ".join(str(value).split())}
                    for name, value in parsed.message.items()
                ]
                _hydrate_context_event(row, headers)
                rows.append(row)
    return rows


def _context_date_utc(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    except (TypeError, ValueError):
        return value


def _context_int(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _context_event_order(row: dict[str, Any]) -> tuple[str, int, str, int]:
    return (
        str(row.get("date_utc") or row.get("indexed_at") or ""),
        int(row.get("created_ns") or 0),
        str(row.get("file_path") or ""),
        int(row.get("id") or 0),
    )


def _header_map(rows: list[Any]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for row in rows:
        name = row["name"]
        value = row["value"]
        if name not in mapped:
            mapped[name] = value
    return mapped
