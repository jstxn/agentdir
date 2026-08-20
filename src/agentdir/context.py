from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .artifacts import add_artifact, artifact_headers, artifact_path
from .events import emit_event
from .envelope import parse_envelope
from .federation import search_federated_memory
from .fsutil import atomic_write_text
from .index import connect_index, update_index
from .locking import lifecycle_lock
from .memory import (
    DEFAULT_MIN_SCORE,
    RETRIEVAL_AUTO,
    RETRIEVAL_DOCUMENT,
    RETRIEVAL_HYBRID,
    RETRIEVAL_SEMANTIC,
    RETRIEVAL_SEMANTIC_HYBRID,
    STOPWORDS as MEMORY_STOPWORDS,
    recent_session_summaries,
    resolve_retrieval_mode,
    search_memory,
)
from .query import query_messages
from .redaction import redact_text
from .review import evidence_rows, format_evidence, format_summary, summarize_session
from .sessions import read_current_session
from .store import AgentDirError, AgentDirStateError, paths_for, validate_id

CONTEXT_PROTOCOL = "agentdir.context-pack.v1"
CONTEXT_BRIEFING_PROTOCOL = "agentdir.context-briefing.v1"
CONTEXT_ENFORCEMENT_MODE = "advisory"
CONTEXT_QUALITY_POLICY = "agentdir.balanced.v3"
CONTEXT_BRIEFING_LIMIT = 5
CONTEXT_QUALITY_ORDER = ("strong", "possible", "current", "weak", "unknown")
CONTEXT_SOURCE_PREFERENCE_ORDER = (
    "current_evidence",
    "decision",
    "evidence",
    "substantive",
    "summary",
    "final_report",
    "lifecycle",
)
CONTEXT_SOURCE_SELECTION_TIERS = (
    ("current_evidence",),
    ("decision", "evidence"),
    ("substantive",),
    ("summary",),
    ("final_report",),
    ("lifecycle",),
)
CONTEXT_REDUNDANT_WITH_DECISION_SESSION = (
    "lifecycle",
    "final_report",
    "summary",
)
CONTEXT_MAX_PER_SESSION = 2
CONTEXT_MAX_PER_CLASS_FIRST_PASS = 2
CONTEXT_SEARCH_CANDIDATE_MULTIPLIER = 12
CONTEXT_QUALITY_THRESHOLDS = {
    RETRIEVAL_DOCUMENT: {"strong": 0.55, "possible": 0.35, "semantic_only_strong": None},
    RETRIEVAL_HYBRID: {"strong": 0.55, "possible": 0.35, "semantic_only_strong": None},
    RETRIEVAL_SEMANTIC: {"strong": 0.70, "possible": 0.40, "semantic_only_strong": 0.70},
    RETRIEVAL_SEMANTIC_HYBRID: {
        "strong": 0.70,
        "possible": 0.40,
        "semantic_only_strong": 0.70,
    },
    RETRIEVAL_AUTO: {"strong": 0.55, "possible": 0.35, "semantic_only_strong": None},
}
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
EVIDENCE_EVENTS = {"tool.call", "tool.result", "file.diff"}
LIFECYCLE_EVENTS = {
    "session.started",
    "session.ended",
    "work.started",
    "work.finished",
    "context.pack.created",
    "context.pack.consumed",
    "context.pack.reviewed",
    "context.sources.cited",
    "context.sources.expanded",
}
CONTEXT_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_+#]*", re.IGNORECASE)
CONTEXT_GENERIC_TERMS = {
    "add",
    "agent",
    "agents",
    "change",
    "code",
    "fix",
    "implement",
    "in",
    "investigate",
    "issue",
    "of",
    "on",
    "problem",
    "repo",
    "repository",
    "task",
    "to",
    "update",
    "work",
    "working",
} | MEMORY_STOPWORDS


@dataclass(frozen=True)
class EmittedContextPack:
    manifest: dict[str, Any]
    event_path: Path
    artifact_sha256: str
    artifact_path: Path


def build_context_pack(
    root: str | Path,
    task: str,
    *,
    session_id: str | None = None,
    memory_limit: int = 8,
    evidence_limit: int = 20,
    recent_limit: int = 5,
    min_score: float = DEFAULT_MIN_SCORE,
    federated: bool = False,
    federation_group: str | None = None,
    retrieval_mode: str = RETRIEVAL_AUTO,
    exclude_session_from_memory: bool = False,
    rebuild: bool = True,
) -> dict[str, Any]:
    if rebuild:
        update_index(root)
    current = read_current_session(root)
    resolved_session = session_id
    if resolved_session is None:
        resolved_session = current.session_id if current else None

    workspace = current.workspace if current and current.session_id == resolved_session else None
    retrieval_query = _context_retrieval_query(task, workspace)
    retrieval_query_state = "specific_terms" if retrieval_query else "no_specific_terms"
    effective_retrieval_mode = resolve_retrieval_mode(root, retrieval_mode)
    search_limit = (
        max(
            memory_limit * CONTEXT_SEARCH_CANDIDATE_MULTIPLIER,
            memory_limit + 24,
        )
        if memory_limit > 0
        else 0
    )
    if search_limit and retrieval_query:
        memory_hits = (
            search_federated_memory(
                root,
                retrieval_query,
                limit=search_limit,
                min_score=min_score,
                retrieval_mode=retrieval_mode,
                group=federation_group,
            )
            if federated or federation_group
            else search_memory(
                root,
                retrieval_query,
                limit=search_limit,
                min_score=min_score,
                retrieval_mode=retrieval_mode,
            )
        )
    else:
        memory_hits = []
    if resolved_session and exclude_session_from_memory:
        memory_hits = _diversify_memory_hits(
            [row for row in memory_hits if row.get("session_id") != resolved_session],
            memory_limit,
        )
        recent = [
            row
            for row in recent_session_summaries(root, limit=recent_limit + 5)
            if row.get("session_id") != resolved_session
        ][:recent_limit]
    else:
        memory_hits = _diversify_memory_hits(memory_hits, memory_limit)
        recent = recent_session_summaries(root, limit=recent_limit)
    evidence = (
        evidence_rows(root, resolved_session, rebuild=False)[-evidence_limit:]
        if resolved_session and evidence_limit > 0
        else []
    )
    current_summary = summarize_session(root, resolved_session, rebuild=False) if resolved_session else None

    return {
        "task": task,
        "retrieval_query": retrieval_query,
        "retrieval_query_state": retrieval_query_state,
        "session_id": resolved_session,
        "current_summary": current_summary,
        "memory_hits": memory_hits,
        "federated": bool(federated or federation_group),
        "federation_group": federation_group,
        "retrieval_mode": effective_retrieval_mode,
        "requested_retrieval_mode": retrieval_mode,
        "recent_session_summaries": recent,
        "evidence": evidence,
        "instructions": [
            "Use memory hits as retrieval hints, not proof.",
            "Use evidence rows for claims about commands, hooks, and diffs.",
            "Run fresh verification before reporting completion.",
        ],
    }


def build_context_manifest(
    pack: dict[str, Any],
    *,
    pack_id: str | None = None,
    selection_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_pack_id = pack_id or new_pack_id()
    retrieval_query = _pack_retrieval_query(pack)
    retrieval_mode = pack.get("retrieval_mode") or RETRIEVAL_HYBRID
    task_terms = _context_terms(retrieval_query)
    memory_sources = [
        _source_entry(
            row,
            origin="memory_hit",
            task_terms=task_terms,
            retrieval_mode=retrieval_mode,
        )
        for row in pack.get("memory_hits") or []
    ]
    recent_summaries = [
        _source_entry(
            row,
            origin="recent_summary",
            task_terms=task_terms,
            retrieval_mode=retrieval_mode,
        )
        for row in pack.get("recent_session_summaries") or []
    ]
    evidence = [
        _source_entry(
            row,
            origin="evidence",
            task_terms=task_terms,
            retrieval_mode=retrieval_mode,
        )
        for row in pack.get("evidence") or []
    ]
    sources = _dedupe_sources([*memory_sources, *recent_summaries, *evidence])
    briefing = _build_context_briefing(
        sources,
        retrieval_query,
        retrieval_mode=retrieval_mode,
        retrieval_query_state=pack.get("retrieval_query_state")
        or ("specific_terms" if retrieval_query else "no_specific_terms"),
    )
    return {
        "protocol": CONTEXT_PROTOCOL,
        "pack_id": resolved_pack_id,
        "task": pack["task"],
        "retrieval_query": retrieval_query,
        "retrieval_query_state": briefing["retrieval_query_state"],
        "session_id": pack.get("session_id"),
        "generated_at": now_iso(),
        "selection_policy": selection_policy or {},
        "federated": bool(pack.get("federated")),
        "federation_group": pack.get("federation_group"),
        "retrieval_mode": retrieval_mode,
        "requested_retrieval_mode": pack.get("requested_retrieval_mode") or retrieval_mode,
        "briefing": briefing,
        "sources": sources,
        "memory_hits": memory_sources,
        "recent_summaries": recent_summaries,
        "evidence": evidence,
        "instructions": pack.get("instructions") or [],
        "enforcement_boundary": (
            "advisory: AgentDir records that a cooperative agent reviewed, used, "
            "dismissed, and cited context, but it cannot prove model attention."
        ),
        "source_counts": _source_counts(sources),
    }


def emit_context_pack(
    root: str | Path,
    pack: dict[str, Any],
    *,
    selection_policy: dict[str, Any] | None = None,
    actor: str = "agent",
    scope: str = "project",
) -> EmittedContextPack:
    session_id = pack.get("session_id")
    if not session_id:
        raise AgentDirError("Context pack emission requires an active or explicit session")
    with lifecycle_lock(root, f"session:{session_id}"):
        update_index(root)
        if query_messages(
            root,
            session_id=session_id,
            event_type="session.ended",
            limit=1,
        ):
            raise AgentDirStateError(
                f"Cannot emit a context pack for ended session {session_id}; start a new session"
            )
        return _emit_context_pack_locked(
            root,
            pack,
            selection_policy=selection_policy,
            actor=actor,
            scope=scope,
        )


def _emit_context_pack_locked(
    root: str | Path,
    pack: dict[str, Any],
    *,
    selection_policy: dict[str, Any] | None,
    actor: str,
    scope: str,
) -> EmittedContextPack:
    session_id = pack["session_id"]
    manifest = build_context_manifest(pack, selection_policy=selection_policy)
    artifact_file = _write_manifest_temp(manifest)
    try:
        artifact = add_artifact(root, artifact_file)
    finally:
        artifact_file.unlink(missing_ok=True)
    body = "\n".join(
        [
            f"pack_id={manifest['pack_id']}",
            f"protocol={CONTEXT_PROTOCOL}",
            f"task={manifest['task']}",
            f"session_id={session_id}",
            f"sources={len(manifest['sources'])}",
            f"artifact_sha256={artifact.sha256}",
            "",
            manifest["enforcement_boundary"],
        ]
    )
    headers: dict[str, str | list[str]] = {
        HEADER_PROTOCOL: CONTEXT_PROTOCOL,
        HEADER_PACK_ID: manifest["pack_id"],
        HEADER_CONTEXT_QUERY: manifest["retrieval_query"],
        HEADER_CONTEXT_SCOPE: scope,
        HEADER_ENFORCEMENT_MODE: CONTEXT_ENFORCEMENT_MODE,
        HEADER_SOURCE_ID: [source["source_id"] for source in manifest["sources"]],
        **artifact_headers(artifact),
    }
    event = emit_event(
        root,
        session_id=session_id,
        event_type=EVENT_CONTEXT_PACK_CREATED,
        subject=f"context pack: {manifest['task']}",
        body=body,
        from_actor=actor,
        extra_headers=headers,
    )
    return EmittedContextPack(
        manifest=manifest,
        event_path=event.path,
        artifact_sha256=artifact.sha256,
        artifact_path=artifact.path,
    )


def read_context_manifest(root: str | Path, pack_id: str, *, rebuild: bool = True) -> dict[str, Any]:
    creation_events = _context_events(
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
                "Context pack manifest briefing references unknown sources: "
                + ", ".join(unknown)
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
    return manifest


def consume_context_sources(
    root: str | Path,
    *,
    pack_id: str,
    source_ids: list[str],
    purpose: str,
    session_id: str | None = None,
    actor: str = "agent",
) -> dict[str, Any]:
    if purpose not in CONSUMPTION_PURPOSES:
        raise AgentDirError(f"Unknown consumption purpose: {purpose}")
    with lifecycle_lock(root, f"pack:{pack_id}"):
        update_index(root)
        manifest = read_context_manifest(root, pack_id, rebuild=False)
        existing_audit = audit_context_pack(root, pack_id, rebuild=False)
        if _latest_context_decision(existing_audit["events"]):
            raise AgentDirStateError(
                "Context review is already terminal; lower-level consumption cannot change its decision"
            )
        selected = _select_manifest_sources(manifest, source_ids)
        manifest_session = manifest["session_id"]
        if session_id and session_id != manifest_session:
            raise AgentDirError(
                f"Context pack {pack_id} belongs to session {manifest_session}, not {session_id}"
            )
        resolved_session = manifest_session
        body = _format_source_event_body("context_consumed", pack_id, purpose, selected)
        event = emit_event(
            root,
            session_id=resolved_session,
            event_type=EVENT_CONTEXT_PACK_CONSUMED,
            subject=f"context consumed: {pack_id}",
            body=body,
            from_actor=actor,
            extra_headers={
                HEADER_PROTOCOL: CONTEXT_PROTOCOL,
                HEADER_PACK_ID: pack_id,
                HEADER_SOURCE_ID: [source["source_id"] for source in selected],
                HEADER_CONSUMPTION_PURPOSE: purpose,
                HEADER_ENFORCEMENT_MODE: CONTEXT_ENFORCEMENT_MODE,
            },
        )
    return {
        "pack_id": pack_id,
        "purpose": purpose,
        "source_ids": [source["source_id"] for source in selected],
        "event_path": str(event.path),
        "enforcement_mode": CONTEXT_ENFORCEMENT_MODE,
    }


def review_context_pack(
    root: str | Path,
    *,
    pack_id: str,
    disposition: str,
    reason: str,
    source_selectors: list[str] | None = None,
    purpose: str = "plan",
    session_id: str | None = None,
    actor: str = "agent",
) -> dict[str, Any]:
    if disposition not in CONTEXT_DISPOSITIONS:
        raise AgentDirError(f"Unknown context disposition: {disposition}")
    if purpose not in CONSUMPTION_PURPOSES:
        raise AgentDirError(f"Unknown consumption purpose: {purpose}")
    normalized_reason = " ".join(reason.split())
    if not normalized_reason:
        raise AgentDirError("Context review reason is required")
    redacted_reason = redact_text(normalized_reason)
    persisted_reason = redacted_reason.text

    with lifecycle_lock(root, f"pack:{pack_id}"):
        update_index(root)
        manifest = read_context_manifest(root, pack_id, rebuild=False)
        briefing_source_ids = _briefing_source_ids(manifest)
        selected = (
            _select_manifest_sources_by_selector(manifest, source_selectors or [])
            if disposition == "used"
            else []
        )
        if disposition == "used" and not selected:
            raise AgentDirError("At least one context source is required for a used decision")
        if disposition != "used" and source_selectors:
            raise AgentDirError(f"Context disposition {disposition} does not accept used sources")

        existing_audit = audit_context_pack(root, pack_id, rebuild=False)
        if disposition in {"no_relevant", "skipped"} and existing_audit["consumed_count"]:
            raise AgentDirStateError("Context already has consumed sources; record a used review instead")

        selected_ids = [source["source_id"] for source in selected]
        used_ids = _canonical_source_ids(
            [*existing_audit["consumed_source_ids"], *selected_ids],
            manifest,
        )
        reviewed_ids = [] if disposition == "skipped" else list(briefing_source_ids)
        dismissed_ids = (
            []
            if disposition == "skipped"
            else [source_id for source_id in briefing_source_ids if source_id not in set(used_ids)]
        )
        manifest_session = manifest["session_id"]
        if session_id and session_id != manifest_session:
            raise AgentDirError(
                f"Context pack {pack_id} belongs to session {manifest_session}, not {session_id}"
            )
        resolved_session = manifest_session
        resolved_purpose = purpose if disposition == "used" else None
        decision_id = _context_decision_id(
            pack_id=pack_id,
            disposition=disposition,
            purpose=resolved_purpose,
            reason=persisted_reason,
            reviewed_source_ids=reviewed_ids,
            used_source_ids=used_ids,
            dismissed_source_ids=dismissed_ids,
        )

        previous = _latest_context_decision(existing_audit["events"])
        if previous:
            if _same_context_decision(
                previous,
                decision_id=decision_id,
                disposition=disposition,
                purpose=resolved_purpose,
                reason=persisted_reason,
                reviewed_source_ids=reviewed_ids,
                used_source_ids=used_ids,
                dismissed_source_ids=dismissed_ids,
            ):
                return _context_review_result(
                    pack_id=pack_id,
                    decision_id=decision_id,
                    revision=1,
                    disposition=disposition,
                    purpose=resolved_purpose,
                    reason=persisted_reason,
                    reviewed_source_ids=reviewed_ids,
                    used_source_ids=used_ids,
                    dismissed_source_ids=dismissed_ids,
                    event_path=previous.get("file_path"),
                    recorded=False,
                )
            raise AgentDirStateError(
                "Context review is already terminal with a different decision; start a new context pack"
            )

        event_type = EVENT_CONTEXT_PACK_CONSUMED if disposition == "used" else EVENT_CONTEXT_PACK_REVIEWED
        review_headers: dict[str, str | list[str] | None] = {
            HEADER_PROTOCOL: CONTEXT_PROTOCOL,
            HEADER_PACK_ID: pack_id,
            HEADER_SOURCE_ID: used_ids if disposition == "used" else reviewed_ids,
            HEADER_REVIEWED_SOURCE_ID: reviewed_ids,
            HEADER_USED_SOURCE_ID: used_ids,
            HEADER_DISMISSED_SOURCE_ID: dismissed_ids,
            HEADER_CONTEXT_DISPOSITION: disposition,
            HEADER_CONTEXT_DECISION_ID: decision_id,
            HEADER_CONTEXT_DECISION_REVISION: "1",
            HEADER_CONSUMPTION_PURPOSE: resolved_purpose,
            HEADER_ENFORCEMENT_MODE: CONTEXT_ENFORCEMENT_MODE,
        }
        if redacted_reason.replacements:
            review_headers["X-AgentDir-Redactions"] = str(redacted_reason.replacements)
            review_headers["X-AgentDir-Redaction-Labels"] = ",".join(redacted_reason.labels)
        event = emit_event(
            root,
            session_id=resolved_session,
            event_type=event_type,
            subject=f"context review {disposition}: {pack_id}",
            body=_format_context_review_body(
                pack_id=pack_id,
                disposition=disposition,
                purpose=resolved_purpose,
                reason=persisted_reason,
                presented_count=len(briefing_source_ids),
                reviewed_source_ids=reviewed_ids,
                used_source_ids=used_ids,
                dismissed_source_ids=dismissed_ids,
            ),
            from_actor=actor,
            extra_headers=review_headers,
        )
        return _context_review_result(
            pack_id=pack_id,
            decision_id=decision_id,
            revision=1,
            disposition=disposition,
            purpose=resolved_purpose,
            reason=persisted_reason,
            reviewed_source_ids=reviewed_ids,
            used_source_ids=used_ids,
            dismissed_source_ids=dismissed_ids,
            event_path=str(event.path),
            recorded=True,
        )


def cite_context_sources(
    root: str | Path,
    *,
    pack_id: str,
    source_ids: list[str] | None = None,
    output_format: str = "md",
    session_id: str | None = None,
    actor: str = "agent",
) -> dict[str, Any]:
    with lifecycle_lock(root, f"pack:{pack_id}"):
        update_index(root)
        manifest = read_context_manifest(root, pack_id, rebuild=False)
        existing_audit = audit_context_pack(root, pack_id, rebuild=False)
        selected_ids = source_ids or _default_citation_sources(manifest, existing_audit)
        selected = _select_manifest_sources(manifest, selected_ids)
        if existing_audit["review_required"]:
            not_used = [
                source["source_id"]
                for source in selected
                if source["source_id"] not in set(existing_audit["consumed_source_ids"])
            ]
            if not_used:
                raise AgentDirStateError(
                    "Cannot cite context before use: " + ", ".join(not_used)
                )
        manifest_session = manifest["session_id"]
        if session_id and session_id != manifest_session:
            raise AgentDirError(
                f"Context pack {pack_id} belongs to session {manifest_session}, not {session_id}"
            )
        resolved_session = manifest_session
        citation = {
            "protocol": CONTEXT_PROTOCOL,
            "pack_id": pack_id,
            "generated_at": now_iso(),
            "enforcement_mode": CONTEXT_ENFORCEMENT_MODE,
            "sources": selected,
            "source_counts": _source_counts(selected),
        }
        rendered = format_context_citation(citation, output_format=output_format)
        event = emit_event(
            root,
            session_id=resolved_session,
            event_type=EVENT_CONTEXT_SOURCES_CITED,
            subject=f"context cited: {pack_id}",
            body=rendered,
            from_actor=actor,
            extra_headers={
                HEADER_PROTOCOL: CONTEXT_PROTOCOL,
                HEADER_PACK_ID: pack_id,
                HEADER_SOURCE_ID: [source["source_id"] for source in selected],
                HEADER_ENFORCEMENT_MODE: CONTEXT_ENFORCEMENT_MODE,
            },
        )
    citation["event_path"] = str(event.path)
    citation["rendered"] = format_context_citation(citation, output_format=output_format)
    return citation


def audit_context_pack(root: str | Path, pack_id: str, *, rebuild: bool = True) -> dict[str, Any]:
    if rebuild:
        update_index(root)
    manifest = read_context_manifest(root, pack_id, rebuild=False)
    events = _context_events(
        root,
        pack_id,
        rebuild=False,
        session_id=str(manifest.get("session_id") or ""),
    )
    consumed: list[str] = []
    cited: list[str] = []
    cited_without_use: list[str] = []
    consumed_seen: set[str] = set()
    terminal_decision_seen = False
    consume_after_decision = False
    for event in events:
        disposition_header = event.get("headers", {}).get(HEADER_CONTEXT_DISPOSITION)
        if event["event_type"] == EVENT_CONTEXT_PACK_CONSUMED:
            if terminal_decision_seen and not disposition_header:
                consume_after_decision = True
            for source_id in event["used_source_ids"]:
                if source_id not in consumed_seen:
                    consumed.append(source_id)
                    consumed_seen.add(source_id)
        if event["event_type"] == EVENT_CONTEXT_SOURCES_CITED:
            for source_id in event["source_ids"]:
                if source_id not in cited:
                    cited.append(source_id)
                if source_id not in consumed_seen and source_id not in cited_without_use:
                    cited_without_use.append(source_id)
        if disposition_header:
            terminal_decision_seen = True
    source_by_id = {source["source_id"]: source for source in manifest["sources"]}
    session_mismatches = [
        (
            f"{event.get('file_path') or event.get('message_id') or 'context event'}: "
            f"event session {event.get('session_id')!r} does not match manifest session "
            f"{manifest['session_id']!r}"
        )
        for event in events
        if event.get("session_id") != manifest["session_id"]
    ]
    session_attribution_enforced = "briefing" in manifest
    session_validation_errors = session_mismatches if session_attribution_enforced else []
    legacy_session_mismatches = session_mismatches if not session_attribution_enforced else []
    unknown_consumed = [source_id for source_id in consumed if source_id not in source_by_id]
    unknown_cited = [source_id for source_id in cited if source_id not in source_by_id]
    source_validation_errors: list[str] = []
    if unknown_consumed:
        source_validation_errors.append(
            "context consumption references unknown sources: " + ", ".join(unknown_consumed)
        )
    if unknown_cited:
        source_validation_errors.append(
            "context citation references unknown sources: " + ", ".join(unknown_cited)
        )
    briefing = manifest.get("briefing") or {}
    presented = _briefing_source_ids(manifest)
    review_required = bool(briefing.get("review_required"))
    decisions = _context_decisions(events)
    latest_decision = decisions[-1] if decisions else None
    decision_signatures = {_computed_context_decision_signature(event) for event in decisions}
    decision_validation_errors = [
        error
        for event in decisions
        for error in _context_decision_validation_errors(event, manifest, presented)
    ]
    disposition = latest_decision["headers"].get(HEADER_CONTEXT_DISPOSITION) if latest_decision else None
    transition_conflict = bool(
        consume_after_decision
        or len(decision_signatures) > 1
        or (disposition in {"no_relevant", "skipped"} and consumed)
        or decision_validation_errors
        or source_validation_errors
        or session_validation_errors
    )
    presented_set = set(presented)
    presented_consumed = [source_id for source_id in consumed if source_id in presented_set]
    additional_consumed = [source_id for source_id in consumed if source_id not in presented_set]
    declared_reviewed = latest_decision["reviewed_source_ids"] if latest_decision else []
    declared_dismissed = latest_decision["dismissed_source_ids"] if latest_decision else []
    reviewed = [
        source_id
        for source_id in presented
        if source_id in set([*declared_reviewed, *presented_consumed])
    ]
    dismissed = [
        source_id
        for source_id in presented
        if source_id in set(declared_dismissed) and source_id not in set(presented_consumed)
    ]
    pending = [
        source_id
        for source_id in presented
        if source_id not in set(reviewed) and source_id not in set(dismissed)
    ]
    compatibility_use_complete = bool(presented) and set(presented).issubset(consumed_seen)
    compatibility_use_partial = bool(consumed) and disposition is None and not compatibility_use_complete
    if transition_conflict:
        review_status = "conflict"
    elif not presented:
        review_status = "not_applicable"
    elif not review_required:
        review_status = "legacy"
    elif disposition == "skipped":
        review_status = "skipped"
    elif disposition in {"used", "no_relevant"} and not pending:
        review_status = "complete"
    elif disposition is None and compatibility_use_complete:
        review_status = "complete"
    elif compatibility_use_partial:
        review_status = "compatibility_partial"
    else:
        review_status = "pending"
    decision_complete = review_status in {"complete", "not_applicable", "legacy"}
    finish_allowed = decision_complete or review_status in {"skipped", "compatibility_partial"}
    cited_without_use_enforced = review_required
    lineage_valid = bool(
        decision_complete
        and not transition_conflict
        and (not cited_without_use_enforced or not cited_without_use)
    )
    evidence_backed = [
        source_id
        for source_id in cited
        if source_by_id.get(source_id, {}).get("source_class") == "evidence"
    ]
    from .context_expansion import audit_context_expansion

    expansion = audit_context_expansion(
        root,
        manifest,
        events,
        presented_consumed,
    )
    return {
        "protocol": CONTEXT_PROTOCOL,
        "pack_id": pack_id,
        "task": manifest.get("task"),
        "session_id": manifest.get("session_id"),
        "retrieved_count": len(manifest["sources"]),
        "presented_count": len(presented),
        "reviewed_count": len(reviewed),
        "used_count": len(presented_consumed),
        "consumed_count": len(consumed),
        "additional_consumed_count": len(additional_consumed),
        "dismissed_count": len(dismissed),
        "pending_count": len(pending),
        "cited_count": len(cited),
        "cited_without_use_count": len(cited_without_use),
        "cited_without_use_enforced": cited_without_use_enforced,
        "evidence_backed_count": len(evidence_backed),
        "source_counts": manifest.get("source_counts", {}),
        "briefing": briefing,
        "review_required": review_required,
        "review_status": review_status,
        "decision": (
            "conflict"
            if transition_conflict
            else disposition
            or (
                "legacy_used"
                if compatibility_use_complete
                else "legacy_partial"
                if compatibility_use_partial
                else review_status
            )
        ),
        "decision_id": latest_decision["headers"].get(HEADER_CONTEXT_DECISION_ID) if latest_decision else None,
        "decision_revision": latest_decision["headers"].get(HEADER_CONTEXT_DECISION_REVISION) if latest_decision else None,
        "decision_reason": _event_body_value(latest_decision, "reason") if latest_decision else None,
        "decision_complete": decision_complete,
        "finish_allowed": finish_allowed,
        "lineage_valid": lineage_valid,
        "transition_conflict": transition_conflict,
        "decision_validation_errors": decision_validation_errors,
        "source_validation_errors": source_validation_errors,
        "session_validation_errors": session_validation_errors,
        "session_attribution_enforced": session_attribution_enforced,
        "legacy_session_mismatches": legacy_session_mismatches,
        "decision_event_count": len(decisions),
        "presented_source_ids": presented,
        "reviewed_source_ids": reviewed,
        "used_source_ids": presented_consumed,
        "consumed_source_ids": consumed,
        "additional_consumed_source_ids": additional_consumed,
        "dismissed_source_ids": dismissed,
        "pending_source_ids": pending,
        "cited_source_ids": cited,
        "cited_without_use_source_ids": cited_without_use,
        "evidence_backed_source_ids": evidence_backed,
        "expansion": expansion,
        "events": events,
        "enforcement_mode": CONTEXT_ENFORCEMENT_MODE,
    }


def format_context_pack(pack: dict[str, Any]) -> str:
    lines = [
        "# AgentDir Context Pack",
        "",
        f"Task: {pack['task']}",
        f"Retrieval query: {_pack_retrieval_query(pack) or '[no specific terms]'}",
        f"Session: {pack.get('session_id') or 'none'}",
    ]

    summary = pack.get("current_summary")
    if summary:
        lines.extend(["", "## Current Session", "", fenced(format_summary(summary), "text")])

    lines.extend(["", "## Relevant Memory"])
    memory_hits = pack.get("memory_hits") or []
    if memory_hits:
        for row in memory_hits:
            lines.extend(
                [
                    "",
                    f"- score={row.get('memory_score')} source={row.get('source_id')} kind={row.get('source_kind')}",
                    f"  session={row.get('session_id') or ''} event={row.get('event_type') or ''}",
                    f"  subject={row.get('subject') or ''}",
                    f"  excerpt={excerpt(row.get('passage_body_text') or row.get('body_text') or '', 320)}",
                ]
            )
    else:
        lines.extend(["", "No relevant memory found."])

    evidence = pack.get("evidence") or []
    if evidence:
        lines.extend(["", "## Current Evidence", "", fenced(format_evidence(evidence), "text")])

    recent = pack.get("recent_session_summaries") or []
    if recent:
        lines.extend(["", "## Recent Session Summaries"])
        for row in recent:
            lines.extend(
                [
                    "",
                    f"- source={row.get('source_id')} session={row.get('session_id') or ''}",
                    f"  {excerpt(row.get('body_text') or '', 420)}",
                ]
            )

    lines.extend(["", "## Agent Instructions"])
    for instruction in pack["instructions"]:
        lines.append(f"- {instruction}")
    return "\n".join(lines).rstrip() + "\n"


def write_context_pack(path: str | Path, pack: dict[str, Any], *, as_json: bool = False) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    if as_json:
        atomic_write_text(target, json.dumps(pack, indent=2, sort_keys=True) + "\n")
    else:
        atomic_write_text(target, format_context_pack(pack))
    return target


def format_context_citation(citation: dict[str, Any], *, output_format: str = "md") -> str:
    if output_format == "json":
        payload = {key: value for key, value in citation.items() if key != "rendered"}
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if output_format != "md":
        raise AgentDirError(f"Unknown citation format: {output_format}")
    lines = [
        "# AgentDir Context Citations",
        "",
        f"Pack: {citation['pack_id']}",
        f"Enforcement: {citation['enforcement_mode']}",
        "",
    ]
    for source in citation["sources"]:
        lines.append(
            f"- `{source['source_class']}` `{source['source_id']}` "
            f"{source.get('event_type') or ''} {source.get('subject') or ''}".rstrip()
        )
        if source.get("file_path"):
            lines.append(f"  file: `{source['file_path']}`")
        if source.get("excerpt"):
            lines.append(f"  excerpt: {source['excerpt']}")
    return "\n".join(lines).rstrip() + "\n"


def format_context_audit(audit: dict[str, Any]) -> str:
    expansion = audit.get("expansion") or {}
    lines = [
        f"pack_id={audit['pack_id']}",
        f"task={audit.get('task') or ''}",
        f"session={audit.get('session_id') or ''}",
        f"retrieved={audit['retrieved_count']}",
        f"presented={audit['presented_count']}",
        f"reviewed={audit['reviewed_count']}",
        f"used={audit['used_count']}",
        f"consumed={audit['consumed_count']}",
        f"additional_consumed={audit.get('additional_consumed_count', 0)}",
        f"dismissed={audit['dismissed_count']}",
        f"pending={audit['pending_count']}",
        f"cited={audit['cited_count']}",
        f"cited_without_use={audit['cited_without_use_count']}",
        f"review_status={audit['review_status']}",
        f"decision={audit['decision']}",
        f"lineage_valid={str(bool(audit.get('lineage_valid'))).lower()}",
        f"evidence_backed={audit['evidence_backed_count']}",
        f"expanded={expansion.get('expanded_source_count', 0)}",
        f"expanded_before_decision={expansion.get('expanded_before_decision_count', 0)}",
        f"expanded_after_decision={expansion.get('expanded_after_decision_count', 0)}",
        f"used_without_prior_expansion={expansion.get('used_without_prior_expansion_count', 0)}",
        f"expansion_receipts={expansion.get('receipt_event_count', 0)}",
        f"expansion_receipts_valid={str(expansion.get('receipts_valid', True)).lower()}",
        f"enforcement={audit['enforcement_mode']}",
    ]
    for error in audit.get("decision_validation_errors") or []:
        lines.append(f"decision_error={error}")
    for error in expansion.get("validation_errors") or []:
        lines.append(f"expansion_error={error}")
    for event in audit["events"]:
        lines.append(
            f"{event.get('date_utc') or event.get('indexed_at') or ''} "
            f"{event['event_type']} sources={len(event['source_ids'])}"
        )
    return "\n".join(lines)


def fenced(text: str, language: str) -> str:
    return f"```{language}\n{text}\n```"


def excerpt(text: str, limit: int) -> str:
    collapsed = " ".join(text.strip().split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_pack_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"ctx-{stamp}-{uuid4().hex[:12]}"


def _source_entry(
    row: dict[str, Any],
    *,
    origin: str,
    task_terms: list[str],
    retrieval_mode: str,
) -> dict[str, Any]:
    source_id = _source_id(row)
    body = row.get("passage_body_text") or row.get("body_text") or ""
    searchable = " ".join([str(row.get("subject") or ""), body])
    overlap_terms = [term for term in task_terms if term in set(_context_terms(searchable))]
    match_quality, match_reasons = _match_quality(
        origin=origin,
        memory_score=row.get("memory_score"),
        overlap_terms=overlap_terms,
        task_term_count=len(task_terms),
        retrieval_mode=str(row.get("retrieval_mode") or retrieval_mode),
    )
    return {
        "source_id": source_id,
        "source_kind": row.get("source_kind") or "message",
        "source_class": _source_class(row, origin),
        "origin": origin,
        "message_id": row.get("message_id"),
        "message_rowid": row.get("message_rowid") or row.get("id"),
        "session_id": row.get("session_id"),
        "event_type": row.get("event_type"),
        "subject": row.get("subject"),
        "from_actor": row.get("from_actor"),
        "to_actor": row.get("to_actor"),
        "task_id": row.get("task_id"),
        "tool": row.get("tool"),
        "tool_exit_code": row.get("tool_exit_code"),
        "git_head": row.get("git_head"),
        "workspace": row.get("workspace"),
        "source_root_id": row.get("source_root_id"),
        "source_root_name": row.get("source_root_name"),
        "source_root_path": row.get("source_root_path"),
        "source_root_visibility": row.get("source_root_visibility"),
        "source_id_original": row.get("source_id_original"),
        "source_file_path": row.get("source_file_path"),
        "date_utc": row.get("date_utc"),
        "indexed_at": row.get("indexed_at"),
        "file_path": row.get("file_path"),
        "memory_score": row.get("memory_score"),
        "retrieval_mode": row.get("retrieval_mode") or retrieval_mode,
        "requested_retrieval_mode": row.get("requested_retrieval_mode"),
        "semantic_score": row.get("semantic_score"),
        "hybrid_score": row.get("hybrid_score"),
        "document_score": row.get("document_score"),
        "fusion_support_weight": row.get("fusion_support_weight"),
        "match_quality": match_quality,
        "match_reasons": match_reasons,
        "overlap_terms": overlap_terms,
        "body_sha256": row.get("body_sha256"),
        "text_sha256": row.get("text_sha256"),
        "passage_ordinal": row.get("passage_ordinal"),
        "passage_text_sha256": row.get("passage_text_sha256"),
        "excerpt": excerpt(body, 320),
    }


def brief_context_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    briefing = manifest.get("briefing") or _build_context_briefing(
        manifest.get("sources") or [],
        _pack_retrieval_query(manifest),
        retrieval_mode=manifest.get("retrieval_mode") or RETRIEVAL_HYBRID,
        retrieval_query_state=manifest.get("retrieval_query_state")
        or ("specific_terms" if _pack_retrieval_query(manifest) else "no_specific_terms"),
    )
    source_by_id = {source["source_id"]: source for source in manifest.get("sources") or []}
    presented: list[dict[str, Any]] = []
    for index, source_id in enumerate(briefing.get("source_ids") or [], start=1):
        source = source_by_id.get(source_id)
        if source is None:
            continue
        presented.append(
            {
                "ref": str(index),
                "source_id": source_id,
                "source_kind": source.get("source_kind"),
                "source_class": source.get("source_class"),
                "match_quality": source.get("match_quality") or "unknown",
                "match_reasons": source.get("match_reasons") or [],
                "memory_score": source.get("memory_score"),
                "retrieval_mode": source.get("retrieval_mode"),
                "requested_retrieval_mode": source.get("requested_retrieval_mode"),
                "semantic_score": source.get("semantic_score"),
                "hybrid_score": source.get("hybrid_score"),
                "session_id": source.get("session_id"),
                "event_type": source.get("event_type"),
                "subject": source.get("subject"),
                "excerpt": source.get("excerpt") or "",
                "excerpt_is_preview": True,
                "excerpt_truncated": str(source.get("excerpt") or "").endswith("..."),
            }
        )
    return {
        **briefing,
        "pack_id": manifest.get("pack_id"),
        "sources": presented,
    }


def _build_context_briefing(
    sources: list[dict[str, Any]],
    retrieval_query: str,
    *,
    retrieval_mode: str,
    retrieval_query_state: str,
    limit: int = CONTEXT_BRIEFING_LIMIT,
) -> dict[str, Any]:
    presented = _select_briefing_sources(sources, limit)
    prior_qualities = [
        source.get("match_quality")
        for source in presented
        if source.get("match_quality") != "current"
    ]
    if retrieval_query_state == "disabled":
        match_state = "context_disabled"
    elif "strong" in prior_qualities:
        match_state = "strong_prior_context"
    elif presented:
        match_state = "no_strong_prior_context"
    else:
        match_state = "no_context_available"
    quality_counts: dict[str, int] = {}
    for source in presented:
        quality = str(source.get("match_quality") or "unknown")
        quality_counts[quality] = quality_counts.get(quality, 0) + 1
    return {
        "protocol": CONTEXT_BRIEFING_PROTOCOL,
        "quality_policy": _quality_policy(retrieval_mode, limit=limit),
        "retrieval_query": retrieval_query,
        "retrieval_query_state": retrieval_query_state,
        "match_state": match_state,
        "source_ids": [source["source_id"] for source in presented],
        "presented_count": len(presented),
        "omitted_count": max(0, len(sources) - len(presented)),
        "quality_counts": quality_counts,
        "review_required": bool(presented),
    }


def _quality_policy(retrieval_mode: str, *, limit: int) -> dict[str, Any]:
    thresholds = CONTEXT_QUALITY_THRESHOLDS.get(
        retrieval_mode,
        CONTEXT_QUALITY_THRESHOLDS[RETRIEVAL_HYBRID],
    )
    return {
        "id": CONTEXT_QUALITY_POLICY,
        "retrieval_mode": retrieval_mode,
        "score_thresholds": dict(thresholds),
        "strong_lexical_rule": {
            "requires_specific_overlap": True,
            "minimum_overlap_terms": 2,
            "or_minimum_coverage": 0.5,
        },
        "possible_lexical_rule": {
            "minimum_overlap_terms": 2,
            "or_minimum_coverage": 0.5,
        },
        "no_specific_terms_quality": "weak",
        "briefing_limit": limit,
        "quality_order": list(CONTEXT_QUALITY_ORDER),
        "source_preference_order": list(CONTEXT_SOURCE_PREFERENCE_ORDER),
        "source_selection_tiers": [list(tier) for tier in CONTEXT_SOURCE_SELECTION_TIERS],
        "redundant_with_decision_or_evidence_session": list(
            CONTEXT_REDUNDANT_WITH_DECISION_SESSION
        ),
        "max_per_session": CONTEXT_MAX_PER_SESSION,
        "max_per_class_first_pass": CONTEXT_MAX_PER_CLASS_FIRST_PASS,
        "search_candidate_multiplier": CONTEXT_SEARCH_CANDIDATE_MULTIPLIER,
        "token_pattern": CONTEXT_TOKEN_RE.pattern,
        "blocked_terms": sorted(CONTEXT_GENERIC_TERMS),
    }


def _select_briefing_sources(
    sources: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    candidates_by_preference = _prefer_context_sources(sources)
    quality_order = CONTEXT_QUALITY_ORDER
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    session_counts: dict[str, int] = {}
    class_counts: dict[str, int] = {}

    def add(source: dict[str, Any]) -> None:
        selected.append(source)
        selected_ids.add(source["source_id"])
        session = str(source.get("session_id") or source["source_id"])
        source_class = str(source.get("source_class") or "retrieval_hint")
        session_counts[session] = session_counts.get(session, 0) + 1
        class_counts[source_class] = class_counts.get(source_class, 0) + 1

    for tier in CONTEXT_SOURCE_SELECTION_TIERS:
        preferred = [
            source
            for source in candidates_by_preference
            if _source_preference(source) in tier
        ]
        for quality in quality_order:
            candidates = [
                source
                for source in preferred
                if (source.get("match_quality") or "unknown") == quality
            ]
            for source in candidates:
                session = str(source.get("session_id") or source["source_id"])
                source_class = str(source.get("source_class") or "retrieval_hint")
                if source["source_id"] in selected_ids:
                    continue
                if (
                    session_counts.get(session, 0)
                    or class_counts.get(source_class, 0) >= CONTEXT_MAX_PER_CLASS_FIRST_PASS
                ):
                    continue
                add(source)
                if len(selected) >= limit:
                    return selected
            for source in candidates:
                if source["source_id"] in selected_ids:
                    continue
                session = str(source.get("session_id") or source["source_id"])
                if session_counts.get(session, 0) >= CONTEXT_MAX_PER_SESSION:
                    continue
                add(source)
                if len(selected) >= limit:
                    return selected
    for source in candidates_by_preference:
        if source["source_id"] in selected_ids:
            continue
        session = str(source.get("session_id") or source["source_id"])
        if session_counts.get(session, 0) >= CONTEXT_MAX_PER_SESSION:
            continue
        add(source)
        if len(selected) >= limit:
            break
    return selected


def _prefer_context_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decision_sessions = {
        str(source.get("session_id"))
        for source in sources
        if source.get("session_id")
        and _source_preference(source) in {"current_evidence", "evidence", "decision"}
    }
    preferred = [
        source
        for source in sources
        if not (
            source.get("session_id")
            and str(source.get("session_id")) in decision_sessions
            and _source_preference(source) in CONTEXT_REDUNDANT_WITH_DECISION_SESSION
        )
    ]
    tier_index = {
        preference: index
        for index, tier in enumerate(CONTEXT_SOURCE_SELECTION_TIERS)
        for preference in tier
    }
    return sorted(
        preferred,
        key=lambda source: (
            tier_index[_source_preference(source)],
            -float(source.get("memory_score") or 0.0),
        ),
    )


def _source_preference(source: dict[str, Any]) -> str:
    event_type = str(source.get("event_type") or "")
    source_kind = str(source.get("source_kind") or "")
    source_class = str(source.get("source_class") or "")
    origin = str(source.get("origin") or "")
    if origin == "evidence":
        return "current_evidence"
    if source_class == "evidence" or event_type in EVIDENCE_EVENTS or event_type == "claim.recorded":
        return "evidence"
    if event_type.startswith("decision."):
        return "decision"
    if source_kind == "session_summary" or event_type == "summary.compacted":
        return "summary"
    if event_type == "work.report.final":
        return "final_report"
    if event_type in LIFECYCLE_EVENTS or event_type.startswith(("session.", "context.")):
        return "lifecycle"
    return "substantive"


def _match_quality(
    *,
    origin: str,
    memory_score: Any,
    overlap_terms: list[str],
    task_term_count: int,
    retrieval_mode: str,
) -> tuple[str, list[str]]:
    if origin == "evidence":
        return "current", ["current-session evidence"]
    score = float(memory_score or 0.0)
    policy = CONTEXT_QUALITY_THRESHOLDS.get(
        retrieval_mode,
        CONTEXT_QUALITY_THRESHOLDS[RETRIEVAL_HYBRID],
    )
    coverage = len(overlap_terms) / max(task_term_count, 1)
    has_specific_terms = task_term_count > 0
    semantic_only_strong = policy["semantic_only_strong"]
    lexical_strong = bool(
        overlap_terms
        and score >= float(policy["strong"])
        and (len(overlap_terms) >= 2 or coverage >= 0.5)
    )
    semantic_strong = bool(
        has_specific_terms
        and semantic_only_strong is not None
        and score >= float(semantic_only_strong)
    )
    if lexical_strong or semantic_strong:
        quality = "strong"
    elif has_specific_terms and (
        score >= float(policy["possible"])
        or len(overlap_terms) >= 2
        or coverage >= 0.5
    ):
        quality = "possible"
    else:
        quality = "weak"
    reasons = [f"score {score:.3f}"] if memory_score is not None else ["unranked recent source"]
    if overlap_terms:
        reasons.append(f"specific terms: {', '.join(overlap_terms[:5])}")
    elif semantic_strong:
        reasons.append(f"semantic-only signal ({retrieval_mode})")
    else:
        reasons.append("no specific task-term overlap")
    return quality, reasons


def _source_id(row: dict[str, Any]) -> str:
    if row.get("source_id"):
        return str(row["source_id"])
    if row.get("file_path"):
        return f"message:{row['file_path']}"
    if row.get("message_id"):
        return f"message-id:{row['message_id']}"
    return f"source:{uuid4().hex}"


def _source_class(row: dict[str, Any], origin: str) -> str:
    event_type = str(row.get("event_type") or "")
    source_kind = str(row.get("source_kind") or "")
    if origin == "evidence" or event_type in EVIDENCE_EVENTS or event_type.startswith("git.hook."):
        return "evidence"
    if source_kind == "session_summary" or event_type == "summary.compacted":
        return "summary"
    return "retrieval_hint"


def _context_retrieval_query(task: str, workspace: str | None) -> str:
    workspace_terms = set(_raw_context_terms(workspace or ""))
    terms = _context_terms(task, excluded=workspace_terms)
    return " ".join(terms)


def _pack_retrieval_query(pack: dict[str, Any]) -> str:
    if "retrieval_query" in pack:
        return str(pack.get("retrieval_query") or "")
    return str(pack.get("task") or "")


def _context_terms(text: str, *, excluded: set[str] | None = None) -> list[str]:
    blocked = set(excluded or set()) | CONTEXT_GENERIC_TERMS
    seen: set[str] = set()
    terms: list[str] = []
    for term in _raw_context_terms(text):
        if len(term) <= 1 or term in blocked or term in seen:
            continue
        seen.add(term)
        terms.append(term)
    return terms


def _raw_context_terms(text: str) -> list[str]:
    return [term.lower() for term in CONTEXT_TOKEN_RE.findall(text)]


def _diversify_memory_hits(hits: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    hits = _prefer_context_sources(hits)
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    session_counts: dict[str, int] = {}

    def keys(row: dict[str, Any]) -> tuple[str, str]:
        source_id = str(row.get("source_id") or row.get("file_path") or id(row))
        session = str(row.get("session_id") or source_id)
        return source_id, session

    def add(row: dict[str, Any]) -> None:
        source_id, session = keys(row)
        selected.append(row)
        selected_ids.add(source_id)
        session_counts[session] = session_counts.get(session, 0) + 1

    for tier in CONTEXT_SOURCE_SELECTION_TIERS:
        preferred = [row for row in hits if _source_preference(row) in tier]
        for row in preferred:
            source_id, session = keys(row)
            if source_id in selected_ids or session_counts.get(session, 0):
                continue
            add(row)
            if len(selected) >= limit:
                return selected
        for row in preferred:
            source_id, session = keys(row)
            per_session_limit = (
                1 if _source_preference(row) == "evidence" else CONTEXT_MAX_PER_SESSION
            )
            if source_id in selected_ids or session_counts.get(session, 0) >= per_session_limit:
                continue
            add(row)
            if len(selected) >= limit:
                return selected
    for row in hits:
        source_id, _ = keys(row)
        if source_id not in selected_ids:
            add(row)
            if len(selected) >= limit:
                break
    return selected


def _dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for source in sources:
        source_id = source["source_id"]
        if source_id in seen:
            continue
        seen.add(source_id)
        deduped.append(source)
    return deduped


def _source_counts(sources: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"evidence": 0, "retrieval_hint": 0, "summary": 0}
    for source in sources:
        source_class = source.get("source_class") or "retrieval_hint"
        counts[source_class] = counts.get(source_class, 0) + 1
    return counts


def _write_manifest_temp(manifest: dict[str, Any]) -> Path:
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".json")
    with handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return Path(handle.name)


def _context_events(
    root: str | Path,
    pack_id: str,
    *,
    event_type: str | None = None,
    rebuild: bool = True,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
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


def _header_map(rows: list[Any]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for row in rows:
        name = row["name"]
        value = row["value"]
        if name not in mapped:
            mapped[name] = value
    return mapped


def _select_manifest_sources(
    manifest: dict[str, Any],
    source_ids: list[str],
) -> list[dict[str, Any]]:
    if not source_ids:
        raise AgentDirError("At least one context source is required")
    source_by_id = {source["source_id"]: source for source in manifest["sources"]}
    missing = [source_id for source_id in source_ids if source_id not in source_by_id]
    if missing:
        raise AgentDirError("Unknown context source: " + ", ".join(missing))
    return [source_by_id[source_id] for source_id in _unique_source_ids(source_ids)]


def _select_manifest_sources_by_selector(
    manifest: dict[str, Any],
    selectors: list[str],
) -> list[dict[str, Any]]:
    if not selectors:
        raise AgentDirError("At least one context source is required")
    briefing_source_ids = _briefing_source_ids(manifest)
    resolved: list[str] = []
    for selector in selectors:
        normalized = selector.strip().lower()
        ordinal = normalized[1:] if normalized.startswith("s") else normalized
        if ordinal.isdigit():
            index = int(ordinal) - 1
            if index < 0 or index >= len(briefing_source_ids):
                raise AgentDirError(f"Unknown context source reference: {selector}")
            resolved.append(briefing_source_ids[index])
        else:
            resolved.append(selector)
    not_presented = [source_id for source_id in resolved if source_id not in set(briefing_source_ids)]
    if not_presented:
        raise AgentDirError(
            "Context source was retrieved but not presented in this briefing: "
            + ", ".join(not_presented)
            + ". Use the lower-level `context consume` command for unpresented sources."
        )
    selected = _select_manifest_sources(manifest, resolved)
    selected_by_id = {source["source_id"]: source for source in selected}
    return [selected_by_id[source_id] for source_id in briefing_source_ids if source_id in selected_by_id]


def _briefing_source_ids(manifest: dict[str, Any]) -> list[str]:
    briefing = manifest.get("briefing") or {}
    source_ids = briefing.get("source_ids")
    if source_ids is not None:
        return _unique_source_ids(source_ids)
    return [source["source_id"] for source in manifest.get("sources") or []]


def _default_citation_sources(
    manifest: dict[str, Any],
    context_audit: dict[str, Any],
) -> list[str]:
    used = context_audit["consumed_source_ids"]
    if used:
        return used
    if not context_audit["review_required"]:
        return [source["source_id"] for source in manifest.get("sources") or []]
    raise AgentDirStateError(
        "No used context sources to cite; record context use or consume a source first"
    )


def _unique_source_ids(source_ids: Any) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for source_id in source_ids:
        if source_id in seen:
            continue
        seen.add(source_id)
        unique.append(source_id)
    return unique


def _latest_context_decision(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    decisions = _context_decisions(events)
    return decisions[-1] if decisions else None


def _context_decisions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event.get("headers", {}).get(HEADER_CONTEXT_DISPOSITION)]


def _same_context_decision(
    event: dict[str, Any],
    *,
    decision_id: str,
    disposition: str,
    purpose: str | None,
    reason: str,
    reviewed_source_ids: list[str],
    used_source_ids: list[str],
    dismissed_source_ids: list[str],
) -> bool:
    headers = event.get("headers") or {}
    stored_decision_id = headers.get(HEADER_CONTEXT_DECISION_ID)
    if stored_decision_id != decision_id:
        return False
    if headers.get(HEADER_CONTEXT_DECISION_REVISION) != "1":
        return False
    return (
        _computed_context_decision_signature(event) == decision_id
        and headers.get(HEADER_CONTEXT_DISPOSITION) == disposition
        and headers.get(HEADER_CONSUMPTION_PURPOSE) == purpose
        and _event_body_value(event, "reason") == reason
        and event.get("reviewed_source_ids") == reviewed_source_ids
        and event.get("used_source_ids") == used_source_ids
        and event.get("dismissed_source_ids") == dismissed_source_ids
    )


def _computed_context_decision_signature(event: dict[str, Any]) -> str:
    headers = event.get("headers") or {}
    return _context_decision_id(
        pack_id=headers.get(HEADER_PACK_ID) or "",
        disposition=headers.get(HEADER_CONTEXT_DISPOSITION) or "",
        purpose=headers.get(HEADER_CONSUMPTION_PURPOSE),
        reason=_event_body_value(event, "reason") or "",
        reviewed_source_ids=event.get("reviewed_source_ids") or [],
        used_source_ids=event.get("used_source_ids") or [],
        dismissed_source_ids=event.get("dismissed_source_ids") or [],
    )


def _context_decision_validation_errors(
    event: dict[str, Any],
    manifest: dict[str, Any],
    presented_source_ids: list[str],
) -> list[str]:
    headers = event.get("headers") or {}
    disposition = headers.get(HEADER_CONTEXT_DISPOSITION) or ""
    purpose = headers.get(HEADER_CONSUMPTION_PURPOSE)
    reason = _event_body_value(event, "reason") or ""
    reviewed = event.get("reviewed_source_ids") or []
    used = event.get("used_source_ids") or []
    dismissed = event.get("dismissed_source_ids") or []
    event_label = str(event.get("file_path") or event.get("message_id") or "decision event")
    errors: list[str] = []

    expected_id = _context_decision_id(
        pack_id=headers.get(HEADER_PACK_ID) or "",
        disposition=disposition,
        purpose=purpose,
        reason=reason,
        reviewed_source_ids=reviewed,
        used_source_ids=used,
        dismissed_source_ids=dismissed,
    )
    stored_id = headers.get(HEADER_CONTEXT_DECISION_ID)
    if not stored_id:
        errors.append(f"{event_label}: decision id is missing")
    elif stored_id != expected_id:
        errors.append(f"{event_label}: decision id does not match its payload")
    revision = headers.get(HEADER_CONTEXT_DECISION_REVISION)
    if revision is None:
        errors.append(f"{event_label}: decision revision is missing")
    elif revision != "1":
        errors.append(f"{event_label}: unsupported decision revision {revision!r}")
    if headers.get(HEADER_PACK_ID) != manifest.get("pack_id"):
        errors.append(f"{event_label}: decision pack id does not match the manifest")
    if not reason:
        errors.append(f"{event_label}: decision reason is missing")
    if disposition not in CONTEXT_DISPOSITIONS:
        errors.append(f"{event_label}: unknown context disposition {disposition!r}")

    source_ids = [source["source_id"] for source in manifest.get("sources") or []]
    source_set = set(source_ids)
    presented_set = set(presented_source_ids)
    for label, values, allowed in (
        ("reviewed", reviewed, presented_set),
        ("used", used, source_set),
        ("dismissed", dismissed, presented_set),
    ):
        unknown = [source_id for source_id in values if source_id not in allowed]
        if unknown:
            errors.append(f"{event_label}: {label} sources are outside the allowed set: {', '.join(unknown)}")
        if len(values) != len(set(values)):
            errors.append(f"{event_label}: {label} sources contain duplicates")

    presented_used = [source_id for source_id in presented_source_ids if source_id in set(used)]
    expected_dismissed = [
        source_id for source_id in presented_source_ids if source_id not in set(presented_used)
    ]
    expected_event_type = (
        EVENT_CONTEXT_PACK_CONSUMED if disposition == "used" else EVENT_CONTEXT_PACK_REVIEWED
    )
    if event.get("event_type") != expected_event_type:
        errors.append(
            f"{event_label}: disposition {disposition!r} requires event type {expected_event_type}"
        )
    if disposition == "used":
        if not used:
            errors.append(f"{event_label}: used decision must include at least one used source")
        if reviewed != presented_source_ids:
            errors.append(f"{event_label}: used decision must review every presented source")
        if dismissed != expected_dismissed:
            errors.append(f"{event_label}: used decision dismissal set is inconsistent")
        if purpose not in CONSUMPTION_PURPOSES:
            errors.append(f"{event_label}: used decision has an invalid purpose")
    elif disposition == "no_relevant":
        if used:
            errors.append(f"{event_label}: no-relevant decision cannot include used sources")
        if reviewed != presented_source_ids or dismissed != presented_source_ids:
            errors.append(
                f"{event_label}: no-relevant decision must review and dismiss every presented source"
            )
        if purpose is not None:
            errors.append(f"{event_label}: no-relevant decision cannot include a purpose")
    elif disposition == "skipped":
        if reviewed or used or dismissed:
            errors.append(f"{event_label}: skipped decision cannot declare source actions")
        if purpose is not None:
            errors.append(f"{event_label}: skipped decision cannot include a purpose")
    return errors


def _context_decision_id(
    *,
    pack_id: str,
    disposition: str,
    purpose: str | None,
    reason: str,
    reviewed_source_ids: list[str],
    used_source_ids: list[str],
    dismissed_source_ids: list[str],
) -> str:
    payload = {
        "pack_id": pack_id,
        "disposition": disposition,
        "purpose": purpose,
        "reason": reason,
        "reviewed_source_ids": reviewed_source_ids,
        "used_source_ids": used_source_ids,
        "dismissed_source_ids": dismissed_source_ids,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"ctxd-{digest[:24]}"


def _event_body_value(event: dict[str, Any] | None, key: str) -> str | None:
    if not event:
        return None
    prefix = f"{key}="
    for line in str(event.get("body_text") or "").splitlines():
        if line.startswith(prefix):
            return line.split("=", 1)[1].strip() or None
    return None


def _format_context_review_body(
    *,
    pack_id: str,
    disposition: str,
    purpose: str | None,
    reason: str,
    presented_count: int,
    reviewed_source_ids: list[str],
    used_source_ids: list[str],
    dismissed_source_ids: list[str],
) -> str:
    lines = [
        "action=context_review",
        f"pack_id={pack_id}",
        f"disposition={disposition}",
    ]
    if purpose:
        lines.append(f"purpose={purpose}")
    lines.extend(
        [
            f"reason={reason}",
            f"presented={presented_count}",
            f"reviewed={len(reviewed_source_ids)}",
            f"used={len(used_source_ids)}",
            f"dismissed={len(dismissed_source_ids)}",
            "",
            "Context review is a cooperative declaration; AgentDir cannot prove model attention.",
        ]
    )
    return "\n".join(lines)


def _context_review_result(
    *,
    pack_id: str,
    decision_id: str,
    revision: int,
    disposition: str,
    purpose: str | None,
    reason: str,
    reviewed_source_ids: list[str],
    used_source_ids: list[str],
    dismissed_source_ids: list[str],
    event_path: str | None,
    recorded: bool,
) -> dict[str, Any]:
    reviewed_set = set(reviewed_source_ids)
    presented_used = [source_id for source_id in used_source_ids if source_id in reviewed_set]
    additional_consumed = [source_id for source_id in used_source_ids if source_id not in reviewed_set]
    return {
        "pack_id": pack_id,
        "decision_id": decision_id,
        "revision": revision,
        "disposition": disposition,
        "purpose": purpose,
        "reason": reason,
        "reviewed_count": len(reviewed_source_ids),
        "used_count": len(presented_used),
        "consumed_count": len(used_source_ids),
        "additional_consumed_count": len(additional_consumed),
        "dismissed_count": len(dismissed_source_ids),
        "reviewed_source_ids": reviewed_source_ids,
        "used_source_ids": presented_used,
        "consumed_source_ids": used_source_ids,
        "additional_consumed_source_ids": additional_consumed,
        "dismissed_source_ids": dismissed_source_ids,
        "event_path": event_path,
        "recorded": recorded,
        "enforcement_mode": CONTEXT_ENFORCEMENT_MODE,
    }


def _canonical_source_ids(source_ids: list[str], manifest: dict[str, Any]) -> list[str]:
    unique = set(_unique_source_ids(source_ids))
    return [
        source["source_id"]
        for source in manifest.get("sources") or []
        if source["source_id"] in unique
    ]


def _format_source_event_body(
    action: str,
    pack_id: str,
    purpose: str | None,
    sources: list[dict[str, Any]],
) -> str:
    lines = [
        f"action={action}",
        f"pack_id={pack_id}",
    ]
    if purpose:
        lines.append(f"purpose={purpose}")
    lines.append(f"sources={len(sources)}")
    lines.append("")
    for source in sources:
        lines.append(
            f"- {source['source_class']} {source['source_id']} "
            f"{source.get('event_type') or ''} {source.get('subject') or ''}".rstrip()
        )
    lines.extend(["", "Context protocol is advisory; source use is recorded for cooperative agents."])
    return "\n".join(lines)
