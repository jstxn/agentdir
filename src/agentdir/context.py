from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .index import rebuild_index
from .memory import DEFAULT_MIN_SCORE, recent_session_summaries, search_memory
from .query import query_messages
from .review import evidence_rows, format_evidence, format_summary, summarize_session
from .sessions import read_current_session


def build_context_pack(
    root: str | Path,
    task: str,
    *,
    session_id: str | None = None,
    memory_limit: int = 8,
    evidence_limit: int = 20,
    recent_limit: int = 5,
    min_score: float = DEFAULT_MIN_SCORE,
) -> dict[str, Any]:
    rebuild_index(root)
    resolved_session = session_id
    if resolved_session is None:
        current = read_current_session(root)
        resolved_session = current.session_id if current else None

    memory_hits = search_memory(root, task, limit=memory_limit, min_score=min_score)
    recent = recent_session_summaries(root, limit=recent_limit)
    evidence = evidence_rows(root, resolved_session)[:evidence_limit] if resolved_session else []
    current_summary = summarize_session(root, resolved_session) if resolved_session else None

    return {
        "task": task,
        "session_id": resolved_session,
        "current_summary": current_summary,
        "memory_hits": memory_hits,
        "recent_session_summaries": recent,
        "evidence": evidence,
        "instructions": [
            "Use memory hits as retrieval hints, not proof.",
            "Use evidence rows for claims about commands, hooks, and diffs.",
            "Run fresh verification before reporting completion.",
        ],
    }


def format_context_pack(pack: dict[str, Any]) -> str:
    lines = [
        "# AgentDir Context Pack",
        "",
        f"Task: {pack['task']}",
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
                    f"  excerpt={excerpt(row.get('body_text') or '', 320)}",
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
        target.write_text(json.dumps(pack, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        target.write_text(format_context_pack(pack), encoding="utf-8")
    return target


def fenced(text: str, language: str) -> str:
    return f"```{language}\n{text}\n```"


def excerpt(text: str, limit: int) -> str:
    collapsed = " ".join(text.strip().split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."
