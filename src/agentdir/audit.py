from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .claims import OUTCOME_PASSED, recorded_claims
from .context import audit_context_pack
from .context_expansion import audit_context_expansion_inventory
from .context_repository import list_context_packs
from .doctor import run_doctor
from .git import git_status_short
from .review import (
    EVIDENCE_FAMILIES,
    evidence_failure_state,
    evidence_rows,
    evidence_truncated,
    summarize_session,
)
from .sessions import read_current_session
from .store import AgentDirError

CHECK_PASS = "pass"
CHECK_WARN = "warn"
CHECK_FAIL = "fail"
CHECK_NOT_APPLICABLE = "not_applicable"
CHECK_STATUSES = (CHECK_PASS, CHECK_WARN, CHECK_FAIL, CHECK_NOT_APPLICABLE)

CLAIM_SUPPORTED = "supported"
CLAIM_PARTIAL = "partial"
CLAIM_UNSUPPORTED = "unsupported"
CLAIM_CONTRADICTED = "contradicted"
# Recorded evidence failed, but the text phrases its result in a way the claim
# patterns cannot check. Distinct from "contradicted": nothing was disproved,
# the text simply dodged review.
CLAIM_UNREVIEWED = "unreviewed"
# Recorded evidence failed and the text says so. Reported for visibility, but it
# is not a problem: the success patterns only know one polarity, so without this
# an honest failure report would be flagged exactly like a text that hid one.
CLAIM_ACKNOWLEDGED = "acknowledged"
CLAIM_STATUSES = (
    CLAIM_SUPPORTED,
    CLAIM_PARTIAL,
    CLAIM_UNSUPPORTED,
    CLAIM_CONTRADICTED,
    CLAIM_UNREVIEWED,
    CLAIM_ACKNOWLEDGED,
)
CLAIM_PROBLEM_STATUSES = (CLAIM_UNSUPPORTED, CLAIM_CONTRADICTED, CLAIM_UNREVIEWED)

CLAIM_FAMILIES = tuple(family for family in EVIDENCE_FAMILIES if family != "diagnostic")

CLAIM_KEYWORDS = {
    "test": ("test", "tests", "pytest", "vitest", "jest", "mocha", "unittest"),
    "lint": ("lint", "eslint", "ruff", "flake8"),
    "typecheck": ("typecheck", "type check", "type-check", "tsc", "mypy", "pyright"),
    "build": ("build",),
    "doctor": ("doctor",),
    "release": ("release", "deploy", "deployment", "publish"),
}

CLAIM_PATTERNS = {
    "test": re.compile(r"\b(test|tests|pytest|vitest|jest)\b[^.\n]*(pass|passed|passing|green|succeed|succeeded)", re.I),
    "lint": re.compile(r"\b(lint|eslint|ruff|flake8)\b[^.\n]*(pass|passed|passing|clean|succeed|succeeded)", re.I),
    "typecheck": re.compile(
        r"\b(typecheck|type check|type-check|tsc|mypy|pyright)\b[^.\n]*(pass|passed|passing|clean|succeed|succeeded)",
        re.I,
    ),
    "build": re.compile(r"\b(build|built)\b[^.\n]*(pass|passed|passing|succeed|succeeded|successful|completed)", re.I),
    "doctor": re.compile(r"\bdoctor\b[^.\n]*(pass|passed|passing|ok|healthy|clean|succeed|succeeded)", re.I),
    "release": re.compile(
        r"\b(release|deploy|deployment|publish)\b[^.\n]*(pass|passed|passing|succeed|succeeded|successful|completed)",
        re.I,
    ),
}

NEGATED_CLAIM_RE = re.compile(r"\b(not run|did not run|didn't run|was not run|wasn't run|without running)\b", re.I)

FAILURE_WORDS_RE = re.compile(
    r"\b(fail|fails|failed|failing|failure|failures|broken|breaking|error|errors|"
    r"red|regressed|regression|regressions|does not pass|did not pass|not passing)\b",
    re.I,
)

# "no test failures", "tests are not failing": failure vocabulary asserting
# success. These are success claims, not acknowledgements of a failure.
NEGATED_FAILURE_RE = re.compile(
    r"\b(no|not|never|zero|without|free of)\b[^.\n]{0,30}?"
    r"\b(fail\w*|error\w*|broken|regress\w*|red)\b",
    re.I,
)

FAMILY_KEYWORD_PATTERNS = {
    family: re.compile(r"\b(" + "|".join(re.escape(word) for word in words) + r")\b", re.I)
    for family, words in CLAIM_KEYWORDS.items()
}

_MISSING = object()


def audit_session(
    root: str | Path,
    session_id: str | None = None,
    *,
    strict: bool = False,
    finishing: bool = False,
    run_health_check: bool = True,
    rebuild: bool = True,
    summary: dict[str, Any] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    latest_pack: dict[str, Any] | None | object = _MISSING,
    context_audit: dict[str, Any] | None | object = _MISSING,
    doctor: dict[str, Any] | None | object = _MISSING,
    expansion_inventory: dict[str, Any] | object = _MISSING,
) -> dict[str, Any]:
    if summary is None:
        summary = summarize_session(root, session_id, rebuild=rebuild)
        rebuild = False
    resolved = summary["session_id"]
    if evidence is None:
        evidence = evidence_rows(root, resolved, rebuild=rebuild)
        rebuild = False
    event_counts = summary.get("event_counts") or {}
    if latest_pack is _MISSING:
        latest_pack = _latest_context_pack(root, resolved, rebuild=rebuild)
        rebuild = False
    if context_audit is _MISSING:
        context_audit = _safe_context_audit(root, latest_pack, rebuild=rebuild)
    if doctor is _MISSING:
        doctor = run_doctor(root).as_dict() if run_health_check else None
    if expansion_inventory is _MISSING:
        try:
            expansion_inventory = audit_context_expansion_inventory(root, resolved)
        except AgentDirError as exc:
            expansion_inventory = {
                "event_count": 0,
                "claimable_event_count": 0,
                "orphan_event_count": 0,
                "receipts_valid": False,
                "validation_errors": [str(exc)],
            }
    current = read_current_session(root)
    is_current = bool(current and current.session_id == resolved and current.status == "active")
    failure_state = evidence_failure_state(evidence)
    failed_evidence = failure_state["unresolved"]
    resolved_failed_evidence = failure_state["resolved"]
    truncated_evidence = [
        row
        for row in evidence
        if (row.get("truncated") if "truncated" in row else evidence_truncated(row))
    ]
    dirty_status = git_status_short(root)
    checks = [
        _check(
            "session_started",
            CHECK_PASS if event_counts.get("session.started") or event_counts.get("work.started") else CHECK_FAIL,
            "session has a start event" if event_counts.get("session.started") or event_counts.get("work.started") else "session has no start event",
        ),
        _check(
            "session_finished",
            CHECK_PASS if event_counts.get("session.ended") or event_counts.get("work.finished") or finishing else CHECK_WARN,
            "session has a finish event" if event_counts.get("session.ended") or event_counts.get("work.finished") else (
                "work finish is in progress" if finishing else "session is still active" if is_current else "session has no finish event"
            ),
        ),
        _check(
            "evidence_present",
            CHECK_PASS if evidence else CHECK_WARN,
            f"{len(evidence)} evidence record(s) captured" if evidence else "no evidence captured",
        ),
        _check(
            "failed_tool_results",
            CHECK_FAIL if failed_evidence else CHECK_PASS,
            f"{len(failed_evidence)} unresolved failed tool result(s)"
            if failed_evidence
            else "no unresolved failed tool results",
        ),
        _check(
            "resolved_failed_tool_results",
            CHECK_PASS if resolved_failed_evidence else CHECK_NOT_APPLICABLE,
            f"{len(resolved_failed_evidence)} historical failed tool result(s) resolved by newer passing evidence"
            if resolved_failed_evidence
            else "no resolved historical tool failures",
        ),
        _check(
            "truncated_evidence",
            CHECK_WARN if truncated_evidence else CHECK_PASS,
            f"{len(truncated_evidence)} tool result(s) with truncated output; evidence may be incomplete"
            if truncated_evidence
            else "no truncated tool output",
        ),
        _check(
            "context_pack_created",
            CHECK_PASS if latest_pack else CHECK_WARN,
            f"context pack {latest_pack['pack_id']} recorded" if latest_pack else "no context pack recorded",
        ),
        _context_review_check(context_audit, latest_pack),
        _context_use_check(context_audit, latest_pack),
        _context_citation_check(context_audit, latest_pack),
        _context_expansion_check(context_audit, latest_pack),
        _context_expansion_inventory_check(expansion_inventory),
        _doctor_check(doctor),
        _check(
            "git_dirty",
            CHECK_WARN if dirty_status else CHECK_PASS,
            "git worktree has changes" if dirty_status else "git worktree is clean",
        ),
    ]
    return {
        "session_id": resolved,
        "ok": not any(check["status"] == CHECK_FAIL for check in checks),
        "strict": strict,
        "summary": summary,
        "checks": checks,
    }


def audit_recorded_claims(
    root: str | Path,
    session_id: str | None = None,
    *,
    strict: bool = False,
    rebuild: bool = True,
    summary: dict[str, Any] | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Check structured claims against evidence.

    No wording is involved: each claim already names its family and outcome, so
    this is a comparison rather than the interpretation `audit_claims` performs.
    """
    if summary is None:
        summary = summarize_session(root, session_id, rebuild=rebuild)
        rebuild = False
    resolved = summary["session_id"]
    if evidence is None:
        evidence = evidence_rows(root, resolved, rebuild=rebuild)
    tool_results = [row for row in evidence if row.get("event_type") == "tool.result"]
    recorded = recorded_claims(root, resolved, rebuild=rebuild)

    claims: list[dict[str, Any]] = []
    for claim in recorded:
        family = claim["family"]
        row = _latest_classified_tool_result(tool_results, family)
        claimed_pass = claim["outcome"] == OUTCOME_PASSED
        if row is None:
            status = CLAIM_UNSUPPORTED
            message = f"claimed {family} {claim['outcome']} but no {family} evidence was recorded"
        elif row.get("tool_exit_code") == 0:
            if claimed_pass:
                truncated = row.get("truncated") if "truncated" in row else evidence_truncated(row)
                status = CLAIM_PARTIAL if truncated else CLAIM_SUPPORTED
                message = (
                    f"latest {family} evidence succeeded but its captured output was truncated; "
                    "evidence may be incomplete"
                    if truncated
                    else f"latest {family} evidence succeeded"
                )
            else:
                status = CLAIM_CONTRADICTED
                message = f"claimed {family} failed but the latest {family} evidence succeeded"
        else:
            exit_code = row.get("tool_exit_code")
            if claimed_pass:
                status = CLAIM_CONTRADICTED
                message = f"claimed {family} passed but the latest {family} evidence failed with exit {exit_code}"
            else:
                status = CLAIM_ACKNOWLEDGED
                message = f"latest {family} evidence failed with exit {exit_code}; the claim reports it as failed"
        claims.append(
            {
                "family": family,
                "status": status,
                "outcome": claim["outcome"],
                "message": message,
                "evidence": _evidence_ref(row),
            }
        )

    claimed_families = [claim["family"] for claim in recorded]
    claims.extend(_unclaimed_failures(tool_results, claimed_families, set(), source="recorded"))
    return {
        "session_id": resolved,
        "ok": not any(claim["status"] in CLAIM_PROBLEM_STATUSES for claim in claims),
        "strict": strict,
        "source": "recorded",
        "claims_detected": len(recorded),
        "claims": claims,
    }


def audit_claims(
    root: str | Path,
    text: str,
    session_id: str | None = None,
    *,
    strict: bool = False,
    rebuild: bool = True,
    summary: dict[str, Any] | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if summary is None:
        summary = summarize_session(root, session_id, rebuild=rebuild)
        rebuild = False
    resolved = summary["session_id"]
    if evidence is None:
        evidence = evidence_rows(root, resolved, rebuild=rebuild)
    tool_results = [row for row in evidence if row.get("event_type") == "tool.result"]
    claims = []
    detected = _detect_claim_families(text)
    for family in detected:
        evidence = _latest_relevant_tool_result(tool_results, family)
        if evidence is None:
            status = CLAIM_UNSUPPORTED
            message = f"no {family} evidence found"
        elif evidence.get("tool_exit_code") == 0:
            if evidence.get("truncated") if "truncated" in evidence else evidence_truncated(evidence):
                status = CLAIM_PARTIAL
                message = (
                    f"latest {family} evidence succeeded but its captured output was truncated; "
                    "evidence may be incomplete"
                )
            else:
                status = CLAIM_SUPPORTED
                message = f"latest {family} evidence succeeded"
        else:
            status = CLAIM_CONTRADICTED
            message = f"latest {family} evidence failed with exit {evidence.get('tool_exit_code')}"
        claims.append(
            {
                "family": family,
                "status": status,
                "message": message,
                "evidence": _evidence_ref(evidence),
            }
        )
    claims.extend(_unclaimed_failures(tool_results, detected, _detect_acknowledged_families(text)))
    return {
        "session_id": resolved,
        "ok": not any(claim["status"] in CLAIM_PROBLEM_STATUSES for claim in claims),
        "strict": strict,
        "source": "text",
        "claims_detected": len(detected),
        "claims": claims,
    }


def _detect_acknowledged_families(text: str) -> set[str]:
    """Families the text reports as failing, rather than claiming success for.

    ``CLAIM_PATTERNS`` only matches success wording, so "tests failed" parses as
    no claim at all. Without this, honest failure reporting would be flagged
    exactly like text that hid the failure.
    """
    families: set[str] = set()
    for sentence in re.split(r"[.\n]", text):
        if not (FAILURE_WORDS_RE.search(sentence) or NEGATED_CLAIM_RE.search(sentence)):
            continue
        # "no test failures" uses failure words to assert success; that is a
        # claim to check, not an acknowledgement.
        if NEGATED_FAILURE_RE.search(sentence):
            continue
        for family, pattern in FAMILY_KEYWORD_PATTERNS.items():
            if pattern.search(sentence):
                families.add(family)
    return families


def _unclaimed_failures(
    tool_results: list[dict[str, Any]],
    detected: list[str],
    acknowledged: set[str],
    *,
    source: str = "text",
) -> list[dict[str, Any]]:
    """Report failed evidence the text made no success claim about.

    Claim detection is keyword-based, so ordinary phrasing ("everything works",
    "verified locally") matches nothing. Reporting ``ok`` in that case reads as
    "audited and clean" when the truth is "not audited at all", which is the
    worst direction for this check to fail in. Text that states the failure
    instead is recorded as acknowledged and does not count against the audit.
    """
    unreviewed: list[dict[str, Any]] = []
    for family in CLAIM_FAMILIES:
        if family in detected:
            continue
        # Only evidence AgentDir actually classified into this family, never the
        # keyword fallback. That fallback exists to attach a claim the text made
        # to plausible evidence; guessing here would report one failed command
        # under every family whose keyword appears anywhere in its output.
        evidence = _latest_classified_tool_result(tool_results, family)
        if evidence is None or evidence.get("tool_exit_code") == 0:
            continue
        exit_code = evidence.get("tool_exit_code")
        if family in acknowledged:
            status = CLAIM_ACKNOWLEDGED
            message = f"latest {family} evidence failed with exit {exit_code}; the text reports it as failing"
        else:
            status = CLAIM_UNREVIEWED
            unclaimed = (
                f"no {family} claim was recorded"
                if source == "recorded"
                else f"the text makes no checkable {family} claim"
            )
            message = f"latest {family} evidence failed with exit {exit_code} but {unclaimed}"
        unreviewed.append(
            {
                "family": family,
                "status": status,
                "message": message,
                "evidence": _evidence_ref(evidence),
            }
        )
    return unreviewed


def format_session_audit(audit: dict[str, Any]) -> str:
    lines = [f"session={audit['session_id']}", f"ok={str(audit['ok']).lower()}"]
    for check in audit["checks"]:
        lines.append(f"{check['id']}={check['status']} {check['message']}")
    return "\n".join(lines)


def format_claims_audit(audit: dict[str, Any]) -> str:
    source = audit.get("source", "text")
    lines = [
        f"session={audit['session_id']}",
        f"ok={str(audit['ok']).lower()}",
        f"source={source}",
        f"claims_detected={audit.get('claims_detected', len(audit['claims']))}",
    ]
    if not audit["claims"]:
        lines.append(
            "claims=none no structured claim recorded"
            if source == "recorded"
            else "claims=none no checkable verification claim found in the text"
        )
        return "\n".join(lines)
    for claim in audit["claims"]:
        evidence = claim.get("evidence") or {}
        evidence_path = evidence.get("file_path") or ""
        lines.append(f"{claim['family']}={claim['status']} {claim['message']} {evidence_path}".rstrip())
    return "\n".join(lines)


def session_audit_gaps(audit: dict[str, Any]) -> list[str]:
    gaps: list[str] = []
    for check in audit.get("checks") or []:
        if check.get("id") == "git_dirty":
            continue
        if check.get("status") in {CHECK_WARN, CHECK_FAIL}:
            gaps.append(f"{check['id']}: {check['message']}")
    return gaps


def claims_audit_gaps(audit: dict[str, Any] | None) -> list[str]:
    if not audit:
        return []
    gaps: list[str] = []
    for claim in audit.get("claims") or []:
        if claim.get("status") in {CLAIM_PARTIAL, *CLAIM_PROBLEM_STATUSES}:
            gaps.append(f"{claim['family']}: {claim['message']}")
    return gaps


def strict_session_exit_code(audit: dict[str, Any]) -> int:
    return 1 if any(check["status"] == CHECK_FAIL for check in audit.get("checks") or []) else 0


def strict_claims_exit_code(audit: dict[str, Any]) -> int:
    return 1 if any(claim["status"] in {CLAIM_PARTIAL, *CLAIM_PROBLEM_STATUSES} for claim in audit.get("claims") or []) else 0


def _check(
    check_id: str,
    status: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in CHECK_STATUSES:
        raise AgentDirError(f"Unknown audit check status: {status}")
    payload = {"id": check_id, "status": status, "message": message}
    if details:
        payload["details"] = details
    return payload


def _context_review_check(
    context_audit: dict[str, Any] | None,
    latest_pack: dict[str, Any] | None,
) -> dict[str, Any]:
    check_id = "context_review_completed"
    if latest_pack is None:
        return _check(check_id, CHECK_NOT_APPLICABLE, "no context pack recorded")
    if context_audit is None:
        return _check(check_id, CHECK_WARN, "context audit unavailable")
    if context_audit.get("error"):
        return _check(check_id, CHECK_FAIL, f"context audit failed: {context_audit['error']}")
    status = context_audit.get("review_status")
    if status == "not_applicable":
        return _check(check_id, CHECK_NOT_APPLICABLE, "context pack presented no sources")
    if status == "legacy":
        return _check(check_id, CHECK_NOT_APPLICABLE, "legacy context pack has no review requirement")
    if status == "complete":
        return _check(
            check_id,
            CHECK_PASS,
            f"{context_audit.get('reviewed_count', 0)} presented context source(s) reviewed",
        )
    if status == "conflict":
        return _check(
            check_id,
            CHECK_FAIL,
            "context has an invalid transition, malformed decision, or conflicting terminal decisions",
        )
    if status == "compatibility_partial":
        return _check(
            check_id,
            CHECK_WARN,
            "legacy lower-level consumption recorded use without a complete briefing disposition",
        )
    return _check(
        check_id,
        CHECK_FAIL,
        "context review was skipped" if status == "skipped" else "context review decision is pending",
    )


def _context_use_check(
    context_audit: dict[str, Any] | None,
    latest_pack: dict[str, Any] | None,
) -> dict[str, Any]:
    check_id = "context_sources_consumed"
    if latest_pack is None:
        return _check(check_id, CHECK_NOT_APPLICABLE, "no context pack recorded")
    if context_audit is None or context_audit.get("error"):
        return _check(check_id, CHECK_NOT_APPLICABLE, "context use unavailable")
    count = int(context_audit.get("used_count") or 0)
    if count:
        return _check(check_id, CHECK_PASS, f"{count} context source(s) used")
    if context_audit.get("decision") == "no_relevant":
        return _check(check_id, CHECK_NOT_APPLICABLE, "review found no relevant context to use")
    return _check(check_id, CHECK_NOT_APPLICABLE, "context use is not required without a relevant source")


def _context_citation_check(
    context_audit: dict[str, Any] | None,
    latest_pack: dict[str, Any] | None,
) -> dict[str, Any]:
    check_id = "context_sources_cited"
    if latest_pack is None:
        return _check(check_id, CHECK_NOT_APPLICABLE, "no context pack recorded")
    if context_audit is None or context_audit.get("error"):
        return _check(check_id, CHECK_NOT_APPLICABLE, "context citation unavailable")
    invalid = int(context_audit.get("cited_without_use_count") or 0)
    if invalid and context_audit.get("cited_without_use_enforced", True):
        return _check(check_id, CHECK_FAIL, f"{invalid} context source(s) cited without use")
    if invalid:
        return _check(
            check_id,
            CHECK_NOT_APPLICABLE,
            f"legacy context pack cited {invalid} source(s) before recorded use",
        )
    count = int(context_audit.get("cited_count") or 0)
    if count:
        return _check(check_id, CHECK_PASS, f"{count} used context source(s) cited")
    return _check(check_id, CHECK_NOT_APPLICABLE, "citation is optional and no sources were cited")


def _context_expansion_check(
    context_audit: dict[str, Any] | None,
    latest_pack: dict[str, Any] | None,
) -> dict[str, Any]:
    check_id = "context_expansion_receipts"
    if latest_pack is None:
        return _check(check_id, CHECK_NOT_APPLICABLE, "no context pack recorded")
    if context_audit is None or context_audit.get("error"):
        return _check(check_id, CHECK_NOT_APPLICABLE, "context expansion audit unavailable")
    expansion = context_audit.get("expansion") or {}
    if not expansion.get("receipts_valid", True):
        invalid = int(expansion.get("invalid_receipt_count") or 0)
        return _check(
            check_id,
            CHECK_WARN,
            f"{invalid} invalid optional context expansion receipt(s); review completion is unaffected",
            details={"validation_errors": expansion.get("validation_errors") or []},
        )
    count = int(expansion.get("receipt_event_count") or 0)
    if count:
        return _check(check_id, CHECK_PASS, f"{count} context expansion receipt(s) validated")
    return _check(
        check_id,
        CHECK_NOT_APPLICABLE,
        "context expansion is optional and no sources were expanded",
    )


def _context_expansion_inventory_check(inventory: dict[str, Any]) -> dict[str, Any]:
    check_id = "context_expansion_receipt_inventory"
    if not inventory.get("receipts_valid", True):
        count = int(inventory.get("orphan_event_count") or 0)
        return _check(
            check_id,
            CHECK_WARN,
            f"{count} optional context expansion receipt(s) cannot be attributed to an owning pack",
            details=inventory,
        )
    count = int(inventory.get("event_count") or 0)
    if count:
        return _check(
            check_id,
            CHECK_PASS,
            f"{count} context expansion receipt claim(s) attributed to owning packs",
            details=inventory,
        )
    return _check(check_id, CHECK_NOT_APPLICABLE, "no context expansion receipts recorded")


def _doctor_check(doctor: dict[str, Any] | None) -> dict[str, Any]:
    if doctor is None:
        return _check("doctor_ok", CHECK_NOT_APPLICABLE, "doctor was not run")
    return _check(
        "doctor_ok",
        CHECK_PASS if doctor.get("ok") else CHECK_FAIL,
        "doctor reported ok" if doctor.get("ok") else "doctor reported errors",
        details={"doctor": doctor},
    )


def _latest_context_pack(root: str | Path, session_id: str, *, rebuild: bool = True) -> dict[str, Any] | None:
    packs = list_context_packs(root, session_id, rebuild=rebuild)
    attention_pack = None
    for pack in packs:
        audit = _safe_context_audit(root, pack, rebuild=False)
        if audit and not audit.get("finish_allowed", False):
            return pack
        expansion = (audit or {}).get("expansion") or {}
        if (
            audit
            and (
                not audit.get("lineage_valid", False)
                or not expansion.get("receipts_valid", True)
            )
            and attention_pack is None
        ):
            attention_pack = pack
    return attention_pack or (packs[-1] if packs else None)


def _safe_context_audit(root: str | Path, latest_pack: dict[str, Any] | None | object, *, rebuild: bool = True) -> dict[str, Any] | None:
    if latest_pack is _MISSING or latest_pack is None:
        return None
    pack_id = latest_pack["pack_id"]
    if latest_pack.get("identity_error"):
        return {
            "error": latest_pack["identity_error"],
            "pack_id": pack_id,
            "review_status": "error",
            "decision_complete": False,
            "finish_allowed": False,
            "lineage_valid": False,
        }
    try:
        return audit_context_pack(root, pack_id, rebuild=rebuild)
    except AgentDirError as exc:
        return {"error": str(exc), "pack_id": pack_id}


def _detect_claim_families(text: str) -> list[str]:
    families: list[str] = []
    negated_failure = _families_in_sentences(text, NEGATED_FAILURE_RE)
    for family in CLAIM_FAMILIES:
        pattern = CLAIM_PATTERNS[family]
        matched = False
        for match in pattern.finditer(text):
            window = text[max(0, match.start() - 40): match.end() + 40]
            if NEGATED_CLAIM_RE.search(window):
                continue
            families.append(family)
            matched = True
            break
        if not matched and family in negated_failure:
            families.append(family)
    return families


def _families_in_sentences(text: str, trigger: re.Pattern[str]) -> set[str]:
    families: set[str] = set()
    for sentence in re.split(r"[.\n]", text):
        if not trigger.search(sentence):
            continue
        for family, pattern in FAMILY_KEYWORD_PATTERNS.items():
            if pattern.search(sentence):
                families.add(family)
    return families


def _latest_classified_tool_result(rows: list[dict[str, Any]], family: str) -> dict[str, Any] | None:
    for row in reversed(rows):
        if row.get("family") == family:
            return row
    return None


def _latest_relevant_tool_result(rows: list[dict[str, Any]], family: str) -> dict[str, Any] | None:
    classified = _latest_classified_tool_result(rows, family)
    if classified is not None:
        return classified
    keywords = CLAIM_KEYWORDS[family]
    for row in reversed(rows):
        haystack = _tool_result_search_text(row)
        if any(keyword in haystack for keyword in keywords):
            return row
    return None


def _tool_result_search_text(row: dict[str, Any]) -> str:
    body_lines = []
    for line in str(row.get("body_text") or "").splitlines():
        if line.startswith(("cwd=", "duration_ms=", "redactions=", "stdout_truncated=", "stderr_truncated=")):
            continue
        body_lines.append(line)
    return " ".join(
        [
            str(row.get("tool") or ""),
            str(row.get("subject") or ""),
            "\n".join(body_lines),
        ]
    ).lower()


def _evidence_ref(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "event_type": row.get("event_type"),
        "tool": row.get("tool"),
        "tool_exit_code": row.get("tool_exit_code"),
        "subject": row.get("subject"),
        "file_path": row.get("file_path"),
        "date_utc": row.get("date_utc"),
        "truncated": row.get("truncated") if "truncated" in row else evidence_truncated(row),
    }
