"""Structured verification claims.

`agentdir audit claims --text` has to infer intent from prose, and every bug it
has had came from wording its patterns understood in only one direction. A claim
recorded here states the family and the outcome outright, so checking it against
evidence is a comparison rather than an interpretation.

Prose auditing stays available for final responses that were not instrumented;
structured claims are the path that cannot be defeated by phrasing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .events import emit_event
from .query import query_messages
from .review import EVIDENCE_FAMILIES, ensure_index, resolve_review_session
from .sessions import read_current_session, start_session
from .store import AgentDirError

CLAIM_EVENT_TYPE = "claim.recorded"

CLAIM_FAMILIES = tuple(family for family in EVIDENCE_FAMILIES if family != "diagnostic")

OUTCOME_PASSED = "passed"
OUTCOME_FAILED = "failed"
# Withdraws an earlier claim. Claims are append-only events, so a mistaken one
# cannot be deleted; retracting supersedes it in the latest-claim view while
# leaving the original in the record.
OUTCOME_RETRACTED = "retracted"
CLAIM_OUTCOMES = (OUTCOME_PASSED, OUTCOME_FAILED, OUTCOME_RETRACTED)

FAMILY_HEADER = "X-AgentDir-Claim-Family"
OUTCOME_HEADER = "X-AgentDir-Claim-Outcome"


def validate_family(family: str) -> str:
    if family not in CLAIM_FAMILIES:
        raise AgentDirError(
            f"Unknown claim family {family!r}; expected one of {', '.join(CLAIM_FAMILIES)}"
        )
    return family


def validate_outcome(outcome: str) -> str:
    if outcome not in CLAIM_OUTCOMES:
        raise AgentDirError(
            f"Unknown claim outcome {outcome!r}; expected one of {', '.join(CLAIM_OUTCOMES)}"
        )
    return outcome


def record_claim(
    root: str | Path,
    family: str,
    outcome: str,
    *,
    note: str | None = None,
    session_id: str | None = None,
    actor: str = "agent",
) -> dict[str, Any]:
    validate_family(family)
    validate_outcome(outcome)
    resolved = session_id
    if resolved is None:
        current = read_current_session(root)
        resolved = (
            current.session_id
            if current
            else start_session(root, title="AgentDir claim").session_id
        )
    body_lines = [f"family={family}", f"outcome={outcome}"]
    if note:
        body_lines.extend(["", note])
    delivered = emit_event(
        root,
        session_id=resolved,
        event_type=CLAIM_EVENT_TYPE,
        body="\n".join(body_lines),
        subject=f"claim {family} {outcome}",
        from_actor=actor,
        extra_headers={FAMILY_HEADER: family, OUTCOME_HEADER: outcome},
    )
    return {
        "family": family,
        "outcome": outcome,
        "note": note,
        "session_id": delivered.session_id,
        "path": str(delivered.path),
    }


def recorded_claims(
    root: str | Path,
    session_id: str | None = None,
    *,
    rebuild: bool = True,
) -> list[dict[str, Any]]:
    """Latest claim per family, in family order.

    A family claimed more than once keeps the last claim: re-running a check
    after a fix should replace the earlier statement, not sit beside it.
    """
    resolved = resolve_review_session(root, session_id)
    if rebuild:
        ensure_index(root)
    rows = query_messages(root, session_id=resolved, limit=10_000)
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("event_type") != CLAIM_EVENT_TYPE:
            continue
        parsed = _parse_claim_row(row)
        if parsed is None:
            continue
        if parsed["outcome"] == OUTCOME_RETRACTED:
            latest.pop(parsed["family"], None)
        else:
            latest[parsed["family"]] = parsed
    return [latest[family] for family in CLAIM_FAMILIES if family in latest]


def format_claims(claims: list[dict[str, Any]]) -> str:
    if not claims:
        return "No structured claims recorded. Record one with 'agentdir claim <family> --passed'."
    lines = [f"Recorded claims ({len(claims)}):"]
    for claim in claims:
        note = f" note={claim['note']}" if claim.get("note") else ""
        lines.append(f"- {claim['family']}={claim['outcome']}{note}")
    return "\n".join(lines)


def _parse_claim_row(row: dict[str, Any]) -> dict[str, Any] | None:
    family: str | None = None
    outcome: str | None = None
    note_lines: list[str] = []
    for line in str(row.get("body_text") or "").splitlines():
        if family is None and line.startswith("family="):
            family = line[len("family="):].strip()
        elif outcome is None and line.startswith("outcome="):
            outcome = line[len("outcome="):].strip()
        elif family is not None and outcome is not None:
            note_lines.append(line)
    if family not in CLAIM_FAMILIES or outcome not in CLAIM_OUTCOMES:
        return None
    note = "\n".join(note_lines).strip() or None
    return {
        "family": family,
        "outcome": outcome,
        "note": note,
        "date_utc": row.get("date_utc"),
        "file_path": row.get("file_path"),
    }
