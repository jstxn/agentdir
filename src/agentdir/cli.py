from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .actors import create_actor, send_message
from .artifacts import add_artifact, artifact_headers
from .doctor import run_doctor
from .envelope import build_envelope, envelope_bytes
from .index import rebuild_index, update_index
from .mailbox import atomic_deliver
from .query import query_messages
from .replay import replay_session
from .store import AgentDirError, init_root, resolve_root, session_mailbox


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
    artifact = add_artifact(root, args.artifact) if args.artifact else None
    message = build_envelope(
        event_type=args.type,
        body=read_body(args.body),
        subject=args.subject,
        from_actor=args.from_actor,
        to_actor=args.to_actor,
        session_id=args.session,
        task_id=args.task,
        workspace=args.workspace,
        git_head=args.git_head,
        tool=args.tool,
        tool_exit_code=args.tool_exit_code,
        parent_message_id=args.parent,
        artifact_headers=artifact_headers(artifact) if artifact else {},
        message_id=args.message_id,
    )
    delivered = atomic_deliver(session_mailbox(root, args.session), envelope_bytes(message))
    print(delivered)
    return 0


def cmd_actor_create(args: argparse.Namespace) -> int:
    inbox, outbox = create_actor(command_root(args, create=True), args.actor_id)
    print(f"inbox={inbox}")
    print(f"outbox={outbox}")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    root = command_root(args, create=True)
    inbox, outbox = send_message(
        root=root,
        from_actor=args.from_actor,
        to_actor=args.to_actor,
        event_type=args.type,
        body=read_body(args.body),
        subject=args.subject,
        session_id=args.session,
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
    emit.add_argument("--session", required=True)
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
