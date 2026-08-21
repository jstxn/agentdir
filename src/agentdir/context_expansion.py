from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol

from .context_repository import (
    EVENT_CONTEXT_SOURCES_EXPANDED,
    context_events,
    read_context_manifest,
)
from .context_review import fold_context_review
from .context_selection import brief_context_manifest
from .envelope import ParsedEnvelope, parse_envelope, validate_required
from .events import emit_event
from .federation import (
    list_registered_roots,
    resolve_registered_root,
    root_id_for_path,
)
from .index import connect_index, update_index
from .locking import lifecycle_lock
from .mailbox import iter_records
from .memory import build_memory_text, session_memory_summary
from .query import query_messages
from .redaction import redact_text
from .sessions import read_current_session, session_pointer_lock
from .store import AgentDirError, AgentDirStateError, paths_for, require_root, validate_id

CONTEXT_EXPANSION_PROTOCOL = "agentdir.context-view.v1"
CONTEXT_EXPANSION_REPRESENTATION = "redacted-canonical-body.v1"
DEFAULT_CONTEXT_EXPANSION_PAGE_BYTES = 4096

HEADER_VIEW_PACK_ID = "X-AgentDir-Context-View-Pack-Id"
HEADER_VIEW_ID = "X-AgentDir-Context-View-Id"
HEADER_VIEW_SOURCE_ID = "X-AgentDir-Context-View-Source-Id"
HEADER_VIEW_SOURCE_REF = "X-AgentDir-Context-View-Source-Ref"
HEADER_VIEW_INTEGRITY = "X-AgentDir-Context-View-Integrity"
HEADER_VIEW_EXTENT = "X-AgentDir-Context-View-Extent"
HEADER_VIEW_REPRESENTATION = "X-AgentDir-Context-View-Representation"
HEADER_VIEW_PAGE = "X-AgentDir-Context-View-Page"
HEADER_VIEW_PAGE_COUNT = "X-AgentDir-Context-View-Page-Count"
HEADER_VIEW_BYTE_START = "X-AgentDir-Context-View-Byte-Start"
HEADER_VIEW_BYTE_END = "X-AgentDir-Context-View-Byte-End"
HEADER_VIEW_SOURCE_BYTES = "X-AgentDir-Context-View-Source-Bytes"
HEADER_VIEW_PAGE_BYTES = "X-AgentDir-Context-View-Page-Bytes"
HEADER_VIEW_SOURCE_SHA256 = "X-AgentDir-Context-View-Source-SHA256"
HEADER_VIEW_REPRESENTATION_SHA256 = "X-AgentDir-Context-View-Representation-SHA256"
HEADER_VIEW_EXPECTED_BODY_SHA256 = "X-AgentDir-Context-View-Expected-Body-SHA256"
HEADER_VIEW_EXPECTED_TEXT_SHA256 = "X-AgentDir-Context-View-Expected-Text-SHA256"
HEADER_VIEW_REDACTIONS = "X-AgentDir-Context-View-Redactions"
HEADER_VIEW_DECISION_PHASE = "X-AgentDir-Context-View-Decision-Phase"
HEADER_VIEW_DECISION_ID = "X-AgentDir-Context-View-Decision-Id"

INTEGRITY_VERIFIED = "verified"
INTEGRITY_LEGACY_UNVERIFIED = "legacy_unverified"
INTEGRITY_CHANGED = "changed"
INTEGRITY_UNAVAILABLE = "unavailable"
CANONICAL_INTEGRITY_STATES = {INTEGRITY_VERIFIED, INTEGRITY_LEGACY_UNVERIFIED}
CANONICAL_EXTENTS = {"full", "bounded"}


class ExpansionDelivery(Protocol):
    def accept(self, result: dict[str, Any]) -> None: ...

    def complete(self, result: dict[str, Any]) -> None: ...


def expand_context_source(
    root: str | Path,
    *,
    pack_id: str,
    source_selector: str,
    page: int = 1,
    actor: str = "agent",
    page_bytes: int = DEFAULT_CONTEXT_EXPANSION_PAGE_BYTES,
    delivery: ExpansionDelivery | None = None,
) -> dict[str, Any]:
    """Resolve, verify, redact, page, and optionally receipt one displayed source."""
    if page < 1:
        raise AgentDirError("Context expansion page must be at least 1")
    if page_bytes < 4:
        raise AgentDirError("Context expansion page size must be at least 4 bytes")

    paths = require_root(root)
    update_index(paths.root)
    initial_manifest = read_context_manifest(paths.root, pack_id, rebuild=False)
    session_id = str(initial_manifest["session_id"])

    # Active-session ownership and receipt creation are one transaction. The
    # same locks also make an expansion either wholly before or wholly after a
    # terminal context decision and session end.
    with session_pointer_lock(paths.root):
        with lifecycle_lock(paths.root, f"session:{session_id}"):
            with lifecycle_lock(paths.root, f"pack:{pack_id}"):
                update_index(paths.root)
                manifest = read_context_manifest(paths.root, pack_id, rebuild=False)
                source, source_ref = _displayed_source(manifest, source_selector)
                resolution = _resolve_source(paths.root, manifest, source)
                result = _delivery_result(
                    manifest,
                    source,
                    source_ref=source_ref,
                    resolution=resolution,
                    page=page,
                    page_bytes=page_bytes,
                )

                audit = fold_context_review(
                    manifest,
                    context_events(
                        paths.root,
                        pack_id,
                        rebuild=False,
                        session_id=session_id,
                    ),
                )
                result["decision"] = {
                    "phase": (
                        "after_decision"
                        if audit.get("decision_event_count")
                        else "before_decision"
                    ),
                    "review_status": audit.get("review_status"),
                    "decision": audit.get("decision"),
                    "decision_id": audit.get("decision_id"),
                }
                result["next_actions"] = _next_actions(result, audit)
                if delivery is not None:
                    delivery.accept(result)
                try:
                    result["receipt"] = _receipt_expansion(
                        paths.root,
                        manifest,
                        result,
                        audit=audit,
                        actor=actor,
                    )
                except (AgentDirError, OSError) as exc:
                    result["receipt"] = {
                        "status": "failed",
                        "recorded": False,
                        "reason": "receipt_write_failed",
                        "view_id": None,
                        "event_path": None,
                        "error": str(exc),
                    }
                    result["warnings"].append(
                        f"context expansion was delivered but its optional receipt failed: {exc}"
                    )
                if delivery is not None:
                    delivery.complete(result)
                return result


def audit_context_expansion(
    root: str | Path,
    manifest: dict[str, Any],
    context_events: list[dict[str, Any]],
    used_source_ids: list[str],
) -> dict[str, Any]:
    """Fold optional expansion receipts without changing review validity."""
    events = _expansion_events(
        root,
        str(manifest["pack_id"]),
        session_id=str(manifest.get("session_id") or ""),
    )
    briefing = brief_context_manifest(manifest)
    displayed = briefing.get("sources") or []
    source_order = [str(source["source_id"]) for source in displayed]
    ref_by_id = {str(source["source_id"]): str(source["ref"]) for source in displayed}
    validation_errors: list[str] = []
    validated: list[dict[str, Any]] = []

    source_by_id = {
        str(source["source_id"]): source
        for source in manifest.get("sources") or []
        if source.get("source_id")
    }
    for event in events:
        payload, errors = _validate_receipt(
            root,
            event,
            manifest,
            ref_by_id,
            source_by_id,
        )
        validation_errors.extend(errors)
        if payload is not None and not errors:
            validated.append({"event": event, "payload": payload})

    by_view_id: dict[str, list[dict[str, Any]]] = {}
    for item in validated:
        by_view_id.setdefault(item["payload"]["view_id"], []).append(item)
    unique_validated: list[dict[str, Any]] = []
    for view_id, items in by_view_id.items():
        if len(items) > 1:
            signatures = {_receipt_signature(item["payload"]) for item in items}
            if len(signatures) == 1:
                validation_errors.append(f"duplicate context expansion receipt: {view_id}")
            else:
                validation_errors.append(f"conflicting context expansion receipt id: {view_id}")
            continue
        unique_validated.append(items[0])

    terminal_events = [
        event
        for event in context_events
        if (event.get("headers") or {}).get("X-AgentDir-Context-Disposition")
    ]
    chronology_invalid: set[tuple[str, str]] = set()
    for item in unique_validated:
        event = item["event"]
        payload = item["payload"]
        preceding = [
            candidate
            for candidate in terminal_events
            if _event_order(candidate) < _event_order(event)
        ]
        phase = payload["decision_phase"]
        if phase == "before_decision" and preceding:
            validation_errors.append(
                f"{_event_label(event)}: before-decision receipt follows a terminal decision"
            )
            chronology_invalid.add(_event_identity(event))
        if phase == "after_decision" and not preceding and "briefing" in manifest:
            validation_errors.append(
                f"{_event_label(event)}: after-decision receipt has no preceding terminal decision"
            )
            chronology_invalid.add(_event_identity(event))
        decision_id = payload.get("decision_id")
        if preceding:
            expected_decision_id = (preceding[-1].get("headers") or {}).get(
                "X-AgentDir-Context-Decision-Id"
            )
            if decision_id != expected_decision_id:
                validation_errors.append(
                    f"{_event_label(event)}: receipt decision id does not match chronology"
                )
                chronology_invalid.add(_event_identity(event))
        elif decision_id:
            validation_errors.append(
                f"{_event_label(event)}: before-decision receipt unexpectedly names a decision"
            )
            chronology_invalid.add(_event_identity(event))

    # Chronology errors invalidate the affected event as a trust signal. Keep
    # the raw count visible, but count only events that remain fully valid.
    if chronology_invalid:
        unique_validated = [
            item
            for item in unique_validated
            if _event_identity(item["event"]) not in chronology_invalid
        ]

    expanded_ids = _ordered_unique(
        [item["payload"]["source_id"] for item in unique_validated],
        source_order,
    )
    before_ids = _ordered_unique(
        [
            item["payload"]["source_id"]
            for item in unique_validated
            if item["payload"]["decision_phase"] == "before_decision"
        ],
        source_order,
    )
    after_ids = _ordered_unique(
        [
            item["payload"]["source_id"]
            for item in unique_validated
            if item["payload"]["decision_phase"] == "after_decision"
        ],
        source_order,
    )
    used_without_prior: list[str] = []
    for source_id in used_source_ids:
        use_events = [
            event
            for event in context_events
            if source_id in (event.get("used_source_ids") or [])
        ]
        first_use = min((_event_order(event) for event in use_events), default=None)
        prior_receipt = any(
            item["payload"]["source_id"] == source_id
            and (first_use is None or _event_order(item["event"]) < first_use)
            for item in unique_validated
        )
        if not prior_receipt:
            used_without_prior.append(source_id)

    return {
        "protocol": CONTEXT_EXPANSION_PROTOCOL,
        "expanded_source_count": len(expanded_ids),
        "expanded_source_ids": expanded_ids,
        "expanded_before_decision_count": len(before_ids),
        "expanded_before_decision_source_ids": before_ids,
        "expanded_after_decision_count": len(after_ids),
        "expanded_after_decision_source_ids": after_ids,
        "used_without_prior_expansion_count": len(used_without_prior),
        "used_without_prior_expansion_source_ids": used_without_prior,
        "receipt_event_count": len(events),
        "valid_receipt_count": len(unique_validated),
        "invalid_receipt_count": max(0, len(events) - len(unique_validated)),
        "receipts_valid": not validation_errors,
        "validation_errors": validation_errors,
    }


def empty_context_expansion_audit() -> dict[str, Any]:
    return {
        "protocol": CONTEXT_EXPANSION_PROTOCOL,
        "expanded_source_count": 0,
        "expanded_source_ids": [],
        "expanded_before_decision_count": 0,
        "expanded_before_decision_source_ids": [],
        "expanded_after_decision_count": 0,
        "expanded_after_decision_source_ids": [],
        "used_without_prior_expansion_count": 0,
        "used_without_prior_expansion_source_ids": [],
        "receipt_event_count": 0,
        "valid_receipt_count": 0,
        "invalid_receipt_count": 0,
        "receipts_valid": True,
        "validation_errors": [],
    }


def audit_context_expansion_inventory(
    root: str | Path,
    session_id: str,
) -> dict[str, Any]:
    """Find optional receipts that cannot be attributed to a readable owning pack."""
    events = _session_expansion_events(root, session_id)
    errors: list[str] = []
    claimable = 0
    manifest_cache: dict[str, dict[str, Any] | AgentDirError] = {}
    for event in events:
        routing_valid = True
        if event.get("event_type") != EVENT_CONTEXT_SOURCES_EXPANDED:
            errors.append(f"{_event_label(event)}: receipt event type is not canonical")
            routing_valid = False
        if event.get("session_id") != session_id:
            errors.append(f"{_event_label(event)}: receipt session header is not canonical")
            routing_valid = False
        if event.get("malformed"):
            errors.append(f"{_event_label(event)}: receipt envelope is malformed")
            routing_valid = False
        pack_values = _header_values(event).get(HEADER_VIEW_PACK_ID) or []
        if len(pack_values) != 1:
            errors.append(
                f"{_event_label(event)}: expected exactly one {HEADER_VIEW_PACK_ID} header"
            )
            continue
        pack_id = pack_values[0]
        if pack_id not in manifest_cache:
            try:
                manifest_cache[pack_id] = read_context_manifest(root, pack_id, rebuild=False)
            except AgentDirError as exc:
                manifest_cache[pack_id] = exc
        manifest = manifest_cache[pack_id]
        if isinstance(manifest, AgentDirError):
            errors.append(f"{_event_label(event)}: receipt claims unreadable pack {pack_id}")
            continue
        if str(manifest.get("session_id") or "") != session_id:
            errors.append(
                f"{_event_label(event)}: receipt pack belongs to a different session"
            )
            continue
        if routing_valid:
            claimable += 1
    return {
        "event_count": len(events),
        "claimable_event_count": claimable,
        "orphan_event_count": len(events) - claimable,
        "receipts_valid": not errors,
        "validation_errors": errors,
    }


def _displayed_source(
    manifest: dict[str, Any],
    selector: str,
) -> tuple[dict[str, Any], str]:
    displayed = brief_context_manifest(manifest).get("sources") or []
    normalized = selector.strip()
    ordinal = normalized[1:] if normalized.lower().startswith("s") else normalized
    if ordinal.isdigit():
        index = int(ordinal) - 1
        if index < 0 or index >= len(displayed):
            raise AgentDirError(f"Unknown context source reference: {selector}")
        selected = displayed[index]
    else:
        matches = [source for source in displayed if source.get("source_id") == normalized]
        if not matches:
            raise AgentDirError(
                "Context source was not displayed in this briefing: " + normalized
            )
        selected = matches[0]
    source_id = str(selected["source_id"])
    source = next(
        source
        for source in manifest.get("sources") or []
        if source.get("source_id") == source_id
    )
    return source, str(selected["ref"])


def _resolve_source(
    controller_root: Path,
    manifest: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, Any]:
    source_root_id = source.get("source_root_id")
    if source_root_id:
        try:
            roots = list_registered_roots(
                controller_root,
                group=manifest.get("federation_group"),
            )
        except AgentDirError as exc:
            return _fallback(source, INTEGRITY_UNAVAILABLE, f"federation scope unavailable: {exc}")
        matches = [root for root in roots if root.get("root_id") == source_root_id]
        if len(matches) != 1 or not matches[0].get("available"):
            return _fallback(source, INTEGRITY_UNAVAILABLE, "registered source root is unavailable")
        try:
            selected_root = resolve_registered_root(str(matches[0]["root_path"]))
        except AgentDirError as exc:
            return _fallback(source, INTEGRITY_UNAVAILABLE, f"registered source root is invalid: {exc}")
        if root_id_for_path(selected_root) != source_root_id:
            return _fallback(source, INTEGRITY_UNAVAILABLE, "registered source root identity changed")
        local_source = {
            **source,
            "source_id": source.get("source_id_original") or source.get("source_id"),
            "file_path": source.get("source_file_path") or source.get("file_path"),
            "source_root_id": None,
        }
        resolved = _resolve_local_source(selected_root, local_source)
        resolved["source_root"] = {
            "root_id": source_root_id,
            "name": matches[0].get("name"),
            "path": str(selected_root),
            "visibility": matches[0].get("visibility"),
        }
        return resolved
    resolved = _resolve_local_source(controller_root, source)
    resolved["source_root"] = None
    return resolved


def _resolve_local_source(root: Path, source: dict[str, Any]) -> dict[str, Any]:
    if source.get("source_kind") == "session_summary" or source.get("event_type") == "summary.compacted":
        return _resolve_session_summary(root, source)
    return _resolve_envelope_source(root, source)


def _resolve_envelope_source(root: Path, source: dict[str, Any]) -> dict[str, Any]:
    direct, direct_changed = _resolve_envelope_candidates(
        _candidate_envelopes(root, source, include_session_scan=False),
        source,
    )
    if direct is not None:
        return direct
    scanned, scanned_changed = _resolve_envelope_candidates(
        _candidate_envelopes(root, source, include_session_scan=True),
        source,
    )
    if scanned is not None:
        return scanned
    if direct_changed or scanned_changed:
        return _fallback(source, INTEGRITY_CHANGED, "retained source digest changed")
    return _fallback(source, INTEGRITY_UNAVAILABLE, "retained source envelope is unavailable")


def _resolve_envelope_candidates(
    candidates: list[Path],
    source: dict[str, Any],
) -> tuple[dict[str, Any] | None, bool]:
    metadata_matches: list[tuple[Path, ParsedEnvelope, str, str]] = []
    valid: list[tuple[Path, ParsedEnvelope, str, str]] = []
    expected_body = _string_or_none(source.get("body_sha256"))
    expected_text = _string_or_none(source.get("text_sha256"))
    for candidate in candidates:
        try:
            parsed = parse_envelope(candidate)
        except (AgentDirError, OSError):
            continue
        if not _matches_source_metadata(parsed, source):
            continue
        body_sha = parsed.body_sha256
        text_sha = _envelope_memory_sha(parsed, source)
        metadata_matches.append((candidate, parsed, body_sha, text_sha))
        if expected_body and body_sha != expected_body:
            continue
        if expected_text and text_sha != expected_text:
            continue
        valid.append((candidate, parsed, body_sha, text_sha))

    if not valid:
        return None, bool(metadata_matches and (expected_body or expected_text))

    body_digests = {item[2] for item in valid}
    if len(body_digests) != 1:
        return _fallback(source, INTEGRITY_UNAVAILABLE, "source identity is ambiguous"), False
    selected = sorted(valid, key=lambda item: str(item[0]))[0]
    integrity = (
        INTEGRITY_VERIFIED if expected_body or expected_text else INTEGRITY_LEGACY_UNVERIFIED
    )
    warnings: list[str] = []
    if len(valid) > 1:
        warnings.append("identical retained source replicas found")
    parsed = selected[1]
    return (
        {
            "body": parsed.body_text,
            "basis": "canonical_envelope",
            "integrity": integrity,
            "integrity_reason": None,
            "source_sha256": selected[2],
            "warnings": warnings,
            "capture_truncated": _truthy(parsed.header("X-AgentDir-Truncated")),
        },
        False,
    )


def _resolve_session_summary(root: Path, source: dict[str, Any]) -> dict[str, Any]:
    session_id = _string_or_none(source.get("session_id"))
    if not session_id:
        return _fallback(source, INTEGRITY_UNAVAILABLE, "summary has no session identity")
    try:
        validate_id(session_id, "session id")
    except AgentDirError:
        return _fallback(source, INTEGRITY_UNAVAILABLE, "summary session identity is unsafe")
    rows = _session_rows(root, session_id)
    if not rows:
        return _fallback(source, INTEGRITY_UNAVAILABLE, "summary source session is unavailable")
    summary = session_memory_summary(session_id, rows)
    actual = hashlib.sha256(summary.strip().encode("utf-8")).hexdigest()
    expected = _string_or_none(source.get("text_sha256"))
    if expected and actual != expected:
        return _fallback(
            source,
            INTEGRITY_CHANGED,
            "derived summary drifted or its session identity was replaced",
        )
    return {
        "body": summary,
        "basis": "canonical_derived_summary",
        "integrity": INTEGRITY_VERIFIED if expected else INTEGRITY_LEGACY_UNVERIFIED,
        "integrity_reason": None,
        "source_sha256": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
        "warnings": [],
        "capture_truncated": False,
    }


def _fallback(source: dict[str, Any], integrity: str, reason: str) -> dict[str, Any]:
    body = str(source.get("excerpt") or "")
    source_root = None
    if source.get("source_root_id"):
        source_root = {
            "root_id": source.get("source_root_id"),
            "name": source.get("source_root_name"),
            "visibility": source.get("source_root_visibility"),
        }
    return {
        "body": body,
        "basis": "manifest_excerpt",
        "integrity": integrity,
        "integrity_reason": reason,
        "source_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "warnings": [reason],
        "capture_truncated": False,
        "source_root": source_root,
    }


def _candidate_envelopes(
    root: Path,
    source: dict[str, Any],
    *,
    include_session_scan: bool,
) -> list[Path]:
    paths = paths_for(root)
    candidates: list[Path] = []
    relative_values = [source.get("source_file_path"), source.get("file_path")]
    source_id = str(source.get("source_id") or "")
    if source_id.startswith("message:"):
        relative_values.append(source_id.removeprefix("message:"))
    for value in relative_values:
        candidate = _contained_relative_path(paths.root, value)
        if candidate is None:
            continue
        candidates.append(candidate)
        try:
            relative = candidate.relative_to(paths.root)
        except ValueError:
            continue
        parts = relative.parts
        if len(parts) >= 2 and parts[0] == "sessions":
            candidates.append(paths.archives / "sessions" / Path(*parts[1:]))

    message_id = _string_or_none(source.get("message_id"))
    if message_id and paths.index_path.is_file():
        try:
            with connect_index(paths.root) as conn:
                indexed = conn.execute(
                    "select file_path from messages where message_id = ? order by file_path",
                    (message_id,),
                ).fetchall()
        except sqlite3.DatabaseError:
            indexed = []
        for row in indexed:
            candidate = _contained_relative_path(paths.root, row["file_path"])
            if candidate is not None:
                candidates.append(candidate)

    session_id = _string_or_none(source.get("session_id"))
    if session_id and include_session_scan:
        try:
            validate_id(session_id, "session id")
        except AgentDirError:
            session_id = None
    if session_id:
        for mailbox in (
            paths.sessions / session_id / "Maildir",
            paths.archives / "sessions" / session_id / "Maildir",
        ):
            candidates.extend(record.path for record in iter_records(mailbox))

    deduped: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        contained = _contained_file(paths.root, candidate)
        if contained is None:
            continue
        identity = str(contained)
        if identity not in seen:
            seen.add(identity)
            deduped.append(contained)
    return deduped


def _contained_relative_path(root: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    relative = Path(value)
    if relative.is_absolute():
        return None
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _contained_file(root: Path, candidate: Path) -> Path | None:
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _matches_source_metadata(parsed: ParsedEnvelope, source: dict[str, Any]) -> bool:
    checks = (
        ("message_id", parsed.message_id),
        ("session_id", parsed.header("X-AgentDir-Session")),
        ("event_type", parsed.header("X-AgentDir-Event-Type")),
        ("subject", parsed.header("Subject")),
        ("tool", parsed.header("X-AgentDir-Tool")),
        ("workspace", parsed.header("X-AgentDir-Workspace")),
        ("git_head", parsed.header("X-AgentDir-Git-Head")),
    )
    return all(
        source.get(key) is None
        or _normalized_metadata(source.get(key)) == _normalized_metadata(actual)
        for key, actual in checks
    )


def _envelope_memory_sha(parsed: ParsedEnvelope, source: dict[str, Any]) -> str:
    text = build_memory_text(
        message_id=source.get("message_id") or parsed.message_id,
        session_id=source.get("session_id") or parsed.header("X-AgentDir-Session"),
        event_type=source.get("event_type") or parsed.header("X-AgentDir-Event-Type"),
        subject=source.get("subject") if source.get("subject") is not None else parsed.header("Subject"),
        tool=source.get("tool") if source.get("tool") is not None else parsed.header("X-AgentDir-Tool"),
        workspace=(
            source.get("workspace")
            if source.get("workspace") is not None
            else parsed.header("X-AgentDir-Workspace")
        ),
        git_head=(
            source.get("git_head")
            if source.get("git_head") is not None
            else parsed.header("X-AgentDir-Git-Head")
        ),
        body_text=parsed.body_text,
    ).strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _session_rows(root: Path, session_id: str) -> list[dict[str, Any]]:
    paths = paths_for(root)
    try:
        validate_id(session_id, "session id")
    except AgentDirError:
        return []
    records = []
    for mailbox in (
        paths.sessions / session_id / "Maildir",
        paths.archives / "sessions" / session_id / "Maildir",
    ):
        records.extend(iter_records(mailbox))
    rows: list[dict[str, Any]] = []
    for record in records:
        contained = _contained_file(paths.root, record.path)
        if contained is None:
            continue
        try:
            parsed = parse_envelope(contained)
        except (AgentDirError, OSError):
            continue
        if parsed.header("X-AgentDir-Session") != session_id:
            continue
        rows.append(
            {
                "event_type": parsed.header("X-AgentDir-Event-Type"),
                "tool": parsed.header("X-AgentDir-Tool"),
                "tool_exit_code": _int_or_none(parsed.header("X-AgentDir-Tool-Exit-Code")),
                "body_text": parsed.body_text,
                "_date": _date_key(parsed.header("Date")),
                "_created_ns": _int_or_none(parsed.header("X-AgentDir-Created-Ns")) or 0,
                "_path": str(contained),
            }
        )
    rows.sort(key=lambda row: (row["_date"], row["_created_ns"], row["_path"]))
    return rows


def _date_key(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    except (TypeError, ValueError):
        return value


def _delivery_result(
    manifest: dict[str, Any],
    source: dict[str, Any],
    *,
    source_ref: str,
    resolution: dict[str, Any],
    page: int,
    page_bytes: int,
) -> dict[str, Any]:
    redacted = redact_text(str(resolution["body"]))
    representation = redacted.text
    pages = _utf8_pages(representation, page_bytes)
    if page > len(pages):
        raise AgentDirError(
            f"Context expansion page {page} is out of range; source has {len(pages)} page(s)"
        )
    current = pages[page - 1]
    canonical = resolution["basis"] != "manifest_excerpt"
    extent = "full" if canonical and len(pages) == 1 else "bounded" if canonical else "stored_excerpt"
    source_root = resolution.get("source_root") or {}
    return {
        "protocol": CONTEXT_EXPANSION_PROTOCOL,
        "pack_id": manifest["pack_id"],
        "session_id": manifest["session_id"],
        "source": {
            "ref": source_ref,
            "source_id": source["source_id"],
            "source_kind": source.get("source_kind"),
            "source_class": source.get("source_class"),
            "source_role": source.get("source_role"),
            "match_quality": source.get("match_quality"),
            "match_reasons": list(source.get("match_reasons") or []),
            "memory_score": source.get("memory_score"),
            "session_id": source.get("session_id"),
            "event_type": source.get("event_type"),
            "subject": source.get("subject"),
            "date_utc": source.get("date_utc"),
            "git_head": source.get("git_head"),
            "root_id": source_root.get("root_id"),
            "root_name": source_root.get("name"),
            "root_visibility": source_root.get("visibility"),
            "capture_truncated": bool(resolution.get("capture_truncated")),
        },
        "integrity": resolution["integrity"],
        "integrity_reason": resolution.get("integrity_reason"),
        "basis": resolution["basis"],
        "extent": extent,
        "page": page,
        "page_count": len(pages),
        "page_bytes": page_bytes,
        "byte_start": current["byte_start"],
        "byte_end": current["byte_end"],
        "source_bytes": len(representation.encode("utf-8")),
        "returned_bytes": len(current["content"].encode("utf-8")),
        "source_chars": len(representation),
        "returned_chars": len(current["content"]),
        "truncated": extent == "bounded",
        "has_previous": page > 1,
        "has_next": page < len(pages),
        "content": current["content"],
        "source_content_sha256": resolution["source_sha256"],
        "representation_sha256": hashlib.sha256(representation.encode("utf-8")).hexdigest(),
        "expected_body_sha256": _digest_marker(source.get("body_sha256")),
        "expected_text_sha256": _digest_marker(source.get("text_sha256")),
        "redactions": {"count": redacted.replacements, "labels": list(redacted.labels)},
        "warnings": list(resolution.get("warnings") or []),
    }


def _utf8_pages(text: str, max_bytes: int) -> list[dict[str, Any]]:
    data = text.encode("utf-8")
    if not data:
        return [{"byte_start": 0, "byte_end": 0, "content": ""}]
    pages: list[dict[str, Any]] = []
    start = 0
    while start < len(data):
        end = min(start + max_bytes, len(data))
        while end > start:
            try:
                content = data[start:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        if end == start:
            raise AgentDirStateError("Unable to advance a UTF-8 context expansion page")
        pages.append({"byte_start": start, "byte_end": end, "content": content})
        start = end
    return pages


def _receipt_expansion(
    root: Path,
    manifest: dict[str, Any],
    result: dict[str, Any],
    *,
    audit: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    if result["integrity"] not in CANONICAL_INTEGRITY_STATES or result["extent"] not in CANONICAL_EXTENTS:
        return {
            "status": "not_recorded",
            "recorded": False,
            "reason": "canonical_source_unavailable",
            "view_id": None,
            "event_path": None,
        }
    current = read_current_session(root)
    if (
        current is None
        or current.status != "active"
        or current.session_id != manifest.get("session_id")
    ):
        return {
            "status": "not_recorded",
            "recorded": False,
            "reason": "session_not_active",
            "view_id": None,
            "event_path": None,
        }
    if query_messages(
        root,
        session_id=current.session_id,
        event_type="session.ended",
        limit=1,
    ):
        return {
            "status": "not_recorded",
            "recorded": False,
            "reason": "session_ended",
            "view_id": None,
            "event_path": None,
        }

    payload = _view_payload(result)
    view_id = _view_id(payload)
    existing = [
        event
        for event in _expansion_events(
            root,
            str(manifest["pack_id"]),
            session_id=str(manifest.get("session_id") or ""),
        )
        if _header_values(event).get(HEADER_VIEW_ID) == [view_id]
    ]
    if existing:
        validated, errors = _validate_receipt(
            root,
            existing[0],
            manifest,
            {str(result["source"]["source_id"]): str(result["source"]["ref"])},
            {
                str(source["source_id"]): source
                for source in manifest.get("sources") or []
                if source.get("source_id")
            },
        )
        if len(existing) == 1 and validated is not None and not errors:
            return {
                "status": "existing",
                "recorded": False,
                "reason": "idempotent_replay",
                "view_id": view_id,
                "event_path": existing[0].get("file_path"),
            }
        return {
            "status": "not_recorded",
            "recorded": False,
            "reason": "receipt_id_conflict",
            "view_id": view_id,
            "event_path": existing[0].get("file_path"),
        }

    phase = "after_decision" if audit.get("decision_event_count") else "before_decision"
    headers: dict[str, str] = {
        "X-AgentDir-Protocol": CONTEXT_EXPANSION_PROTOCOL,
        HEADER_VIEW_PACK_ID: payload["pack_id"],
        HEADER_VIEW_ID: view_id,
        HEADER_VIEW_SOURCE_ID: payload["source_id"],
        HEADER_VIEW_SOURCE_REF: payload["source_ref"],
        HEADER_VIEW_INTEGRITY: payload["integrity"],
        HEADER_VIEW_EXTENT: payload["extent"],
        HEADER_VIEW_REPRESENTATION: payload["representation"],
        HEADER_VIEW_PAGE: payload["page"],
        HEADER_VIEW_PAGE_COUNT: payload["page_count"],
        HEADER_VIEW_BYTE_START: payload["byte_start"],
        HEADER_VIEW_BYTE_END: payload["byte_end"],
        HEADER_VIEW_SOURCE_BYTES: payload["source_bytes"],
        HEADER_VIEW_PAGE_BYTES: payload["page_bytes"],
        HEADER_VIEW_SOURCE_SHA256: payload["source_sha256"],
        HEADER_VIEW_REPRESENTATION_SHA256: payload["representation_sha256"],
        HEADER_VIEW_EXPECTED_BODY_SHA256: payload["expected_body_sha256"],
        HEADER_VIEW_EXPECTED_TEXT_SHA256: payload["expected_text_sha256"],
        HEADER_VIEW_REDACTIONS: payload["redactions"],
        HEADER_VIEW_DECISION_PHASE: phase,
    }
    if audit.get("decision_id"):
        headers[HEADER_VIEW_DECISION_ID] = str(audit["decision_id"])
    body = _receipt_body(payload, view_id=view_id, decision_phase=phase)
    event = emit_event(
        root,
        session_id=current.session_id,
        event_type=EVENT_CONTEXT_SOURCES_EXPANDED,
        subject=f"context source expanded: {payload['pack_id']} source {payload['source_ref']}",
        body=body,
        from_actor=actor,
        extra_headers=headers,
        message_id=f"<{view_id}@agentdir.local>",
    )
    return {
        "status": "recorded",
        "recorded": True,
        "reason": "active_session_receipt",
        "view_id": view_id,
        "event_path": str(event.path),
    }


def _view_payload(result: dict[str, Any]) -> dict[str, str]:
    return {
        "protocol": CONTEXT_EXPANSION_PROTOCOL,
        "pack_id": str(result["pack_id"]),
        "source_id": str(result["source"]["source_id"]),
        "source_ref": str(result["source"]["ref"]),
        "integrity": str(result["integrity"]),
        "extent": str(result["extent"]),
        "representation": CONTEXT_EXPANSION_REPRESENTATION,
        "page": str(result["page"]),
        "page_count": str(result["page_count"]),
        "byte_start": str(result["byte_start"]),
        "byte_end": str(result["byte_end"]),
        "source_bytes": str(result["source_bytes"]),
        "page_bytes": str(result["page_bytes"]),
        "source_sha256": str(result["source_content_sha256"]),
        "representation_sha256": str(result["representation_sha256"]),
        "expected_body_sha256": str(result["expected_body_sha256"]),
        "expected_text_sha256": str(result["expected_text_sha256"]),
        "redactions": str(result["redactions"]["count"]),
    }


def _view_id(payload: dict[str, str]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "ctxview-" + hashlib.sha256(encoded).hexdigest()[:32]


def _expansion_events(
    root: str | Path,
    pack_id: str,
    *,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    sql = """
        select distinct m.*
        from messages m
        join headers hp on hp.message_rowid = m.id
        where m.event_type = ? and lower(hp.name) = lower(?) and hp.value = ?
        order by coalesce(m.date_utc, m.indexed_at), coalesce(m.created_ns, 0), m.file_path, m.id
    """
    try:
        with connect_index(root) as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    sql,
                    (EVENT_CONTEXT_SOURCES_EXPANDED, HEADER_VIEW_PACK_ID, pack_id),
                ).fetchall()
            ]
            for row in rows:
                headers = conn.execute(
                    "select name, value from headers where message_rowid = ? order by rowid",
                    (row["id"],),
                ).fetchall()
                mapped: dict[str, list[str]] = {}
                for header in headers:
                    mapped.setdefault(str(header["name"]), []).append(str(header["value"]))
                row["header_values"] = mapped
        if session_id:
            rows.extend(_archived_expansion_events(root, pack_id, session_id))
        rows.sort(key=_event_order)
        return rows
    except sqlite3.DatabaseError as exc:
        raise AgentDirError(f"Context expansion receipts cannot be read: {exc}") from exc


def _session_expansion_events(root: str | Path, session_id: str) -> list[dict[str, Any]]:
    try:
        validate_id(session_id, "session id")
    except AgentDirError:
        return []
    sql = """
        select distinct m.*
        from messages m
        where m.mailbox_path = ?
          and (
            m.event_type = ?
            or exists (
              select 1 from headers hv
              where hv.message_rowid = m.id
                and lower(hv.name) in (lower(?), lower(?), lower(?))
            )
          )
        order by coalesce(m.date_utc, m.indexed_at), coalesce(m.created_ns, 0), m.file_path, m.id
    """
    try:
        with connect_index(root) as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    sql,
                    (
                        str(Path("sessions") / session_id / "Maildir"),
                        EVENT_CONTEXT_SOURCES_EXPANDED,
                        HEADER_VIEW_PACK_ID,
                        HEADER_VIEW_ID,
                        HEADER_VIEW_SOURCE_ID,
                    ),
                ).fetchall()
            ]
            for row in rows:
                headers = conn.execute(
                    "select name, value from headers where message_rowid = ? order by rowid",
                    (row["id"],),
                ).fetchall()
                mapped: dict[str, list[str]] = {}
                for header in headers:
                    mapped.setdefault(str(header["name"]), []).append(str(header["value"]))
                row["header_values"] = mapped
        rows.extend(_archived_expansion_events(root, None, session_id))
        deduped = {
            _event_identity(row): row
            for row in rows
        }
        return sorted(deduped.values(), key=_event_order)
    except sqlite3.DatabaseError as exc:
        raise AgentDirError(f"Context expansion receipts cannot be read: {exc}") from exc


def _archived_expansion_events(
    root: str | Path,
    pack_id: str | None,
    session_id: str,
) -> list[dict[str, Any]]:
    paths = paths_for(root)
    try:
        validate_id(session_id, "session id")
    except AgentDirError:
        return []
    rows: list[dict[str, Any]] = []
    mailbox = paths.archives / "sessions" / session_id / "Maildir"
    for record in iter_records(mailbox):
        contained = _contained_file(paths.root, record.path)
        if contained is None:
            continue
        try:
            parsed = parse_envelope(contained)
        except (AgentDirError, OSError):
            continue
        parsed_event_type = parsed.header("X-AgentDir-Event-Type")
        view_pack_ids = parsed.headers(HEADER_VIEW_PACK_ID)
        if pack_id is None:
            has_view_identity = bool(
                view_pack_ids
                or parsed.headers(HEADER_VIEW_ID)
                or parsed.headers(HEADER_VIEW_SOURCE_ID)
            )
            if parsed_event_type != EVENT_CONTEXT_SOURCES_EXPANDED and not has_view_identity:
                continue
        else:
            if parsed_event_type != EVENT_CONTEXT_SOURCES_EXPANDED:
                continue
            if pack_id not in view_pack_ids:
                continue
        mapped: dict[str, list[str]] = {}
        for name, value in parsed.message.items():
            mapped.setdefault(name, []).append(" ".join(str(value).split()))
        rows.append(
            {
                "id": 0,
                "message_id": parsed.message_id,
                "event_type": parsed_event_type,
                "subject": parsed.header("Subject"),
                "session_id": parsed.header("X-AgentDir-Session"),
                "date_utc": _date_key(parsed.header("Date")),
                "created_ns": _int_or_none(parsed.header("X-AgentDir-Created-Ns")) or 0,
                "file_path": str(contained.relative_to(paths.root.resolve())),
                "body_text": parsed.body_text,
                "indexed_at": None,
                "malformed": bool(validate_required(parsed)),
                "header_values": mapped,
            }
        )
    return rows


def _validate_receipt(
    root: str | Path,
    event: dict[str, Any],
    manifest: dict[str, Any],
    ref_by_id: dict[str, str],
    source_by_id: dict[str, dict[str, Any]],
) -> tuple[dict[str, str] | None, list[str]]:
    values = _header_values(event)
    errors: list[str] = []
    required = (
        "X-AgentDir-Protocol",
        HEADER_VIEW_PACK_ID,
        HEADER_VIEW_ID,
        HEADER_VIEW_SOURCE_ID,
        HEADER_VIEW_SOURCE_REF,
        HEADER_VIEW_INTEGRITY,
        HEADER_VIEW_EXTENT,
        HEADER_VIEW_REPRESENTATION,
        HEADER_VIEW_PAGE,
        HEADER_VIEW_PAGE_COUNT,
        HEADER_VIEW_BYTE_START,
        HEADER_VIEW_BYTE_END,
        HEADER_VIEW_SOURCE_BYTES,
        HEADER_VIEW_PAGE_BYTES,
        HEADER_VIEW_SOURCE_SHA256,
        HEADER_VIEW_REPRESENTATION_SHA256,
        HEADER_VIEW_EXPECTED_BODY_SHA256,
        HEADER_VIEW_EXPECTED_TEXT_SHA256,
        HEADER_VIEW_REDACTIONS,
        HEADER_VIEW_DECISION_PHASE,
    )
    payload_values: dict[str, str] = {}
    for name in required:
        found = values.get(name) or []
        if len(found) != 1:
            errors.append(f"{_event_label(event)}: expected exactly one {name} header")
        else:
            payload_values[name] = found[0]
    decision_values = values.get(HEADER_VIEW_DECISION_ID) or []
    if len(decision_values) > 1:
        errors.append(f"{_event_label(event)}: expected at most one {HEADER_VIEW_DECISION_ID} header")
    if errors:
        return None, errors

    source_id = payload_values[HEADER_VIEW_SOURCE_ID]
    source_ref = payload_values[HEADER_VIEW_SOURCE_REF]
    payload = {
        "protocol": payload_values["X-AgentDir-Protocol"],
        "pack_id": payload_values[HEADER_VIEW_PACK_ID],
        "source_id": source_id,
        "source_ref": source_ref,
        "integrity": payload_values[HEADER_VIEW_INTEGRITY],
        "extent": payload_values[HEADER_VIEW_EXTENT],
        "representation": payload_values[HEADER_VIEW_REPRESENTATION],
        "page": payload_values[HEADER_VIEW_PAGE],
        "page_count": payload_values[HEADER_VIEW_PAGE_COUNT],
        "byte_start": payload_values[HEADER_VIEW_BYTE_START],
        "byte_end": payload_values[HEADER_VIEW_BYTE_END],
        "source_bytes": payload_values[HEADER_VIEW_SOURCE_BYTES],
        "page_bytes": payload_values[HEADER_VIEW_PAGE_BYTES],
        "source_sha256": payload_values[HEADER_VIEW_SOURCE_SHA256],
        "representation_sha256": payload_values[HEADER_VIEW_REPRESENTATION_SHA256],
        "expected_body_sha256": payload_values[HEADER_VIEW_EXPECTED_BODY_SHA256],
        "expected_text_sha256": payload_values[HEADER_VIEW_EXPECTED_TEXT_SHA256],
        "redactions": payload_values[HEADER_VIEW_REDACTIONS],
        "view_id": payload_values[HEADER_VIEW_ID],
        "decision_phase": payload_values[HEADER_VIEW_DECISION_PHASE],
        "decision_id": decision_values[0] if decision_values else "",
    }
    if payload["pack_id"] != manifest.get("pack_id"):
        errors.append(f"{_event_label(event)}: receipt pack id does not match the manifest")
    if payload["protocol"] != CONTEXT_EXPANSION_PROTOCOL:
        errors.append(f"{_event_label(event)}: receipt protocol is unsupported")
    if values.get("X-AgentDir-Pack-Id"):
        errors.append(
            f"{_event_label(event)}: optional receipt must not use X-AgentDir-Pack-Id"
        )
    if event.get("session_id") != manifest.get("session_id"):
        errors.append(f"{_event_label(event)}: receipt session does not match the manifest")
    if event.get("malformed"):
        errors.append(f"{_event_label(event)}: receipt envelope is malformed")
    if source_id not in ref_by_id:
        errors.append(f"{_event_label(event)}: receipt source was not displayed")
    elif ref_by_id[source_id] != source_ref:
        errors.append(f"{_event_label(event)}: receipt source reference is inconsistent")
    if payload["integrity"] not in CANONICAL_INTEGRITY_STATES:
        errors.append(f"{_event_label(event)}: receipt integrity is not canonical")
    if payload["extent"] not in CANONICAL_EXTENTS:
        errors.append(f"{_event_label(event)}: receipt extent is not canonical")
    if payload["representation"] != CONTEXT_EXPANSION_REPRESENTATION:
        errors.append(f"{_event_label(event)}: receipt representation is unsupported")
    if payload["decision_phase"] not in {"before_decision", "after_decision"}:
        errors.append(f"{_event_label(event)}: receipt decision phase is invalid")
    integers: dict[str, int] = {}
    for field in (
        "page",
        "page_count",
        "byte_start",
        "byte_end",
        "source_bytes",
        "page_bytes",
        "redactions",
    ):
        try:
            integers[field] = int(payload[field])
        except ValueError:
            errors.append(f"{_event_label(event)}: receipt {field} is not an integer")
    if not errors:
        if integers["page"] < 1 or integers["page"] > integers["page_count"]:
            errors.append(f"{_event_label(event)}: receipt page range is invalid")
        if not 0 <= integers["byte_start"] <= integers["byte_end"] <= integers["source_bytes"]:
            errors.append(f"{_event_label(event)}: receipt byte range is invalid")
        if payload["extent"] == "full" and integers["page_count"] != 1:
            errors.append(f"{_event_label(event)}: full receipt must have one page")
        if payload["extent"] == "bounded" and integers["page_count"] <= 1:
            errors.append(f"{_event_label(event)}: bounded receipt must have multiple pages")
        if integers["redactions"] < 0:
            errors.append(f"{_event_label(event)}: receipt redaction count is invalid")
        if integers["page_bytes"] < 4:
            errors.append(f"{_event_label(event)}: receipt page size is invalid")
    for field in ("source_sha256", "representation_sha256"):
        value = payload[field]
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            errors.append(f"{_event_label(event)}: receipt {field} is not a SHA-256 digest")
    for field in ("expected_body_sha256", "expected_text_sha256"):
        value = payload[field]
        if value != "none" and not _is_sha256(value):
            errors.append(f"{_event_label(event)}: receipt {field} is invalid")
    source = source_by_id.get(source_id)
    if source is not None:
        expected_body = _digest_marker(source.get("body_sha256"))
        expected_text = _digest_marker(source.get("text_sha256"))
        if payload["expected_body_sha256"] != expected_body:
            errors.append(f"{_event_label(event)}: receipt expected body digest changed")
        if payload["expected_text_sha256"] != expected_text:
            errors.append(f"{_event_label(event)}: receipt expected text digest changed")
        if expected_body != "none" and payload["source_sha256"] != expected_body:
            errors.append(f"{_event_label(event)}: receipt source digest does not match the manifest")
        if payload["integrity"] == INTEGRITY_VERIFIED and expected_body == expected_text == "none":
            errors.append(f"{_event_label(event)}: verified receipt has no expected manifest digest")
        if payload["integrity"] == INTEGRITY_LEGACY_UNVERIFIED and (
            expected_body != "none" or expected_text != "none"
        ):
            errors.append(f"{_event_label(event)}: legacy receipt conflicts with manifest digests")
    id_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"view_id", "decision_phase", "decision_id"}
    }
    if _view_id(id_payload) != payload["view_id"]:
        errors.append(f"{_event_label(event)}: receipt view id does not match its payload")
    expected_message_id = f"<{payload['view_id']}@agentdir.local>"
    if event.get("message_id") != expected_message_id:
        errors.append(f"{_event_label(event)}: receipt message id is not canonical")
    expected_subject = (
        f"context source expanded: {payload['pack_id']} source {payload['source_ref']}"
    )
    if _normalized_metadata(event.get("subject")) != expected_subject:
        errors.append(f"{_event_label(event)}: receipt subject is not canonical")
    expected_body = _receipt_body(
        id_payload,
        view_id=payload["view_id"],
        decision_phase=payload["decision_phase"],
    )
    if str(event.get("body_text") or "").strip() != expected_body.strip():
        errors.append(f"{_event_label(event)}: receipt body is not metadata-only canonical form")
    if source is not None and not errors:
        resolution = _resolve_source(require_root(root).root, manifest, source)
        if resolution["integrity"] in CANONICAL_INTEGRITY_STATES:
            try:
                expected_result = _delivery_result(
                    manifest,
                    source,
                    source_ref=source_ref,
                    resolution=resolution,
                    page=integers["page"],
                    page_bytes=integers["page_bytes"],
                )
            except AgentDirError as exc:
                errors.append(f"{_event_label(event)}: receipt page cannot be reproduced: {exc}")
            else:
                expected_payload = _view_payload(expected_result)
                for field, expected_value in expected_payload.items():
                    if payload.get(field) != expected_value:
                        errors.append(
                            f"{_event_label(event)}: receipt {field} does not match retained content"
                        )
    return payload, errors


def _receipt_body(
    payload: dict[str, str],
    *,
    view_id: str,
    decision_phase: str,
) -> str:
    return "\n".join(
        [
            f"context_view_pack_id={payload['pack_id']}",
            f"context_view_id={view_id}",
            f"source_id={payload['source_id']}",
            f"source_ref={payload['source_ref']}",
            f"page={payload['page']}/{payload['page_count']}",
            f"byte_range={payload['byte_start']}:{payload['byte_end']}",
            f"integrity={payload['integrity']}",
            f"extent={payload['extent']}",
            f"decision_phase={decision_phase}",
            "",
            "Metadata-only context expansion receipt; source content is not copied here.",
        ]
    )


def _header_values(event: dict[str, Any]) -> dict[str, list[str]]:
    raw = event.get("header_values") or {}
    normalized: dict[str, list[str]] = {}
    for name, values in raw.items():
        normalized.setdefault(str(name).lower(), []).extend(str(value) for value in values)
    result = dict(raw)
    canonical_names = (
        "X-AgentDir-Protocol",
        "X-AgentDir-Pack-Id",
        HEADER_VIEW_PACK_ID,
        HEADER_VIEW_ID,
        HEADER_VIEW_SOURCE_ID,
        HEADER_VIEW_SOURCE_REF,
        HEADER_VIEW_INTEGRITY,
        HEADER_VIEW_EXTENT,
        HEADER_VIEW_REPRESENTATION,
        HEADER_VIEW_PAGE,
        HEADER_VIEW_PAGE_COUNT,
        HEADER_VIEW_BYTE_START,
        HEADER_VIEW_BYTE_END,
        HEADER_VIEW_SOURCE_BYTES,
        HEADER_VIEW_PAGE_BYTES,
        HEADER_VIEW_SOURCE_SHA256,
        HEADER_VIEW_REPRESENTATION_SHA256,
        HEADER_VIEW_EXPECTED_BODY_SHA256,
        HEADER_VIEW_EXPECTED_TEXT_SHA256,
        HEADER_VIEW_REDACTIONS,
        HEADER_VIEW_DECISION_PHASE,
        HEADER_VIEW_DECISION_ID,
    )
    for name in canonical_names:
        values = normalized.get(name.lower())
        if values is not None:
            result[name] = values
    return result


def _receipt_signature(payload: dict[str, str]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _event_order(event: dict[str, Any]) -> tuple[str, int, str, int]:
    return (
        str(event.get("date_utc") or event.get("indexed_at") or ""),
        int(event.get("created_ns") or 0),
        str(event.get("file_path") or ""),
        int(event.get("id") or 0),
    )


def _event_label(event: dict[str, Any]) -> str:
    return str(event.get("file_path") or event.get("message_id") or "expansion receipt")


def _event_identity(event: dict[str, Any]) -> tuple[str, str]:
    return (
        str(event.get("message_id") or ""),
        str(event.get("file_path") or event.get("id") or ""),
    )


def _ordered_unique(values: list[str], source_order: list[str]) -> list[str]:
    wanted = set(values)
    ordered = [source_id for source_id in source_order if source_id in wanted]
    ordered.extend(sorted(wanted - set(source_order)))
    return ordered


def _next_actions(result: dict[str, Any], audit: dict[str, Any]) -> dict[str, list[str]]:
    actions: dict[str, list[str]] = {}
    if result.get("has_next"):
        actions["next_page"] = [
            "work",
            "context",
            "--pack",
            str(result["pack_id"]),
            "--expand",
            str(result["source"]["ref"]),
            "--page",
            str(int(result["page"]) + 1),
        ]
    if result.get("has_previous"):
        actions["previous_page"] = [
            "work",
            "context",
            "--pack",
            str(result["pack_id"]),
            "--expand",
            str(result["source"]["ref"]),
            "--page",
            str(int(result["page"]) - 1),
        ]
    if audit.get("review_required") and not audit.get("decision_complete"):
        actions["use"] = [
            "work",
            "context",
            "--pack",
            str(result["pack_id"]),
            "--use",
            str(result["source"]["ref"]),
            "--reason",
            "<how it helps>",
        ]
    return actions


def _string_or_none(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None


def _normalized_metadata(value: Any) -> str:
    return " ".join(str(value or "").split())


def _digest_marker(value: Any) -> str:
    return str(value) if value is not None and str(value) else "none"


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}
