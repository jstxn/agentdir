from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .artifacts import add_artifact, artifact_headers, artifact_path
from .context_repository import (
    CONSUMPTION_PURPOSES,
    CONTEXT_BRIEFING_PROTOCOL,
    CONTEXT_DISPOSITIONS,
    CONTEXT_ENFORCEMENT_MODE,
    CONTEXT_PROTOCOL,
    EVENT_CONTEXT_PACK_CONSUMED,
    EVENT_CONTEXT_PACK_CREATED,
    EVENT_CONTEXT_PACK_REVIEWED,
    EVENT_CONTEXT_SOURCES_CITED,
    EVENT_CONTEXT_SOURCES_EXPANDED,
    HEADER_CONSUMPTION_PURPOSE,
    HEADER_CONTEXT_DECISION_ID,
    HEADER_CONTEXT_DECISION_REVISION,
    HEADER_CONTEXT_DISPOSITION,
    HEADER_CONTEXT_QUERY,
    HEADER_CONTEXT_SCOPE,
    HEADER_DISMISSED_SOURCE_ID,
    HEADER_ENFORCEMENT_MODE,
    HEADER_PACK_ID,
    HEADER_PROTOCOL,
    HEADER_REVIEWED_SOURCE_ID,
    HEADER_SOURCE_ID,
    HEADER_USED_SOURCE_ID,
    context_events as _context_events,
    context_pack_body_ids,
    context_pack_identity_error,
    read_context_manifest,
)
from .context_review import (
    briefing_source_ids as _briefing_source_ids,
    canonical_source_ids as _canonical_source_ids,
    context_decision_id as _context_decision_id,
    context_review_result as _context_review_result,
    event_body_value as _event_body_value,
    fold_context_review as _fold_context_review,
    format_context_review_body as _format_context_review_body,
    latest_context_decision as _latest_context_decision,
    same_context_decision as _same_context_decision,
    unique_source_ids as _unique_source_ids,
)
from .context_selection import (
    CONTEXT_BRIEFING_LIMIT,
    CONTEXT_GENERIC_TERMS,
    CONTEXT_MAX_PER_CLASS_FIRST_PASS,
    CONTEXT_MAX_PER_SESSION,
    CONTEXT_QUALITY_ORDER,
    CONTEXT_QUALITY_POLICY,
    CONTEXT_QUALITY_THRESHOLDS,
    CONTEXT_REDUNDANT_WITH_DECISION_SESSION,
    CONTEXT_SEARCH_CANDIDATE_MULTIPLIER,
    CONTEXT_SOURCE_PREFERENCE_ORDER,
    CONTEXT_SOURCE_SELECTION_TIERS,
    CONTEXT_TOKEN_RE,
    EVIDENCE_EVENTS,
    LIFECYCLE_EVENTS,
    MEMORY_STOPWORDS,
    brief_context_manifest,
    build_context_briefing as _build_context_briefing,
    diversify_memory_hits as _diversify_memory_hits,
    match_quality as _match_quality,
    pack_retrieval_query as _pack_retrieval_query,
    source_class as _source_class,
    source_role as _source_role,
)
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
    recent_session_summaries,
    resolve_retrieval_mode,
    search_memory,
)
from .query import query_messages
from .redaction import redact_text
from .review import evidence_rows, format_evidence, format_summary, summarize_session
from .sessions import read_current_session
from .store import AgentDirError, AgentDirStateError, paths_for, validate_id

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
            retrieval_query=retrieval_query,
            task_intent=task,
        )
        recent = [
            row
            for row in recent_session_summaries(root, limit=recent_limit + 5)
            if row.get("session_id") != resolved_session
        ][:recent_limit]
    else:
        memory_hits = _diversify_memory_hits(
            memory_hits,
            memory_limit,
            retrieval_query=retrieval_query,
            task_intent=task,
        )
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
        task_intent=str(pack.get("task") or retrieval_query),
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
    audit = _fold_context_review(manifest, events)

    from .context_expansion import audit_context_expansion

    return {
        **audit,
        "expansion": audit_context_expansion(
            root,
            manifest,
            events,
            audit["used_source_ids"],
        ),
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
        display_role = source.get("source_role") or source["source_class"]
        lines.append(
            f"- `{display_role}` `{source['source_id']}` "
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
    canonical_body = str(row.get("body_text") or "")
    passage_body = str(row.get("passage_body_text") or "")
    searchable_body = passage_body or canonical_body
    canonical_class = _source_class(row, origin)
    canonical_role = _source_role(row, origin)
    preview_body = (
        canonical_body
        if canonical_role in {"decision", "evidence"} and canonical_body.strip()
        else searchable_body
    )
    searchable = " ".join([str(row.get("subject") or ""), searchable_body])
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
        "source_class": canonical_class,
        "source_role": canonical_role,
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
        "excerpt": excerpt(preview_body, 320),
    }


def _source_id(row: dict[str, Any]) -> str:
    if row.get("source_id"):
        return str(row["source_id"])
    if row.get("file_path"):
        return f"message:{row['file_path']}"
    if row.get("message_id"):
        return f"message-id:{row['message_id']}"
    return f"source:{uuid4().hex}"


def _context_retrieval_query(task: str, workspace: str | None) -> str:
    workspace_terms = set(_raw_context_terms(workspace or ""))
    terms = _context_terms(task, excluded=workspace_terms)
    return " ".join(terms)


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
        display_role = source.get("source_role") or source["source_class"]
        lines.append(
            f"- {display_role} {source['source_id']} "
            f"{source.get('event_type') or ''} {source.get('subject') or ''}".rstrip()
        )
    lines.extend(["", "Context protocol is advisory; source use is recorded for cooperative agents."])
    return "\n".join(lines)
