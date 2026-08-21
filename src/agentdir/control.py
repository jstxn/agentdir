from __future__ import annotations

import json
import shlex
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .audit import (
    audit_claims,
    audit_recorded_claims,
    audit_session,
    claims_audit_gaps,
    session_audit_gaps,
)
from .context import (
    audit_context_pack,
    brief_context_manifest,
    build_context_pack,
    emit_context_pack,
    read_context_manifest,
    review_context_pack,
)
from .context_repository import list_context_packs
from .context_projection import (
    build_context_projection,
    context_needs_attention as _context_needs_attention,
)
from .daemon import memory_daemon_status
from .context_expansion import (
    ExpansionDelivery,
    empty_context_expansion_audit,
    expand_context_source,
)
from .doctor import run_doctor
from .events import emit_event
from .federation import doctor_registered_roots, list_registered_roots, list_root_groups
from .git import git_branch, git_head, git_status_short, workspace_name
from .index import update_index
from .locking import lifecycle_lock
from .memory import (
    DEFAULT_MIN_SCORE,
    RETRIEVAL_AUTO,
    memory_stats,
    resolve_retrieval_mode,
)
from .review import evidence_brief, evidence_rows, format_evidence, format_summary, summarize_session
from .rendering import rich_status
from .sessions import (
    SessionState,
    end_session,
    ensure_session,
    read_current_session,
    read_last_session,
    read_session_state,
    require_current_session,
    session_git_cwd,
    session_pointer_lock,
    write_session_state,
)
from .store import AgentDirError, AgentDirStateError, init_root, paths_for, require_root

EVENT_WORK_STARTED = "work.started"
EVENT_WORK_FINISHED = "work.finished"
EVENT_WORK_REPORT_FINAL = "work.report.final"


def adopt_repo(
    root: str | Path,
    *,
    install_hooks_result: list[dict[str, Any]],
    codex_skill_path: str | None,
    generic_guidance_path: str | None = None,
    integrations: list[dict[str, Any]] | None = None,
    gitignore: dict[str, Any] | None = None,
) -> dict[str, Any]:
    paths = init_root(root)
    doctor = run_doctor(paths.root)
    return {
        "root": str(paths.root),
        "version": _store_version(paths.root),
        "hooks": install_hooks_result,
        "codex_skill": codex_skill_path,
        "generic_guidance": generic_guidance_path,
        "integrations": integrations or [],
        "gitignore": gitignore,
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
                "session": {"active": False, "current": None, "latest": None, "summary": None},
                "context": {"latest_pack": None},
                "evidence": {"session_id": None, "count": 0},
                "memory": {"indexed": False},
                "federation": {"roots": [], "groups": []},
                "health": doctor.as_dict(),
            }
        )
        return status

    if rebuild:
        update_index(paths.root)

    current = read_current_session(paths.root)
    latest = current or read_last_session(paths.root)
    summary: dict[str, Any] | None = None
    evidence_count = 0
    if latest:
        summary = summarize_session(paths.root, latest.session_id, rebuild=False)
        evidence_count = len(evidence_rows(paths.root, latest.session_id, rebuild=False))

    context_projection = build_context_projection(
        paths.root,
        latest.session_id if latest else None,
        fallback_any=latest is None,
        rebuild=False,
    )
    packs = context_projection["packs"]
    latest_pack = context_projection["latest_pack"]
    pack_audits = context_projection["pack_audits"]
    context_audit = context_projection["audit"]
    attention_audit = context_projection["attention_audit"]
    blocking_packs = context_projection["blocking_packs"]
    attention_packs = context_projection["attention_packs"]
    expansion_inventory = context_projection["expansion_receipt_inventory"]
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
                "latest": asdict(latest) if latest else None,
                "summary": summary,
            },
            "context": {
                "latest_pack": latest_pack,
                "audit": context_audit,
                "attention_audit": attention_audit,
                "pack_audits": pack_audits,
                "pack_count": len(packs),
                "blocking_packs": blocking_packs,
                "attention_packs": attention_packs,
                "expansion_receipt_inventory": expansion_inventory,
            },
            "evidence": {
                "session_id": latest.session_id if latest else None,
                "count": evidence_count,
            },
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
        f"session_latest={session['latest']['session_id'] if session.get('latest') else ''}",
        f"evidence_session={evidence.get('session_id') or ''}",
        f"evidence={evidence['count']}",
        f"context_pack={context['latest_pack']['pack_id'] if context.get('latest_pack') else ''}",
        f"context_pack_count={context.get('pack_count', 0)}",
        f"context_blocking_packs={','.join(context.get('blocking_packs') or [])}",
        f"context_attention_packs={','.join(context.get('attention_packs') or [])}",
        f"context_audit_pack={(context.get('audit') or {}).get('pack_id') or ''}",
        f"context_attention_audit_pack={(context.get('attention_audit') or {}).get('pack_id') or ''}",
    ]
    expansion_inventory = context.get("expansion_receipt_inventory") or {}
    lines.extend(
        [
            f"context_expansion_orphan_receipts={expansion_inventory.get('orphan_event_count', 0)}",
            "context_expansion_inventory_valid="
            f"{str(expansion_inventory.get('receipts_valid', True)).lower()}",
        ]
    )
    context_audit = context.get("audit") or {}
    if context_audit and not context_audit.get("error"):
        expansion = context_audit.get("expansion") or empty_context_expansion_audit()
        lines.extend(
            [
                f"context_retrieved={context_audit.get('retrieved_count', 0)}",
                f"context_presented={context_audit.get('presented_count', 0)}",
                f"context_reviewed={context_audit.get('reviewed_count', 0)}",
                f"context_used={context_audit.get('used_count', 0)}",
                f"context_consumed={context_audit.get('consumed_count', 0)}",
                f"context_additional_consumed={context_audit.get('additional_consumed_count', 0)}",
                f"context_dismissed={context_audit.get('dismissed_count', 0)}",
                f"context_pending={context_audit.get('pending_count', 0)}",
                f"context_cited={context_audit.get('cited_count', 0)}",
                f"context_cited_without_use={context_audit.get('cited_without_use_count', 0)}",
                f"context_review_status={context_audit.get('review_status') or ''}",
                f"context_decision_complete={str(bool(context_audit.get('decision_complete'))).lower()}",
                f"context_expanded={expansion.get('expanded_source_count', 0)}",
                f"context_expanded_before_decision={expansion.get('expanded_before_decision_count', 0)}",
                f"context_expanded_after_decision={expansion.get('expanded_after_decision_count', 0)}",
                f"context_used_without_prior_expansion={expansion.get('used_without_prior_expansion_count', 0)}",
                f"context_expansion_receipts={expansion.get('receipt_event_count', 0)}",
                f"context_expansion_receipts_valid={str(expansion.get('receipts_valid', True)).lower()}",
            ]
        )
    elif context_audit.get("error"):
        lines.extend(
            [
                "context_review_status=error",
                f"context_audit_error={context_audit['error']}",
            ]
        )
    lines.extend(
        [
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
    )
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
    emit_context: bool = True,
    federated: bool = False,
    federation_group: str | None = None,
    memory_limit: int = 8,
    evidence_limit: int = 20,
    recent_limit: int = 5,
    min_score: float = DEFAULT_MIN_SCORE,
    retrieval_mode: str = RETRIEVAL_AUTO,
    invocation: tuple[str, ...] = ("agentdir",),
) -> dict[str, Any]:
    paths = init_root(root)
    with lifecycle_lock(paths.root, "work-start"):
        with session_pointer_lock(paths.root):
            return _start_work_locked(
                paths.root,
                task,
                actor=actor,
                emit_context=emit_context,
                federated=federated,
                federation_group=federation_group,
                memory_limit=memory_limit,
                evidence_limit=evidence_limit,
                recent_limit=recent_limit,
                min_score=min_score,
                retrieval_mode=retrieval_mode,
                invocation=invocation,
            )


def _start_work_locked(
    root: Path,
    task: str,
    *,
    actor: str,
    emit_context: bool,
    federated: bool,
    federation_group: str | None,
    memory_limit: int,
    evidence_limit: int,
    recent_limit: int,
    min_score: float,
    retrieval_mode: str,
    invocation: tuple[str, ...],
) -> dict[str, Any]:
    paths = paths_for(root)
    session = ensure_session(paths.root, title=task, actor=actor)
    update_index(paths.root)
    previous_context = build_context_projection(
        paths.root,
        session.session_id,
        rebuild=False,
    )
    previous_audit = previous_context["blocking_audit"]
    if previous_audit:
        context_prefix = _scoped_command_prefix(
            invocation,
            ("work", "context"),
            paths.root,
        )
        previous_pack_id = previous_audit["pack_id"]
        if previous_audit.get("error"):
            raise AgentDirStateError(
                f"Context pack {previous_pack_id} cannot be audited and cannot be superseded: "
                f"{previous_audit['error']}"
            )
        if not previous_audit.get("finish_allowed", False):
            if previous_audit.get("review_status") != "pending":
                raise AgentDirStateError(
                    f"Context pack {previous_pack_id} is "
                    f"{previous_audit.get('review_status') or 'invalid'} and cannot be superseded"
                )
            raise AgentDirStateError(
                f"Context review is still pending for {previous_pack_id}. "
                f"Re-open it with `{context_prefix} --show --pack {previous_pack_id}`, then run "
                f"`{context_prefix} --pack {previous_pack_id} "
                "--use <number> [--use <number> ...] --reason \"<how they help>\"` "
                "in one terminal review, or record a reasoned no-relevant decision."
            )
    if session.title != task:
        session.title = task
        write_session_state(paths.root, session)
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
    if emit_context:
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
            exclude_session_from_memory=True,
            rebuild=False,
        )
    else:
        effective_retrieval_mode = resolve_retrieval_mode(paths.root, retrieval_mode)
        pack = {
            "task": task,
            "retrieval_query": "",
            "retrieval_query_state": "disabled",
            "session_id": session.session_id,
            "memory_hits": [],
            "evidence": [],
            "recent_session_summaries": [],
            "federated": False,
            "federation_group": None,
            "retrieval_mode": effective_retrieval_mode,
            "requested_retrieval_mode": retrieval_mode,
            "instructions": [],
        }
    emitted = emit_context_pack(
        paths.root,
        pack,
        selection_policy={
            "context_enabled": emit_context,
            "memory_limit": memory_limit if emit_context else 0,
            "evidence_limit": evidence_limit if emit_context else 0,
            "recent_limit": recent_limit if emit_context else 0,
            "min_score": min_score,
            "federated": (federated or bool(federation_group)) if emit_context else False,
            "federation_group": federation_group if emit_context else None,
            "retrieval_mode": pack.get("retrieval_mode"),
            "requested_retrieval_mode": retrieval_mode,
        },
        actor=actor,
        scope=(federation_group or ("federated" if federated else "project")) if emit_context else "disabled",
    )
    briefing = brief_context_manifest(emitted.manifest)
    return {
        "session": asdict(session),
        "task": task,
        "context": {
            "memory_hits": len(pack.get("memory_hits") or []),
            "evidence": len(pack.get("evidence") or []),
            "recent_session_summaries": len(pack.get("recent_session_summaries") or []),
            "federated": bool(pack.get("federated")),
            "federation_group": federation_group,
            "retrieval_query": pack.get("retrieval_query") if "retrieval_query" in pack else task,
        },
        "context_briefing": briefing,
        "context_pack": emitted.manifest,
        "event_path": str(emitted.event_path),
    }


def format_work_start(
    result: dict[str, Any],
    *,
    invocation: tuple[str, ...] = ("agentdir",),
    command_root: str | Path | None = None,
) -> str:
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
    briefing = result.get("context_briefing") or {}
    lines.extend(
        [
            f"context_match={briefing.get('match_state') or 'no_context_available'}",
            f"context_presented={briefing.get('presented_count', 0)}",
            f"context_omitted={briefing.get('omitted_count', 0)}",
        ]
    )
    for source in briefing.get("sources") or []:
        subject = " ".join(str(source.get("subject") or "").split())
        excerpt = " ".join(str(source.get("excerpt") or "").split())
        if len(excerpt) > 220:
            excerpt = excerpt[:217] + "..."
        lines.append(
            f"[{source['ref']}] {source['match_quality']} "
            f"{source.get('source_role') or source['source_class']}: "
            f"{subject or '(untitled context)'}"
        )
        lines.append(f"    preview: {excerpt or '(no excerpt)'}")
    audit = result.get("context_audit") or None
    if result.get("context_pack") and briefing.get("sources"):
        pack_id = result["context_pack"]["pack_id"]
        context_prefix = _scoped_command_prefix(
            invocation,
            ("work", "context"),
            command_root,
        )
        audit_prefix = _scoped_command_prefix(
            invocation,
            ("audit", "context"),
            command_root,
        )
        explain_source = next(
            (
                source
                for source in briefing.get("sources") or []
                if (source.get("next_actions") or {}).get("explain")
            ),
            None,
        )
        if explain_source is None:
            lines.append("")
        lines.append(f"context_expand={context_prefix} --pack {pack_id} --expand <number>")
        if explain_source is not None:
            lines.append(
                f"context_explain_{explain_source['ref']}="
                + _format_scoped_action(
                    invocation,
                    explain_source["next_actions"]["explain"],
                    command_root,
                )
            )
        if audit and audit.get("error"):
            lines.extend(
                [
                    "context_review_status=error",
                    f"context_audit_error={audit['error']}",
                    f"context_audit={audit_prefix} --pack {pack_id}",
                ]
            )
        elif audit and audit.get("review_status") != "pending":
            ref_by_id = {
                source["source_id"]: source["ref"]
                for source in briefing.get("sources") or []
            }
            used_refs = [
                ref_by_id[source_id]
                for source_id in audit.get("used_source_ids") or []
                if source_id in ref_by_id
            ]
            dismissed_refs = [
                ref_by_id[source_id]
                for source_id in audit.get("dismissed_source_ids") or []
                if source_id in ref_by_id
            ]
            lines.extend(
                [
                    f"context_review_status={audit.get('review_status') or ''}",
                    f"context_decision={audit.get('decision') or ''}",
                    f"context_used_refs={','.join(used_refs)}",
                    f"context_dismissed_refs={','.join(dismissed_refs)}",
                ]
            )
            if audit.get("decision_reason"):
                lines.append(f"context_reason={audit['decision_reason']}")
        elif briefing.get("review_required"):
            lines.extend(
                [
                    f'context_use={context_prefix} --pack {pack_id} --use <number> '
                    f'[--use <number> ...] --reason "<how they help>"',
                    f'context_none={context_prefix} --pack {pack_id} --none-relevant --reason "<why none help>"',
                    f'context_skip={context_prefix} --pack {pack_id} --skip --reason "<why review was not possible>"',
                ]
            )
        expansion = (audit or {}).get("expansion") or {}
        if not expansion.get("receipts_valid", True):
            lines.append("context_expansion_receipts_valid=false")
            lines.append(
                f"context_expansion_errors={len(expansion.get('validation_errors') or [])}"
            )
    elif not briefing.get("review_required"):
        lines.append("context_review=not_applicable")
    return "\n".join(lines) + "\n"


def show_work_context(
    root: str | Path,
    *,
    session_id: str | None = None,
    pack_id: str | None = None,
) -> dict[str, Any]:
    paths = require_root(root)
    if session_id and pack_id:
        raise AgentDirError("Choose either a context session or pack, not both")
    update_index(paths.root)
    if pack_id:
        resolved_pack_id = pack_id
    else:
        session = _resolve_session(paths.root, session_id)
        latest_pack = latest_context_pack(
            paths.root,
            session.session_id,
            rebuild=False,
        )
        if latest_pack is None:
            raise AgentDirStateError(f"No context pack for session {session.session_id}")
        resolved_pack_id = latest_pack["pack_id"]
    manifest = read_context_manifest(paths.root, resolved_pack_id, rebuild=False)
    briefing = brief_context_manifest(manifest)
    context_audit = audit_context_pack(paths.root, resolved_pack_id, rebuild=False)
    return {
        "session": {
            "session_id": manifest.get("session_id") or "",
        },
        "task": manifest.get("task") or "",
        "context": {
            "memory_hits": len(manifest.get("memory_hits") or []),
            "evidence": len(manifest.get("evidence") or []),
            "recent_session_summaries": len(manifest.get("recent_summaries") or []),
            "federated": bool(manifest.get("federated")),
            "federation_group": manifest.get("federation_group"),
            "retrieval_query": manifest.get("retrieval_query") or "",
        },
        "context_briefing": briefing,
        "context_pack": manifest,
        "context_audit": context_audit,
        "event_path": None,
    }


def expand_work_context(
    root: str | Path,
    *,
    source_selector: str,
    page: int = 1,
    actor: str = "agent",
    session_id: str | None = None,
    pack_id: str | None = None,
    delivery: ExpansionDelivery | None = None,
) -> dict[str, Any]:
    paths = require_root(root)
    if session_id and pack_id:
        raise AgentDirError("Choose either a context session or pack, not both")
    if pack_id:
        resolved_pack_id = pack_id
    else:
        session = _resolve_session(paths.root, session_id)
        latest_pack = latest_context_pack(paths.root, session.session_id)
        if latest_pack is None:
            raise AgentDirStateError(f"No context pack for session {session.session_id}")
        resolved_pack_id = latest_pack["pack_id"]
    return expand_context_source(
        paths.root,
        pack_id=resolved_pack_id,
        source_selector=source_selector,
        page=page,
        actor=actor,
        delivery=delivery,
    )


def format_context_expansion(
    result: dict[str, Any],
    *,
    invocation: tuple[str, ...] = ("agentdir",),
    command_root: str | Path | None = None,
) -> str:
    return format_context_expansion_content(
        result,
        invocation=invocation,
        command_root=command_root,
    ) + format_context_expansion_completion(result)


def format_context_expansion_content(
    result: dict[str, Any],
    *,
    invocation: tuple[str, ...] = ("agentdir",),
    command_root: str | Path | None = None,
) -> str:
    source = result["source"]
    lines = [
        f"context_pack={result['pack_id']}",
        f"context_source={source['ref']}",
        f"source_id={source['source_id']}",
        f"source_class={source.get('source_class') or ''}",
        f"source_role={source.get('source_role') or source.get('source_class') or ''}",
        f"match_quality={source.get('match_quality') or 'unknown'}",
        f"subject={source.get('subject') or ''}",
        f"integrity={result['integrity']}",
        f"basis={result['basis']}",
        f"extent={result['extent']}",
        f"page={result['page']}/{result['page_count']}",
        f"bytes={result['byte_start']}:{result['byte_end']}/{result['source_bytes']}",
        f"chars={result['returned_chars']}/{result['source_chars']}",
        f"truncated={str(bool(result['truncated'])).lower()}",
        f"redactions={result['redactions']['count']}",
        f"decision_phase={result['decision']['phase']}",
        "semantics=content-returned-not-model-attention",
    ]
    if source.get("event_type"):
        lines.append(f"source_event={source['event_type']}")
    if source.get("session_id"):
        lines.append(f"source_session={source['session_id']}")
    if source.get("root_id"):
        lines.append(f"source_root={source.get('root_name') or source['root_id']}")
        lines.append(f"source_root_id={source['root_id']}")
        if source.get("root_visibility"):
            lines.append(f"source_root_visibility={source['root_visibility']}")
    if result.get("integrity_reason"):
        lines.append(f"integrity_reason={result['integrity_reason']}")
    if source.get("capture_truncated"):
        lines.append("source_capture_truncated=true")
    lines.extend(["", "--- source content ---", result["content"], "--- end source content ---"])
    for label in ("previous_page", "next_page", "use"):
        action = (result.get("next_actions") or {}).get(label)
        if action:
            lines.append(
                f"{label}={_format_scoped_action(invocation, action, command_root)}"
            )
    return "\n".join(lines).rstrip() + "\n"


def format_context_expansion_completion(result: dict[str, Any]) -> str:
    receipt = result["receipt"]
    lines = [f"receipt={receipt['status']}"]
    if receipt.get("reason"):
        lines.append(f"receipt_reason={receipt['reason']}")
    if receipt.get("error"):
        lines.append(f"receipt_error={receipt['error']}")
    for warning in result.get("warnings") or []:
        lines.append(f"warning={warning}")
    return "\n".join(lines).rstrip() + "\n"


def _scoped_command_prefix(
    invocation: tuple[str, ...],
    command: tuple[str, ...],
    root: str | Path | None,
) -> str:
    parts = [*invocation, *command]
    if root is not None:
        parts.extend(("--root", str(Path(root).resolve())))
    return shlex.join(parts)


def _format_scoped_action(
    invocation: tuple[str, ...],
    action: list[str],
    root: str | Path | None,
) -> str:
    parts = [*invocation]
    if (
        root is not None
        and action[:2] in (["work", "context"], ["memory", "explain"])
        and "--root" not in action
    ):
        parts.extend((*action[:2], "--root", str(Path(root).resolve()), *action[2:]))
    else:
        parts.extend(action)
    return shlex.join(parts)


def review_work_context(
    root: str | Path,
    *,
    disposition: str,
    reason: str,
    source_selectors: list[str] | None = None,
    purpose: str = "plan",
    actor: str = "agent",
    session_id: str | None = None,
    pack_id: str | None = None,
) -> dict[str, Any]:
    paths = require_root(root)
    if session_id and pack_id:
        raise AgentDirError("Choose either a context session or pack, not both")
    if pack_id:
        resolved_pack_id = pack_id
        resolved_session_id = None
    else:
        session = _resolve_session(paths.root, session_id)
        latest_pack = latest_context_pack(paths.root, session.session_id)
        if latest_pack is None:
            raise AgentDirStateError(
                f"No context pack for session {session.session_id}. "
                'Run `agentdir work start "<task>"` to create one.'
            )
        resolved_pack_id = latest_pack["pack_id"]
        resolved_session_id = session.session_id
    return review_context_pack(
        paths.root,
        pack_id=resolved_pack_id,
        disposition=disposition,
        reason=reason,
        source_selectors=source_selectors,
        purpose=purpose,
        session_id=resolved_session_id,
        actor=actor,
    )


def finish_work(
    root: str | Path,
    *,
    session_id: str | None = None,
    actor: str = "agent",
    run_health_check: bool = True,
    end: bool = True,
    claims_text: str | None = None,
    invocation: tuple[str, ...] = ("agentdir",),
) -> dict[str, Any]:
    paths = require_root(root)
    with lifecycle_lock(paths.root, "work-start"):
        with session_pointer_lock(paths.root):
            resolved_session = _resolve_session(paths.root, session_id)
            with lifecycle_lock(paths.root, f"session:{resolved_session.session_id}"):
                return _finish_work_locked(
                    paths.root,
                    session_id=resolved_session.session_id,
                    actor=actor,
                    run_health_check=run_health_check,
                    end=end,
                    claims_text=claims_text,
                    invocation=invocation,
                )


def _finish_work_locked(
    root: Path,
    *,
    session_id: str,
    actor: str,
    run_health_check: bool,
    end: bool,
    claims_text: str | None,
    invocation: tuple[str, ...],
) -> dict[str, Any]:
    paths = require_root(root)
    session = _resolve_session(paths.root, session_id)
    report = build_final_report(
        paths.root,
        session_id=session.session_id,
        run_health_check=run_health_check,
        claims_text=claims_text,
        finishing=True,
    )
    context_payload = report.get("context", {})
    context_audit = next(
        (
            audit
            for audit in context_payload.get("pack_audits") or []
            if not audit.get("finish_allowed", False)
        ),
        context_payload.get("audit") or {},
    )
    if context_audit and not context_audit.get("finish_allowed", False):
        context_prefix = _scoped_command_prefix(
            invocation,
            ("work", "context"),
            paths.root,
        )
        audit_prefix = _scoped_command_prefix(
            invocation,
            ("audit", "context"),
            paths.root,
        )
        target = context_audit.get("pack_id")
        selector = f"--pack {target}" if target else f"--session {session.session_id}"
        if context_audit.get("error"):
            raise AgentDirStateError(
                f"Context review cannot be certified for {target or session.session_id}: "
                f"{context_audit['error']}"
            )
        if context_audit.get("review_status") != "pending":
            raise AgentDirStateError(
                f"Context review cannot be certified for {target or session.session_id}: "
                f"status is {context_audit.get('review_status') or 'invalid'}. "
                f"Inspect `{audit_prefix} --pack {target}`."
            )
        raise AgentDirStateError(
            f"Context review is {context_audit.get('review_status') or 'incomplete'}. "
            f"Re-open it with `{context_prefix} --show {selector}`, then run "
            f"`{context_prefix} {selector} --use <number> [--use <number> ...] "
            f"--reason \"<how they help>\"`, "
            "record `--none-relevant`, or record `--skip`."
        )
    if end:
        current = read_current_session(paths.root)
        if current is None or current.session_id != session.session_id:
            raise AgentDirStateError(
                f"Session {session.session_id} is not the active session and cannot be ended. "
                "Rerun with `--keep-session` to emit its final report without changing the active session."
            )
    rendered = format_final_report(report)
    ending_git_head = report["git"]["head"]
    event = emit_event(
        paths.root,
        session_id=session.session_id,
        event_type=EVENT_WORK_REPORT_FINAL,
        subject="final report",
        body=rendered,
        from_actor=actor,
        workspace=session.workspace,
        git_head=ending_git_head,
    )
    emit_event(
        paths.root,
        session_id=session.session_id,
        event_type=EVENT_WORK_FINISHED,
        subject=f"work finished: {report['task']}",
        body=f"session_id={session.session_id}\nreport_event={event.path}",
        from_actor=actor,
        workspace=session.workspace,
        git_head=ending_git_head,
    )
    ended = None
    if end:
        ended = end_session(
            paths.root,
            summary="AgentDir work finished; final report emitted.",
            actor=actor,
            expected_session_id=session.session_id,
        )
    return {
        "report": report,
        "rendered": rendered,
        "event_path": str(event.path),
        "ended_session": asdict(ended) if ended else None,
    }


def brief_work_finish(result: dict[str, Any]) -> dict[str, Any]:
    """Project the final agent handoff without the forensic report payload."""
    report = result["report"]
    handoff = dict(report["agent_handoff"])
    unresolved = list(
        handoff.get("unresolved_failed_evidence")
        or handoff.get("failed_evidence")
        or []
    )
    historical = list(handoff.get("historical_failed_evidence") or [])
    resolved = list(handoff.get("resolved_failed_evidence") or [])
    actionable = _newest_failure_per_family(unresolved)
    historical_presented = historical[-5:]
    resolved_presented = resolved[-5:]
    handoff.update(
        {
            "failed_evidence": actionable,
            "unresolved_failed_evidence": actionable,
            "historical_failed_evidence": historical_presented,
            "resolved_failed_evidence": resolved_presented,
            "failure_evidence_counts": {
                "unresolved_total": len(unresolved),
                "unresolved_presented": len(actionable),
                "historical_total": len(historical),
                "historical_presented": len(historical_presented),
                "resolved_total": len(resolved),
                "resolved_presented": len(resolved_presented),
            },
        }
    )
    payload = {
        "task": report["task"],
        "agent_handoff": handoff,
        "git": report["git"],
        "health": report["health"],
        "event_path": result["event_path"],
        "ended_session": result["ended_session"],
    }
    return _byte_bound_work_finish_brief(payload)


def _newest_failure_per_family(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in reversed(items):
        family = str(item.get("family") or "unknown")
        if family in seen:
            continue
        seen.add(family)
        selected.append(item)
    selected.reverse()
    return selected


def _byte_bound_work_finish_brief(payload: dict[str, Any]) -> dict[str, Any]:
    profiles = (
        (128, 10, 32, 8),
        (64, 5, 20, 6),
        (32, 3, 12, 5),
    )
    for profile_index, (max_string, max_list, max_dict, max_depth) in enumerate(profiles):
        stats = {
            "truncated_strings": 0,
            "omitted_list_items": 0,
            "omitted_dict_items": 0,
            "depth_truncations": 0,
        }
        bounded = _bound_brief_value(
            payload,
            stats=stats,
            max_string=max_string,
            max_list=max_list,
            max_dict=max_dict,
            max_depth=max_depth,
        )
        bounded["brief_projection"] = {
            "bounded": True,
            "degraded": profile_index > 0,
            "max_bytes": 20_000,
            "max_string_chars": max_string,
            "max_list_items": max_list,
            **stats,
        }
        if len(json.dumps(bounded, indent=2).encode("utf-8")) < 20_000:
            return bounded
    return _minimal_work_finish_brief(payload)


def _bound_brief_value(
    value: Any,
    *,
    stats: dict[str, int],
    max_string: int,
    max_list: int,
    max_dict: int,
    max_depth: int,
    depth: int = 0,
) -> Any:
    if isinstance(value, str):
        if len(value) <= max_string:
            return value
        stats["truncated_strings"] += 1
        marker = "...<truncated>..."
        prefix_length = (max_string - len(marker)) // 2
        suffix_length = max_string - len(marker) - prefix_length
        return f"{value[:prefix_length]}{marker}{value[-suffix_length:]}"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= max_depth:
        stats["depth_truncations"] += 1
        return "<truncated:depth>"
    if isinstance(value, dict):
        items = list(value.items())
        if len(items) > max_dict:
            stats["omitted_dict_items"] += len(items) - max_dict
        return {
            key: _bound_brief_value(
                item,
                stats=stats,
                max_string=max_string,
                max_list=max_list,
                max_dict=max_dict,
                max_depth=max_depth,
                depth=depth + 1,
            )
            for key, item in items[:max_dict]
        }
    if isinstance(value, (list, tuple)):
        if len(value) > max_list:
            stats["omitted_list_items"] += len(value) - max_list
        return [
            _bound_brief_value(
                item,
                stats=stats,
                max_string=max_string,
                max_list=max_list,
                max_dict=max_dict,
                max_depth=max_depth,
                depth=depth + 1,
            )
            for item in value[:max_list]
        ]
    return _bound_brief_value(
        str(value),
        stats=stats,
        max_string=max_string,
        max_list=max_list,
        max_dict=max_dict,
        max_depth=max_depth,
        depth=depth,
    )


def _minimal_work_finish_brief(payload: dict[str, Any]) -> dict[str, Any]:
    handoff = payload.get("agent_handoff") or {}
    context = handoff.get("context_lineage") or {}
    claim_support = handoff.get("claim_support") or {}
    minimal = {
        "task": payload.get("task"),
        "agent_handoff": {
            "status": handoff.get("status"),
            "verification": [
                {
                    "family": item.get("family"),
                    "currently_failing": item.get("currently_failing"),
                    "unresolved_failed": item.get("unresolved_failed"),
                }
                for item in (handoff.get("verification") or [])[:7]
            ],
            "failure_evidence_counts": handoff.get("failure_evidence_counts") or {},
            "claim_support": {
                "claims": [
                    {"family": claim.get("family"), "status": claim.get("status")}
                    for claim in (claim_support.get("claims") or [])[:7]
                ]
            },
            "context_lineage": {
                key: context.get(key)
                for key in (
                    "pack_id",
                    "ok",
                    "review_status",
                    "decision_complete",
                    "retrieved",
                    "presented",
                    "used",
                    "pending",
                    "cited_without_use",
                )
            },
            "known_gaps": (handoff.get("known_gaps") or [])[:3],
            "recommended_agent_actions": (
                handoff.get("recommended_agent_actions") or []
            )[:3],
        },
        "git": payload.get("git"),
        "health": {"ok": (payload.get("health") or {}).get("ok")},
        "event_path": payload.get("event_path"),
        "ended_session": {
            key: (payload.get("ended_session") or {}).get(key)
            for key in ("session_id", "status", "git_head", "ended_at")
        },
    }
    stats = {
        "truncated_strings": 0,
        "omitted_list_items": 0,
        "omitted_dict_items": 0,
        "depth_truncations": 0,
    }
    bounded = _bound_brief_value(
        minimal,
        stats=stats,
        max_string=64,
        max_list=7,
        max_dict=16,
        max_depth=5,
    )
    bounded["brief_projection"] = {
        "bounded": True,
        "degraded": True,
        "max_bytes": 20_000,
        "max_string_chars": 64,
        "max_list_items": 7,
        **stats,
    }
    return bounded


def build_final_report(
    root: str | Path,
    *,
    session_id: str | None = None,
    run_health_check: bool = True,
    claims_text: str | None = None,
    finishing: bool = False,
) -> dict[str, Any]:
    paths = require_root(root)
    session = _resolve_session(paths.root, session_id)
    git_cwd = session_git_cwd(paths.root, session)
    update_index(paths.root)
    summary = summarize_session(paths.root, session.session_id, rebuild=False)
    evidence = evidence_rows(paths.root, session.session_id, rebuild=False)
    context_projection = build_context_projection(
        paths.root,
        session.session_id,
        rebuild=False,
    )
    packs = context_projection["packs"]
    latest_pack = context_projection["latest_pack"]
    pack_audits = context_projection["pack_audits"]
    context_audit = context_projection["audit"]
    blocking_audit = context_projection["blocking_audit"]
    attention_audit = context_projection["attention_audit"]
    expansion_inventory = context_projection["expansion_receipt_inventory"]
    audit_for_session = blocking_audit or attention_audit or context_audit
    pack_for_session = (
        next((pack for pack in packs if pack["pack_id"] == audit_for_session["pack_id"]), latest_pack)
        if audit_for_session
        else latest_pack
    )
    doctor = run_doctor(paths.root).as_dict() if run_health_check else None
    session_audit = audit_session(
        paths.root,
        session.session_id,
        finishing=finishing,
        run_health_check=run_health_check,
        rebuild=False,
        summary=summary,
        evidence=evidence,
        latest_pack=pack_for_session,
        context_audit=audit_for_session,
        doctor=doctor,
        expansion_inventory=expansion_inventory,
    )
    if claims_text is not None:
        claims_audit = audit_claims(
            paths.root,
            claims_text,
            session.session_id,
            rebuild=False,
            summary=summary,
            evidence=evidence,
        )
    else:
        # Structured claims need no prose to check, so the handoff can report
        # claim support even when no final text was supplied.
        recorded = audit_recorded_claims(
            paths.root,
            session.session_id,
            rebuild=False,
            summary=summary,
            evidence=evidence,
        )
        claims_audit = recorded if recorded["claims"] else None
    gaps = _known_gaps(summary, evidence, context_audit, doctor, session_audit, claims_audit)
    brief = evidence_brief(evidence)
    handoff = _agent_handoff(
        summary=summary,
        evidence_brief_payload=brief,
        context_audit=audit_for_session,
        doctor=doctor,
        session_audit=session_audit,
        claims_audit=claims_audit,
        known_gaps=gaps,
    )
    return {
        "task": session.title,
        "session": asdict(session),
        "summary": summary,
        "evidence": evidence,
        "evidence_brief": brief,
        "context": {
            "latest_pack": latest_pack,
            "audit": context_audit,
            "attention_audit": attention_audit,
            "pack_audits": pack_audits,
            "blocking_packs": context_projection["blocking_packs"],
            "attention_packs": context_projection["attention_packs"],
            "expansion_receipt_inventory": expansion_inventory,
        },
        "session_audit": session_audit,
        "claim_support": claims_audit,
        "agent_handoff": handoff,
        "git": {
            "workspace": session.workspace,
            "branch": git_branch(git_cwd) if git_cwd else None,
            "head": (git_head(git_cwd) if git_cwd else None) or session.git_head,
            "dirty": bool(git_status_short(git_cwd)) if git_cwd else False,
        },
        "health": doctor,
        "known_gaps": gaps,
    }


def format_final_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    context = report["context"]
    health = report.get("health")
    session_audit = report.get("session_audit") or {}
    claim_support = report.get("claim_support")
    handoff = report.get("agent_handoff") or {}
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
        f"- events_before_final_report: {summary['events']}",
        f"- tool_results: {summary['tool_results']}",
        f"- failed_tools: {summary['failed_tools']}",
        f"- resolved_failed_tools: {summary.get('resolved_failed_tools', 0)}",
        f"- unresolved_failed_tools: {summary.get('unresolved_failed_tools', summary['failed_tools'])}",
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
    blocking_packs = context.get("blocking_packs") or []
    lines.append(f"- blocking_packs: {', '.join(blocking_packs) if blocking_packs else 'none'}")
    attention_packs = context.get("attention_packs") or []
    lines.append(f"- attention_packs: {', '.join(attention_packs) if attention_packs else 'none'}")
    if audit:
        lines.append(f"- audit_pack: {audit.get('pack_id') or 'none'}")
    attention_audit = context.get("attention_audit") or {}
    if attention_audit:
        lines.append(f"- attention_audit_pack: {attention_audit.get('pack_id') or 'none'}")
    expansion_inventory = context.get("expansion_receipt_inventory") or {}
    lines.append(
        f"- expansion_orphan_receipts: {expansion_inventory.get('orphan_event_count', 0)}"
    )
    lines.append(
        "- expansion_inventory_valid: "
        f"{str(expansion_inventory.get('receipts_valid', True)).lower()}"
    )
    if audit and not audit.get("error"):
        expansion = audit.get("expansion") or empty_context_expansion_audit()
        lines.append(f"- retrieved: {audit['retrieved_count']}")
        lines.append(f"- presented: {audit['presented_count']}")
        lines.append(f"- reviewed: {audit['reviewed_count']}")
        lines.append(f"- used: {audit['used_count']}")
        lines.append(f"- consumed: {audit['consumed_count']}")
        lines.append(f"- additional_consumed: {audit.get('additional_consumed_count', 0)}")
        lines.append(f"- dismissed: {audit['dismissed_count']}")
        lines.append(f"- pending: {audit['pending_count']}")
        lines.append(f"- cited: {audit['cited_count']}")
        lines.append(f"- cited_without_use: {audit['cited_without_use_count']}")
        lines.append(f"- review_status: {audit['review_status']}")
        lines.append(f"- decision_complete: {str(audit['decision_complete']).lower()}")
        if audit.get("decision_reason"):
            lines.append(f"- review_reason: {audit['decision_reason']}")
        lines.append(f"- evidence_backed: {audit['evidence_backed_count']}")
        lines.append(f"- expanded: {expansion['expanded_source_count']}")
        lines.append(
            f"- expanded_before_decision: {expansion['expanded_before_decision_count']}"
        )
        lines.append(f"- expanded_after_decision: {expansion['expanded_after_decision_count']}")
        lines.append(
            "- used_without_prior_expansion: "
            f"{expansion['used_without_prior_expansion_count']}"
        )
        lines.append(f"- expansion_receipts: {expansion['receipt_event_count']}")
        lines.append(
            f"- expansion_receipts_valid: {str(expansion['receipts_valid']).lower()}"
        )
    elif audit and audit.get("error"):
        lines.append(f"- audit_error: {audit['error']}")
    lines.extend(["", "## Session Audit", ""])
    if session_audit:
        lines.append(f"- ok: {str(session_audit.get('ok', False)).lower()}")
        for check in session_audit.get("checks") or []:
            lines.append(f"- {check['id']}: {check['status']} - {check['message']}")
    else:
        lines.append("- audit: not run")
    if claim_support is not None:
        lines.extend(["", "## Claim Support", ""])
        if claim_support.get("claims"):
            for claim in claim_support["claims"]:
                lines.append(f"- {claim['family']}: {claim['status']} - {claim['message']}")
        else:
            lines.append("- claims: none detected")
    lines.extend(["", "## Agent Handoff", ""])
    if handoff:
        lines.append(f"- status: {handoff.get('status')}")
        actions = handoff.get("recommended_agent_actions") or []
        if actions:
            for action in actions:
                lines.append(f"- action: {action}")
        else:
            lines.append("- action: none")
    else:
        lines.append("- handoff: not available")
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


def _agent_handoff(
    *,
    summary: dict[str, Any],
    evidence_brief_payload: dict[str, Any],
    context_audit: dict[str, Any] | None,
    doctor: dict[str, Any] | None,
    session_audit: dict[str, Any] | None,
    claims_audit: dict[str, Any] | None,
    known_gaps: list[str],
) -> dict[str, Any]:
    failed_evidence = evidence_brief_payload.get("failed_evidence") or []
    historical_failed_evidence = (
        evidence_brief_payload.get("historical_failed_evidence") or []
    )
    resolved_failed_evidence = evidence_brief_payload.get("resolved_failed_evidence") or []
    fail_checks = [
        check
        for check in (session_audit or {}).get("checks", [])
        if check.get("status") == "fail"
    ]
    receipt_attention_checks = [
        check
        for check in (session_audit or {}).get("checks", [])
        if check.get("id") == "context_expansion_receipt_inventory"
        and check.get("status") == "warn"
    ]
    unsupported_claims = []
    contradicted_claims = []
    if claims_audit:
        unsupported_claims = [
            claim
            for claim in claims_audit.get("claims", [])
            if claim.get("status") == "unsupported"
        ]
        contradicted_claims = [
            claim
            for claim in claims_audit.get("claims", [])
            if claim.get("status") == "contradicted"
        ]
    doctor_errors = (doctor or {}).get("errors") or []
    invalid_context = bool(context_audit and _context_needs_attention(context_audit))
    needs_attention = bool(
        failed_evidence
        or fail_checks
        or unsupported_claims
        or contradicted_claims
        or doctor_errors
        or invalid_context
        or receipt_attention_checks
    )
    return {
        "status": "needs_attention" if needs_attention else "ok",
        "verification": evidence_brief_payload.get("families") or [],
        "failed_evidence": failed_evidence,
        "unresolved_failed_evidence": failed_evidence,
        "historical_failed_evidence": historical_failed_evidence,
        "resolved_failed_evidence": resolved_failed_evidence,
        "claim_support": claims_audit,
        "context_lineage": _context_lineage(context_audit),
        "known_gaps": known_gaps,
        "recommended_agent_actions": _recommended_agent_actions(
            summary=summary,
            failed_evidence=failed_evidence,
            unsupported_claims=unsupported_claims,
            contradicted_claims=contradicted_claims,
            doctor_errors=doctor_errors,
            session_audit=session_audit,
        ),
        "final_response_guidance": _final_response_guidance(
            failed_evidence=failed_evidence,
            unsupported_claims=unsupported_claims,
            contradicted_claims=contradicted_claims,
            doctor_errors=doctor_errors,
        ),
    }


def _context_lineage(context_audit: dict[str, Any] | None) -> dict[str, Any]:
    if context_audit is None:
        return {
            "pack_id": None,
            "retrieved": 0,
            "presented": 0,
            "reviewed": 0,
            "used": 0,
            "dismissed": 0,
            "pending": 0,
            "consumed": 0,
            "additional_consumed": 0,
            "cited": 0,
            "cited_without_use": 0,
            "review_status": "not_available",
            "decision_complete": False,
            "evidence_backed": 0,
            "expansion": empty_context_expansion_audit(),
            "ok": False,
        }
    if context_audit.get("error"):
        return {
            "pack_id": context_audit.get("pack_id"),
            "retrieved": 0,
            "presented": 0,
            "reviewed": 0,
            "used": 0,
            "dismissed": 0,
            "pending": 0,
            "consumed": 0,
            "additional_consumed": 0,
            "cited": 0,
            "cited_without_use": 0,
            "review_status": "error",
            "decision_complete": False,
            "evidence_backed": 0,
            "expansion": empty_context_expansion_audit(),
            "ok": False,
            "error": context_audit.get("error"),
        }
    retrieved = context_audit.get("retrieved_count", 0)
    presented = context_audit.get("presented_count", 0)
    reviewed = context_audit.get("reviewed_count", 0)
    used = context_audit.get("used_count", 0)
    dismissed = context_audit.get("dismissed_count", 0)
    pending = context_audit.get("pending_count", 0)
    cited = context_audit.get("cited_count", 0)
    cited_without_use = context_audit.get("cited_without_use_count", 0)
    return {
        "pack_id": context_audit.get("pack_id"),
        "retrieved": retrieved,
        "presented": presented,
        "reviewed": reviewed,
        "used": used,
        "dismissed": dismissed,
        "pending": pending,
        "consumed": context_audit.get("consumed_count", used),
        "additional_consumed": context_audit.get("additional_consumed_count", 0),
        "cited": cited,
        "cited_without_use": cited_without_use,
        "review_status": context_audit.get("review_status"),
        "decision_complete": bool(context_audit.get("decision_complete")),
        "decision": context_audit.get("decision"),
        "reason": context_audit.get("decision_reason"),
        "evidence_backed": context_audit.get("evidence_backed_count", 0),
        "expansion": context_audit.get("expansion") or empty_context_expansion_audit(),
        "ok": bool(context_audit.get("lineage_valid")),
    }


def _recommended_agent_actions(
    *,
    summary: dict[str, Any],
    failed_evidence: list[dict[str, Any]],
    unsupported_claims: list[dict[str, Any]],
    contradicted_claims: list[dict[str, Any]],
    doctor_errors: list[str],
    session_audit: dict[str, Any] | None,
) -> list[str]:
    actions: list[str] = []
    if failed_evidence:
        actions.append("Inspect failed evidence and rerun the relevant check after fixing it.")
    if contradicted_claims:
        actions.append("Do not claim contradicted checks passed until newer successful evidence exists.")
    if unsupported_claims:
        families = ", ".join(sorted({str(claim.get("family")) for claim in unsupported_claims}))
        actions.append(f"Capture evidence or remove unsupported final-response claims for: {families}.")
    if doctor_errors:
        actions.append("Run `agentdir doctor` and resolve health errors before treating the session as clean.")
    if summary.get("tool_results", 0) == 0:
        actions.append("Run evidence-bearing verification through `agentdir run -- <command>` when final claims need support.")
    if session_audit:
        failing_ids = [
            check.get("id")
            for check in session_audit.get("checks", [])
            if check.get("status") == "fail" and check.get("id") != "failed_tool_results"
        ]
        for check_id in failing_ids:
            actions.append(f"Resolve failing session audit check: {check_id}.")
        if any(
            check.get("id") == "context_expansion_receipt_inventory"
            and check.get("status") == "warn"
            for check in session_audit.get("checks", [])
        ):
            actions.append(
                "Inspect optional context expansion receipts that cannot be attributed to an owning pack."
            )
    return actions


def _final_response_guidance(
    *,
    failed_evidence: list[dict[str, Any]],
    unsupported_claims: list[dict[str, Any]],
    contradicted_claims: list[dict[str, Any]],
    doctor_errors: list[str],
) -> list[str]:
    guidance = [
        "Base final verification claims only on supported AgentDir evidence.",
        "Mention AgentDir only when it clarifies evidence, blockers, or setup problems.",
    ]
    if failed_evidence:
        guidance.append("Call out failed checks plainly instead of summarizing the session as clean.")
    if unsupported_claims:
        guidance.append("Avoid unsupported test, lint, typecheck, build, doctor, or release claims.")
    if contradicted_claims:
        guidance.append("Treat contradicted claims as blockers until newer passing evidence exists.")
    if doctor_errors:
        guidance.append("Surface doctor errors as setup or health blockers.")
    return guidance


def latest_context_pack(
    root: str | Path,
    session_id: str | None = None,
    *,
    fallback_any: bool = False,
    rebuild: bool = True,
) -> dict[str, Any] | None:
    rows = context_packs(
        root,
        session_id,
        fallback_any=fallback_any,
        rebuild=rebuild,
    )
    return rows[-1] if rows else None


def context_packs(
    root: str | Path,
    session_id: str | None = None,
    *,
    fallback_any: bool = False,
    rebuild: bool = True,
) -> list[dict[str, Any]]:
    """Compatibility facade for context repository pack discovery."""
    return list_context_packs(
        root,
        session_id,
        fallback_any=fallback_any,
        rebuild=rebuild,
    )


def _resolve_session(root: str | Path, session_id: str | None) -> SessionState:
    if session_id:
        persisted = read_session_state(root, session_id)
        if persisted:
            return persisted
        summary = summarize_session(root, session_id)
        return SessionState(
            session_id=session_id,
            title=session_id,
            actor="agent",
            workspace="unknown",
            git_head=None,
            started_at=summary.get("first_event") or "",
            status="unknown",
        )
    return require_current_session(root)


def _safe_memory_stats(root: str | Path) -> dict[str, Any]:
    # Status must stay usable when the index is absent or stale; anything
    # beyond expected index errors should propagate as a real bug.
    try:
        stats = memory_stats(root)
    except (AgentDirError, sqlite3.Error, OSError) as exc:
        return {"indexed": False, "error": str(exc)}
    return {"indexed": True, **stats}


def _store_version(root: str | Path) -> str | None:
    path = paths_for(root).meta / "VERSION"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip()


def _known_gaps(
    summary: dict[str, Any],
    evidence: list[dict[str, Any]],
    context_audit: dict[str, Any] | None,
    doctor: dict[str, Any] | None,
    session_audit: dict[str, Any] | None = None,
    claims_audit: dict[str, Any] | None = None,
) -> list[str]:
    gaps: list[str] = []
    if session_audit:
        gaps.extend(session_audit_gaps(session_audit))
    if claims_audit:
        gaps.extend(claims_audit_gaps(claims_audit))
    expansion = (context_audit or {}).get("expansion") or {}
    if expansion and not expansion.get("receipts_valid", True):
        gaps.append(
            f"Context expansion has {len(expansion.get('validation_errors') or [])} invalid receipt record(s)"
        )
    if session_audit:
        return gaps
    if summary.get("failed_tools"):
        gaps.append(f"{summary['failed_tools']} captured tool result(s) failed")
    if not evidence:
        gaps.append("No evidence captured")
    if context_audit is None:
        gaps.append("No context pack recorded")
    elif context_audit.get("error"):
        gaps.append(f"Context audit failed: {context_audit['error']}")
    else:
        if context_audit.get("review_required") and not context_audit.get("decision_complete"):
            gaps.append(f"Context review is {context_audit.get('review_status') or 'pending'}")
        if context_audit.get("cited_without_use_count", 0):
            gaps.append("Context sources were cited without being used")
    if doctor:
        for warning in doctor.get("warnings", []):
            gaps.append(f"Doctor warning: {warning}")
        for error in doctor.get("errors", []):
            gaps.append(f"Doctor error: {error}")
    return gaps
