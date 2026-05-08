from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .actors import create_actor, send_message
from .artifacts import add_artifact
from .context import build_context_pack, format_context_pack, write_context_pack
from .capture import DEFAULT_MAX_CAPTURE_BYTES, run_tool
from .doctor import run_doctor
from .events import emit_event
from .hooks import hook_status, install_hooks, record_hook_event, uninstall_hooks
from .index import rebuild_index, update_index
from .memory import (
    DEFAULT_MIN_SCORE,
    explain_memory_match,
    format_memory_explanation,
    format_memory_hits,
    memory_stats,
    search_memory,
)
from .query import query_messages
from .replay import replay_session
from .review import evidence_rows, format_evidence, format_summary, summarize_session
from .sessions import end_session, ensure_session, read_current_session, start_session
from .skills import install_codex_skill
from .store import AgentDirError, init_root, resolve_root


def read_body(path: str | None) -> str:
    if not path or path == "-":
        return sys.stdin.read()
    return Path(path).expanduser().read_text(encoding="utf-8")


def print_json(data: object) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def cmd_init(args: argparse.Namespace) -> int:
    root_arg = args.root_option or args.root
    print(init_root(resolve_root(root_arg, args.scope)).root)
    return 0


def cmd_root(args: argparse.Namespace) -> int:
    root = resolve_root(args.root_option, args.scope)
    if args.json:
        print_json({"root": str(root), "scope": args.scope or "default"})
    else:
        print(root)
    return 0


def command_root(args: argparse.Namespace, *, create: bool = False) -> Path:
    root_arg = getattr(args, "root_option", None) or getattr(args, "root", None)
    root = resolve_root(root_arg, getattr(args, "scope", None))
    if create:
        init_root(root)
    return root


def cmd_emit(args: argparse.Namespace) -> int:
    root = command_root(args, create=True)
    session = read_current_session(root) if not args.session else None
    session_id = args.session or (session.session_id if session else None)
    if not session_id:
        session_id = start_session(root, title="AgentDir manual event").session_id
    delivered = emit_event(
        root,
        session_id=session_id,
        event_type=args.type,
        body=read_body(args.body),
        subject=args.subject,
        from_actor=args.from_actor,
        to_actor=args.to_actor,
        task_id=args.task,
        workspace=args.workspace,
        git_head=args.git_head,
        tool=args.tool,
        tool_exit_code=args.tool_exit_code,
        parent_message_id=args.parent,
        artifact=args.artifact,
        message_id=args.message_id,
    )
    print(delivered.path)
    return 0


def cmd_actor_create(args: argparse.Namespace) -> int:
    inbox, outbox = create_actor(command_root(args, create=True), args.actor_id)
    print(f"inbox={inbox}")
    print(f"outbox={outbox}")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    root = command_root(args, create=True)
    current = read_current_session(root) if not args.session else None
    inbox, outbox = send_message(
        root=root,
        from_actor=args.from_actor,
        to_actor=args.to_actor,
        event_type=args.type,
        body=read_body(args.body),
        subject=args.subject,
        session_id=args.session or (current.session_id if current else None),
        task_id=args.task,
        message_id=args.message_id,
    )
    print(f"inbox={inbox}")
    print(f"outbox={outbox}")
    return 0


def cmd_artifact_add(args: argparse.Namespace) -> int:
    artifact = add_artifact(command_root(args, create=True), args.path)
    print_json(
        {
            "sha256": artifact.sha256,
            "path": str(artifact.path),
            "bytes": artifact.bytes,
            "mime_type": artifact.mime_type,
        }
    )
    return 0


def cmd_index_rebuild(args: argparse.Namespace) -> int:
    result = rebuild_index(command_root(args, create=True))
    print_json({"indexed": result.indexed, "malformed": result.malformed, "duplicates": result.duplicates})
    return 0


def cmd_index_update(args: argparse.Namespace) -> int:
    result = update_index(command_root(args, create=True))
    print_json({"indexed": result.indexed, "malformed": result.malformed, "duplicates": result.duplicates})
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    root = command_root(args)
    if args.semantic and args.text:
        raise AgentDirError("Use either --text for literal search or --semantic for vector memory search")
    if args.semantic:
        rebuild_index(root)
        rows = search_memory(
            root,
            args.semantic,
            session_id=args.session,
            event_type=args.type,
            actor=args.actor,
            task_id=args.task,
            tool=args.tool,
            git_head=args.git_head,
            workspace=args.workspace,
            since=args.since,
            until=args.until,
            limit=args.limit,
            min_score=args.min_score,
        )
    else:
        rows = query_messages(
            root,
            session_id=args.session,
            event_type=args.type,
            actor=args.actor,
            task_id=args.task,
            tool=args.tool,
            git_head=args.git_head,
            workspace=args.workspace,
            text=args.text,
            since=args.since,
            until=args.until,
            limit=args.limit,
        )
    if args.json:
        print_json(rows)
    elif args.semantic:
        print(format_memory_hits(rows))
    else:
        for row in rows:
            body = (row.get("body_text") or "").strip().replace("\n", "\\n")
            print(
                f"{row.get('date_header') or row.get('indexed_at')} "
                f"{row.get('event_type') or 'unknown'} "
                f"{row.get('subject') or ''} "
                f"{body} "
                f"{row.get('file_path')}"
            )
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    for line in replay_session(command_root(args), args.session):
        print(line)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    report = run_doctor(command_root(args))
    if args.json:
        print_json(report.as_dict())
    else:
        print(f"ok={str(report.ok).lower()}")
        for warning in report.warnings:
            print(f"warning: {warning}")
        for error in report.errors:
            print(f"error: {error}")
    return 0 if report.ok else 1


def cmd_session_start(args: argparse.Namespace) -> int:
    state = start_session(
        command_root(args, create=True),
        session_id=args.session_id,
        title=args.title,
        actor=args.actor,
        note=read_body(args.note) if args.note else None,
    )
    if args.json:
        print_json(asdict(state))
    else:
        print(state.session_id)
    return 0


def cmd_session_current(args: argparse.Namespace) -> int:
    state = read_current_session(command_root(args))
    if state is None:
        raise AgentDirError("No active AgentDir session")
    if args.json:
        print_json(asdict(state))
    else:
        print(state.session_id)
    return 0


def cmd_session_ensure(args: argparse.Namespace) -> int:
    state = ensure_session(
        command_root(args, create=True),
        session_id=args.session_id,
        title=args.title,
        actor=args.actor,
    )
    if args.json:
        print_json(asdict(state))
    else:
        print(state.session_id)
    return 0


def cmd_session_end(args: argparse.Namespace) -> int:
    state = end_session(
        command_root(args),
        status=args.status,
        summary=read_body(args.summary) if args.summary else None,
        actor=args.actor,
    )
    if args.json:
        print_json(asdict(state))
    else:
        print(state.session_id)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    command = list(args.command or [])
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise AgentDirError("Usage: agentdir run -- <command> [args...]")
    return run_tool(
        command_root(args, create=True),
        argv=command,
        session_id=args.session,
        tool_name=args.name,
        cwd=args.cwd,
        max_capture_bytes=args.max_capture_bytes,
        redact=not args.no_redact,
    )


def cmd_hooks_install(args: argparse.Namespace) -> int:
    infos = install_hooks(
        command_root(args, create=True),
        hooks=args.hooks,
        force=args.force,
    )
    if args.json:
        print_json([asdict(info) for info in infos])
    else:
        for info in infos:
            print(f"{info.hook}: installed {info.path}")
    return 0


def cmd_hooks_status(args: argparse.Namespace) -> int:
    infos = hook_status(hooks=args.hooks)
    if args.json:
        print_json([asdict(info) for info in infos])
    else:
        for info in infos:
            state = "managed" if info.managed else "unmanaged" if info.installed else "missing"
            print(f"{info.hook}: {state} {info.path}")
    return 0


def cmd_hooks_uninstall(args: argparse.Namespace) -> int:
    infos = uninstall_hooks(hooks=args.hooks)
    if args.json:
        print_json([asdict(info) for info in infos])
    else:
        for info in infos:
            print(f"{info.hook}: {'installed' if info.installed else 'removed'} {info.path}")
    return 0


def cmd_hooks_record(args: argparse.Namespace) -> int:
    hook_args = list(args.hook_args or [])
    if hook_args and hook_args[0] == "--":
        hook_args = hook_args[1:]
    record_hook_event(
        command_root(args, create=True),
        hook=args.hook,
        original_exit_code=args.original_exit_code,
        stdin_file=args.stdin_file,
        hook_args=hook_args,
    )
    return 0


def cmd_skills_install_codex(args: argparse.Namespace) -> int:
    installed = install_codex_skill(
        command_root(args, create=True),
        target=args.target,
        force=args.force,
    )
    if args.json:
        print_json({"target": installed.target, "path": str(installed.path)})
    else:
        print(installed.path)
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    root = command_root(args, create=True)
    hooks = [] if args.no_hooks else install_hooks(root, force=args.force)
    skill = None
    if args.codex_skill != "none":
        skill = install_codex_skill(root, target=args.codex_skill, force=args.force)
    result = {
        "root": str(root),
        "hooks": [asdict(info) for info in hooks],
        "codex_skill": str(skill.path) if skill else None,
    }
    if args.json:
        print_json(result)
    else:
        print(f"root={root}")
        if hooks:
            print(f"hooks={len(hooks)}")
        if skill:
            print(f"codex_skill={skill.path}")
    return 0


def cmd_summarize(args: argparse.Namespace) -> int:
    summary = summarize_session(command_root(args), args.session)
    if args.json:
        print_json(summary)
    else:
        print(format_summary(summary))
    return 0


def cmd_evidence(args: argparse.Namespace) -> int:
    rows = evidence_rows(command_root(args), args.session)
    if args.json:
        print_json(rows)
    else:
        print(format_evidence(rows))
    return 0


def cmd_memory_search(args: argparse.Namespace) -> int:
    root = command_root(args)
    if not args.no_rebuild:
        rebuild_index(root)
    rows = search_memory(
        root,
        args.query,
        session_id=args.session,
        event_type=args.type,
        actor=args.actor,
        task_id=args.task,
        tool=args.tool,
        git_head=args.git_head,
        workspace=args.workspace,
        since=args.since,
        until=args.until,
        limit=args.limit,
        min_score=args.min_score,
    )
    if args.json:
        print_json(rows)
    else:
        print(format_memory_hits(rows))
    return 0


def cmd_memory_explain(args: argparse.Namespace) -> int:
    root = command_root(args)
    if not args.no_rebuild:
        rebuild_index(root)
    explanation = explain_memory_match(
        root,
        args.query,
        source_id=args.source,
        min_score=args.min_score,
    )
    if args.json:
        print_json(explanation)
    else:
        print(format_memory_explanation(explanation))
    return 0


def cmd_memory_stats(args: argparse.Namespace) -> int:
    root = command_root(args)
    if not args.no_rebuild:
        rebuild_index(root)
    stats = memory_stats(root)
    if args.json:
        print_json(stats)
    else:
        print(f"messages={stats['messages']}")
        print(f"memory_documents={stats['memory_documents']}")
        print(f"message_documents={stats['message_documents']}")
        print(f"session_summary_documents={stats['session_summary_documents']}")
        print(f"coverage={stats['coverage']:.3f}")
        print(f"vector_dim={stats['vector_dim']}")
    return 0


def cmd_context_build(args: argparse.Namespace) -> int:
    pack = build_context_pack(
        command_root(args),
        args.task,
        session_id=args.session,
        memory_limit=args.memory_limit,
        evidence_limit=args.evidence_limit,
        recent_limit=args.recent_limit,
        min_score=args.min_score,
    )
    if args.output:
        path = write_context_pack(args.output, pack, as_json=args.json)
        print(path)
    elif args.json:
        print_json(pack)
    else:
        print(format_context_pack(pack), end="")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentdir")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("root", nargs="?")
    add_scope_args(init)
    init.set_defaults(func=cmd_init)

    root = sub.add_parser("root")
    add_scope_args(root)
    root.add_argument("--json", action="store_true")
    root.set_defaults(func=cmd_root)

    emit = sub.add_parser("emit")
    add_scope_args(emit)
    emit.add_argument("--session")
    emit.add_argument("--type", required=True)
    emit.add_argument("--body")
    emit.add_argument("--subject")
    emit.add_argument("--from", dest="from_actor", default="agent")
    emit.add_argument("--to", dest="to_actor")
    emit.add_argument("--task")
    emit.add_argument("--workspace")
    emit.add_argument("--git-head")
    emit.add_argument("--tool")
    emit.add_argument("--tool-exit-code", type=int)
    emit.add_argument("--parent")
    emit.add_argument("--artifact")
    emit.add_argument("--message-id")
    emit.set_defaults(func=cmd_emit)

    session = sub.add_parser("session")
    session_sub = session.add_subparsers(dest="session_command", required=True)
    session_start = session_sub.add_parser("start")
    add_scope_args(session_start)
    session_start.add_argument("--id", dest="session_id")
    session_start.add_argument("--title")
    session_start.add_argument("--actor", default="agent")
    session_start.add_argument("--note")
    session_start.add_argument("--json", action="store_true")
    session_start.set_defaults(func=cmd_session_start)
    session_current = session_sub.add_parser("current")
    add_scope_args(session_current)
    session_current.add_argument("--json", action="store_true")
    session_current.set_defaults(func=cmd_session_current)
    session_ensure = session_sub.add_parser("ensure")
    add_scope_args(session_ensure)
    session_ensure.add_argument("--id", dest="session_id")
    session_ensure.add_argument("--title")
    session_ensure.add_argument("--actor", default="agent")
    session_ensure.add_argument("--json", action="store_true")
    session_ensure.set_defaults(func=cmd_session_ensure)
    session_end = session_sub.add_parser("end")
    add_scope_args(session_end)
    session_end.add_argument("--status", default="completed")
    session_end.add_argument("--summary")
    session_end.add_argument("--actor", default="agent")
    session_end.add_argument("--json", action="store_true")
    session_end.set_defaults(func=cmd_session_end)

    actor = sub.add_parser("actor")
    actor_sub = actor.add_subparsers(dest="actor_command", required=True)
    actor_create = actor_sub.add_parser("create")
    add_scope_args(actor_create)
    actor_create.add_argument("actor_id")
    actor_create.set_defaults(func=cmd_actor_create)

    send = sub.add_parser("send")
    add_scope_args(send)
    send.add_argument("--from", dest="from_actor", required=True)
    send.add_argument("--to", dest="to_actor", required=True)
    send.add_argument("--type", required=True)
    send.add_argument("--body")
    send.add_argument("--subject")
    send.add_argument("--session")
    send.add_argument("--task")
    send.add_argument("--message-id")
    send.set_defaults(func=cmd_send)

    artifact = sub.add_parser("artifact")
    artifact_sub = artifact.add_subparsers(dest="artifact_command", required=True)
    artifact_add = artifact_sub.add_parser("add")
    add_scope_args(artifact_add)
    artifact_add.add_argument("path")
    artifact_add.set_defaults(func=cmd_artifact_add)

    index = sub.add_parser("index")
    index_sub = index.add_subparsers(dest="index_command", required=True)
    rebuild = index_sub.add_parser("rebuild")
    add_scope_args(rebuild)
    rebuild.set_defaults(func=cmd_index_rebuild)
    update = index_sub.add_parser("update")
    add_scope_args(update)
    update.set_defaults(func=cmd_index_update)

    query = sub.add_parser("query")
    add_scope_args(query)
    query.add_argument("--session")
    query.add_argument("--type")
    query.add_argument("--actor")
    query.add_argument("--task")
    query.add_argument("--tool")
    query.add_argument("--git-head")
    query.add_argument("--workspace")
    query.add_argument("--text")
    query.add_argument("--semantic")
    query.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
    query.add_argument("--since")
    query.add_argument("--until")
    query.add_argument("--limit", type=int, default=100)
    query.add_argument("--json", action="store_true")
    query.set_defaults(func=cmd_query)

    replay = sub.add_parser("replay")
    add_scope_args(replay)
    replay.add_argument("--session", required=True)
    replay.set_defaults(func=cmd_replay)

    doctor = sub.add_parser("doctor")
    add_scope_args(doctor)
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    run = sub.add_parser("run")
    add_scope_args(run)
    run.add_argument("--session")
    run.add_argument("--name")
    run.add_argument("--cwd")
    run.add_argument("--max-capture-bytes", type=int, default=DEFAULT_MAX_CAPTURE_BYTES)
    run.add_argument("--no-redact", action="store_true")
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_run)

    hooks = sub.add_parser("hooks")
    hooks_sub = hooks.add_subparsers(dest="hooks_command", required=True)
    hooks_install = hooks_sub.add_parser("install")
    add_scope_args(hooks_install)
    hooks_install.add_argument("--hook", action="append", dest="hooks")
    hooks_install.add_argument("--force", action="store_true")
    hooks_install.add_argument("--json", action="store_true")
    hooks_install.set_defaults(func=cmd_hooks_install)
    hooks_status = hooks_sub.add_parser("status")
    hooks_status.add_argument("--hook", action="append", dest="hooks")
    hooks_status.add_argument("--json", action="store_true")
    hooks_status.set_defaults(func=cmd_hooks_status)
    hooks_uninstall = hooks_sub.add_parser("uninstall")
    hooks_uninstall.add_argument("--hook", action="append", dest="hooks")
    hooks_uninstall.add_argument("--json", action="store_true")
    hooks_uninstall.set_defaults(func=cmd_hooks_uninstall)
    hooks_record = hooks_sub.add_parser("record")
    add_scope_args(hooks_record)
    hooks_record.add_argument("--hook", required=True)
    hooks_record.add_argument("--original-exit-code", type=int, required=True)
    hooks_record.add_argument("--stdin-file")
    hooks_record.add_argument("hook_args", nargs=argparse.REMAINDER)
    hooks_record.set_defaults(func=cmd_hooks_record)

    skills = sub.add_parser("skills")
    skills_sub = skills.add_subparsers(dest="skills_command", required=True)
    skills_install = skills_sub.add_parser("install")
    skills_install_sub = skills_install.add_subparsers(dest="skill_name", required=True)
    codex_skill = skills_install_sub.add_parser("codex")
    add_scope_args(codex_skill)
    codex_skill.add_argument("--target", choices=("user", "project", "store"), default="user")
    codex_skill.add_argument("--force", action="store_true")
    codex_skill.add_argument("--json", action="store_true")
    codex_skill.set_defaults(func=cmd_skills_install_codex)

    setup = sub.add_parser("setup")
    add_scope_args(setup)
    setup.add_argument("--no-hooks", action="store_true")
    setup.add_argument("--codex-skill", choices=("user", "project", "store", "none"), default="user")
    setup.add_argument("--force", action="store_true")
    setup.add_argument("--json", action="store_true")
    setup.set_defaults(func=cmd_setup)

    summarize = sub.add_parser("summarize")
    add_scope_args(summarize)
    summarize.add_argument("--session")
    summarize.add_argument("--json", action="store_true")
    summarize.set_defaults(func=cmd_summarize)

    evidence = sub.add_parser("evidence")
    add_scope_args(evidence)
    evidence.add_argument("--session")
    evidence.add_argument("--json", action="store_true")
    evidence.set_defaults(func=cmd_evidence)

    memory = sub.add_parser("memory")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    memory_search = memory_sub.add_parser("search")
    add_scope_args(memory_search)
    memory_search.add_argument("query")
    memory_search.add_argument("--session")
    memory_search.add_argument("--type")
    memory_search.add_argument("--actor")
    memory_search.add_argument("--task")
    memory_search.add_argument("--tool")
    memory_search.add_argument("--git-head")
    memory_search.add_argument("--workspace")
    memory_search.add_argument("--since")
    memory_search.add_argument("--until")
    memory_search.add_argument("--limit", type=int, default=10)
    memory_search.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
    memory_search.add_argument("--no-rebuild", action="store_true")
    memory_search.add_argument("--json", action="store_true")
    memory_search.set_defaults(func=cmd_memory_search)
    memory_explain = memory_sub.add_parser("explain")
    add_scope_args(memory_explain)
    memory_explain.add_argument("query")
    memory_explain.add_argument("--source")
    memory_explain.add_argument("--min-score", type=float, default=0.0)
    memory_explain.add_argument("--no-rebuild", action="store_true")
    memory_explain.add_argument("--json", action="store_true")
    memory_explain.set_defaults(func=cmd_memory_explain)
    memory_stats_parser = memory_sub.add_parser("stats")
    add_scope_args(memory_stats_parser)
    memory_stats_parser.add_argument("--no-rebuild", action="store_true")
    memory_stats_parser.add_argument("--json", action="store_true")
    memory_stats_parser.set_defaults(func=cmd_memory_stats)

    context = sub.add_parser("context")
    context_sub = context.add_subparsers(dest="context_command", required=True)
    context_build = context_sub.add_parser("build")
    add_scope_args(context_build)
    context_build.add_argument("task")
    context_build.add_argument("--session")
    context_build.add_argument("--memory-limit", type=int, default=8)
    context_build.add_argument("--evidence-limit", type=int, default=20)
    context_build.add_argument("--recent-limit", type=int, default=5)
    context_build.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
    context_build.add_argument("--output")
    context_build.add_argument("--json", action="store_true")
    context_build.set_defaults(func=cmd_context_build)

    return parser


def add_scope_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", dest="root_option")
    parser.add_argument("--scope", choices=("project", "user", "global", "machine"))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except AgentDirError as exc:
        print(f"agentdir: {exc}", file=sys.stderr)
        return 2
