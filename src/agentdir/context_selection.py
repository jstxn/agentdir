from __future__ import annotations

import re
from typing import Any

from .context_repository import CONTEXT_BRIEFING_PROTOCOL
from .memory import (
    RETRIEVAL_AUTO,
    RETRIEVAL_DOCUMENT,
    RETRIEVAL_HYBRID,
    RETRIEVAL_SEMANTIC,
    RETRIEVAL_SEMANTIC_HYBRID,
    STOPWORDS as MEMORY_STOPWORDS,
)

CONTEXT_QUALITY_POLICY = "agentdir.balanced.v6"
CONTEXT_BRIEFING_LIMIT = 5
CONTEXT_QUALITY_ORDER = ("strong", "possible", "current", "weak", "unknown")
CONTEXT_SOURCE_PREFERENCE_ORDER = (
    "current_evidence",
    "decision",
    "evidence",
    "substantive",
    "summary",
    "operational",
    "final_report",
    "lifecycle",
    "historical_tool",
)
CONTEXT_SOURCE_SELECTION_TIERS = (
    ("current_evidence",),
    ("decision", "evidence"),
    ("substantive",),
    ("summary",),
    ("operational",),
    ("final_report",),
    ("lifecycle",),
    ("historical_tool",),
)
CONTEXT_REDUNDANT_WITH_DECISION_SESSION = (
    "lifecycle",
    "final_report",
    "summary",
)
CONTEXT_MAX_PER_SESSION = 2
CONTEXT_MAX_PER_CLASS_FIRST_PASS = 2
CONTEXT_EXPLORATORY_LIMIT = 2
CONTEXT_SEARCH_CANDIDATE_MULTIPLIER = 12
MAX_SUMMARY_COUNT_DIGITS = 18
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
EVIDENCE_EVENTS = {"tool.call", "tool.result", "file.diff"}
ROUTINE_HISTORICAL_TOOL_EVENTS = {"tool.call", "tool.result"}
OPERATIONAL_EVENTS = {"claim.recorded"}
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
SUMMARY_LOW_SIGNAL_EVENTS = LIFECYCLE_EVENTS | OPERATIONAL_EVENTS | {"work.report.final"}
LOW_SIGNAL_PREFERENCES = {"operational", "final_report", "lifecycle"}
HARD_OMITTED_PREFERENCES = {"operational", "lifecycle"}
FINAL_REPORT_FALLBACK_QUALITIES = {"strong", "possible"}
HIGHER_SIGNAL_QUALITIES = {"strong", "possible", "current"}
LOW_SIGNAL_TARGET_TERMS = {
    "operational": {"claim", "claims", "hook", "hooks"},
    "final_report": {"handoff", "report", "reports"},
    "lifecycle": {"lifecycle", "session", "sessions"},
}
CONTEXT_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_+#]*", re.IGNORECASE)
WORK_START_INTENT = "__agentdir_work_start__"
WORK_FINISH_INTENT = "__agentdir_work_finish__"
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


def brief_context_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return the stable numbered view shared by display, review, and expansion."""
    retrieval_query = pack_retrieval_query(manifest)
    briefing = manifest.get("briefing") or build_context_briefing(
        manifest.get("sources") or [],
        retrieval_query,
        retrieval_mode=manifest.get("retrieval_mode") or RETRIEVAL_HYBRID,
        retrieval_query_state=manifest.get("retrieval_query_state")
        or ("specific_terms" if retrieval_query else "no_specific_terms"),
        task_intent=str(manifest.get("task") or retrieval_query),
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
                "source_role": source.get("source_role")
                or source_role(source, str(source.get("origin") or "memory")),
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
                "next_actions": _source_next_actions(source, retrieval_query),
            }
        )
    return {
        **briefing,
        "pack_id": manifest.get("pack_id"),
        "sources": presented,
    }


def build_context_briefing(
    sources: list[dict[str, Any]],
    retrieval_query: str,
    *,
    retrieval_mode: str,
    retrieval_query_state: str,
    limit: int = CONTEXT_BRIEFING_LIMIT,
    task_intent: str | None = None,
) -> dict[str, Any]:
    task_terms = _selection_terms(task_intent or retrieval_query)
    presented = _select_briefing_sources(
        sources,
        limit,
        task_terms=task_terms,
        retrieval_query=retrieval_query,
    )
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


def match_quality(
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


def diversify_memory_hits(
    hits: list[dict[str, Any]],
    limit: int,
    *,
    retrieval_query: str = "",
    task_intent: str | None = None,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    task_terms = _selection_terms(task_intent or retrieval_query)
    hits = _prefer_context_sources(
        hits,
        task_terms=task_terms,
        retrieval_query=retrieval_query,
    )
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
        preferred = [
            row
            for row in hits
            if _selection_preference(row, task_terms=task_terms) in tier
        ]
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
                1
                if _selection_preference(row, task_terms=task_terms) == "evidence"
                else CONTEXT_MAX_PER_SESSION
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


def pack_retrieval_query(pack: dict[str, Any]) -> str:
    if "retrieval_query" in pack:
        return str(pack.get("retrieval_query") or "")
    return str(pack.get("task") or "")


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
        "historical_tool_evidence": {
            "evidence_tier_requires": "specific_task_overlap_or_strong_match",
            "unmatched_manifest_tier": "historical_tool_fallback",
            "unmatched_briefing_eligibility": "omitted",
            "current_session_tier": "current_evidence",
        },
        "redundant_with_decision_or_evidence_session": list(
            CONTEXT_REDUNDANT_WITH_DECISION_SESSION
        ),
        "lifecycle_only_summaries": "classified_as_lifecycle",
        "low_signal_omitted_unless_task_targeted": sorted(HARD_OMITTED_PREFERENCES),
        "final_report_fallback": "only_when_no_higher_signal_source_survives",
        "final_report_fallback_limit": 1,
        "final_report_fallback_qualities": sorted(FINAL_REPORT_FALLBACK_QUALITIES),
        "weak_unknown_backfill": {
            "after_relevant_source": 0,
            "exploratory_limit": CONTEXT_EXPLORATORY_LIMIT,
        },
        "low_signal_target_terms": {
            preference: sorted(terms)
            for preference, terms in LOW_SIGNAL_TARGET_TERMS.items()
        },
        "ordered_lifecycle_intent_phrases": ["work start", "work finish"],
        "max_per_session": CONTEXT_MAX_PER_SESSION,
        "max_per_class_first_pass": CONTEXT_MAX_PER_CLASS_FIRST_PASS,
        "search_candidate_multiplier": CONTEXT_SEARCH_CANDIDATE_MULTIPLIER,
        "token_pattern": CONTEXT_TOKEN_RE.pattern,
        "blocked_terms": sorted(CONTEXT_GENERIC_TERMS),
    }


def _select_briefing_sources(
    sources: list[dict[str, Any]],
    limit: int,
    *,
    task_terms: set[str],
    retrieval_query: str,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    briefing_candidates = [
        source
        for source in sources
        if not (
            _source_preference(source) == "evidence"
            and not _historical_tool_evidence_is_supported(
                source,
                task_terms=task_terms,
            )
        )
    ]
    candidates_by_preference = _prefer_context_sources(
        briefing_candidates,
        task_terms=task_terms,
        retrieval_query=retrieval_query,
    )
    relevant = [
        source
        for source in candidates_by_preference
        if str(source.get("match_quality") or "unknown") in HIGHER_SIGNAL_QUALITIES
    ]
    if relevant:
        candidates_by_preference = relevant
    else:
        return _select_exploratory_sources(
            candidates_by_preference,
            min(limit, CONTEXT_EXPLORATORY_LIMIT),
        )
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    session_counts: dict[str, int] = {}
    class_counts: dict[str, int] = {}

    def add(source: dict[str, Any]) -> None:
        selected.append(source)
        selected_ids.add(source["source_id"])
        session = str(source.get("session_id") or source["source_id"])
        source_class = _selection_class(source, task_terms=task_terms)
        session_counts[session] = session_counts.get(session, 0) + 1
        class_counts[source_class] = class_counts.get(source_class, 0) + 1

    for tier in CONTEXT_SOURCE_SELECTION_TIERS:
        preferred = [
            source
            for source in candidates_by_preference
            if _selection_preference(source, task_terms=task_terms) in tier
        ]
        for quality in CONTEXT_QUALITY_ORDER:
            candidates = [
                source
                for source in preferred
                if (source.get("match_quality") or "unknown") == quality
            ]
            for source in candidates:
                session = str(source.get("session_id") or source["source_id"])
                source_class = _selection_class(source, task_terms=task_terms)
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


def _select_exploratory_sources(
    sources: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    selected_sessions: set[str] = set()
    selected_classes: set[str] = set()
    session_counts: dict[str, int] = {}

    def keys(source: dict[str, Any]) -> tuple[str, str, str]:
        source_id = str(source["source_id"])
        session = str(source.get("session_id") or source_id)
        source_class = str(source.get("source_class") or "retrieval_hint")
        return source_id, session, source_class

    def add(source: dict[str, Any]) -> None:
        source_id, session, source_class = keys(source)
        selected.append(source)
        selected_ids.add(source_id)
        selected_sessions.add(session)
        selected_classes.add(source_class)
        session_counts[session] = session_counts.get(session, 0) + 1

    for source in sources:
        source_id, session, source_class = keys(source)
        if source_id in selected_ids or session in selected_sessions:
            continue
        if source_class in selected_classes:
            continue
        add(source)
        if len(selected) >= limit:
            return selected
    for source in sources:
        source_id, session, _ = keys(source)
        if source_id in selected_ids or session in selected_sessions:
            continue
        add(source)
        if len(selected) >= limit:
            return selected
    for source in sources:
        source_id, session, _ = keys(source)
        if source_id in selected_ids or session_counts.get(session, 0) >= CONTEXT_MAX_PER_SESSION:
            continue
        add(source)
        if len(selected) >= limit:
            return selected
    return selected


def _prefer_context_sources(
    sources: list[dict[str, Any]],
    *,
    task_terms: set[str],
    retrieval_query: str,
) -> list[dict[str, Any]]:
    decision_sessions = {
        str(source.get("session_id"))
        for source in sources
        if source.get("session_id")
        and _source_preference(source) in {"current_evidence", "evidence", "decision"}
    }
    nonredundant = [
        source
        for source in sources
        if not (
            source.get("session_id")
            and str(source.get("session_id")) in decision_sessions
            and _source_preference(source) in CONTEXT_REDUNDANT_WITH_DECISION_SESSION
        )
    ]
    eligible = [
        source
        for source in nonredundant
        if _selection_preference(source, task_terms=task_terms) is not None
    ]
    has_higher_signal = any(
        _selection_preference(source, task_terms=task_terms) != "final_report"
        and _source_match_quality(source, retrieval_query) in HIGHER_SIGNAL_QUALITIES
        for source in eligible
    )
    nonfallback = [
        source
        for source in eligible
        if _selection_preference(source, task_terms=task_terms) != "final_report"
    ]
    if has_higher_signal:
        preferred = nonfallback
    else:
        fallback_candidates = [
            source
            for source in eligible
            if _selection_preference(source, task_terms=task_terms) == "final_report"
            and _source_match_quality(source, retrieval_query)
            in FINAL_REPORT_FALLBACK_QUALITIES
        ]
        best_fallback = (
            {
                **min(
                    fallback_candidates,
                    key=lambda source: (
                        CONTEXT_QUALITY_ORDER.index(
                            _source_match_quality(source, retrieval_query)
                        ),
                        -float(source.get("memory_score") or 0.0),
                    ),
                ),
                "_context_final_fallback": True,
            }
            if fallback_candidates
            else None
        )
        preferred = [*nonfallback, *([best_fallback] if best_fallback else [])]
    tier_index = {
        preference: index
        for index, tier in enumerate(CONTEXT_SOURCE_SELECTION_TIERS)
        for preference in tier
    }
    return sorted(
        preferred,
        key=lambda source: (
            tier_index[_selection_preference(source, task_terms=task_terms)],
            -float(source.get("memory_score") or 0.0),
        ),
    )


def _source_preference(source: dict[str, Any]) -> str:
    event_type = str(source.get("event_type") or "")
    source_kind = str(source.get("source_kind") or "")
    source_class = str(source.get("source_class") or "")
    source_role_value = str(source.get("source_role") or "")
    origin = str(source.get("origin") or "")
    if origin == "evidence":
        return "current_evidence"
    if source_role_value in {
        "decision",
        "evidence",
        "summary",
        "operational",
        "final_report",
        "lifecycle",
        "substantive",
    }:
        return source_role_value
    if source_class == "decision" or event_type.startswith("decision."):
        return "decision"
    if (
        source_class == "summary"
        or source_kind == "session_summary"
        or event_type == "summary.compacted"
    ):
        if _summary_contains_only_low_signal_events(source):
            return "lifecycle"
        return "summary"
    if source_class == "operational" or event_type in OPERATIONAL_EVENTS or event_type.startswith(
        "git.hook."
    ):
        return "operational"
    if source_class == "final_report" or event_type == "work.report.final":
        return "final_report"
    if (
        source_class == "lifecycle"
        or event_type in LIFECYCLE_EVENTS
        or event_type.startswith(("session.", "context."))
    ):
        return "lifecycle"
    if source_class == "evidence" or event_type in EVIDENCE_EVENTS:
        return "evidence"
    return "substantive"


def _summary_contains_only_low_signal_events(source: dict[str, Any]) -> bool:
    body = str(source.get("body_text") or source.get("passage_body_text") or "")
    lines = body.splitlines()
    event_total_lines = [line for line in lines if line.startswith("Events: ")]
    counts_lines = [line for line in lines if line.startswith("Event counts: ")]
    if len(event_total_lines) != 1 or len(counts_lines) != 1:
        return False
    event_total_text = event_total_lines[0].removeprefix("Events: ")
    expected_event_total = _parse_positive_summary_count(event_total_text)
    if expected_event_total is None:
        return False
    counts_line = counts_lines[0]
    event_types: set[str] = set()
    counted_events = 0
    for entry in counts_line.removeprefix("Event counts: ").split(","):
        event_type, separator, count = entry.strip().rpartition("=")
        parsed_count = _parse_positive_summary_count(count)
        if (
            not separator
            or not event_type
            or parsed_count is None
            or event_type in event_types
        ):
            return False
        event_types.add(event_type)
        counted_events += parsed_count
    if counted_events != expected_event_total:
        return False
    return bool(event_types) and all(
        event_type in SUMMARY_LOW_SIGNAL_EVENTS or event_type.startswith("git.hook.")
        for event_type in event_types
    )


def _parse_positive_summary_count(value: str) -> int | None:
    if (
        not value
        or len(value) > MAX_SUMMARY_COUNT_DIGITS
        or re.fullmatch(r"[0-9]+", value) is None
    ):
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def source_class(source: dict[str, Any], origin: str) -> str:
    """Return the stable v1 class persisted in context manifests and views."""
    event_type = str(source.get("event_type") or "")
    source_kind = str(source.get("source_kind") or "")
    if origin == "evidence" or event_type in EVIDENCE_EVENTS or event_type.startswith("git.hook."):
        return "evidence"
    if source_kind == "session_summary" or event_type == "summary.compacted":
        return "summary"
    return "retrieval_hint"


def source_role(source: dict[str, Any], origin: str) -> str:
    """Return the additive semantic role used for selection and user display."""
    if origin == "evidence":
        return "evidence"
    preference = _source_preference(source)
    return "evidence" if preference == "current_evidence" else preference


def _selection_preference(
    source: dict[str, Any],
    *,
    task_terms: set[str],
) -> str | None:
    if source.get("_context_final_fallback"):
        return "decision"
    preference = _source_preference(source)
    if preference == "evidence" and not _historical_tool_evidence_is_supported(
        source,
        task_terms=task_terms,
    ):
        return "historical_tool"
    if preference not in LOW_SIGNAL_PREFERENCES:
        return preference
    targeted = _task_targets_low_signal(
        source,
        preference=preference,
        task_terms=task_terms,
    )
    if preference == "final_report":
        return "substantive" if targeted else "final_report"
    if not targeted:
        return None
    if preference == "operational":
        return "evidence"
    return "substantive"


def _historical_tool_evidence_is_supported(
    source: dict[str, Any],
    *,
    task_terms: set[str],
) -> bool:
    if str(source.get("event_type") or "") not in ROUTINE_HISTORICAL_TOOL_EVENTS:
        return True
    overlap_terms = source.get("overlap_terms")
    if overlap_terms is None:
        searchable = " ".join(
            [
                str(source.get("subject") or ""),
                str(source.get("passage_body_text") or source.get("body_text") or ""),
            ]
        )
        source_terms = {
            term.lower()
            for term in CONTEXT_TOKEN_RE.findall(searchable)
            if len(term) > 1 and term.lower() not in CONTEXT_GENERIC_TERMS
        }
        overlap_terms = {
            term
            for term in task_terms.intersection(source_terms)
            if term not in CONTEXT_GENERIC_TERMS and not term.startswith("__agentdir_")
        }
    if overlap_terms:
        return True
    return _source_match_quality(source, " ".join(sorted(task_terms))) == "strong"


def _selection_class(source: dict[str, Any], *, task_terms: set[str]) -> str:
    return str(
        source.get("source_role")
        or source_role(source, str(source.get("origin") or "memory"))
        or source.get("source_class")
        or "retrieval_hint"
    )


def _task_targets_low_signal(
    source: dict[str, Any],
    *,
    preference: str,
    task_terms: set[str],
) -> bool:
    if task_terms.intersection(LOW_SIGNAL_TARGET_TERMS[preference]):
        return True
    event_type = str(source.get("event_type") or "")
    if event_type.startswith("git.hook."):
        hook_terms = set(CONTEXT_TOKEN_RE.findall(event_type.removeprefix("git.hook.").lower()))
        return bool(hook_terms) and hook_terms.issubset(task_terms)
    if event_type == "work.started":
        return WORK_START_INTENT in task_terms
    if event_type == "work.finished":
        return WORK_FINISH_INTENT in task_terms
    return False


def _selection_terms(text: str) -> set[str]:
    ordered = [
        term.lower()
        for term in CONTEXT_TOKEN_RE.findall(text)
        if len(term) > 1 and term.lower() not in MEMORY_STOPWORDS
    ]
    terms = set(ordered)
    for first, second in zip(ordered, ordered[1:]):
        if first == "work" and second in {"start", "started"}:
            terms.add(WORK_START_INTENT)
        if first == "work" and second in {"end", "ended", "finish", "finished"}:
            terms.add(WORK_FINISH_INTENT)
    return terms


def _source_match_quality(source: dict[str, Any], retrieval_query: str) -> str:
    existing = str(source.get("match_quality") or "")
    if existing:
        return existing
    query_terms = [
        term.lower()
        for term in CONTEXT_TOKEN_RE.findall(retrieval_query)
        if len(term) > 1 and term.lower() not in CONTEXT_GENERIC_TERMS
    ]
    if not query_terms:
        return "unknown"
    searchable = " ".join(
        [
            str(source.get("subject") or ""),
            str(source.get("passage_body_text") or source.get("body_text") or ""),
        ]
    )
    source_terms = {
        term.lower()
        for term in CONTEXT_TOKEN_RE.findall(searchable)
        if len(term) > 1 and term.lower() not in CONTEXT_GENERIC_TERMS
    }
    quality, _ = match_quality(
        origin=str(source.get("origin") or "memory_hit"),
        memory_score=source.get("memory_score"),
        overlap_terms=[term for term in query_terms if term in source_terms],
        task_term_count=len(query_terms),
        retrieval_mode=str(source.get("retrieval_mode") or RETRIEVAL_HYBRID),
    )
    return quality


def _source_next_actions(
    source: dict[str, Any],
    retrieval_query: str,
) -> dict[str, list[str]]:
    if not retrieval_query or source.get("memory_score") is None:
        return {}
    source_id = str(source.get("source_id_original") or source.get("source_id") or "")
    if not source_id:
        return {}
    action = ["memory", "explain"]
    if source.get("source_root_path"):
        action.extend(("--root", str(source["source_root_path"])))
    action.extend((retrieval_query, "--source", source_id))
    requested_mode = str(source.get("requested_retrieval_mode") or RETRIEVAL_AUTO)
    if requested_mode in {
        RETRIEVAL_DOCUMENT,
        RETRIEVAL_HYBRID,
        RETRIEVAL_SEMANTIC,
        RETRIEVAL_SEMANTIC_HYBRID,
    }:
        action.extend(("--retrieval", requested_mode))
    action.append("--no-rebuild")
    return {"explain": action}
