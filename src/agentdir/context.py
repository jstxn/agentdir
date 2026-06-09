from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .artifacts import add_artifact, artifact_headers, artifact_path
from .events import emit_event
from .federation import search_federated_memory
from .index import connect_index, rebuild_index
from .memory import DEFAULT_MIN_SCORE, RETRIEVAL_HYBRID, recent_session_summaries, search_memory
from .query import query_messages
from .review import evidence_rows, format_evidence, format_summary, summarize_session
from .sessions import read_current_session
from .store import AgentDirError

CONTEXT_PROTOCOL = "agentdir.context-pack.v1"
CONTEXT_ENFORCEMENT_MODE = "advisory"
EVENT_CONTEXT_PACK_CREATED = "context.pack.created"
EVENT_CONTEXT_PACK_CONSUMED = "context.pack.consumed"
EVENT_CONTEXT_SOURCES_CITED = "context.sources.cited"
HEADER_PROTOCOL = "X-AgentDir-Protocol"
HEADER_PACK_ID = "X-AgentDir-Pack-Id"
HEADER_CONTEXT_QUERY = "X-AgentDir-Context-Query"
HEADER_CONTEXT_SCOPE = "X-AgentDir-Context-Scope"
HEADER_SOURCE_ID = "X-AgentDir-Source-Id"
HEADER_CONSUMPTION_PURPOSE = "X-AgentDir-Consumption-Purpose"
HEADER_ENFORCEMENT_MODE = "X-AgentDir-Enforcement-Mode"
CONSUMPTION_PURPOSES = ("plan", "tool", "answer", "handoff")
EVIDENCE_EVENTS = {"tool.call", "tool.result", "file.diff"}


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
    retrieval_mode: str = RETRIEVAL_HYBRID,
    exclude_session_from_memory: bool = False,
) -> dict[str, Any]:
    rebuild_index(root)
    resolved_session = session_id
    if resolved_session is None:
        current = read_current_session(root)
        resolved_session = current.session_id if current else None

    search_limit = memory_limit if not (resolved_session and exclude_session_from_memory) else max(memory_limit * 3, memory_limit + 5)
    memory_hits = (
        search_federated_memory(
            root,
            task,
            limit=search_limit,
            min_score=min_score,
            retrieval_mode=retrieval_mode,
            group=federation_group,
        )
        if federated or federation_group
        else search_memory(
            root,
            task,
            limit=search_limit,
            min_score=min_score,
            retrieval_mode=retrieval_mode,
        )
    )
    if resolved_session and exclude_session_from_memory:
        memory_hits = [row for row in memory_hits if row.get("session_id") != resolved_session][:memory_limit]
        recent = [
            row
            for row in recent_session_summaries(root, limit=recent_limit + 5)
            if row.get("session_id") != resolved_session
        ][:recent_limit]
    else:
        memory_hits = memory_hits[:memory_limit]
        recent = recent_session_summaries(root, limit=recent_limit)
    evidence = evidence_rows(root, resolved_session)[:evidence_limit] if resolved_session else []
    current_summary = summarize_session(root, resolved_session) if resolved_session else None

    return {
        "task": task,
        "session_id": resolved_session,
        "current_summary": current_summary,
        "memory_hits": memory_hits,
        "federated": bool(federated or federation_group),
        "federation_group": federation_group,
        "retrieval_mode": retrieval_mode,
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
    memory_sources = [_source_entry(row, origin="memory_hit") for row in pack.get("memory_hits") or []]
    recent_summaries = [
        _source_entry(row, origin="recent_summary")
        for row in pack.get("recent_session_summaries") or []
    ]
    evidence = [_source_entry(row, origin="evidence") for row in pack.get("evidence") or []]
    sources = _dedupe_sources([*memory_sources, *recent_summaries, *evidence])
    return {
        "protocol": CONTEXT_PROTOCOL,
        "pack_id": resolved_pack_id,
        "task": pack["task"],
        "session_id": pack.get("session_id"),
        "generated_at": now_iso(),
        "selection_policy": selection_policy or {},
        "federated": bool(pack.get("federated")),
        "federation_group": pack.get("federation_group"),
        "retrieval_mode": pack.get("retrieval_mode") or RETRIEVAL_HYBRID,
        "sources": sources,
        "memory_hits": memory_sources,
        "recent_summaries": recent_summaries,
        "evidence": evidence,
        "instructions": pack.get("instructions") or [],
        "enforcement_boundary": (
            "advisory: AgentDir records that a cooperative agent requested, "
            "consumed, and cited context, but it cannot prove model attention."
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
        HEADER_CONTEXT_QUERY: manifest["task"],
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
    event = _context_event(root, pack_id, EVENT_CONTEXT_PACK_CREATED, rebuild=rebuild)
    sha = event["headers"].get("X-AgentDir-Blob-SHA256")
    if not sha:
        raise AgentDirError(f"Context pack has no manifest artifact: {pack_id}")
    manifest_file = artifact_path(root, sha)
    if not manifest_file.is_file():
        raise AgentDirError(f"Context pack manifest artifact is missing: {sha}")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if manifest.get("protocol") != CONTEXT_PROTOCOL:
        raise AgentDirError(f"Unsupported context pack protocol: {manifest.get('protocol')}")
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
    manifest = read_context_manifest(root, pack_id)
    selected = _select_manifest_sources(manifest, source_ids)
    resolved_session = session_id or manifest.get("session_id")
    if not resolved_session:
        raise AgentDirError("Context consumption requires a session")
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


def cite_context_sources(
    root: str | Path,
    *,
    pack_id: str,
    source_ids: list[str] | None = None,
    output_format: str = "md",
    session_id: str | None = None,
    actor: str = "agent",
) -> dict[str, Any]:
    manifest = read_context_manifest(root, pack_id)
    selected = _select_manifest_sources(manifest, source_ids or _default_citation_sources(root, pack_id, manifest))
    resolved_session = session_id or manifest.get("session_id")
    if not resolved_session:
        raise AgentDirError("Context citation requires a session")
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
        rebuild_index(root)
    manifest = read_context_manifest(root, pack_id, rebuild=False)
    events = _context_events(root, pack_id, rebuild=False)
    consumed = _unique_source_ids(
        source_id
        for event in events
        if event["event_type"] == EVENT_CONTEXT_PACK_CONSUMED
        for source_id in event["source_ids"]
    )
    cited = _unique_source_ids(
        source_id
        for event in events
        if event["event_type"] == EVENT_CONTEXT_SOURCES_CITED
        for source_id in event["source_ids"]
    )
    source_by_id = {source["source_id"]: source for source in manifest["sources"]}
    evidence_backed = [
        source_id
        for source_id in cited
        if source_by_id.get(source_id, {}).get("source_class") == "evidence"
    ]
    return {
        "protocol": CONTEXT_PROTOCOL,
        "pack_id": pack_id,
        "task": manifest.get("task"),
        "session_id": manifest.get("session_id"),
        "retrieved_count": len(manifest["sources"]),
        "consumed_count": len(consumed),
        "cited_count": len(cited),
        "evidence_backed_count": len(evidence_backed),
        "source_counts": manifest.get("source_counts", {}),
        "consumed_source_ids": consumed,
        "cited_source_ids": cited,
        "evidence_backed_source_ids": evidence_backed,
        "events": events,
        "enforcement_mode": CONTEXT_ENFORCEMENT_MODE,
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
    lines = [
        f"pack_id={audit['pack_id']}",
        f"task={audit.get('task') or ''}",
        f"session={audit.get('session_id') or ''}",
        f"retrieved={audit['retrieved_count']}",
        f"consumed={audit['consumed_count']}",
        f"cited={audit['cited_count']}",
        f"evidence_backed={audit['evidence_backed_count']}",
        f"enforcement={audit['enforcement_mode']}",
    ]
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


def _source_entry(row: dict[str, Any], *, origin: str) -> dict[str, Any]:
    source_id = _source_id(row)
    body = row.get("body_text") or ""
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
        "body_sha256": row.get("body_sha256"),
        "text_sha256": row.get("text_sha256"),
        "excerpt": excerpt(body, 320),
    }


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


def _context_event(root: str | Path, pack_id: str, event_type: str, *, rebuild: bool = True) -> dict[str, Any]:
    events = _context_events(root, pack_id, event_type=event_type, rebuild=rebuild)
    if not events:
        raise AgentDirError(f"Unknown context pack: {pack_id}")
    return events[-1]


def _context_events(
    root: str | Path,
    pack_id: str,
    *,
    event_type: str | None = None,
    rebuild: bool = True,
) -> list[dict[str, Any]]:
    if rebuild:
        rebuild_index(root)
    clauses = ["hp.name = ?", "hp.value = ?"]
    params: list[Any] = [HEADER_PACK_ID, pack_id]
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
            row["headers"] = _header_map(headers)
            row["source_ids"] = [header["value"] for header in headers if header["name"] == HEADER_SOURCE_ID]
    return rows


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


def _default_citation_sources(root: str | Path, pack_id: str, manifest: dict[str, Any]) -> list[str]:
    consumed = audit_context_pack(root, pack_id)["consumed_source_ids"]
    if consumed:
        return consumed
    return [source["source_id"] for source in manifest["sources"]]


def _unique_source_ids(source_ids: Any) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for source_id in source_ids:
        if source_id in seen:
            continue
        seen.add(source_id)
        unique.append(source_id)
    return unique


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
