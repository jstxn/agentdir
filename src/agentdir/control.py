from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from .context import (
    audit_context_pack,
    build_context_pack,
    emit_context_pack,
)
from .daemon import memory_daemon_status
from .doctor import run_doctor
from .events import emit_event
from .federation import doctor_registered_roots, list_registered_roots, list_root_groups
from .git import git_branch, git_head, git_status_short, workspace_name
from .index import rebuild_index
from .memory import DEFAULT_MIN_SCORE, RETRIEVAL_HYBRID, memory_stats
from .query import query_messages
from .review import evidence_rows, format_evidence, format_summary, summarize_session
from .rendering import rich_status
from .sessions import SessionState, end_session, ensure_session, read_current_session, require_current_session
from .store import AgentDirError, init_root, paths_for, require_root

EVENT_WORK_STARTED = "work.started"
EVENT_WORK_FINISHED = "work.finished"
EVENT_WORK_REPORT_FINAL = "work.report.final"


def adopt_repo(
    root: str | Path,
    *,
    install_hooks_result: list[dict[str, Any]],
    codex_skill_path: str | None,
) -> dict[str, Any]:
    paths = init_root(root)
    doctor = run_doctor(paths.root)
    return {
        "root": str(paths.root),
        "version": _store_version(paths.root),
        "hooks": install_hooks_result,
        "codex_skill": codex_skill_path,
        "doctor": doctor.as_dict(),
        "next": "agentdir work start \"<task>\"",
    }


def build_status(
    root: str | Path,
    *,
    scope: str | None = None,
    rebuild: bool = True,
) -> dict[str, Any]:
    paths = paths_for(root)
    initialized = (paths.meta / "VERSION").is_file()
    status: dict[str, Any] = {
        "root": {
            "path": str(paths.root),
            "scope": scope or "default",
            "initialized": initialized,
            "version": _store_version(paths.root) if initialized else None,
        },
        "git": {
            "workspace": workspace_name(),
            "branch": git_branch(),
            "head": git_head(),
            "dirty": bool(git_status_short()),
        },
    }
    if not initialized:
        doctor = run_doctor(paths.root)
        status.update(
            {
                "session": {"active": False, "current": None},
                "context": {"latest_pack": None},
                "evidence": {"count": 0},
                "memory": {"indexed": False},
                "federation": {"roots": [], "groups": []},
                "health": doctor.as_dict(),
            }
        )
        return status

    if rebuild:
        rebuild_index(paths.root)

    current = read_current_session(paths.root)
    summary: dict[str, Any] | None = None
    evidence_count = 0
    if current:
        summary = summarize_session(paths.root, current.session_id)
        evidence_count = len(evidence_rows(paths.root, current.session_id))

    latest_pack = latest_context_pack(
        paths.root,
        current.session_id if current else None,
        fallback_any=True,
    )
    memory = _safe_memory_stats(paths.root)
    memory["daemon"] = memory_daemon_status(paths.root)
    roots = doctor_registered_roots(paths.root)
    groups = list_root_groups(paths.root)
    doctor = run_doctor(paths.root)
    status.update(
        {
            "session": {
                "active": current is not None and current.status == "active",
                "current": asdict(current) if current else None,
                "summary": summary,
            },
            "context": {"latest_pack": latest_pack},
            "evidence": {"count": evidence_count},
            "memory": memory,
            "federation": {
                "roots": roots,
                "groups": groups,
                "registered_roots": len(roots),
                "stale_roots": sum(1 for root in roots if root.get("stale")),
            },
            "health": doctor.as_dict(),
        }
    )
    return status


def format_status(status: dict[str, Any]) -> str:
    rendered = rich_status(status)
    if rendered is not None:
        return rendered
    root = status["root"]
    session = status["session"]
    context = status["context"]
    evidence = status["evidence"]
    memory = status["memory"]
    federation = status["federation"]
    health = status["health"]
    lines = [
        "AgentDir Status",
        "",
        f"root={root['path']}",
        f"scope={root['scope']}",
        f"initialized={str(root['initialized']).lower()}",
        f"version={root.get('version') or ''}",
        "",
        f"session_active={str(session['active']).lower()}",
        f"session={session['current']['session_id'] if session.get('current') else ''}",
        f"evidence={evidence['count']}",
        f"context_pack={context['latest_pack']['pack_id'] if context.get('latest_pack') else ''}",
        "",
        f"memory_indexed={str(memory.get('indexed', False)).lower()}",
        f"memory_documents={memory.get('memory_documents', 0)}",
        f"passages={memory.get('passages', 0)}",
        "",
        f"registered_roots={federation['registered_roots']}",
        f"stale_roots={federation['stale_roots']}",
        "",
        f"doctor_ok={str(health['ok']).lower()}",
    ]
    for warning in health.get("warnings", []):
        lines.append(f"warning: {warning}")
    for error in health.get("errors", []):
        lines.append(f"error: {error}")
    return "\n".join(lines).rstrip() + "\n"


def start_work(
    root: str | Path,
    task: str,
    *,
    actor: str = "agent",
    emit_context: bool = False,
    federated: bool = False,
    federation_group: str | None = None,
    memory_limit: int = 8,
    evidence_limit: int = 20,
    recent_limit: int = 5,
    min_score: float = DEFAULT_MIN_SCORE,
    retrieval_mode: str = RETRIEVAL_HYBRID,
) -> dict[str, Any]:
    paths = init_root(root)
    session = ensure_session(paths.root, title=task, actor=actor)
    emit_event(
        paths.root,
        session_id=session.session_id,
        event_type=EVENT_WORK_STARTED,
        subject=f"work started: {task}",
        body=f"task={task}\nsession_id={session.session_id}",
        from_actor=actor,
        workspace=session.workspace,
        git_head=session.git_head,
    )
    pack = build_context_pack(
        paths.root,
        task,
        session_id=session.session_id,
        memory_limit=memory_limit,
        evidence_limit=evidence_limit,
        recent_limit=recent_limit,
        min_score=min_score,
        federated=federated or bool(federation_group),
        federation_group=federation_group,
        retrieval_mode=retrieval_mode,
    )
    emitted = None
    if emit_context:
        emitted = emit_context_pack(
            paths.root,
            pack,
            selection_policy={
                "memory_limit": memory_limit,
                "evidence_limit": evidence_limit,
                "recent_limit": recent_limit,
                "min_score": min_score,
                "federated": federated or bool(federation_group),
                "federation_group": federation_group,
                "retrieval_mode": retrieval_mode,
            },
            actor=actor,
            scope=federation_group or ("federated" if federated else "project"),
        )
    return {
        "session": asdict(session),
        "task": task,
        "context": {
            "memory_hits": len(pack.get("memory_hits") or []),
            "evidence": len(pack.get("evidence") or []),
            "recent_session_summaries": len(pack.get("recent_session_summaries") or []),
            "federated": bool(pack.get("federated")),
            "federation_group": federation_group,
        },
        "context_pack": emitted.manifest if emitted else None,
        "event_path": str(emitted.event_path) if emitted else None,
    }


def format_work_start(result: dict[str, Any]) -> str:
    session = result["session"]
    context = result["context"]
    lines = [
        f"session={session['session_id']}",
        f"task={result['task']}",
        f"memory_hits={context['memory_hits']}",
        f"evidence={context['evidence']}",
        f"recent_session_summaries={context['recent_session_summaries']}",
        f"federated={str(context['federated']).lower()}",
    ]
    if context.get("federation_group"):
        lines.append(f"group={context['federation_group']}")
    if result.get("context_pack"):
        lines.append(f"context_pack={result['context_pack']['pack_id']}")
    return "\n".join(lines) + "\n"


def finish_work(
    root: str | Path,
    *,
    session_id: str | None = None,
    actor: str = "agent",
    run_health_check: bool = True,
    end: bool = True,
) -> dict[str, Any]:
    paths = require_root(root)
    session = _resolve_session(paths.root, session_id)
    report = build_final_report(
        paths.root,
        session_id=session.session_id,
        run_health_check=run_health_check,
    )
    rendered = format_final_report(report)
    event = emit_event(
        paths.root,
        session_id=session.session_id,
        event_type=EVENT_WORK_REPORT_FINAL,
        subject="final report",
        body=rendered,
        from_actor=actor,
        workspace=session.workspace,
        git_head=git_head(),
    )
    emit_event(
        paths.root,
        session_id=session.session_id,
        event_type=EVENT_WORK_FINISHED,
        subject=f"work finished: {session.title}",
        body=f"session_id={session.session_id}\nreport_event={event.path}",
        from_actor=actor,
        workspace=session.workspace,
        git_head=git_head(),
    )
    ended = None
    if end:
        ended = end_session(
            paths.root,
            summary="AgentDir work finished; final report emitted.",
            actor=actor,
        )
    return {
        "report": report,
        "rendered": rendered,
        "event_path": str(event.path),
        "ended_session": asdict(ended) if ended else None,
    }


def build_final_report(
    root: str | Path,
    *,
    session_id: str | None = None,
    run_health_check: bool = True,
) -> dict[str, Any]:
    paths = require_root(root)
    session = _resolve_session(paths.root, session_id)
    summary = summarize_session(paths.root, session.session_id)
    evidence = evidence_rows(paths.root, session.session_id)
    latest_pack = latest_context_pack(paths.root, session.session_id)
    context_audit = None
    if latest_pack:
        try:
            context_audit = audit_context_pack(paths.root, latest_pack["pack_id"])
        except AgentDirError as exc:
            context_audit = {"error": str(exc), "pack_id": latest_pack["pack_id"]}
    doctor = run_doctor(paths.root).as_dict() if run_health_check else None
    gaps = _known_gaps(summary, evidence, context_audit, doctor)
    return {
        "task": session.title,
        "session": asdict(session),
        "summary": summary,
        "evidence": evidence,
        "context": {
            "latest_pack": latest_pack,
            "audit": context_audit,
        },
        "git": {
            "workspace": session.workspace,
            "branch": git_branch(),
            "head": git_head(),
            "dirty": bool(git_status_short()),
        },
        "health": doctor,
        "known_gaps": gaps,
    }


def format_final_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    context = report["context"]
    health = report.get("health")
    lines = [
        "# AgentDir Final Report",
        "",
        "## Task",
        "",
        report["task"],
        "",
        "## Session",
        "",
        f"- session: `{summary['session_id']}`",
        f"- events: {summary['events']}",
        f"- tool_results: {summary['tool_results']}",
        f"- failed_tools: {summary['failed_tools']}",
        "",
        "## Evidence",
        "",
    ]
    if report["evidence"]:
        lines.append("```text")
        lines.append(format_evidence(report["evidence"]))
        lines.append("```")
    else:
        lines.append("No evidence captured.")
    lines.extend(["", "## Context", ""])
    latest_pack = context.get("latest_pack")
    audit = context.get("audit")
    if latest_pack:
        lines.append(f"- pack: `{latest_pack['pack_id']}`")
    else:
        lines.append("- pack: none")
    if audit and not audit.get("error"):
        lines.append(f"- retrieved: {audit['retrieved_count']}")
        lines.append(f"- consumed: {audit['consumed_count']}")
        lines.append(f"- cited: {audit['cited_count']}")
        lines.append(f"- evidence_backed: {audit['evidence_backed_count']}")
    elif audit and audit.get("error"):
        lines.append(f"- audit_error: {audit['error']}")
    lines.extend(["", "## Health", ""])
    if health:
        lines.append(f"- doctor_ok: {str(health['ok']).lower()}")
        for warning in health.get("warnings", []):
            lines.append(f"- warning: {warning}")
        for error in health.get("errors", []):
            lines.append(f"- error: {error}")
    else:
        lines.append("- doctor: not run")
    lines.extend(["", "## Known Gaps", ""])
    if report["known_gaps"]:
        for gap in report["known_gaps"]:
            lines.append(f"- {gap}")
    else:
        lines.append("- none")
    return "\n".join(lines).rstrip() + "\n"


def latest_context_pack(
    root: str | Path,
    session_id: str | None = None,
    *,
    fallback_any: bool = False,
) -> dict[str, Any] | None:
    rebuild_index(root)
    rows = query_messages(
        root,
        session_id=session_id,
        event_type="context.pack.created",
        limit=10_000,
    )
    if not rows and session_id and fallback_any:
        rows = query_messages(root, event_type="context.pack.created", limit=10_000)
    for row in reversed(rows):
        pack_id = _pack_id_from_body(row.get("body_text") or "")
        if pack_id:
            return {
                "pack_id": pack_id,
                "session_id": row.get("session_id"),
                "event_path": row.get("file_path"),
                "subject": row.get("subject"),
                "date_utc": row.get("date_utc"),
            }
    return None


def _resolve_session(root: str | Path, session_id: str | None) -> SessionState:
    if session_id:
        current = read_current_session(root)
        if current and current.session_id == session_id:
            return current
        summary = summarize_session(root, session_id)
        return SessionState(
            session_id=session_id,
            title=session_id,
            actor="agent",
            workspace=workspace_name(),
            git_head=git_head(),
            started_at=summary.get("first_event") or "",
            status="unknown",
        )
    return require_current_session(root)


def _safe_memory_stats(root: str | Path) -> dict[str, Any]:
    try:
        stats = memory_stats(root)
    except Exception as exc:
        return {"indexed": False, "error": str(exc)}
    return {"indexed": True, **stats}


def _store_version(root: str | Path) -> str | None:
    path = paths_for(root).meta / "VERSION"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip()


def _pack_id_from_body(body: str) -> str | None:
    for line in body.splitlines():
        if line.startswith("pack_id="):
            return line.split("=", 1)[1].strip() or None
    return None


def _known_gaps(
    summary: dict[str, Any],
    evidence: list[dict[str, Any]],
    context_audit: dict[str, Any] | None,
    doctor: dict[str, Any] | None,
) -> list[str]:
    gaps: list[str] = []
    if summary.get("failed_tools"):
        gaps.append(f"{summary['failed_tools']} captured tool result(s) failed")
    if not evidence:
        gaps.append("No evidence captured")
    if context_audit is None:
        gaps.append("No context pack recorded")
    elif context_audit.get("error"):
        gaps.append(f"Context audit failed: {context_audit['error']}")
    else:
        if context_audit.get("retrieved_count", 0) and not context_audit.get("consumed_count", 0):
            gaps.append("Context pack emitted but no sources consumed")
        if context_audit.get("consumed_count", 0) and not context_audit.get("cited_count", 0):
            gaps.append("Context sources consumed but not cited")
    if doctor:
        for warning in doctor.get("warnings", []):
            gaps.append(f"Doctor warning: {warning}")
        for error in doctor.get("errors", []):
            gaps.append(f"Doctor error: {error}")
    return gaps
