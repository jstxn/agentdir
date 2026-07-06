from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .index import rebuild_index
from .query import query_messages
from .sessions import require_current_session

EVIDENCE_FAMILIES = ("test", "lint", "typecheck", "build", "doctor", "release", "diagnostic")

_FAMILY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "test": (
        "pytest",
        "unittest",
        "test",
        "tests",
        "npm test",
        "pnpm test",
        "yarn test",
        "go test",
        "cargo test",
        "rspec",
    ),
    "lint": ("lint", "eslint", "ruff", "flake8", "prettier", "clippy", "golangci-lint"),
    "typecheck": ("typecheck", "type-check", "tsc", "mypy", "pyright", "sorbet", "staticcheck"),
    "build": ("build", "npm run build", "pnpm build", "yarn build", "cargo build", "go build", "make"),
    "doctor": ("doctor", "health", "diagnose", "diagnostic"),
    "release": ("release", "publish", "pack", "version", "tag", "twine", "npm publish"),
}

_DIAGNOSTIC_EVENTS = {"file.diff"}


def resolve_review_session(root: str | Path, session_id: str | None) -> str:
    if session_id:
        return session_id
    return require_current_session(root).session_id


def ensure_index(root: str | Path) -> None:
    rebuild_index(root)


def summarize_session(root: str | Path, session_id: str | None = None, *, rebuild: bool = True) -> dict[str, Any]:
    resolved = resolve_review_session(root, session_id)
    if rebuild:
        ensure_index(root)
    rows = query_messages(root, session_id=resolved, limit=10_000)
    counts = Counter(row.get("event_type") or "unknown" for row in rows)
    tool_results = [row for row in rows if row.get("event_type") == "tool.result"]
    failed_tools = [row for row in tool_results if row.get("tool_exit_code") not in (None, 0)]
    return {
        "session_id": resolved,
        "events": len(rows),
        "event_counts": dict(sorted(counts.items())),
        "tool_results": len(tool_results),
        "failed_tools": len(failed_tools),
        "first_event": rows[0]["date_header"] if rows else None,
        "last_event": rows[-1]["date_header"] if rows else None,
    }


def format_summary(summary: dict[str, Any]) -> str:
    lines = [
        f"session={summary['session_id']}",
        f"events={summary['events']}",
        f"tool_results={summary['tool_results']}",
        f"failed_tools={summary['failed_tools']}",
    ]
    for event_type, count in summary["event_counts"].items():
        lines.append(f"{event_type}={count}")
    return "\n".join(lines)


def evidence_rows(root: str | Path, session_id: str | None = None, *, rebuild: bool = True) -> list[dict[str, Any]]:
    resolved = resolve_review_session(root, session_id)
    if rebuild:
        ensure_index(root)
    rows = query_messages(root, session_id=resolved, limit=10_000)
    wanted = {"tool.call", "tool.result", "file.diff"}
    evidence = [
        row
        for row in rows
        if row.get("event_type") in wanted or str(row.get("event_type") or "").startswith("git.hook.")
    ]
    for row in evidence:
        row["family"] = classify_evidence(row)
        row["failed"] = evidence_failed(row)
        row["truncated"] = evidence_truncated(row)
    return evidence


def classify_evidence(row: dict[str, Any]) -> str:
    event_type = str(row.get("event_type") or "")
    if event_type in _DIAGNOSTIC_EVENTS or event_type.startswith("git.hook."):
        return "diagnostic"
    primary = " ".join(str(row.get(key) or "") for key in ("tool", "subject")).lower()
    for family in EVIDENCE_FAMILIES:
        if family == "diagnostic":
            continue
        if any(keyword in primary for keyword in _FAMILY_KEYWORDS.get(family, ())):
            return family
    haystack = _evidence_search_text(row)
    for family in EVIDENCE_FAMILIES:
        if family == "diagnostic":
            continue
        if any(keyword in haystack for keyword in _FAMILY_KEYWORDS.get(family, ())):
            return family
    return "diagnostic"


def evidence_failed(row: dict[str, Any]) -> bool:
    return row.get("event_type") == "tool.result" and row.get("tool_exit_code") not in (None, 0)


def evidence_truncated(row: dict[str, Any]) -> bool:
    if row.get("event_type") != "tool.result":
        return False
    # Only trust the structured prelude that run_tool writes before the
    # "stdout:" marker; captured tool output cannot spoof these lines.
    for line in str(row.get("body_text") or "").splitlines():
        if line == "stdout:":
            break
        if line in ("stdout_truncated=true", "stderr_truncated=true"):
            return True
    return False


def evidence_ref(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_type": row.get("event_type"),
        "family": row.get("family") or classify_evidence(row),
        "tool": row.get("tool"),
        "exit_code": row.get("tool_exit_code"),
        "subject": row.get("subject"),
        "date": row.get("date_header") or row.get("date_utc") or row.get("indexed_at"),
        "path": row.get("file_path"),
        "failed": row.get("failed") if "failed" in row else evidence_failed(row),
        "truncated": row.get("truncated") if "truncated" in row else evidence_truncated(row),
    }


def filter_evidence(
    rows: list[dict[str, Any]],
    *,
    family: str | None = None,
    failed: bool = False,
) -> list[dict[str, Any]]:
    if family and family not in EVIDENCE_FAMILIES:
        raise ValueError(f"unknown evidence family: {family}")
    filtered: list[dict[str, Any]] = []
    for row in rows:
        row_family = row.get("family") or classify_evidence(row)
        row_failed = row.get("failed") if "failed" in row else evidence_failed(row)
        if family and row_family != family:
            continue
        if failed and not row_failed:
            continue
        copied = dict(row)
        copied["family"] = row_family
        copied["failed"] = row_failed
        filtered.append(copied)
    return filtered


def evidence_brief(rows: list[dict[str, Any]], *, family: str | None = None, failed: bool = False) -> dict[str, Any]:
    filtered = filter_evidence(rows, family=family, failed=failed)
    grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in EVIDENCE_FAMILIES}
    for row in filtered:
        grouped[str(row.get("family") or classify_evidence(row))].append(row)
    failed_rows = [row for row in filtered if row.get("failed") or evidence_failed(row)]
    families: list[dict[str, Any]] = []
    latest_by_family: dict[str, dict[str, Any]] = {}
    for name in EVIDENCE_FAMILIES:
        items = grouped[name]
        if not items:
            continue
        latest = evidence_ref(items[-1])
        latest_by_family[name] = latest
        families.append(
            {
                "family": name,
                "total": len(items),
                "failed": sum(1 for row in items if row.get("failed") or evidence_failed(row)),
                "latest": latest,
            }
        )
    return {
        "families": families,
        "latest_by_family": latest_by_family,
        "failed_evidence": [evidence_ref(row) for row in failed_rows],
        "counts": {
            "total": len(filtered),
            "failed": len(failed_rows),
            "families": len(families),
        },
    }


def format_evidence_brief(brief: dict[str, Any]) -> str:
    lines: list[str] = []
    failed = brief.get("failed_evidence") or []
    if failed:
        lines.append("Failed evidence:")
        for item in failed:
            lines.append(_format_evidence_ref(item))
    families = brief.get("families") or []
    if families:
        if lines:
            lines.append("")
        lines.append("Latest evidence by family:")
        for family in families:
            lines.append(_format_evidence_ref(family["latest"], prefix=f"{family['family']}: "))
    if not lines:
        return "No evidence captured."
    return "\n".join(lines)


def timeline_rows(
    root: str | Path,
    session_id: str | None = None,
    *,
    limit: int = 100,
    rebuild: bool = True,
) -> list[dict[str, Any]]:
    resolved = resolve_review_session(root, session_id)
    if rebuild:
        ensure_index(root)
    rows = query_messages(root, session_id=resolved, limit=limit)
    return [timeline_ref(row) for row in rows]


def timeline_ref(row: dict[str, Any]) -> dict[str, Any]:
    event_type = row.get("event_type")
    item = {
        "date": row.get("date_header") or row.get("date_utc") or row.get("indexed_at"),
        "event_type": event_type,
        "subject": row.get("subject"),
        "tool": row.get("tool"),
        "exit_code": row.get("tool_exit_code"),
        "actor": row.get("from_actor"),
        "path": row.get("file_path"),
    }
    if event_type in {"tool.call", "tool.result", "file.diff"} or str(event_type or "").startswith("git.hook."):
        item["family"] = classify_evidence(row)
        item["failed"] = evidence_failed(row)
    return item


def format_timeline(rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for row in rows:
        date = row.get("date") or "unknown-date"
        event_type = row.get("event_type") or "unknown"
        subject = row.get("subject") or ""
        tool = row.get("tool") or ""
        exit_code = row.get("exit_code")
        family = row.get("family") or ""
        detail_parts = []
        if tool:
            detail_parts.append(str(tool))
        if family:
            detail_parts.append(f"family={family}")
        if exit_code is not None:
            detail_parts.append(f"exit={exit_code}")
        detail = " ".join(detail_parts)
        lines.append(f"{date} {event_type} {detail} {subject}".rstrip())
    return "\n".join(lines)


def format_evidence(rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for row in rows:
        date = row.get("date_header") or row.get("indexed_at") or "unknown-date"
        event_type = row.get("event_type") or "unknown"
        tool = row.get("tool") or ""
        exit_code = row.get("tool_exit_code")
        subject = row.get("subject") or ""
        file_path = row.get("file_path") or ""
        family = row.get("family") or classify_evidence(row)
        detail = f" exit={exit_code}" if exit_code is not None else ""
        family_detail = f" family={family}" if family else ""
        truncated_detail = " truncated=true" if (row.get("truncated") if "truncated" in row else evidence_truncated(row)) else ""
        lines.append(f"{date} {event_type} {tool}{detail}{family_detail}{truncated_detail} {subject} [{file_path}]")
    return "\n".join(lines)


def _evidence_search_text(row: dict[str, Any]) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in ("tool", "subject", "body_text", "event_type", "message_id")
    ).lower()


def _format_evidence_ref(item: dict[str, Any], *, prefix: str = "- ") -> str:
    tool = item.get("tool") or ""
    exit_code = item.get("exit_code")
    subject = item.get("subject") or ""
    date = item.get("date") or "unknown-date"
    exit_text = f" exit={exit_code}" if exit_code is not None else ""
    failed = " failed=true" if item.get("failed") else ""
    truncated = " truncated=true" if item.get("truncated") else ""
    return f"{prefix}{date} {item.get('event_type') or 'unknown'} {tool}{exit_text}{failed}{truncated} {subject}".rstrip()
