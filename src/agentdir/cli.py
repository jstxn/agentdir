from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .actors import create_actor, send_message
from .artifacts import add_artifact
from .audit import (
    audit_claims,
    audit_recorded_claims,
    audit_session,
    format_claims_audit,
    format_session_audit,
    strict_claims_exit_code,
    strict_session_exit_code,
)
from .claims import (
    CLAIM_FAMILIES,
    OUTCOME_FAILED,
    OUTCOME_PASSED,
    OUTCOME_RETRACTED,
    format_claims,
    record_claim,
    recorded_claims,
)
from .context import (
    CONSUMPTION_PURPOSES,
    audit_context_pack,
    build_context_pack,
    cite_context_sources,
    consume_context_sources,
    emit_context_pack,
    format_context_audit,
    format_context_pack,
    write_context_pack,
)
from .capture import DEFAULT_MAX_CAPTURE_BYTES, run_tool
from .control import (
    adopt_repo,
    build_final_report,
    build_status,
    expand_work_context,
    finish_work,
    format_context_expansion_completion,
    format_context_expansion_content,
    format_final_report,
    format_status,
    format_work_start,
    review_work_context,
    show_work_context,
    start_work,
)
from .daemon import format_memory_daemon_status, memory_daemon_status
from .daemon import run_memory_daemon, start_memory_daemon, stop_memory_daemon
from .doctor import run_doctor
from .environment import (
    HOOK_MANAGER_CORE_HOOKSPATH,
    detect_environment,
    has_rule_generator,
)
from .events import emit_event
from .federation import VISIBILITY_CHOICES, add_root_to_group, create_root_group
from .federation import doctor_registered_roots, format_federated_hits, format_registered_roots
from .federation import format_root_diagnostics, format_root_groups, format_root_suggestions
from .federation import list_registered_roots, list_root_groups, rebuild_registered_roots, register_root, remove_registered_root
from .federation import remove_root_from_group, suggest_roots
from .federation import search_federated_memory
from .gitignore import GITIGNORE_CHOICES, ensure_agentdir_ignored, gitignore_plan
from .hooks import (
    DEFAULT_HOOKS,
    MANAGED_MARKER,
    can_refresh_manager_hook_backup,
    hook_status,
    install_hooks,
    record_hook_event,
    resolve_git_hooks_dir,
    uninstall_hooks,
)
from .index import rebuild_index, update_index
from .memory import (
    DEFAULT_MIN_SCORE,
    RETRIEVAL_AUTO,
    RETRIEVAL_MODES,
    explain_memory_match,
    memory_backend_status,
)
from .memory import configure_embeddings, configure_team_backend, configure_vector_backend
from .memory import format_memory_explanation, format_memory_hits, memory_stats, search_memory
from .query import query_messages
from .replay import replay_session
from .retention import (
    archive_sessions,
    format_retention_result,
    prune_sessions,
)
from .review import (
    EVIDENCE_FAMILIES,
    evidence_brief,
    evidence_rows,
    filter_evidence,
    format_evidence,
    format_evidence_brief,
    format_summary,
    format_timeline,
    summarize_session,
    timeline_rows,
)
from .sessions import end_session, ensure_session, read_current_session, require_current_session, start_session
from .rendering import rich_doctor
from .secrets import (
    format_secret_findings,
    format_secret_redaction,
    redact_secret_records,
    scan_secret_records,
)
from .skills import (
    BROAD_PROJECT_INTEGRATIONS,
    INTEGRATION_NAMES,
    InstalledSkill,
    codex_skill_path_no_create,
    generic_guidance_path_no_create,
    install_codex_skill,
    install_generic_guidance,
    install_integrations,
    integration_doctor,
    integration_plan,
    uninstall_integrations,
)
from .store import AgentDirError, init_root, require_root, resolve_root
from .upgrade import UpgradeOptions, format_upgrade_result, upgrade_agentdir, upgrade_exit_code


def read_body(path: str | None) -> str:
    if not path or path == "-":
        return sys.stdin.read()
    return Path(path).expanduser().read_text(encoding="utf-8")


def read_body_or_literal(value: str | None) -> str | None:
    if not value:
        return None
    if value == "-":
        return sys.stdin.read()
    path = Path(value).expanduser()
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return value


def print_json(data: object) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


class _ContextExpansionCliDelivery:
    def __init__(
        self,
        *,
        json_output: bool,
        invocation: tuple[str, ...],
        command_root: Path,
    ) -> None:
        self.json_output = json_output
        self.invocation = invocation
        self.command_root = command_root

    def accept(self, result: dict[str, Any]) -> None:
        if self.json_output:
            base = {key: value for key, value in result.items() if key != "warnings"}
            rendered = json.dumps(base, indent=2, sort_keys=True)
            prefix, separator, _closing = rendered.rpartition("\n}")
            if not separator:
                raise AgentDirError("Context expansion JSON delivery could not be prepared")
            sys.stdout.write(prefix)
        else:
            sys.stdout.write(
                format_context_expansion_content(
                    result,
                    invocation=self.invocation,
                    command_root=self.command_root,
                )
            )
        sys.stdout.flush()

    def complete(self, result: dict[str, Any]) -> None:
        if self.json_output:
            receipt = json.dumps(result["receipt"], indent=2, sort_keys=True).replace(
                "\n", "\n  "
            )
            warnings = json.dumps(result.get("warnings") or [], indent=2).replace(
                "\n", "\n  "
            )
            sys.stdout.write(f',\n  "receipt": {receipt},\n  "warnings": {warnings}\n}}\n')
        else:
            sys.stdout.write(format_context_expansion_completion(result))
        sys.stdout.flush()


def setup_plan(args: argparse.Namespace, *, mode: str) -> dict[str, object]:
    root = command_root(args, create=False)
    environment = detect_environment()
    selection = resolve_setup_selection(args, environment)
    already_adopted = bool(
        mode == "adopt"
        and getattr(args, "if_needed", False)
        and (root / "VERSION").is_file()
    )
    if already_adopted:
        return {
            "mode": mode,
            "dry_run": True,
            "root": str(root),
            "already_adopted": True,
            "would_create_root": False,
            "hooks": [],
            "codex_skill": None,
            "generic_guidance": None,
            "integrations": [],
            "gitignore": {
                "target": "none",
                "action": "none",
                "changed": False,
                "reason": "already_adopted",
            },
            "environment": environment,
            "adjustments": [],
            "warnings": [],
        }
    skill_target = getattr(args, "codex_skill", getattr(args, "install_skill", "none"))
    codex_skill = None
    if skill_target != "none":
        path = codex_skill_path_no_create(root, target=skill_target)
        codex_skill = {
            "target": skill_target,
            "path": str(path),
            "action": "update" if path.exists() else "create",
            "exists": path.exists(),
            "would_write": True,
        }
    generic = None
    if selection["install_legacy_generic"]:
        path = generic_guidance_path_no_create(root, target=args.install_generic)
        generic = {
            "target": args.install_generic,
            "path": str(path),
            "action": "update" if path.exists() else "create",
            "exists": path.exists(),
            "would_write": True,
        }
    names = selection["integrations"]
    return {
        "mode": mode,
        "dry_run": True,
        "root": str(root),
        "already_adopted": False,
        "would_create_root": not (root / "VERSION").is_file(),
        "hooks": [] if args.no_hooks else hooks_install_plan(force=args.force),
        "codex_skill": codex_skill,
        "generic_guidance": generic,
        "integrations": integration_plan(
            root,
            names,
            target=args.integration_target,
            force=args.force,
        ) if names else [],
        "gitignore": gitignore_plan(target=getattr(args, "gitignore", "none"), cwd=Path.cwd()),
        "environment": environment,
        "adjustments": selection["adjustments"],
        "warnings": selection["warnings"],
    }


def print_setup_plan(plan: dict[str, object]) -> None:
    print(f"mode={plan['mode']}")
    print(f"root={plan['root']}")
    print(f"dry_run={str(plan['dry_run']).lower()}")
    print(f"already_adopted={str(plan.get('already_adopted', False)).lower()}")
    print(f"would_create_root={str(plan['would_create_root']).lower()}")
    print(f"hooks={len(plan['hooks'])}")  # type: ignore[arg-type]
    integrations = plan.get("integrations") or []
    print(f"integrations={len(integrations)}")
    gitignore = plan.get("gitignore") or {}
    if isinstance(gitignore, dict):
        print(f"gitignore={gitignore.get('target')}:{gitignore.get('action')}")
    for adjustment in plan.get("adjustments") or []:
        print(f"skipped {adjustment['name']}: {adjustment['reason']}")
    for warning in plan.get("warnings") or []:
        print(f"warning: {warning}")


def resolve_setup_selection(
    args: argparse.Namespace,
    environment: dict[str, object],
) -> dict[str, object]:
    """Pick integrations and warnings for adopt/setup given detected repo tooling.

    In rulesync repos the generated tool files (CLAUDE.md, AGENTS.md,
    .cursor/rules, ...) are regenerated from .rulesync/ and would silently lose
    managed blocks, so the default target becomes the rulesync source rule.
    ``--install-integrations project-files`` restores project-file selection;
    generated-header files still require ``--force`` before they are edited.
    """
    mode = getattr(args, "install_integrations", "all")
    integration_target = getattr(args, "integration_target", "project")
    install_generic = getattr(args, "install_generic", "none")
    adjustments: list[dict[str, str]] = []
    warnings: list[str] = []
    rulesync = has_rule_generator(environment)

    if mode == "none":
        names: list[str] = []
    else:
        names = list(BROAD_PROJECT_INTEGRATIONS)
        if install_generic != integration_target and "generic" in names:
            names.remove("generic")

    redirected = bool(rulesync and mode == "all" and integration_target == "project" and names)
    if redirected:
        for name in names:
            adjustments.append(
                {
                    "name": name,
                    "action": "skip",
                    "reason": "rulesync regenerates this file; the managed rule moved to .rulesync/rules/agentdir.md",
                }
            )
        names = ["rulesync"]
        warnings.append(
            "rulesync detected: agent rule files in this repo are generated from .rulesync/ and a "
            "managed block in them would be wiped by the next generate. AgentDir installs the source "
            "rule .rulesync/rules/agentdir.md instead; run 'rulesync generate' to propagate it. Use "
            "--install-integrations project-files to write the tool files directly anyway."
        )
    elif rulesync and mode == "project-files":
        warnings.append(
            "rulesync detected but --install-integrations project-files was given: managed blocks in "
            "generated files will be wiped by the next 'rulesync generate'. Files with a generated "
            "header remain protected unless --force is also given."
        )

    install_legacy_generic = install_generic != "none" and not (
        install_generic == integration_target and mode != "none" and not redirected
    )
    if redirected:
        # AGENTS.md is one of the generated files; the rulesync rule covers it.
        install_legacy_generic = install_legacy_generic and install_generic != "project"
    elif rulesync and mode == "none" and install_legacy_generic and install_generic == "project":
        install_legacy_generic = False
        adjustments.append(
            {
                "name": "generic",
                "action": "skip",
                "reason": "AGENTS.md is generated by rulesync; use .rulesync/rules/agentdir.md or --install-generic store",
            }
        )

    if not getattr(args, "no_hooks", False):
        warnings.extend(hook_manager_warnings(environment))

    return {
        "integrations": names,
        "install_legacy_generic": install_legacy_generic,
        "adjustments": adjustments,
        "warnings": warnings,
        "redirected_to_rulesync": redirected,
    }


def hook_manager_warnings(environment: dict[str, object]) -> list[str]:
    warnings: list[str] = []
    for manager in environment.get("hook_managers") or []:
        name = manager.get("name")
        if name == HOOK_MANAGER_CORE_HOOKSPATH:
            warnings.append(
                f"git core.hooksPath={manager.get('hooks_path')} is set; AgentDir installs its hook "
                "shims into that directory. Tools that regenerate it will remove them; 'agentdir "
                "doctor' flags lost shims."
            )
            continue
        warnings.append(
            f"{name} detected: reinstalling its hooks (for example via a package install) overwrites "
            "AgentDir hook shims and silently stops git recording. The shims chain the existing "
            f"{name} hooks, 'agentdir doctor' flags lost shims, and 'agentdir hooks install' "
            "restores them. See docs/INSTALL.md for hook-manager coexistence."
        )
    return warnings


def selected_gitignore_target(args: argparse.Namespace) -> str:
    target = getattr(args, "gitignore", "none")
    if target != "ask":
        return target
    if getattr(args, "json", False) or not sys.stdin.isatty() or not sys.stderr.isatty():
        return "none"

    sys.stderr.write(
        "Ignore .agentdir? [P]roject .gitignore / [U]ser Git ignore / [N]one "
        "(default: project): "
    )
    sys.stderr.flush()
    answer = sys.stdin.readline().strip().lower()
    if answer in ("", "p", "project"):
        return "project"
    if answer in ("u", "user"):
        return "user"
    if answer in ("n", "none", "no"):
        return "none"
    raise AgentDirError("Invalid gitignore choice; use project, user, or none")


def print_setup_notices(
    *,
    integrations: list[dict[str, object]],
    skill: InstalledSkill | None,
    generic: InstalledSkill | None,
    selection: dict[str, object],
) -> None:
    for item in integrations:
        if item.get("skipped"):
            print(f"skipped {item['name']}: {item['skipped']}")
    if skill and skill.skipped:
        print(f"skipped codex skill: {skill.skipped}")
    if generic and generic.skipped:
        print(f"skipped generic: {generic.skipped}")
    for adjustment in selection.get("adjustments") or []:
        print(f"skipped {adjustment['name']}: {adjustment['reason']}")
    for warning in selection.get("warnings") or []:
        print(f"warning: {warning}")


def installed_skill_payload(installed: InstalledSkill) -> dict[str, object]:
    return {
        "target": installed.target,
        "path": str(installed.path),
        "updated": installed.updated,
        "backup_path": str(installed.backup_path) if installed.backup_path else None,
        "skipped": installed.skipped,
    }


def format_gitignore_result(result: dict[str, object] | None) -> str:
    if not result:
        return "none"
    target = result.get("target")
    action = result.get("action")
    path = result.get("path")
    changed = result.get("changed")
    suffix = f":{path}" if path else ""
    changed_text = "changed" if changed else "unchanged"
    return f"{target}:{action}:{changed_text}{suffix}"


def hooks_install_plan(*, force: bool = False, hooks: list[str] | None = None) -> list[dict[str, object]]:
    hooks_dir = git_hooks_dir_no_create()
    plan: list[dict[str, object]] = []
    for name in hooks or list(DEFAULT_HOOKS):
        path = hooks_dir / name
        original = hooks_dir / f"{name}.agentdir-original"
        existing = path.read_text(encoding="utf-8", errors="ignore") if path.is_file() else ""
        backup = original.read_text(encoding="utf-8", errors="ignore") if original.is_file() else ""
        managed = MANAGED_MARKER in existing
        refresh_manager_backup = can_refresh_manager_hook_backup(existing, backup)
        if not path.exists():
            action = "create"
        elif managed:
            action = "update"
        elif original.exists() and not force and not refresh_manager_backup:
            action = "refuse"
        elif refresh_manager_backup:
            action = "refresh-manager-backup"
        else:
            action = "backup-and-install"
        plan.append(
            {
                "hook": name,
                "path": str(path),
                "action": action,
                "installed": path.exists(),
                "managed": managed,
                "original": str(original) if original.exists() else None,
                "would_write": action != "refuse",
                "would_refuse": action == "refuse",
            }
        )
    return plan


def hooks_uninstall_plan(hooks: list[str] | None = None) -> list[dict[str, object]]:
    hooks_dir = git_hooks_dir_no_create()
    plan: list[dict[str, object]] = []
    for name in hooks or list(DEFAULT_HOOKS):
        path = hooks_dir / name
        original = hooks_dir / f"{name}.agentdir-original"
        existing = path.read_text(encoding="utf-8", errors="ignore") if path.is_file() else ""
        managed = MANAGED_MARKER in existing
        if managed and original.exists():
            action = "restore-original"
        elif managed:
            action = "remove"
        elif path.exists():
            action = "preserve-unmanaged"
        else:
            action = "none"
        plan.append(
            {
                "hook": name,
                "path": str(path),
                "action": action,
                "installed": path.exists(),
                "managed": managed,
                "original": str(original) if original.exists() else None,
                "would_write": action in {"restore-original", "remove"},
            }
        )
    return plan


def git_hooks_dir_no_create() -> Path:
    return resolve_git_hooks_dir()


def cmd_init(args: argparse.Namespace) -> int:
    root_arg = args.root_option or args.root
    print(init_root(resolve_root(root_arg, args.scope)).root)
    return 0


def cmd_root(args: argparse.Namespace) -> int:
    root = resolve_root(args.root_option, args.scope)
    if args.require_initialized:
        require_root(root)
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
    if args.json:
        print_json(
            {
                "path": str(delivered.path),
                "session_id": delivered.session_id,
                "event_type": delivered.event_type,
            }
        )
    else:
        print(delivered.path)
    return 0


def cmd_actor_create(args: argparse.Namespace) -> int:
    inbox, outbox = create_actor(command_root(args, create=True), args.actor_id)
    if args.json:
        print_json({"actor_id": args.actor_id, "inbox": str(inbox), "outbox": str(outbox)})
    else:
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
    if args.json:
        print_json(
            {
                "from": args.from_actor,
                "to": args.to_actor,
                "event_type": args.type,
                "inbox": str(inbox),
                "outbox": str(outbox),
            }
        )
    else:
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


def cmd_roots_register(args: argparse.Namespace) -> int:
    entry = register_root(command_root(args, create=True), args.path, name=args.name, visibility=args.visibility)
    if args.json:
        print_json(entry)
    else:
        print(f"{entry['root_id']} {entry['name']} {entry['root_path']}")
    return 0


def cmd_roots_list(args: argparse.Namespace) -> int:
    roots = list_registered_roots(command_root(args, create=True))
    if args.json:
        print_json(roots)
    else:
        print(format_registered_roots(roots), end="")
    return 0


def cmd_roots_remove(args: argparse.Namespace) -> int:
    removed = remove_registered_root(command_root(args, create=True), args.identifier)
    if args.json:
        print_json(removed)
    else:
        print(f"removed={removed['root_id']}")
    return 0


def cmd_roots_rebuild(args: argparse.Namespace) -> int:
    results = rebuild_registered_roots(
        command_root(args, create=True),
        group=args.group,
        stale_only=args.stale,
    )
    if args.json:
        print_json(results)
    else:
        for result in results:
            if result.get("skipped"):
                status = f"skipped={result.get('reason')}"
            else:
                status = f"indexed={result['indexed']} malformed={result['malformed']}" if result.get("ok") else f"error={result.get('error')}"
            print(f"{result['root_id']} {status}")
    return 0


def cmd_roots_suggest(args: argparse.Namespace) -> int:
    suggestions = suggest_roots(command_root(args, create=True), near=args.near)
    if args.json:
        print_json(suggestions)
    else:
        print(format_root_suggestions(suggestions), end="")
    return 0


def cmd_roots_doctor(args: argparse.Namespace) -> int:
    diagnostics = doctor_registered_roots(command_root(args, create=True), group=args.group)
    if args.json:
        print_json(diagnostics)
    else:
        print(format_root_diagnostics(diagnostics), end="")
    return 0


def cmd_roots_group_create(args: argparse.Namespace) -> int:
    group = create_root_group(command_root(args, create=True), args.name, args.members)
    if args.json:
        print_json(group)
    else:
        print(f"{group['name']} roots={len(group['root_ids'])}")
    return 0


def cmd_roots_group_list(args: argparse.Namespace) -> int:
    groups = list_root_groups(command_root(args, create=True))
    if args.json:
        print_json(groups)
    else:
        print(format_root_groups(groups), end="")
    return 0


def cmd_roots_group_add(args: argparse.Namespace) -> int:
    group = add_root_to_group(command_root(args, create=True), args.name, args.root_id)
    if args.json:
        print_json(group)
    else:
        print(f"{group['name']} roots={len(group['root_ids'])}")
    return 0


def cmd_roots_group_remove(args: argparse.Namespace) -> int:
    group = remove_root_from_group(command_root(args, create=True), args.name, args.root_id)
    if args.json:
        print_json(group)
    else:
        print(f"{group['name']} roots={len(group['root_ids'])}")
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    result = archive_sessions(
        command_root(args, create=True),
        sessions=args.sessions,
        older_than_days=args.older_than_days,
        keep_recent=args.keep_recent,
        apply=args.apply,
    )
    if args.json:
        print_json(result.as_dict())
    else:
        print(format_retention_result(result))
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    result = prune_sessions(
        command_root(args, create=True),
        sessions=args.sessions,
        older_than_days=args.older_than_days,
        keep_recent=args.keep_recent,
        include_live_sessions=args.include_live_sessions,
        apply=args.apply,
    )
    if args.json:
        print_json(result.as_dict())
    else:
        print(format_retention_result(result))
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    root = command_root(args)
    if args.semantic and args.text:
        raise AgentDirError("Use either --text for literal search or --semantic for vector memory search")
    if args.semantic:
        update_index(root)
        search = search_federated_memory if args.federated else search_memory
        rows = search(
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
            retrieval_mode=args.retrieval,
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
        print(format_federated_hits(rows) if args.federated else format_memory_hits(rows))
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
    root = command_root(args)
    update_index(root)
    if args.json:
        rows = query_messages(root, session_id=args.session, limit=10_000)
        print_json(
            [
                {
                    "date": row.get("date_header") or row.get("indexed_at"),
                    "event_type": row.get("event_type"),
                    "subject": row.get("subject"),
                    "tool": row.get("tool"),
                    "tool_exit_code": row.get("tool_exit_code"),
                    "body_text": row.get("body_text"),
                    "file_path": row.get("file_path"),
                }
                for row in rows
            ]
        )
        return 0
    for line in replay_session(root, args.session):
        print(line)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    report = run_doctor(command_root(args))
    if args.json:
        print_json(report.as_dict())
    else:
        rendered = rich_doctor(report.as_dict())
        if rendered is not None:
            print(rendered, end="")
        else:
            print(f"ok={str(report.ok).lower()}")
            for warning in report.warnings:
                print(f"warning: {warning}")
            for error in report.errors:
                print(f"error: {error}")
    return 0 if report.ok else 1


def cmd_secrets_scan(args: argparse.Namespace) -> int:
    findings = scan_secret_records(command_root(args))
    if args.json:
        print_json([asdict(finding) for finding in findings])
    else:
        print(format_secret_findings(findings))
    return 1 if findings else 0


def cmd_secrets_redact(args: argparse.Namespace) -> int:
    result = redact_secret_records(command_root(args), apply=args.apply)
    if args.json:
        print_json(result)
    else:
        print(format_secret_redaction(result))
    return 0


def cmd_upgrade(args: argparse.Namespace) -> int:
    result = upgrade_agentdir(
        UpgradeOptions(
            repo=args.upgrade_repo,
            version=args.upgrade_version,
            adopt=not args.upgrade_no_adopt,
            install_skill=args.upgrade_install_skill,
            hooks=not args.upgrade_no_hooks,
            dry_run=args.upgrade_dry_run,
        )
    )
    if args.upgrade_json:
        print_json(result)
    else:
        print(format_upgrade_result(result))
    return upgrade_exit_code(result)


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
        summary=read_body_or_literal(args.summary),
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
    root = command_root(args, create=True)
    session_mode = args.session or "auto"
    session_id: str | None
    if session_mode == "auto":
        session_id = None
    elif session_mode == "require":
        session_id = require_current_session(root).session_id
    elif session_mode == "create":
        session_id = start_session(root, title=f"AgentDir run: {command[0]}").session_id
    else:
        session_id = session_mode
    result = run_tool(
        root,
        argv=command,
        session_id=session_id,
        tool_name=args.name,
        cwd=args.cwd,
        max_capture_bytes=args.max_capture_bytes,
        redact=not args.no_redact,
        timeout=args.timeout,
    )
    if args.json:
        print_json(
            {
                "exit_code": result.exit_code,
                "session_id": result.session_id,
                "tool": result.tool,
                "duration_ms": result.duration_ms,
                "truncated_streams": list(result.truncated_streams),
                "timed_out": result.timed_out,
                "event_path": result.event_path,
            }
        )
    return result.exit_code


def cmd_hooks_install(args: argparse.Namespace) -> int:
    root = command_root(args, create=False)
    infos = install_hooks(
        root,
        hooks=args.hooks,
        force=args.force,
    )
    if args.json:
        print_json([asdict(info) for info in infos])
    else:
        for info in infos:
            suffix = f" (chains {info.owner})" if info.owner else ""
            print(f"{info.hook}: installed {info.path}{suffix}")
        for warning in hook_manager_warnings(detect_environment()):
            print(f"warning: {warning}")
    return 0


def cmd_hooks_status(args: argparse.Namespace) -> int:
    infos = hook_status(hooks=args.hooks)
    if args.json:
        print_json([asdict(info) for info in infos])
    else:
        for info in infos:
            state = "managed" if info.managed else "unmanaged" if info.installed else "missing"
            suffix = f" (owned by {info.owner})" if info.owner else ""
            print(f"{info.hook}: {state} {info.path}{suffix}")
    return 0


def cmd_hooks_uninstall(args: argparse.Namespace) -> int:
    infos = uninstall_hooks(hooks=args.hooks, root=command_root(args, create=False))
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
    if args.json:
        print_json(
            {
                "recorded": True,
                "hook": args.hook,
                "original_exit_code": args.original_exit_code,
            }
        )
    return 0


def cmd_skills_install_codex(args: argparse.Namespace) -> int:
    installed = install_codex_skill(
        command_root(args, create=True),
        target=args.target,
        force=args.force,
    )
    if args.json:
        print_json(installed_skill_payload(installed))
    elif installed.skipped:
        print(f"skipped {installed.path}: {installed.skipped}")
    else:
        print(installed.path)
    return 0


def cmd_skills_install_generic(args: argparse.Namespace) -> int:
    installed = install_generic_guidance(
        command_root(args, create=True),
        target=args.target,
        force=args.force,
    )
    if args.json:
        print_json(installed_skill_payload(installed))
    elif installed.skipped:
        print(f"skipped {installed.path}: {installed.skipped}")
    else:
        print(installed.path)
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    if args.dry_run:
        result = setup_plan(args, mode="setup")
        if args.json:
            print_json(result)
        else:
            print_setup_plan(result)
        return 0
    root = command_root(args, create=False)
    environment = detect_environment()
    selection = resolve_setup_selection(args, environment)
    if args.no_hooks:
        init_root(root)
        hooks = []
    else:
        hooks = install_hooks(root, force=args.force)
    skill = None
    if args.codex_skill != "none":
        skill = install_codex_skill(root, target=args.codex_skill, force=args.force)
    generic = None
    if selection["install_legacy_generic"]:
        generic = install_generic_guidance(root, target=args.install_generic, force=args.force)
    integrations = []
    names = selection["integrations"]
    if names:
        integrations = install_integrations(root, names, target=args.integration_target, force=args.force)
    gitignore = ensure_agentdir_ignored(target=selected_gitignore_target(args), cwd=Path.cwd())
    result = {
        "root": str(root),
        "hooks": [asdict(info) for info in hooks],
        "codex_skill": str(skill.path) if skill else None,
        "codex_skill_skipped": skill.skipped if skill else None,
        "generic_guidance": str(generic.path) if generic else None,
        "generic_guidance_skipped": generic.skipped if generic else None,
        "integrations": integrations,
        "gitignore": gitignore,
        "environment": environment,
        "adjustments": selection["adjustments"],
        "warnings": selection["warnings"],
    }
    if args.json:
        print_json(result)
    else:
        print(f"root={root}")
        if hooks:
            print(f"hooks={len(hooks)}")
        if skill and not skill.skipped:
            print(f"codex_skill={skill.path}")
        if generic and not generic.skipped:
            print(f"generic_guidance={generic.path}")
        if integrations:
            print(f"integrations={len(integrations)}")
        print(f"gitignore={format_gitignore_result(gitignore)}")
        print_setup_notices(integrations=integrations, skill=skill, generic=generic, selection=selection)
    return 0


def cmd_adopt(args: argparse.Namespace) -> int:
    if args.dry_run:
        result = setup_plan(args, mode="adopt")
        if args.json:
            print_json(result)
        else:
            print_setup_plan(result)
        return 0
    root = command_root(args, create=False)
    environment = detect_environment()
    selection = resolve_setup_selection(args, environment)
    if args.if_needed and (root / "VERSION").is_file():
        paths = require_root(root)
        result = {
            "root": str(paths.root),
            "version": (paths.meta / "VERSION").read_text(encoding="utf-8").strip(),
            "hooks": [],
            "codex_skill": None,
            "generic_guidance": None,
            "integrations": [],
            "gitignore": {
                "target": "none",
                "action": "none",
                "changed": False,
                "reason": "already_adopted",
            },
            "doctor": run_doctor(paths.root).as_dict(),
            "next": 'agentdir work start "<task>"',
            "environment": environment,
            "adjustments": [],
            "warnings": [],
            "codex_skill_skipped": None,
            "generic_guidance_skipped": None,
            "already_adopted": True,
            "changed": False,
        }
        if args.json:
            print_json(result)
        else:
            print(f"root={result['root']}")
            print("already_adopted=true")
            print("changed=false")
            print(f"doctor_ok={str(result['doctor']['ok']).lower()}")
            print(f"next={result['next']}")
        return 0
    if args.no_hooks:
        init_root(root)
        hooks = []
    else:
        hooks = install_hooks(root, force=args.force)
    skill = None
    if args.install_skill != "none":
        skill = install_codex_skill(root, target=args.install_skill, force=args.force)
    generic = None
    if selection["install_legacy_generic"]:
        generic = install_generic_guidance(root, target=args.install_generic, force=args.force)
    integrations = []
    names = selection["integrations"]
    if names:
        integrations = install_integrations(root, names, target=args.integration_target, force=args.force)
    gitignore = ensure_agentdir_ignored(target=selected_gitignore_target(args), cwd=Path.cwd())
    result = adopt_repo(
        root,
        install_hooks_result=[asdict(info) for info in hooks],
        codex_skill_path=str(skill.path) if skill and not skill.skipped else None,
        generic_guidance_path=str(generic.path) if generic and not generic.skipped else None,
        integrations=integrations,
        gitignore=gitignore,
    )
    result["environment"] = environment
    result["adjustments"] = selection["adjustments"]
    result["warnings"] = selection["warnings"]
    result["codex_skill_skipped"] = skill.skipped if skill else None
    result["generic_guidance_skipped"] = generic.skipped if generic else None
    if args.json:
        print_json(result)
    else:
        print(f"root={result['root']}")
        print(f"doctor_ok={str(result['doctor']['ok']).lower()}")
        if hooks:
            print(f"hooks={len(hooks)}")
        if skill and not skill.skipped:
            print(f"codex_skill={skill.path}")
        if generic and not generic.skipped:
            print(f"generic_guidance={generic.path}")
        if integrations:
            print(f"integrations={len(integrations)}")
        print(f"gitignore={format_gitignore_result(gitignore)}")
        print_setup_notices(integrations=integrations, skill=skill, generic=generic, selection=selection)
        print(f"next={result['next']}")
    return 0


def cmd_unadopt(args: argparse.Namespace) -> int:
    root = command_root(args, create=False)
    hooks = [] if args.no_hooks else uninstall_hooks(root=root) if args.apply else hooks_uninstall_plan()
    project_integrations = uninstall_integrations(root, ["all", "rulesync"], target="project", apply=args.apply)
    store_integrations = uninstall_integrations(root, ["all", "rulesync"], target="store", apply=args.apply)
    result = {
        "root": str(root),
        "applied": args.apply,
        "hooks": [asdict(info) if hasattr(info, "__dataclass_fields__") else info for info in hooks],
        "integrations": {
            "project": project_integrations,
            "store": store_integrations,
        },
        "preserved": [str(root)],
    }
    if args.json:
        print_json(result)
    else:
        print(f"root={root}")
        print(f"applied={str(args.apply).lower()}")
        print(f"hooks={len(result['hooks'])}")
        print(f"project_integrations={len(project_integrations)}")
        print(f"store_integrations={len(store_integrations)}")
        print(f"preserved={root}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    status = build_status(command_root(args), scope=args.scope, rebuild=not args.no_rebuild)
    if args.json:
        print_json(status)
    else:
        print(format_status(status), end="")
    return 0 if status["health"]["ok"] else 1


def cmd_work_start(args: argparse.Namespace) -> int:
    root = command_root(args, create=True)
    result = start_work(
        root,
        args.task,
        actor=args.actor,
        emit_context=args.emit_context,
        federated=args.federated,
        federation_group=args.group,
        memory_limit=args.memory_limit,
        evidence_limit=args.evidence_limit,
        recent_limit=args.recent_limit,
        min_score=args.min_score,
        retrieval_mode=args.retrieval,
        invocation=args.invocation,
    )
    if args.json:
        print_json(result)
    else:
        print(
            format_work_start(
                result,
                invocation=args.invocation,
                command_root=root,
            ),
            end="",
        )
    return 0


def cmd_work_context(args: argparse.Namespace) -> int:
    root = command_root(args)
    if args.page is not None and not args.expand_source:
        raise AgentDirError("--page requires --expand")
    if args.expand_source:
        page = 1 if args.page is None else args.page
        if page < 1:
            raise AgentDirError("Context expansion page must be at least 1")
        delivery = _ContextExpansionCliDelivery(
            json_output=args.json,
            invocation=args.invocation,
            command_root=root,
        )
        expand_work_context(
            root,
            source_selector=args.expand_source,
            page=page,
            actor=args.actor,
            session_id=args.session,
            pack_id=args.pack,
            delivery=delivery,
        )
        return 0
    if args.show:
        result = show_work_context(
            root,
            session_id=args.session,
            pack_id=args.pack,
        )
        if args.json:
            print_json(result)
        else:
            print(
                format_work_start(
                    result,
                    invocation=args.invocation,
                    command_root=root,
                ),
                end="",
            )
        return 0
    if not args.reason:
        raise AgentDirError("Context review reason is required")
    disposition = "used" if args.sources else "no_relevant" if args.none_relevant else "skipped"
    result = review_work_context(
        root,
        disposition=disposition,
        reason=args.reason,
        source_selectors=args.sources,
        purpose=args.purpose,
        actor=args.actor,
        session_id=args.session,
        pack_id=args.pack,
    )
    if args.json:
        print_json(result)
    else:
        print(f"context_reviewed={result['pack_id']}")
        print(f"disposition={result['disposition']}")
        print(f"reviewed={result['reviewed_count']}")
        print(f"used={result['used_count']}")
        print(f"dismissed={result['dismissed_count']}")
        print(f"recorded={str(result['recorded']).lower()}")
        print(f"reason={result['reason']}")
    return 0


def cmd_work_finish(args: argparse.Namespace) -> int:
    claims_text = read_body(args.claims) if args.claims else None
    result = finish_work(
        command_root(args),
        session_id=args.session,
        actor=args.actor,
        run_health_check=not args.no_doctor,
        end=not args.keep_session,
        claims_text=claims_text,
        invocation=args.invocation,
    )
    if args.json:
        print_json({key: value for key, value in result.items() if key != "rendered"})
    else:
        print(result["rendered"], end="")
    return 0


def cmd_report_final(args: argparse.Namespace) -> int:
    claims_text = read_body(args.claims) if args.claims else None
    report = build_final_report(
        command_root(args),
        session_id=args.session,
        run_health_check=not args.no_doctor,
        claims_text=claims_text,
    )
    if args.format == "json":
        print_json(report)
    else:
        print(format_final_report(report), end="")
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
    if args.family or args.failed:
        rows = filter_evidence(rows, family=args.family, failed=args.failed)
    if args.brief:
        brief = evidence_brief(rows, family=None, failed=False)
        if args.json:
            print_json(brief)
        else:
            print(format_evidence_brief(brief))
        return 0
    if args.json:
        print_json(rows)
    else:
        print(format_evidence(rows))
    return 0


def cmd_timeline(args: argparse.Namespace) -> int:
    rows = timeline_rows(command_root(args), args.session, limit=args.limit)
    if args.json:
        print_json(rows)
    else:
        print(format_timeline(rows))
    return 0


def cmd_integrations_install(args: argparse.Namespace) -> int:
    root = command_root(args, create=args.target == "store")
    result = install_integrations(root, [args.name], target=args.target, force=args.force)
    if args.json:
        print_json(result)
    else:
        for item in result:
            print(f"{item['name']}: {item['path']}")
    return 0


def cmd_integrations_doctor(args: argparse.Namespace) -> int:
    result = integration_doctor(command_root(args, create=False), ["all"], target=args.target)
    if args.json:
        print_json(result)
    else:
        print(f"ok={str(result['ok']).lower()}")
        for check in result["checks"]:
            print(f"{check['name']}: {check['state']} {check['path']}")
    return 0


def cmd_memory_search(args: argparse.Namespace) -> int:
    root = command_root(args)
    if not args.no_rebuild:
        update_index(root)
    search_kwargs = {
        "session_id": args.session,
        "event_type": args.type,
        "actor": args.actor,
        "task_id": args.task,
        "tool": args.tool,
        "git_head": args.git_head,
        "workspace": args.workspace,
        "since": args.since,
        "until": args.until,
        "limit": args.limit,
        "min_score": args.min_score,
        "retrieval_mode": args.retrieval,
    }
    rows = search_federated_memory(
        root,
        args.query,
        rebuild=not args.no_rebuild,
        group=args.group,
        **search_kwargs,
    ) if args.federated or args.group else search_memory(root, args.query, **search_kwargs)
    if args.json:
        print_json(rows)
    else:
        print(format_federated_hits(rows) if args.federated or args.group else format_memory_hits(rows))
    return 0


def cmd_memory_explain(args: argparse.Namespace) -> int:
    root = command_root(args)
    if not args.no_rebuild:
        update_index(root)
    explanation = explain_memory_match(
        root,
        args.query,
        source_id=args.source,
        min_score=args.min_score,
        retrieval_mode=args.retrieval,
    )
    if args.json:
        print_json(explanation)
    else:
        print(format_memory_explanation(explanation))
    return 0


def cmd_memory_stats(args: argparse.Namespace) -> int:
    root = command_root(args)
    if not args.no_rebuild:
        update_index(root)
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
        print(f"passages={stats['passages']}")
        print(f"terms={stats['terms']}")
        print(f"retrieval_backend={stats['retrieval_backend']}")
    return 0


def cmd_memory_backend_list(args: argparse.Namespace) -> int:
    root = command_root(args)
    if not args.no_rebuild:
        update_index(root)
    status = memory_backend_status(root)
    if args.json:
        print_json(status)
    else:
        print(f"active={status['active']}")
        print(f"source_of_truth={status['source_of_truth']}")
        for backend in status["backends"]:
            print(
                f"{backend['name']} enabled={str(backend['enabled']).lower()} "
                f"kind={backend['kind']}"
            )
    return 0


def cmd_memory_backend_configure(args: argparse.Namespace) -> int:
    root = command_root(args, create=True)
    update_index(root)
    status = configure_vector_backend(root, args.backend)
    if args.json:
        print_json(status)
    else:
        print(f"vector_backend={status['config'].get('vector_backend') or 'none'}")
        print(f"active={status['active']}")
    return 0


def cmd_memory_embeddings_configure(args: argparse.Namespace) -> int:
    root = command_root(args, create=True)
    update_index(root)
    status = configure_embeddings(root, args.provider, model=args.model)
    if args.json:
        print_json(status)
    else:
        embeddings = status["config"].get("embeddings") or {}
        print(f"embedding_provider={embeddings.get('provider') or 'none'}")
        print(f"embedding_model={embeddings.get('model') or ''}")
        print(f"active={status['active']}")
    return 0


def cmd_memory_team_configure(args: argparse.Namespace) -> int:
    root = command_root(args, create=True)
    update_index(root)
    status = configure_team_backend(root, args.backend)
    if args.json:
        print_json(status)
    else:
        print(f"team_backend={status['config'].get('team_backend') or 'none'}")
        print(f"active={status['active']}")
    return 0


def cmd_memory_daemon_start(args: argparse.Namespace) -> int:
    status = start_memory_daemon(
        command_root(args, create=True),
        interval=args.interval,
        group=args.group,
        force=args.force,
    )
    if args.json:
        print_json(status)
    else:
        print(format_memory_daemon_status(status), end="")
    return 0


def cmd_memory_daemon_status(args: argparse.Namespace) -> int:
    status = memory_daemon_status(command_root(args))
    if args.json:
        print_json(status)
    else:
        print(format_memory_daemon_status(status), end="")
    return 0


def cmd_memory_daemon_stop(args: argparse.Namespace) -> int:
    status = stop_memory_daemon(command_root(args), timeout=args.timeout)
    if args.json:
        print_json(status)
    else:
        print(format_memory_daemon_status(status), end="")
    return 0


def cmd_memory_daemon_run(args: argparse.Namespace) -> int:
    status = run_memory_daemon(
        command_root(args, create=True),
        interval=args.interval,
        group=args.group,
        once=args.once,
    )
    if args.json:
        print_json(status)
    else:
        print(format_memory_daemon_status(status), end="")
    return 0


def cmd_context_build(args: argparse.Namespace) -> int:
    root = command_root(args, create=args.emit)
    if args.emit and not args.session and read_current_session(root) is None:
        ensure_session(root, title=f"Context pack: {args.task}")
    pack = build_context_pack(
        root,
        args.task,
        session_id=args.session,
        memory_limit=args.memory_limit,
        evidence_limit=args.evidence_limit,
        recent_limit=args.recent_limit,
        min_score=args.min_score,
        federated=args.federated,
        federation_group=args.group,
        retrieval_mode=args.retrieval,
    )
    emitted = None
    if args.emit:
        emitted = emit_context_pack(
            root,
            pack,
            selection_policy={
                "memory_limit": args.memory_limit,
                "evidence_limit": args.evidence_limit,
                "recent_limit": args.recent_limit,
                "min_score": args.min_score,
                "federated": args.federated,
                "federation_group": args.group,
                "retrieval_mode": args.retrieval,
            },
            scope=args.group or ("federated" if args.federated else args.scope or "project"),
        )
    if args.output:
        path = write_context_pack(args.output, pack, as_json=args.json)
        if emitted and args.json:
            print_json(
                {
                    "context_output": str(path),
                    "event_path": str(emitted.event_path),
                    "artifact": {
                        "sha256": emitted.artifact_sha256,
                        "path": str(emitted.artifact_path),
                    },
                    "manifest": emitted.manifest,
                }
            )
        else:
            print(path)
            if emitted:
                print(f"context_pack={emitted.manifest['pack_id']}")
    elif args.json:
        if emitted:
            print_json(
                {
                    "event_path": str(emitted.event_path),
                    "artifact": {
                        "sha256": emitted.artifact_sha256,
                        "path": str(emitted.artifact_path),
                    },
                    "manifest": emitted.manifest,
                }
            )
        else:
            print_json(pack)
    else:
        print(format_context_pack(pack), end="")
        if emitted:
            print(f"\nContext pack: {emitted.manifest['pack_id']}")
    return 0


def cmd_context_consume(args: argparse.Namespace) -> int:
    result = consume_context_sources(
        command_root(args),
        pack_id=args.pack,
        source_ids=args.sources,
        purpose=args.purpose,
        session_id=args.session,
        actor=args.actor,
    )
    if args.json:
        print_json(result)
    else:
        print(f"context_consumed={result['pack_id']}")
        print(f"purpose={result['purpose']}")
        print(f"sources={len(result['source_ids'])}")
    return 0


def cmd_context_cite(args: argparse.Namespace) -> int:
    citation = cite_context_sources(
        command_root(args),
        pack_id=args.pack,
        source_ids=args.sources,
        output_format=args.format,
        session_id=args.session,
        actor=args.actor,
    )
    if args.json:
        print_json(citation)
    else:
        print(citation["rendered"], end="")
    return 0


def cmd_audit_context(args: argparse.Namespace) -> int:
    audit = audit_context_pack(command_root(args), args.pack)
    if args.json:
        print_json(audit)
    else:
        print(format_context_audit(audit))
    return 0


def cmd_audit_session(args: argparse.Namespace) -> int:
    audit = audit_session(command_root(args), args.session, strict=args.strict)
    if args.json:
        print_json(audit)
    else:
        print(format_session_audit(audit))
    return strict_session_exit_code(audit) if args.strict else 0


def cmd_audit_claims(args: argparse.Namespace) -> int:
    root = command_root(args)
    if args.text is None:
        audit = audit_recorded_claims(root, args.session, strict=args.strict)
    else:
        audit = audit_claims(root, read_body(args.text), args.session, strict=args.strict)
    if args.json:
        print_json(audit)
    else:
        print(format_claims_audit(audit))
    return strict_claims_exit_code(audit) if args.strict else 0


def cmd_claim(args: argparse.Namespace) -> int:
    if args.retract:
        outcome = OUTCOME_RETRACTED
    elif args.failed:
        outcome = OUTCOME_FAILED
    else:
        outcome = OUTCOME_PASSED
    result = record_claim(
        command_root(args, create=True),
        args.family,
        outcome,
        note=args.note,
        session_id=args.session,
        actor=args.actor,
    )
    if args.json:
        print_json(result)
    else:
        print(f"claim={result['family']} outcome={result['outcome']} session={result['session_id']}")
    return 0


def cmd_claim_list(args: argparse.Namespace) -> int:
    claims = recorded_claims(command_root(args), args.session)
    if args.json:
        print_json({"claims": claims})
    else:
        print(format_claims(claims))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentdir")
    parser.add_argument("--version", action="version", version=f"agentdir {__version__}")
    parser.add_argument(
        "--upgrade",
        action="store_true",
        help="legacy shortcut for 'agentdir update'",
    )
    parser.add_argument("--upgrade-version", help="release tag to install instead of the latest release")
    parser.add_argument("--upgrade-repo", default="jstxn/agentdir", help="GitHub repo to install from")
    parser.add_argument(
        "--upgrade-install-skill",
        choices=("user", "project", "store", "none"),
        default="user",
        help="Codex skill target to use during re-adoption",
    )
    parser.add_argument("--upgrade-no-adopt", action="store_true", help="only reinstall, do not re-adopt repo")
    parser.add_argument("--upgrade-no-hooks", action="store_true", help="re-adopt without installing Git hooks")
    parser.add_argument("--upgrade-dry-run", action="store_true", help="show the upgrade plan without changing files")
    parser.add_argument("--upgrade-json", action="store_true", help="print upgrade result as JSON")
    sub = parser.add_subparsers(dest="command")

    update_command = sub.add_parser(
        "update",
        help="reinstall AgentDir from a release and re-adopt the current repo",
        description=(
            "Reinstall AgentDir from the latest GitHub Release, then re-adopt "
            "the current repository and run doctor."
        ),
    )
    update_command.add_argument(
        "--version",
        dest="upgrade_version",
        help="release tag to install instead of the latest release",
    )
    update_command.add_argument(
        "--repo",
        dest="upgrade_repo",
        default="jstxn/agentdir",
        help="GitHub repo to install from",
    )
    update_command.add_argument(
        "--install-skill",
        dest="upgrade_install_skill",
        choices=("user", "project", "store", "none"),
        default="user",
        help="Codex skill target to use during re-adoption",
    )
    update_command.add_argument(
        "--no-adopt",
        dest="upgrade_no_adopt",
        action="store_true",
        help="only reinstall; do not re-adopt the current repo",
    )
    update_command.add_argument(
        "--no-hooks",
        dest="upgrade_no_hooks",
        action="store_true",
        help="re-adopt without installing Git hooks",
    )
    update_command.add_argument(
        "--dry-run",
        dest="upgrade_dry_run",
        action="store_true",
        help="show the update plan without changing files",
    )
    update_command.add_argument(
        "--json",
        dest="upgrade_json",
        action="store_true",
        help="print the update result as JSON",
    )
    update_command.set_defaults(func=cmd_upgrade)

    init = sub.add_parser("init")
    init.add_argument("root", nargs="?")
    add_scope_args(init)
    init.set_defaults(func=cmd_init)

    root = sub.add_parser("root")
    add_scope_args(root)
    root.add_argument(
        "--require",
        dest="require_initialized",
        action="store_true",
        help="fail with exit 3 unless the resolved local or shared store is initialized",
    )
    root.add_argument("--json", action="store_true")
    root.set_defaults(func=cmd_root)

    status = sub.add_parser("status")
    add_scope_args(status)
    status.add_argument("--no-rebuild", action="store_true")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

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
    emit.add_argument("--json", action="store_true")
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
    actor_create.add_argument("--json", action="store_true")
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
    send.add_argument("--json", action="store_true")
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
    rebuild.add_argument("--json", action="store_true")
    rebuild.set_defaults(func=cmd_index_rebuild)
    update = index_sub.add_parser("update")
    add_scope_args(update)
    update.add_argument("--json", action="store_true")
    update.set_defaults(func=cmd_index_update)

    roots = sub.add_parser("roots")
    roots_sub = roots.add_subparsers(dest="roots_command", required=True)
    roots_register = roots_sub.add_parser("register")
    add_scope_args(roots_register)
    roots_register.add_argument("path")
    roots_register.add_argument("--name")
    roots_register.add_argument("--visibility", choices=VISIBILITY_CHOICES, default="private")
    roots_register.add_argument("--json", action="store_true")
    roots_register.set_defaults(func=cmd_roots_register)
    roots_list = roots_sub.add_parser("list")
    add_scope_args(roots_list)
    roots_list.add_argument("--json", action="store_true")
    roots_list.set_defaults(func=cmd_roots_list)
    roots_remove = roots_sub.add_parser("remove")
    add_scope_args(roots_remove)
    roots_remove.add_argument("identifier")
    roots_remove.add_argument("--json", action="store_true")
    roots_remove.set_defaults(func=cmd_roots_remove)
    roots_rebuild = roots_sub.add_parser("rebuild")
    add_scope_args(roots_rebuild)
    roots_rebuild.add_argument("--group")
    roots_rebuild.add_argument("--stale", action="store_true")
    roots_rebuild.add_argument("--json", action="store_true")
    roots_rebuild.set_defaults(func=cmd_roots_rebuild)
    roots_suggest = roots_sub.add_parser("suggest")
    add_scope_args(roots_suggest)
    roots_suggest.add_argument("--near")
    roots_suggest.add_argument("--json", action="store_true")
    roots_suggest.set_defaults(func=cmd_roots_suggest)
    roots_doctor = roots_sub.add_parser("doctor")
    add_scope_args(roots_doctor)
    roots_doctor.add_argument("--group")
    roots_doctor.add_argument("--json", action="store_true")
    roots_doctor.set_defaults(func=cmd_roots_doctor)
    roots_group = roots_sub.add_parser("group")
    roots_group_sub = roots_group.add_subparsers(dest="roots_group_command", required=True)
    roots_group_create = roots_group_sub.add_parser("create")
    add_scope_args(roots_group_create)
    roots_group_create.add_argument("name")
    roots_group_create.add_argument("--member", action="append", dest="members", required=True)
    roots_group_create.add_argument("--json", action="store_true")
    roots_group_create.set_defaults(func=cmd_roots_group_create)
    roots_group_list = roots_group_sub.add_parser("list")
    add_scope_args(roots_group_list)
    roots_group_list.add_argument("--json", action="store_true")
    roots_group_list.set_defaults(func=cmd_roots_group_list)
    roots_group_add = roots_group_sub.add_parser("add")
    add_scope_args(roots_group_add)
    roots_group_add.add_argument("name")
    roots_group_add.add_argument("root_id")
    roots_group_add.add_argument("--json", action="store_true")
    roots_group_add.set_defaults(func=cmd_roots_group_add)
    roots_group_remove = roots_group_sub.add_parser("remove")
    add_scope_args(roots_group_remove)
    roots_group_remove.add_argument("name")
    roots_group_remove.add_argument("root_id")
    roots_group_remove.add_argument("--json", action="store_true")
    roots_group_remove.set_defaults(func=cmd_roots_group_remove)

    archive = sub.add_parser("archive")
    add_scope_args(archive)
    archive.add_argument("--session", action="append", dest="sessions")
    archive.add_argument("--older-than-days", type=int)
    archive.add_argument("--keep-recent", type=int)
    archive.add_argument("--apply", action="store_true")
    archive.add_argument("--json", action="store_true")
    archive.set_defaults(func=cmd_archive)

    prune = sub.add_parser("prune")
    add_scope_args(prune)
    prune.add_argument("--session", action="append", dest="sessions")
    prune.add_argument("--older-than-days", type=int)
    prune.add_argument("--keep-recent", type=int)
    prune.add_argument("--include-live-sessions", action="store_true")
    prune.add_argument("--apply", action="store_true")
    prune.add_argument("--json", action="store_true")
    prune.set_defaults(func=cmd_prune)

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
    query.add_argument("--federated", action="store_true")
    query.add_argument("--retrieval", choices=RETRIEVAL_MODES, default=RETRIEVAL_AUTO)
    query.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
    query.add_argument("--since")
    query.add_argument("--until")
    query.add_argument("--limit", type=int, default=100)
    query.add_argument("--json", action="store_true")
    query.set_defaults(func=cmd_query)

    replay = sub.add_parser("replay")
    add_scope_args(replay)
    replay.add_argument("--session", required=True)
    replay.add_argument("--json", action="store_true")
    replay.set_defaults(func=cmd_replay)

    doctor = sub.add_parser("doctor")
    add_scope_args(doctor)
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    secrets = sub.add_parser("secrets")
    secrets_sub = secrets.add_subparsers(dest="secrets_command", required=True)
    secrets_scan = secrets_sub.add_parser("scan")
    add_scope_args(secrets_scan)
    secrets_scan.add_argument("--json", action="store_true")
    secrets_scan.set_defaults(func=cmd_secrets_scan)
    secrets_redact = secrets_sub.add_parser("redact")
    add_scope_args(secrets_redact)
    secrets_redact.add_argument("--apply", action="store_true")
    secrets_redact.add_argument("--json", action="store_true")
    secrets_redact.set_defaults(func=cmd_secrets_redact)

    run = sub.add_parser("run")
    add_scope_args(run)
    run.add_argument(
        "--session",
        help="session mode: auto (default), require, create, or an explicit session id",
    )
    run.add_argument("--name")
    run.add_argument("--cwd")
    run.add_argument("--max-capture-bytes", type=int, default=DEFAULT_MAX_CAPTURE_BYTES)
    run.add_argument("--no-redact", action="store_true")
    run.add_argument("--timeout", type=float, help="kill the command after this many seconds (exit 124)")
    run.add_argument("--json", action="store_true", help="print a JSON run summary after the command output")
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
    add_scope_args(hooks_uninstall)
    hooks_uninstall.add_argument("--hook", action="append", dest="hooks")
    hooks_uninstall.add_argument("--json", action="store_true")
    hooks_uninstall.set_defaults(func=cmd_hooks_uninstall)
    hooks_record = hooks_sub.add_parser("record")
    add_scope_args(hooks_record)
    hooks_record.add_argument("--hook", required=True)
    hooks_record.add_argument("--original-exit-code", type=int, required=True)
    hooks_record.add_argument("--stdin-file")
    hooks_record.add_argument("--json", action="store_true")
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
    generic_guidance = skills_install_sub.add_parser("generic")
    add_scope_args(generic_guidance)
    generic_guidance.add_argument("--target", choices=("project", "store"), default="store")
    generic_guidance.add_argument("--force", action="store_true")
    generic_guidance.add_argument("--json", action="store_true")
    generic_guidance.set_defaults(func=cmd_skills_install_generic)

    setup = sub.add_parser("setup")
    add_scope_args(setup)
    setup.add_argument("--no-hooks", action="store_true")
    setup.add_argument("--codex-skill", choices=("user", "project", "store", "none"), default="user")
    setup.add_argument("--install-generic", choices=("project", "store", "none"), default="project")
    setup.add_argument(
        "--install-integrations",
        choices=("all", "none", "project-files"),
        default="all",
        help=(
            "all adapts to rule generators; project-files selects tool files "
            "(generated headers still require --force)"
        ),
    )
    setup.add_argument("--integration-target", choices=("project", "store"), default="project")
    setup.add_argument("--gitignore", choices=GITIGNORE_CHOICES, default="ask")
    setup.add_argument("--dry-run", action="store_true")
    setup.add_argument("--force", action="store_true")
    setup.add_argument("--json", action="store_true")
    setup.set_defaults(func=cmd_setup)

    adopt = sub.add_parser("adopt")
    add_scope_args(adopt)
    adopt.add_argument("--install-skill", choices=("user", "project", "store", "none"), default="user")
    adopt.add_argument("--install-generic", choices=("project", "store", "none"), default="project")
    adopt.add_argument(
        "--install-integrations",
        choices=("all", "none", "project-files"),
        default="all",
        help=(
            "all adapts to rule generators; project-files selects tool files "
            "(generated headers still require --force)"
        ),
    )
    adopt.add_argument("--integration-target", choices=("project", "store"), default="project")
    adopt.add_argument("--gitignore", choices=GITIGNORE_CHOICES, default="ask")
    adopt.add_argument("--no-hooks", action="store_true")
    adopt.add_argument(
        "--if-needed",
        action="store_true",
        help="reuse an initialized local or linked-worktree store without refreshing setup files",
    )
    adopt.add_argument("--dry-run", action="store_true")
    adopt.add_argument("--force", action="store_true")
    adopt.add_argument("--json", action="store_true")
    adopt.set_defaults(func=cmd_adopt)

    unadopt = sub.add_parser("unadopt")
    add_scope_args(unadopt)
    unadopt.add_argument("--apply", action="store_true")
    unadopt.add_argument("--no-hooks", action="store_true")
    unadopt.add_argument("--json", action="store_true")
    unadopt.set_defaults(func=cmd_unadopt)

    integrations = sub.add_parser("integrations")
    integrations_sub = integrations.add_subparsers(dest="integrations_command", required=True)
    integrations_install = integrations_sub.add_parser("install")
    add_scope_args(integrations_install)
    integrations_install.add_argument("name", choices=(*INTEGRATION_NAMES, "all"))
    integrations_install.add_argument("--target", choices=("project", "store"), required=True)
    integrations_install.add_argument("--force", action="store_true")
    integrations_install.add_argument("--json", action="store_true")
    integrations_install.set_defaults(func=cmd_integrations_install)
    integrations_doctor = integrations_sub.add_parser("doctor")
    add_scope_args(integrations_doctor)
    integrations_doctor.add_argument("--target", choices=("project", "store"), default="project")
    integrations_doctor.add_argument("--json", action="store_true")
    integrations_doctor.set_defaults(func=cmd_integrations_doctor)

    work = sub.add_parser("work")
    work_sub = work.add_subparsers(dest="work_command", required=True)
    work_start = work_sub.add_parser("start")
    add_scope_args(work_start)
    work_start.add_argument("task")
    work_start.add_argument("--actor", default="agent")
    work_start.set_defaults(emit_context=True)
    work_start.add_argument("--emit-context", dest="emit_context", action="store_true", help=argparse.SUPPRESS)
    work_start.add_argument(
        "--no-context",
        dest="emit_context",
        action="store_false",
        help="skip retrieval and record a zero-source context opt-out marker",
    )
    work_start.add_argument("--federated", action="store_true")
    work_start.add_argument("--group")
    work_start.add_argument("--memory-limit", type=int, default=8)
    work_start.add_argument("--evidence-limit", type=int, default=20)
    work_start.add_argument("--recent-limit", type=int, default=5)
    work_start.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
    work_start.add_argument("--retrieval", choices=RETRIEVAL_MODES, default=RETRIEVAL_AUTO)
    work_start.add_argument("--json", action="store_true")
    work_start.set_defaults(func=cmd_work_start)
    work_context = work_sub.add_parser("context")
    add_scope_args(work_context)
    disposition = work_context.add_mutually_exclusive_group(required=True)
    disposition.add_argument("--show", action="store_true", help="re-open the persisted numbered briefing")
    disposition.add_argument(
        "--expand",
        dest="expand_source",
        metavar="SOURCE",
        help="read a bounded page of one displayed source without deciding the pack",
    )
    disposition.add_argument("--use", action="append", dest="sources")
    disposition.add_argument("--none-relevant", action="store_true")
    disposition.add_argument("--skip", action="store_true")
    work_context.add_argument("--reason")
    work_context.add_argument("--purpose", choices=CONSUMPTION_PURPOSES, default="plan")
    work_context.add_argument("--actor", default="agent")
    work_context.add_argument("--page", type=int, help="1-based page for --expand")
    context_target = work_context.add_mutually_exclusive_group()
    context_target.add_argument("--session")
    context_target.add_argument("--pack")
    work_context.add_argument("--json", action="store_true")
    work_context.set_defaults(func=cmd_work_context)
    work_finish = work_sub.add_parser("finish")
    add_scope_args(work_finish)
    work_finish.add_argument("--session")
    work_finish.add_argument("--actor", default="agent")
    work_finish.add_argument("--keep-session", action="store_true")
    work_finish.add_argument("--no-doctor", action="store_true")
    work_finish.add_argument("--claims")
    work_finish.add_argument("--json", action="store_true")
    work_finish.set_defaults(func=cmd_work_finish)

    report = sub.add_parser("report")
    report_sub = report.add_subparsers(dest="report_command", required=True)
    report_final = report_sub.add_parser("final")
    add_scope_args(report_final)
    report_final.add_argument("--session")
    report_final.add_argument("--format", choices=("md", "json"), default="md")
    report_final.add_argument("--no-doctor", action="store_true")
    report_final.add_argument("--claims")
    report_final.set_defaults(func=cmd_report_final)

    summarize = sub.add_parser("summarize")
    add_scope_args(summarize)
    summarize.add_argument("--session")
    summarize.add_argument("--json", action="store_true")
    summarize.set_defaults(func=cmd_summarize)

    evidence = sub.add_parser("evidence")
    add_scope_args(evidence)
    evidence.add_argument("--session")
    evidence.add_argument("--brief", action="store_true")
    evidence.add_argument("--family", choices=EVIDENCE_FAMILIES)
    evidence.add_argument("--failed", action="store_true")
    evidence.add_argument("--json", action="store_true")
    evidence.set_defaults(func=cmd_evidence)

    timeline = sub.add_parser("timeline")
    add_scope_args(timeline)
    timeline.add_argument("--session")
    timeline.add_argument("--limit", type=int, default=100)
    timeline.add_argument("--json", action="store_true")
    timeline.set_defaults(func=cmd_timeline)

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
    memory_search.add_argument("--retrieval", choices=RETRIEVAL_MODES, default=RETRIEVAL_AUTO)
    memory_search.add_argument("--federated", action="store_true")
    memory_search.add_argument("--group")
    memory_search.add_argument("--no-rebuild", action="store_true")
    memory_search.add_argument("--json", action="store_true")
    memory_search.set_defaults(func=cmd_memory_search)
    memory_explain = memory_sub.add_parser("explain")
    add_scope_args(memory_explain)
    memory_explain.add_argument("query")
    memory_explain.add_argument("--source")
    memory_explain.add_argument("--min-score", type=float, default=0.0)
    memory_explain.add_argument("--retrieval", choices=RETRIEVAL_MODES, default=RETRIEVAL_AUTO)
    memory_explain.add_argument("--no-rebuild", action="store_true")
    memory_explain.add_argument("--json", action="store_true")
    memory_explain.set_defaults(func=cmd_memory_explain)
    memory_stats_parser = memory_sub.add_parser("stats")
    add_scope_args(memory_stats_parser)
    memory_stats_parser.add_argument("--no-rebuild", action="store_true")
    memory_stats_parser.add_argument("--json", action="store_true")
    memory_stats_parser.set_defaults(func=cmd_memory_stats)
    memory_backend = memory_sub.add_parser("backend")
    memory_backend_sub = memory_backend.add_subparsers(dest="memory_backend_command", required=True)
    memory_backend_list = memory_backend_sub.add_parser("list")
    add_scope_args(memory_backend_list)
    memory_backend_list.add_argument("--no-rebuild", action="store_true")
    memory_backend_list.add_argument("--json", action="store_true")
    memory_backend_list.set_defaults(func=cmd_memory_backend_list)
    memory_backend_configure = memory_backend_sub.add_parser("configure")
    add_scope_args(memory_backend_configure)
    memory_backend_configure.add_argument("backend", choices=("sqlite-vec", "none"))
    memory_backend_configure.add_argument("--json", action="store_true")
    memory_backend_configure.set_defaults(func=cmd_memory_backend_configure)
    memory_embeddings = memory_sub.add_parser("embeddings")
    memory_embeddings_sub = memory_embeddings.add_subparsers(dest="memory_embeddings_command", required=True)
    memory_embeddings_configure = memory_embeddings_sub.add_parser("configure")
    add_scope_args(memory_embeddings_configure)
    memory_embeddings_configure.add_argument("provider", choices=("fastembed", "none"))
    memory_embeddings_configure.add_argument("--model")
    memory_embeddings_configure.add_argument("--json", action="store_true")
    memory_embeddings_configure.set_defaults(func=cmd_memory_embeddings_configure)
    memory_team = memory_sub.add_parser("team")
    memory_team_sub = memory_team.add_subparsers(dest="memory_team_command", required=True)
    memory_team_configure = memory_team_sub.add_parser("configure")
    add_scope_args(memory_team_configure)
    memory_team_configure.add_argument("backend", choices=("qdrant", "lancedb", "none"))
    memory_team_configure.add_argument("--json", action="store_true")
    memory_team_configure.set_defaults(func=cmd_memory_team_configure)
    memory_daemon = memory_sub.add_parser("daemon")
    memory_daemon_sub = memory_daemon.add_subparsers(dest="memory_daemon_command", required=True)
    memory_daemon_start = memory_daemon_sub.add_parser("start")
    add_scope_args(memory_daemon_start)
    memory_daemon_start.add_argument("--interval", type=float, default=2.0)
    memory_daemon_start.add_argument("--group")
    memory_daemon_start.add_argument("--force", action="store_true")
    memory_daemon_start.add_argument("--json", action="store_true")
    memory_daemon_start.set_defaults(func=cmd_memory_daemon_start)
    memory_daemon_status_parser = memory_daemon_sub.add_parser("status")
    add_scope_args(memory_daemon_status_parser)
    memory_daemon_status_parser.add_argument("--json", action="store_true")
    memory_daemon_status_parser.set_defaults(func=cmd_memory_daemon_status)
    memory_daemon_stop = memory_daemon_sub.add_parser("stop")
    add_scope_args(memory_daemon_stop)
    memory_daemon_stop.add_argument("--timeout", type=float, default=5.0)
    memory_daemon_stop.add_argument("--json", action="store_true")
    memory_daemon_stop.set_defaults(func=cmd_memory_daemon_stop)
    memory_daemon_run = memory_daemon_sub.add_parser("run")
    add_scope_args(memory_daemon_run)
    memory_daemon_run.add_argument("--interval", type=float, default=2.0)
    memory_daemon_run.add_argument("--group")
    memory_daemon_run.add_argument("--once", action="store_true")
    memory_daemon_run.add_argument("--json", action="store_true")
    memory_daemon_run.set_defaults(func=cmd_memory_daemon_run)

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
    context_build.add_argument("--retrieval", choices=RETRIEVAL_MODES, default=RETRIEVAL_AUTO)
    context_build.add_argument("--federated", action="store_true")
    context_build.add_argument("--group")
    context_build.add_argument("--output")
    context_build.add_argument("--emit", action="store_true")
    context_build.add_argument("--json", action="store_true")
    context_build.set_defaults(func=cmd_context_build)
    context_consume = context_sub.add_parser("consume")
    add_scope_args(context_consume)
    context_consume.add_argument("--pack", required=True)
    context_consume.add_argument("--source", action="append", dest="sources", required=True)
    context_consume.add_argument("--purpose", choices=CONSUMPTION_PURPOSES, required=True)
    context_consume.add_argument("--session")
    context_consume.add_argument("--actor", default="agent")
    context_consume.add_argument("--json", action="store_true")
    context_consume.set_defaults(func=cmd_context_consume)
    context_cite = context_sub.add_parser("cite")
    add_scope_args(context_cite)
    context_cite.add_argument("--pack", required=True)
    context_cite.add_argument("--source", action="append", dest="sources")
    context_cite.add_argument("--format", choices=("md", "json"), default="md")
    context_cite.add_argument("--session")
    context_cite.add_argument("--actor", default="agent")
    context_cite.add_argument("--json", action="store_true")
    context_cite.set_defaults(func=cmd_context_cite)

    audit = sub.add_parser("audit")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    audit_context = audit_sub.add_parser("context")
    add_scope_args(audit_context)
    audit_context.add_argument("--pack", required=True)
    audit_context.add_argument("--json", action="store_true")
    audit_context.set_defaults(func=cmd_audit_context)
    audit_session_parser = audit_sub.add_parser("session")
    add_scope_args(audit_session_parser)
    audit_session_parser.add_argument("--session")
    audit_session_parser.add_argument("--strict", action="store_true")
    audit_session_parser.add_argument("--json", action="store_true")
    audit_session_parser.set_defaults(func=cmd_audit_session)
    audit_claims_parser = audit_sub.add_parser(
        "claims",
        help="check claims against evidence; without --text, checks recorded structured claims",
    )
    add_scope_args(audit_claims_parser)
    audit_claims_parser.add_argument(
        "--text",
        help="audit prose instead of recorded claims ('-' reads stdin)",
    )
    audit_claims_parser.add_argument("--session")
    audit_claims_parser.add_argument("--strict", action="store_true")
    audit_claims_parser.add_argument("--json", action="store_true")
    audit_claims_parser.set_defaults(func=cmd_audit_claims)

    claim_parser = sub.add_parser(
        "claim",
        help="record a structured verification claim checked against evidence",
    )
    claim_sub = claim_parser.add_subparsers(dest="claim_command", required=True)

    claim_list_parser = claim_sub.add_parser("list", help="show the latest claim per family")
    add_scope_args(claim_list_parser)
    claim_list_parser.add_argument("--session")
    claim_list_parser.add_argument("--json", action="store_true")
    claim_list_parser.set_defaults(func=cmd_claim_list)

    for family in CLAIM_FAMILIES:
        family_parser = claim_sub.add_parser(family, help=f"record a {family} claim")
        add_scope_args(family_parser)
        outcome_group = family_parser.add_mutually_exclusive_group(required=True)
        outcome_group.add_argument("--passed", action="store_true", help=f"{family} check passed")
        outcome_group.add_argument("--failed", action="store_true", help=f"{family} check failed")
        outcome_group.add_argument(
            "--retract",
            action="store_true",
            help=f"withdraw the recorded {family} claim",
        )
        family_parser.add_argument("--note")
        family_parser.add_argument("--session")
        family_parser.add_argument("--actor", default="agent")
        family_parser.add_argument("--json", action="store_true")
        family_parser.set_defaults(func=cmd_claim, family=family)

    return parser


def add_scope_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", dest="root_option")
    parser.add_argument("--scope", choices=("project", "user", "global", "machine"))
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress stdout; use the exit code (errors and --json still print)",
    )


def command_invocation(argv: list[str] | None = None) -> tuple[str, ...]:
    """Return the executable prefix that actually entered this CLI process."""
    if argv is not None:
        return ("agentdir",)
    original = list(getattr(sys, "orig_argv", []) or [])
    for index in range(len(original) - 1):
        if original[index] == "-m" and original[index + 1] == "agentdir":
            return tuple(original[: index + 2])
    invoked = Path(sys.argv[0])
    if invoked.name == "agentdir":
        return (str(invoked),)
    return ("agentdir",)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.invocation = command_invocation(argv)
    wants_json = bool(getattr(args, "json", False) or getattr(args, "upgrade_json", False))
    if getattr(args, "quiet", False) and not wants_json:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    try:
        if args.upgrade:
            return int(cmd_upgrade(args))
        if not hasattr(args, "func"):
            parser.print_help(sys.stderr)
            return 2
        return int(args.func(args))
    except AgentDirError as exc:
        exit_code = getattr(exc, "exit_code", 2)
        if wants_json:
            print_json(
                {
                    "success": False,
                    "exit_code": exit_code,
                    "error": str(exc),
                    "error_code": type(exc).__name__,
                    "data": None,
                }
            )
        print(f"agentdir: {exc}", file=sys.stderr)
        return int(exit_code)
