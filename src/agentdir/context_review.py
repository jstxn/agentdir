from __future__ import annotations

import hashlib
import json
from typing import Any

from .context_repository import (
    CONSUMPTION_PURPOSES,
    CONTEXT_DISPOSITIONS,
    CONTEXT_ENFORCEMENT_MODE,
    CONTEXT_PROTOCOL,
    EVENT_CONTEXT_PACK_CONSUMED,
    EVENT_CONTEXT_PACK_REVIEWED,
    EVENT_CONTEXT_SOURCES_CITED,
    HEADER_CONSUMPTION_PURPOSE,
    HEADER_CONTEXT_DECISION_ID,
    HEADER_CONTEXT_DECISION_REVISION,
    HEADER_CONTEXT_DISPOSITION,
    HEADER_PACK_ID,
)


def fold_context_review(
    manifest: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Fold immutable pack actions into one terminal-review projection."""
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
    presented = briefing_source_ids(manifest)
    review_required = bool(briefing.get("review_required"))
    decisions = context_decisions(events)
    latest_decision = decisions[-1] if decisions else None
    decision_signatures = {computed_context_decision_signature(event) for event in decisions}
    decision_validation_errors = [
        error
        for event in decisions
        for error in context_decision_validation_errors(event, manifest, presented)
    ]
    disposition = (
        latest_decision["headers"].get(HEADER_CONTEXT_DISPOSITION)
        if latest_decision
        else None
    )
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
    return {
        "protocol": CONTEXT_PROTOCOL,
        "pack_id": manifest["pack_id"],
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
        "decision_id": (
            latest_decision["headers"].get(HEADER_CONTEXT_DECISION_ID)
            if latest_decision
            else None
        ),
        "decision_revision": (
            latest_decision["headers"].get(HEADER_CONTEXT_DECISION_REVISION)
            if latest_decision
            else None
        ),
        "decision_reason": event_body_value(latest_decision, "reason") if latest_decision else None,
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
        "events": events,
        "enforcement_mode": CONTEXT_ENFORCEMENT_MODE,
    }


def briefing_source_ids(manifest: dict[str, Any]) -> list[str]:
    briefing = manifest.get("briefing") or {}
    source_ids = briefing.get("source_ids")
    if source_ids is not None:
        return unique_source_ids(source_ids)
    return [source["source_id"] for source in manifest.get("sources") or []]


def unique_source_ids(source_ids: Any) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for source_id in source_ids:
        if source_id in seen:
            continue
        seen.add(source_id)
        unique.append(source_id)
    return unique


def latest_context_decision(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    decisions = context_decisions(events)
    return decisions[-1] if decisions else None


def context_decisions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event.get("headers", {}).get(HEADER_CONTEXT_DISPOSITION)]


def same_context_decision(
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
        computed_context_decision_signature(event) == decision_id
        and headers.get(HEADER_CONTEXT_DISPOSITION) == disposition
        and headers.get(HEADER_CONSUMPTION_PURPOSE) == purpose
        and event_body_value(event, "reason") == reason
        and event.get("reviewed_source_ids") == reviewed_source_ids
        and event.get("used_source_ids") == used_source_ids
        and event.get("dismissed_source_ids") == dismissed_source_ids
    )


def context_decision_id(
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


def event_body_value(event: dict[str, Any] | None, key: str) -> str | None:
    if not event:
        return None
    prefix = f"{key}="
    for line in str(event.get("body_text") or "").splitlines():
        if line.startswith(prefix):
            return line.split("=", 1)[1].strip() or None
    return None


def format_context_review_body(
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


def context_review_result(
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


def canonical_source_ids(
    source_ids: list[str],
    manifest: dict[str, Any],
) -> list[str]:
    unique = set(unique_source_ids(source_ids))
    return [
        source["source_id"]
        for source in manifest.get("sources") or []
        if source["source_id"] in unique
    ]


def computed_context_decision_signature(event: dict[str, Any]) -> str:
    headers = event.get("headers") or {}
    return context_decision_id(
        pack_id=headers.get(HEADER_PACK_ID) or "",
        disposition=headers.get(HEADER_CONTEXT_DISPOSITION) or "",
        purpose=headers.get(HEADER_CONSUMPTION_PURPOSE),
        reason=event_body_value(event, "reason") or "",
        reviewed_source_ids=event.get("reviewed_source_ids") or [],
        used_source_ids=event.get("used_source_ids") or [],
        dismissed_source_ids=event.get("dismissed_source_ids") or [],
    )


def context_decision_validation_errors(
    event: dict[str, Any],
    manifest: dict[str, Any],
    presented_source_ids: list[str],
) -> list[str]:
    headers = event.get("headers") or {}
    disposition = headers.get(HEADER_CONTEXT_DISPOSITION) or ""
    purpose = headers.get(HEADER_CONSUMPTION_PURPOSE)
    reason = event_body_value(event, "reason") or ""
    reviewed = event.get("reviewed_source_ids") or []
    used = event.get("used_source_ids") or []
    dismissed = event.get("dismissed_source_ids") or []
    event_label = str(event.get("file_path") or event.get("message_id") or "decision event")
    errors: list[str] = []

    expected_id = context_decision_id(
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
            errors.append(
                f"{event_label}: {label} sources are outside the allowed set: {', '.join(unknown)}"
            )
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
