from __future__ import annotations

import json
import os
import shlex
import sqlite3
import subprocess
import sys
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from agentdir.context_expansion import (
    HEADER_VIEW_ID,
    HEADER_VIEW_SOURCE_SHA256,
    _expansion_events,
    _receipt_body,
    _resolve_session_summary,
    _validate_receipt,
    _view_id,
    _view_payload,
    expand_context_source,
)


def find_project_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError("could not locate project root")


PROJECT_ROOT = find_project_root()
SRC_ROOT = PROJECT_ROOT / "src"
CLI_INVOCATION = shlex.join((sys.executable, "-m", "agentdir"))


def scoped_work_context_invocation(root: Path) -> str:
    return f"{CLI_INVOCATION} work context --root {shlex.quote(str(root.resolve()))}"


def run_cli(
    *args: str,
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
    expected_returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_ROOT)
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(
        [sys.executable, "-m", "agentdir", *args],
        cwd=cwd or PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == expected_returncode, (
        f"expected exit code {expected_returncode}, got {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return result


def init_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init"], cwd=path, check=True, text=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "agentdir@example.test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "AgentDir Test"], cwd=path, check=True)
    return path


def query_rows(root: Path, event_type: str | None = None) -> list[dict[str, object]]:
    db = root / "indexes" / "agentdir.sqlite3"
    sql = "select * from messages"
    params: tuple[object, ...] = ()
    if event_type:
        sql += " where event_type = ?"
        params = (event_type,)
    sql += " order by id"
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def displayed_source_ref(payload: dict[str, object], *, event_type: str = "agent.message") -> str:
    manifest = payload["context_pack"]
    briefing = payload["context_briefing"]
    assert isinstance(manifest, dict)
    assert isinstance(briefing, dict)
    source_by_id = {
        source["source_id"]: source
        for source in manifest["sources"]
        if isinstance(source, dict)
    }
    for index, source_id in enumerate(briefing["source_ids"], start=1):
        if source_by_id[source_id].get("event_type") == event_type:
            return str(index)
    raise AssertionError(f"no displayed {event_type} source in context pack")


def test_cli_version_reports_package_version() -> None:
    result = run_cli("--version")

    assert result.stdout.strip() == "agentdir 0.8.0"


def test_session_current_and_sessionless_emit_use_project_store(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    body = tmp_path / "body.txt"
    body.write_text("agent-first event", encoding="utf-8")

    started = run_cli("session", "start", "--id", "agent-session", "--title", "Agent First", cwd=repo)
    current = run_cli("session", "current", "--json", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(body), cwd=repo)
    run_cli("index", "rebuild", cwd=repo)

    root = repo / ".agentdir"
    current_payload = json.loads(current.stdout)
    rows = query_rows(root)
    assert started.stdout.strip() == "agent-session"
    assert current_payload["session_id"] == "agent-session"
    assert {row["event_type"] for row in rows} == {"session.started", "agent.message"}
    assert all(row["session_id"] == "agent-session" for row in rows)

    run_cli("session", "end", "--summary", str(body), cwd=repo)
    run_cli("session", "current", cwd=repo, expected_returncode=2)


def test_session_ensure_creates_and_reuses_active_session(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")

    created = run_cli("session", "ensure", "--title", "Hands Off Agent Work", "--json", cwd=repo)
    reused = run_cli("session", "ensure", "--title", "Ignored Because Active", "--json", cwd=repo)
    current = run_cli("session", "current", "--json", cwd=repo)
    run_cli("index", "rebuild", cwd=repo)

    created_payload = json.loads(created.stdout)
    reused_payload = json.loads(reused.stdout)
    current_payload = json.loads(current.stdout)
    rows = query_rows(repo / ".agentdir", "session.started")

    assert created_payload["session_id"] == reused_payload["session_id"]
    assert current_payload["session_id"] == created_payload["session_id"]
    assert created_payload["title"] == "Hands Off Agent Work"
    assert len(rows) == 1


def test_explicit_session_ids_cannot_be_reused_after_end(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("session", "start", "--id", "immutable-session", cwd=repo)
    run_cli("session", "end", cwd=repo)

    rejected = run_cli(
        "session",
        "start",
        "--id",
        "immutable-session",
        "--json",
        cwd=repo,
        expected_returncode=3,
    )

    assert "already exists and cannot be reused" in rejected.stderr
    run_cli("session", "current", cwd=repo, expected_returncode=2)


def test_session_id_is_reserved_when_started_event_is_suppressed(tmp_path: Path) -> None:
    from agentdir.sessions import read_current_session, start_session
    from agentdir.store import AgentDirStateError

    repo = init_repo(tmp_path / "repo")
    store = repo / ".agentdir"
    first = start_session(
        store,
        session_id="reserved-session",
        title="first owner",
        cwd=repo,
        emit_started=False,
    )

    try:
        start_session(
            store,
            session_id="reserved-session",
            title="second owner",
            cwd=repo,
            emit_started=False,
        )
    except AgentDirStateError as exc:
        assert "already exists and cannot be reused" in str(exc)
    else:
        raise AssertionError("an eventless session id was reused")

    current = read_current_session(store)
    assert current is not None
    assert current.session_id == first.session_id
    assert current.title == "first owner"
    assert (store / "sessions" / first.session_id).is_dir()


def test_run_wraps_tool_call_result_exit_code_and_redacted_output(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("session", "start", "--id", "run-session", cwd=repo)

    command = (
        "import sys; "
        "print('hello from wrapped command'); "
        "print('API_KEY=abcdef1234567890', file=sys.stderr)"
    )
    result = run_cli(
        "run",
        "--name",
        "python",
        "--",
        sys.executable,
        "-c",
        command,
        cwd=repo,
    )
    run_cli("index", "rebuild", cwd=repo)

    rows = query_rows(repo / ".agentdir")
    result_rows = [row for row in rows if row["event_type"] == "tool.result"]
    assert "hello from wrapped command" in result.stdout
    assert len(result_rows) == 1
    assert result_rows[0]["tool"] == "python"
    assert result_rows[0]["tool_exit_code"] == 0
    assert "<redacted:key-value-secret>" in str(result_rows[0]["body_text"])


def test_run_returns_wrapped_command_exit_code(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")

    run_cli(
        "run",
        "--session",
        "failed-run",
        "--name",
        "python",
        "--",
        sys.executable,
        "-c",
        "import sys; sys.exit(7)",
        cwd=repo,
        expected_returncode=7,
    )
    run_cli("index", "rebuild", cwd=repo)

    result_rows = query_rows(repo / ".agentdir", "tool.result")
    assert result_rows[0]["session_id"] == "failed-run"
    assert result_rows[0]["tool_exit_code"] == 7


def test_upgrade_dry_run_plans_install_and_adoption(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")

    result = run_cli(
        "--upgrade",
        "--upgrade-dry-run",
        "--upgrade-version",
        "v9.9.9",
        "--upgrade-install-skill",
        "none",
        cwd=repo,
    )

    assert "target_version=v9.9.9" in result.stdout
    assert "install=install jstxn/agentdir@v9.9.9" in result.stdout
    assert "adopt=agentdir adopt --install-skill none" in result.stdout


def test_update_subcommand_matches_legacy_upgrade_json(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")

    legacy = run_cli(
        "--upgrade",
        "--upgrade-dry-run",
        "--upgrade-version",
        "v9.9.9",
        "--upgrade-install-skill",
        "store",
        "--upgrade-no-hooks",
        "--upgrade-json",
        cwd=repo,
    )
    update = run_cli(
        "update",
        "--dry-run",
        "--version",
        "v9.9.9",
        "--install-skill",
        "store",
        "--no-hooks",
        "--json",
        cwd=repo,
    )

    assert json.loads(update.stdout) == json.loads(legacy.stdout)
    payload = json.loads(update.stdout)
    assert payload["adopt"]["command"] == "agentdir adopt --install-skill store --no-hooks"


def test_update_subcommand_can_skip_re_adoption(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")

    result = run_cli(
        "update",
        "--dry-run",
        "--version",
        "v9.9.9",
        "--no-adopt",
        "--json",
        cwd=repo,
    )

    payload = json.loads(result.stdout)
    assert payload["adopt"]["planned"] is False
    assert payload["doctor"]["planned"] is False


def test_hooks_install_record_and_uninstall_preserve_original_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    hooks_dir = repo / ".git" / "hooks"
    original = hooks_dir / "pre-commit"
    original.write_text("#!/bin/sh\necho original-ran > original-marker\nexit 4\n", encoding="utf-8")
    original.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "agentdir"
    shim.write_text(
        f"#!/bin/sh\nPYTHONPATH='{SRC_ROOT}' exec '{sys.executable}' -m agentdir \"$@\"\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "PYTHONPATH": str(SRC_ROOT)}

    run_cli("hooks", "install", "--hook", "pre-commit", cwd=repo)
    status = run_cli("hooks", "status", "--hook", "pre-commit", "--json", cwd=repo)
    hook_result = subprocess.run(
        [str(hooks_dir / "pre-commit")],
        cwd=repo,
        env={**os.environ, **env},
        text=True,
        capture_output=True,
    )
    run_cli("index", "rebuild", cwd=repo)
    rows = query_rows(repo / ".agentdir", "git.hook.pre-commit")

    assert json.loads(status.stdout)[0]["managed"] is True
    assert hook_result.returncode == 4
    assert (repo / "original-marker").read_text(encoding="utf-8").strip() == "original-ran"
    assert rows[0]["tool_exit_code"] == 4

    run_cli("hooks", "uninstall", "--hook", "pre-commit", cwd=repo)
    restored = (hooks_dir / "pre-commit").read_text(encoding="utf-8")
    assert "AgentDir managed hook" not in restored
    assert "original-ran" in restored


def test_hook_record_without_active_session_auto_closes_background_session(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")

    run_cli("hooks", "record", "--hook", "pre-commit", "--original-exit-code", "0", cwd=repo)
    run_cli("session", "current", cwd=repo, expected_returncode=2)
    run_cli("index", "rebuild", cwd=repo)

    rows = query_rows(repo / ".agentdir")
    event_types = [row["event_type"] for row in rows]

    assert event_types == ["session.started", "git.hook.pre-commit", "session.ended"]
    assert all(row["session_id"] == rows[0]["session_id"] for row in rows)


def test_hook_record_uses_existing_active_session_without_ending_it(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    started = run_cli("work", "start", "active hook session", "--json", cwd=repo)
    session_id = json.loads(started.stdout)["session"]["session_id"]

    run_cli("hooks", "record", "--hook", "pre-commit", "--original-exit-code", "0", cwd=repo)
    current = run_cli("session", "current", "--json", cwd=repo)
    run_cli("index", "rebuild", cwd=repo)

    rows = query_rows(repo / ".agentdir", "git.hook.pre-commit")

    assert json.loads(current.stdout)["session_id"] == session_id
    assert rows[0]["session_id"] == session_id


def test_setup_installs_hooks_and_user_codex_skill(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    home = tmp_path / "home"
    home.mkdir()

    result = run_cli(
        "setup",
        "--codex-skill",
        "user",
        "--json",
        cwd=repo,
        env_extra={"HOME": str(home)},
    )
    payload = json.loads(result.stdout)
    skill_path = home / ".codex" / "skills" / "agentdir" / "SKILL.md"

    assert payload["root"] == str(repo / ".agentdir")
    assert skill_path.is_file()
    skill_text = skill_path.read_text(encoding="utf-8")
    assert "The user should not have to run AgentDir commands during normal coding work." in skill_text
    assert "agentdir root --require --quiet" in skill_text
    assert "agentdir adopt --if-needed --gitignore user" in skill_text
    assert "do not decide whether agentdir is set up" in skill_text.lower()
    assert "`.agentdir`" in skill_text
    assert 'agentdir work start "<short task>"' in skill_text
    assert 'agentdir work context --use <number> --reason "<how it helps>"' in skill_text
    assert 'agentdir work context --none-relevant --reason "<why>"' in skill_text
    assert "agentdir status" in skill_text
    assert "agentdir run -- <command>" in skill_text
    assert "agentdir context consume --pack <pack-id>" in skill_text
    assert "agentdir roots suggest" in skill_text
    assert "agentdir memory search --group <name>" in skill_text
    assert "agentdir work finish" in skill_text
    assert "Do not wrap routine exploration commands" in skill_text
    assert (repo / ".git" / "hooks" / "pre-commit").is_file()


def test_codex_skill_install_updates_existing_agentdir_skill(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    home = tmp_path / "home"
    skill_path = home / ".codex" / "skills" / "agentdir" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "---\nname: agentdir\n---\n\n# AgentDir\n\nold generated guidance\n",
        encoding="utf-8",
    )

    run_cli(
        "skills",
        "install",
        "codex",
        "--target",
        "user",
        cwd=repo,
        env_extra={"HOME": str(home)},
    )

    skill_text = skill_path.read_text(encoding="utf-8")
    backup_text = skill_path.with_suffix(".md.bak").read_text(encoding="utf-8")
    assert "old generated guidance" not in skill_text
    assert "<!-- agentdir-managed-skill -->" in skill_text
    assert "old generated guidance" in backup_text


def test_adopt_installs_skill_runs_doctor_and_reports_next_step(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    home = tmp_path / "home"
    home.mkdir()

    result = run_cli(
        "adopt",
        "--install-skill",
        "user",
        "--json",
        cwd=repo,
        env_extra={"HOME": str(home)},
    )
    payload = json.loads(result.stdout)

    assert payload["root"] == str(repo / ".agentdir")
    assert payload["doctor"]["ok"] is True
    assert payload["gitignore"]["target"] == "none"
    assert payload["gitignore"]["reason"] == "selected_none"
    assert payload["next"] == 'agentdir work start "<task>"'
    assert (home / ".codex" / "skills" / "agentdir" / "SKILL.md").is_file()
    assert (repo / ".git" / "hooks" / "pre-commit").is_file()
    assert (repo / "AGENTS.md").is_file()
    assert (repo / "CLAUDE.md").is_file()
    assert (repo / ".github" / "copilot-instructions.md").is_file()
    assert (repo / ".cursor" / "rules" / "agentdir.mdc").is_file()
    assert (repo / ".windsurf" / "rules" / "agentdir.md").is_file()
    doctor = json.loads(run_cli("integrations", "doctor", "--json", cwd=repo, env_extra={"HOME": str(home)}).stdout)
    codex_check = next(check for check in doctor["checks"] if check["name"] == "codex")
    assert codex_check["state"] == "installed"
    assert codex_check["effective_target"] == "user"


def test_adopt_preflights_hooks_before_creating_store_or_guidance(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    blocked_hooks = tmp_path / "blocked-hooks"
    blocked_hooks.write_text("not a directory\n", encoding="utf-8")
    subprocess.run(
        ["git", "config", "core.hooksPath", str(blocked_hooks)],
        cwd=repo,
        check=True,
    )

    result = run_cli(
        "adopt",
        "--install-skill",
        "store",
        "--gitignore",
        "project",
        "--json",
        cwd=repo,
        expected_returncode=2,
    )

    assert "hook" in result.stderr.lower()
    assert not (repo / ".agentdir").exists()
    assert not (repo / ".gitignore").exists()
    assert not (repo / "AGENTS.md").exists()
    assert not (repo / "CLAUDE.md").exists()
    assert not (repo / ".github").exists()
    assert not (repo / ".cursor").exists()
    assert not (repo / ".windsurf").exists()

    recovered = json.loads(
        run_cli(
            "adopt",
            "--install-skill",
            "store",
            "--gitignore",
            "project",
            "--no-hooks",
            "--json",
            cwd=repo,
        ).stdout
    )

    assert recovered["doctor"]["ok"] is True
    assert (repo / ".agentdir" / "VERSION").is_file()
    assert (repo / "AGENTS.md").is_file()
    assert (repo / ".gitignore").read_text(encoding="utf-8").splitlines() == [".agentdir/"]
    assert blocked_hooks.read_text(encoding="utf-8") == "not a directory\n"


def test_adopt_can_add_agentdir_to_project_gitignore(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    home = tmp_path / "home"
    home.mkdir()

    result = run_cli(
        "adopt",
        "--install-skill",
        "none",
        "--install-generic",
        "none",
        "--install-integrations",
        "none",
        "--no-hooks",
        "--gitignore",
        "project",
        "--json",
        cwd=repo,
        env_extra={"HOME": str(home)},
    )
    rerun = run_cli(
        "adopt",
        "--install-skill",
        "none",
        "--install-generic",
        "none",
        "--install-integrations",
        "none",
        "--no-hooks",
        "--gitignore",
        "project",
        "--json",
        cwd=repo,
        env_extra={"HOME": str(home)},
    )
    payload = json.loads(result.stdout)
    rerun_payload = json.loads(rerun.stdout)

    assert payload["gitignore"]["target"] == "project"
    assert payload["gitignore"]["action"] == "create"
    assert payload["gitignore"]["changed"] is True
    assert rerun_payload["gitignore"]["changed"] is False
    assert (repo / ".gitignore").read_text(encoding="utf-8").splitlines() == [".agentdir/"]


def test_adopt_can_add_agentdir_to_user_gitignore(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    home = tmp_path / "home"
    xdg_config = tmp_path / "xdg"
    home.mkdir()

    result = run_cli(
        "adopt",
        "--install-skill",
        "none",
        "--install-generic",
        "none",
        "--install-integrations",
        "none",
        "--no-hooks",
        "--gitignore",
        "user",
        "--json",
        cwd=repo,
        env_extra={"HOME": str(home), "XDG_CONFIG_HOME": str(xdg_config)},
    )
    payload = json.loads(result.stdout)
    ignore_path = xdg_config / "git" / "ignore"

    assert payload["gitignore"]["target"] == "user"
    assert payload["gitignore"]["path"] == str(ignore_path.resolve())
    assert payload["gitignore"]["changed"] is True
    assert ignore_path.read_text(encoding="utf-8").splitlines() == [".agentdir/"]


def test_adopt_dry_run_does_not_write_and_unadopt_preserves_evidence(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    home = tmp_path / "home"
    home.mkdir()
    original_hook = repo / ".git" / "hooks" / "pre-commit"
    original_hook.write_text("#!/bin/sh\necho original\n", encoding="utf-8")
    original_hook.chmod(0o755)
    agents = repo / "AGENTS.md"
    agents.write_text("# Local Agent Notes\n\nKeep this note.\n", encoding="utf-8")

    dry_run = run_cli("adopt", "--dry-run", "--json", cwd=repo, env_extra={"HOME": str(home)})
    setup_dry_run = run_cli("setup", "--dry-run", "--json", cwd=repo, env_extra={"HOME": str(home)})
    dry_payload = json.loads(dry_run.stdout)
    setup_dry_payload = json.loads(setup_dry_run.stdout)

    assert dry_payload["dry_run"] is True
    assert setup_dry_payload["dry_run"] is True
    assert dry_payload["gitignore"]["action"] == "prompt"
    assert setup_dry_payload["gitignore"]["action"] == "prompt"
    assert dry_payload["would_create_root"] is True
    assert setup_dry_payload["would_create_root"] is True
    assert not (repo / ".agentdir").exists()
    assert "agentdir-managed-generic:start" not in agents.read_text(encoding="utf-8")

    adopted = run_cli("adopt", "--json", cwd=repo, env_extra={"HOME": str(home)})
    adopt_payload = json.loads(adopted.stdout)
    run_cli("work", "start", "unadopt keeps evidence", cwd=repo, env_extra={"HOME": str(home)})
    plan = run_cli("unadopt", "--json", cwd=repo, env_extra={"HOME": str(home)})
    plan_payload = json.loads(plan.stdout)
    assert "agentdir-managed-generic:start" in agents.read_text(encoding="utf-8")
    applied = run_cli("unadopt", "--apply", "--json", cwd=repo, env_extra={"HOME": str(home)})
    applied_payload = json.loads(applied.stdout)
    agents_text = agents.read_text(encoding="utf-8")
    hook_text = original_hook.read_text(encoding="utf-8")

    assert adopt_payload["integrations"]
    assert plan_payload["applied"] is False
    assert applied_payload["applied"] is True
    assert (repo / ".agentdir").is_dir()
    assert (repo / ".agentdir" / "sessions").is_dir()
    assert "Keep this note." in agents_text
    assert "agentdir-managed-generic:start" not in agents_text
    assert "AgentDir managed hook" not in hook_text
    assert "echo original" in hook_text
    assert not (repo / "CLAUDE.md").exists()
    assert not (repo / ".github" / "copilot-instructions.md").exists()
    assert not (repo / ".cursor" / "rules" / "agentdir.mdc").exists()
    assert not (repo / ".windsurf" / "rules" / "agentdir.md").exists()


def test_integrations_install_all_project_preserves_unmanaged_content(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    agents = repo / "AGENTS.md"
    claude = repo / "CLAUDE.md"
    agents.write_text("# Existing Agents\n\nKeep AGENTS content.\n", encoding="utf-8")
    claude.write_text("# Existing Claude\n\nKeep CLAUDE content.\n", encoding="utf-8")

    installed = run_cli("integrations", "install", "all", "--target", "project", "--json", cwd=repo)
    rerun = run_cli("integrations", "install", "all", "--target", "project", "--json", cwd=repo)
    doctor = run_cli("integrations", "doctor", "--json", cwd=repo)

    installed_payload = json.loads(installed.stdout)
    rerun_payload = json.loads(rerun.stdout)
    doctor_payload = json.loads(doctor.stdout)
    agents_text = agents.read_text(encoding="utf-8")
    claude_text = claude.read_text(encoding="utf-8")

    assert {item["name"] for item in installed_payload} == {"generic", "codex", "claude", "copilot", "cursor", "windsurf"}
    assert {item["name"] for item in rerun_payload} == {"generic", "codex", "claude", "copilot", "cursor", "windsurf"}
    assert doctor_payload["ok"] is True
    assert "Keep AGENTS content." in agents_text
    assert "Keep CLAUDE content." in claude_text
    assert agents_text.count("agentdir-managed-generic:start") == 1
    assert claude_text.count("agentdir-managed-claude:start") == 1
    assert (repo / ".agents" / "skills" / "agentdir" / "SKILL.md").is_file()
    assert (repo / ".github" / "copilot-instructions.md").is_file()
    assert (repo / ".cursor" / "rules" / "agentdir.mdc").is_file()
    assert (repo / ".windsurf" / "rules" / "agentdir.md").is_file()


def test_integrations_project_rerun_is_byte_stable_for_managed_only_files(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")

    run_cli("integrations", "install", "all", "--target", "project", "--json", cwd=repo)
    tracked = [
        repo / "AGENTS.md",
        repo / "CLAUDE.md",
        repo / ".github" / "copilot-instructions.md",
    ]
    before = {path: path.read_text(encoding="utf-8") for path in tracked}
    rerun = run_cli("integrations", "install", "all", "--target", "project", "--json", cwd=repo)
    after = {path: path.read_text(encoding="utf-8") for path in tracked}

    assert before == after
    assert all(not text.startswith("\n") for text in after.values())
    assert all(item["updated"] is False for item in json.loads(rerun.stdout))


def test_generated_agent_guidance_uses_user_gitignore(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")

    run_cli("integrations", "install", "all", "--target", "project", "--json", cwd=repo)

    guidance_paths = [
        repo / "AGENTS.md",
        repo / ".agents" / "skills" / "agentdir" / "SKILL.md",
        repo / "CLAUDE.md",
        repo / ".github" / "copilot-instructions.md",
        repo / ".cursor" / "rules" / "agentdir.mdc",
        repo / ".windsurf" / "rules" / "agentdir.md",
    ]
    for path in guidance_paths:
        text = path.read_text(encoding="utf-8")
        assert "agentdir root --require --quiet" in text, path
        assert "agentdir adopt --if-needed --gitignore user" in text, path
        assert "do not decide whether agentdir is set up" in text.lower(), path
        assert "`.agentdir`" in text, path


def test_integrations_install_all_store_target(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")

    installed = run_cli("integrations", "install", "all", "--target", "store", "--json", cwd=repo)
    doctor = run_cli("integrations", "doctor", "--target", "store", "--json", cwd=repo)

    installed_payload = json.loads(installed.stdout)
    doctor_payload = json.loads(doctor.stdout)

    assert {item["name"] for item in installed_payload} == {"generic", "codex", "claude", "copilot", "cursor", "windsurf"}
    assert doctor_payload["ok"] is True
    assert (repo / ".agentdir" / "integrations" / "generic" / "AGENTS.md").is_file()
    assert (repo / ".agentdir" / "integrations" / "codex" / "skills" / "agentdir" / "SKILL.md").is_file()
    assert (repo / ".agentdir" / "integrations" / "claude" / "CLAUDE.md").is_file()
    assert (repo / ".agentdir" / "integrations" / "copilot" / "copilot-instructions.md").is_file()
    assert (repo / ".agentdir" / "integrations" / "cursor" / "agentdir.mdc").is_file()
    assert (repo / ".agentdir" / "integrations" / "windsurf" / "agentdir.md").is_file()


def test_work_start_status_report_and_finish_are_agent_owned_flow(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")

    started = run_cli("work", "start", "control plane task", "--emit-context", "--json", cwd=repo)
    start_payload = json.loads(started.stdout)
    session_id = start_payload["session"]["session_id"]
    pack_id = start_payload["context_pack"]["pack_id"]
    run_cli(
        "run",
        "--name",
        "python",
        "--",
        sys.executable,
        "-c",
        "print('control plane evidence passed')",
        cwd=repo,
    )

    status = run_cli("status", "--json", cwd=repo)
    report = run_cli("report", "final", "--format", "json", cwd=repo)
    finished = run_cli("work", "finish", "--json", cwd=repo)
    run_cli("session", "current", cwd=repo, expected_returncode=2)
    run_cli("index", "rebuild", cwd=repo)

    status_payload = json.loads(status.stdout)
    report_payload = json.loads(report.stdout)
    finish_payload = json.loads(finished.stdout)
    rows = query_rows(repo / ".agentdir")
    event_types = [row["event_type"] for row in rows]

    assert status_payload["session"]["current"]["session_id"] == session_id
    assert status_payload["context"]["latest_pack"]["pack_id"] == pack_id
    assert report_payload["summary"]["session_id"] == session_id
    assert report_payload["context"]["latest_pack"]["pack_id"] == pack_id
    assert any(row["tool"] == "python" and row["tool_exit_code"] == 0 for row in report_payload["evidence"])
    assert finish_payload["ended_session"]["session_id"] == session_id
    assert "work.started" in event_types
    assert "context.pack.created" in event_types
    assert "work.report.final" in event_types
    assert "work.finished" in event_types
    assert "session.ended" in event_types


def test_work_context_records_used_briefing_and_handoff_funnel(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text(
        "checkout redirect callback state regression was fixed by preserving the callback state",
        encoding="utf-8",
    )
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)

    started = run_cli(
        "work",
        "start",
        "checkout redirect callback state regression",
        "--emit-context",
        cwd=repo,
    )

    assert "context_match=strong_prior_context" in started.stdout
    assert "[1] strong" in started.stdout
    rendered_pack_id = next(
        line.split("=", 1)[1]
        for line in started.stdout.splitlines()
        if line.startswith("context_pack=")
    )
    assert (
        f"context_use={scoped_work_context_invocation(repo / '.agentdir')} "
        f"--pack {rendered_pack_id} --use <number>"
        in started.stdout
    )
    first = json.loads(
        run_cli(
            "work",
            "context",
            "--use",
            "1",
            "--reason",
            "the callback-state pattern constrains the repair plan",
            "--json",
            cwd=repo,
        ).stdout
    )
    repeated = json.loads(
        run_cli(
            "work",
            "context",
            "--use",
            "s1",
            "--reason",
            "the callback-state pattern constrains the repair plan",
            "--json",
            cwd=repo,
        ).stdout
    )
    status = json.loads(run_cli("status", "--json", cwd=repo).stdout)
    finished = json.loads(run_cli("work", "finish", "--json", cwd=repo).stdout)
    lineage = finished["report"]["agent_handoff"]["context_lineage"]

    assert first["recorded"] is True
    assert repeated["recorded"] is False
    assert status["context"]["audit"]["review_status"] == "complete"
    assert status["context"]["audit"]["used_count"] == 1
    assert lineage["retrieved"] >= lineage["presented"] == lineage["reviewed"]
    assert lineage["used"] == 1
    assert lineage["dismissed"] == lineage["presented"] - 1
    assert lineage["cited"] == 0
    assert lineage["review_status"] == "complete"
    assert lineage["ok"] is True


def test_work_context_no_relevant_completes_review_without_use_or_citation(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("generic historical build output context marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli(
            "work",
            "start",
            "historical build output context marker",
            "--emit-context",
            "--json",
            cwd=repo,
        ).stdout
    )

    decision = json.loads(
        run_cli(
            "work",
            "context",
            "--none-relevant",
            "--reason",
            "the returned build record does not constrain this task",
            "--json",
            cwd=repo,
        ).stdout
    )
    audit = json.loads(
        run_cli("audit", "context", "--pack", started["context_pack"]["pack_id"], "--json", cwd=repo).stdout
    )

    assert decision["disposition"] == "no_relevant"
    assert audit["review_status"] == "complete"
    assert audit["reviewed_count"] == audit["presented_count"]
    assert audit["used_count"] == 0
    assert audit["dismissed_count"] == audit["presented_count"]
    assert audit["cited_count"] == 0
    assert audit["decision_reason"] == "the returned build record does not constrain this task"
    run_cli("work", "finish", "--json", cwd=repo)


def test_redacted_review_reasons_keep_used_and_no_relevant_decisions_stable(tmp_path: Path) -> None:
    for disposition in ("used", "no-relevant"):
        repo = init_repo(tmp_path / disposition)
        prior = tmp_path / f"prior-{disposition}.txt"
        prior.write_text(
            f"checkout redirect redacted decision marker {disposition}",
            encoding="utf-8",
        )
        run_cli("session", "start", "--id", f"prior-{disposition}", cwd=repo)
        run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
        run_cli("session", "end", "--summary", str(prior), cwd=repo)
        started = json.loads(
            run_cli(
                "work",
                "start",
                f"checkout redirect redacted decision marker {disposition}",
                "--json",
                cwd=repo,
            ).stdout
        )
        pack_id = started["context_pack"]["pack_id"]
        decision_args = ("--use", "1") if disposition == "used" else ("--none-relevant",)
        command = (
            "work",
            "context",
            "--pack",
            pack_id,
            *decision_args,
            "--reason",
            "token=abcdefghijklmnop is not context evidence",
            "--json",
        )

        first = json.loads(run_cli(*command, cwd=repo).stdout)
        repeated = json.loads(run_cli(*command, cwd=repo).stdout)
        audit = json.loads(
            run_cli("audit", "context", "--pack", pack_id, "--json", cwd=repo).stdout
        )
        finished = json.loads(run_cli("work", "finish", "--json", cwd=repo).stdout)

        assert first["recorded"] is True
        assert repeated["recorded"] is False
        assert first["decision_id"] == repeated["decision_id"] == audit["decision_id"]
        assert first["reason"] == "<redacted:key-value-secret> is not context evidence"
        assert "abcdefghijklmnop" not in json.dumps(first)
        assert audit["review_status"] == "complete"
        assert audit["decision_validation_errors"] == []
        assert any(
            event["headers"].get("X-AgentDir-Redactions") == "1"
            for event in audit["events"]
        )
        assert finished["report"]["agent_handoff"]["context_lineage"]["ok"] is True


def test_work_start_records_context_by_default_and_keeps_plain_briefing_compact(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect callback state regression marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)

    started = run_cli("work", "start", "checkout redirect callback state regression", cwd=repo)
    status = json.loads(run_cli("status", "--json", cwd=repo).stdout)

    assert "context_pack=ctx-" in started.stdout
    pack_id = status["context"]["latest_pack"]["pack_id"]
    context_invocation = scoped_work_context_invocation(repo / ".agentdir")
    assert f"context_expand={context_invocation} --pack {pack_id} --expand <number>" in started.stdout
    assert f"context_use={context_invocation} --pack {pack_id} --use <number>" in started.stdout
    assert "source=" not in started.stdout
    assert len(started.stdout.splitlines()) <= 25
    assert status["context"]["latest_pack"] is not None
    assert status["context"]["audit"]["review_status"] == "pending"

    printed_expand = next(
        line.split("=", 1)[1]
        for line in started.stdout.splitlines()
        if line.startswith("context_expand=")
    ).replace("<number>", "1")
    copied = subprocess.run(
        shlex.split(printed_expand),
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(SRC_ROOT)},
        text=True,
        capture_output=True,
    )
    assert copied.returncode == 0, copied.stderr
    assert f"context_pack={pack_id}" in copied.stdout


def test_work_context_expands_clean_bounded_pages_and_records_read_before_use(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior_text = "context expansion canonical marker\n" + "".join(
        f"line-{index:04d} preserves canonical context expansion detail {index}\n"
        for index in range(220)
    )
    prior.write_text(prior_text, encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli(
            "work",
            "start",
            "canonical context expansion detail",
            "--json",
            cwd=repo,
        ).stdout
    )
    pack_id = started["context_pack"]["pack_id"]
    source_ref = displayed_source_ref(started)

    first = json.loads(
        run_cli(
            "work",
            "context",
            "--pack",
            pack_id,
            "--expand",
            source_ref,
            "--page",
            "1",
            "--json",
            cwd=repo,
        ).stdout
    )
    second = json.loads(
        run_cli(
            "work",
            "context",
            "--pack",
            pack_id,
            "--expand",
            source_ref,
            "--page",
            "2",
            "--json",
            cwd=repo,
        ).stdout
    )
    repeated = run_cli(
        "work",
        "context",
        "--pack",
        pack_id,
        "--expand",
        source_ref,
        "--page",
        "1",
        cwd=repo,
    )

    assert first["pack_id"] == pack_id
    assert first["source"]["ref"] == source_ref
    assert first["source"]["event_type"] == "agent.message"
    assert first["source"]["match_quality"] in {"strong", "possible", "weak"}
    assert first["integrity"] == "verified"
    assert first["basis"] == "canonical_envelope"
    assert first["extent"] == "bounded"
    assert first["page"] == 1
    assert first["page_count"] >= 2
    assert first["byte_start"] == 0
    assert first["returned_bytes"] <= 4096
    assert first["truncated"] is True
    assert "context expansion canonical marker" in first["content"]
    assert first["receipt"]["status"] == "recorded"
    assert first["receipt"]["recorded"] is True
    assert second["byte_start"] == first["byte_end"]
    assert second["content"] != first["content"]
    assert second["receipt"]["status"] == "recorded"
    assert "receipt=existing" in repeated.stdout
    assert (
        f"next_page={scoped_work_context_invocation(repo / '.agentdir')} --pack {pack_id} "
        f"--expand {source_ref} --page 2"
        in repeated.stdout
    )

    run_cli(
        "work",
        "context",
        "--pack",
        pack_id,
        "--use",
        source_ref,
        "--reason",
        "the expanded canonical record supplies the implementation details",
        cwd=repo,
    )
    status = json.loads(run_cli("status", "--json", cwd=repo).stdout)
    expansion = status["context"]["audit"]["expansion"]
    lineage = json.loads(
        run_cli("report", "final", "--format", "json", cwd=repo).stdout
    )["agent_handoff"]["context_lineage"]

    assert expansion["expanded_source_count"] == 1
    assert expansion["expanded_before_decision_count"] == 1
    assert expansion["expanded_after_decision_count"] == 0
    assert expansion["used_without_prior_expansion_count"] == 0
    assert expansion["receipt_event_count"] == 2
    assert expansion["receipts_valid"] is True
    assert lineage["expansion"] == expansion

    root = repo / ".agentdir"
    with sqlite3.connect(root / "indexes" / "agentdir.sqlite3") as conn:
        direct_receipts = conn.execute(
            "select count(*) from memory_documents where event_type = 'context.sources.expanded'"
        ).fetchone()[0]
        summaries = [
            row[0]
            for row in conn.execute(
                "select body_text from memory_documents where source_kind = 'session_summary'"
            ).fetchall()
        ]
    assert direct_receipts == 0
    assert all("- context.sources.expanded:" not in summary for summary in summaries)
    assert status["memory"]["coverage"] == 1.0


def test_context_expansion_pages_unicode_and_deduplicates_concurrent_receipts(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior_text = (
        "unicode context expansion boundary marker\n"
        + ("unicode context expansion boundary marker " * 110)
        + "🙂é漢字"
        + (" unicode context expansion boundary marker" * 110)
        + "\nunicode context expansion tail"
    )
    prior.write_text(prior_text, encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli(
            "work",
            "start",
            "unicode context expansion boundary marker",
            "--json",
            cwd=repo,
        ).stdout
    )
    pack_id = started["context_pack"]["pack_id"]
    source_ref = displayed_source_ref(started)

    def expand_first_page() -> dict[str, object]:
        return json.loads(
            run_cli(
                "work",
                "context",
                "--pack",
                pack_id,
                "--expand",
                source_ref,
                "--page",
                "1",
                "--json",
                cwd=repo,
            ).stdout
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(expand_first_page), pool.submit(expand_first_page))
        concurrent = [future.result() for future in futures]

    assert {item["receipt"]["status"] for item in concurrent} == {"recorded", "existing"}
    first = concurrent[0]
    content = [str(first["content"])]
    for page in range(2, int(first["page_count"]) + 1):
        expanded = json.loads(
            run_cli(
                "work",
                "context",
                "--pack",
                pack_id,
                "--expand",
                source_ref,
                "--page",
                str(page),
                "--json",
                cwd=repo,
            ).stdout
        )
        assert expanded["returned_bytes"] <= 4096
        content.append(expanded["content"])
    assert "".join(content).strip() == prior_text.strip()

    run_cli(
        "work",
        "context",
        "--pack",
        pack_id,
        "--expand",
        source_ref,
        "--page",
        "0",
        "--json",
        cwd=repo,
        expected_returncode=2,
    )
    run_cli(
        "work",
        "context",
        "--pack",
        pack_id,
        "--expand",
        source_ref,
        "--page",
        str(int(first["page_count"]) + 1),
        "--json",
        cwd=repo,
        expected_returncode=2,
    )
    run_cli("index", "update", cwd=repo)
    root = repo / ".agentdir"
    with sqlite3.connect(root / "indexes" / "agentdir.sqlite3") as conn:
        receipt_rows = conn.execute(
            "select id from messages where event_type = 'context.sources.expanded' order by id"
        ).fetchall()
        header_names = {
            row[0]
            for row in conn.execute(
                "select name from headers where message_rowid = ?",
                (receipt_rows[0][0],),
            ).fetchall()
        }
    assert len(receipt_rows) == int(first["page_count"])
    assert "X-AgentDir-Context-View-Pack-Id" in header_names
    assert "X-AgentDir-Pack-Id" not in header_names


def test_context_expansion_returns_content_when_optional_receipt_write_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("optional receipt failure must not hide expanded content", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli(
            "work",
            "start",
            "optional receipt failure expanded content",
            "--json",
            cwd=repo,
        ).stdout
    )
    pack_id = started["context_pack"]["pack_id"]
    source_ref = displayed_source_ref(started)

    def fail_receipt(*_args, **_kwargs):
        raise OSError("simulated receipt fsync failure")

    monkeypatch.setattr("agentdir.context_expansion.emit_event", fail_receipt)
    expanded = expand_context_source(
        repo / ".agentdir",
        pack_id=pack_id,
        source_selector=source_ref,
    )

    assert "optional receipt failure" in expanded["content"]
    assert expanded["receipt"]["status"] == "failed"
    assert expanded["receipt"]["reason"] == "receipt_write_failed"
    assert "simulated receipt fsync failure" in expanded["receipt"]["error"]
    assert any("optional receipt failed" in warning for warning in expanded["warnings"])


def test_context_expansion_stdout_failure_emits_no_receipt(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("stdout delivery failure receipt boundary marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli("work", "start", "stdout delivery failure receipt boundary", "--json", cwd=repo).stdout
    )
    env = {**os.environ, "PYTHONPATH": str(SRC_ROOT)}
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agentdir",
            "work",
            "context",
            "--pack",
            started["context_pack"]["pack_id"],
            "--expand",
            displayed_source_ref(started),
            "--json",
        ],
        cwd=repo,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    process.stdout.close()
    returncode = process.wait(timeout=10)
    stderr = process.stderr.read() if process.stderr is not None else ""
    run_cli("index", "update", cwd=repo)

    assert returncode != 0, stderr
    assert query_rows(repo / ".agentdir", "context.sources.expanded") == []


def test_context_expansion_receipt_binds_verified_content_to_manifest_digest(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("receipt manifest digest binding marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli("work", "start", "receipt manifest digest binding marker", "--json", cwd=repo).stdout
    )
    manifest = started["context_pack"]
    pack_id = manifest["pack_id"]
    session_id = manifest["session_id"]
    source_ref = displayed_source_ref(started)
    expanded = expand_context_source(
        repo / ".agentdir",
        pack_id=pack_id,
        source_selector=source_ref,
    )
    run_cli("index", "update", cwd=repo)
    source_id = expanded["source"]["source_id"]
    event = deepcopy(
        _expansion_events(repo / ".agentdir", pack_id, session_id=session_id)[0]
    )
    forged_payload = _view_payload(expanded)
    forged_payload["source_sha256"] = "0" * 64
    forged_view_id = _view_id(forged_payload)
    event["header_values"][HEADER_VIEW_SOURCE_SHA256] = ["0" * 64]
    event["header_values"][HEADER_VIEW_ID] = [forged_view_id]
    event["message_id"] = f"<{forged_view_id}@agentdir.local>"
    event["body_text"] = _receipt_body(
        forged_payload,
        view_id=forged_view_id,
        decision_phase="before_decision",
    )

    payload, errors = _validate_receipt(
        repo / ".agentdir",
        event,
        manifest,
        {source_id: source_ref},
        {source["source_id"]: source for source in manifest["sources"]},
    )

    assert payload is not None
    assert any("source_sha256 does not match retained content" in error for error in errors), errors


def test_context_expansion_rejects_symlinked_source_outside_the_store(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("outside symlink containment expansion marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli(
            "work",
            "start",
            "outside symlink containment expansion marker",
            "--json",
            cwd=repo,
        ).stdout
    )
    pack_id = started["context_pack"]["pack_id"]
    source_ref = displayed_source_ref(started)
    source_id = started["context_briefing"]["source_ids"][int(source_ref) - 1]
    source = next(
        item for item in started["context_pack"]["sources"] if item["source_id"] == source_id
    )
    envelope = repo / ".agentdir" / source["file_path"]
    outside = tmp_path / "outside-source.eml"
    envelope.rename(outside)
    envelope.symlink_to(outside)

    expanded = json.loads(
        run_cli(
            "work",
            "context",
            "--pack",
            pack_id,
            "--expand",
            source_ref,
            "--json",
            cwd=repo,
        ).stdout
    )

    assert expanded["integrity"] == "unavailable"
    assert expanded["basis"] == "manifest_excerpt"
    assert expanded["receipt"]["status"] == "not_recorded"


def test_context_expansion_normalizes_retained_header_whitespace(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("header whitespace expansion marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli(
        "emit",
        "--type",
        "agent.message",
        "--subject",
        "subject  with   spacing",
        "--body",
        str(prior),
        cwd=repo,
    )
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli("work", "start", "header whitespace expansion marker", "--json", cwd=repo).stdout
    )
    expanded = json.loads(
        run_cli(
            "work",
            "context",
            "--pack",
            started["context_pack"]["pack_id"],
            "--expand",
            displayed_source_ref(started),
            "--json",
            cwd=repo,
        ).stdout
    )

    assert expanded["integrity"] == "verified"
    assert expanded["basis"] == "canonical_envelope"


def test_context_expansion_rejects_unsafe_summary_session_identity(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("init", str(repo / ".agentdir"))
    outside_session = tmp_path / "outside-session"

    resolved = _resolve_session_summary(
        repo / ".agentdir",
        {
            "session_id": str(outside_session),
            "source_kind": "session_summary",
            "excerpt": "bounded captured summary preview",
        },
    )

    assert resolved["integrity"] == "unavailable"
    assert resolved["basis"] == "manifest_excerpt"
    assert resolved["integrity_reason"] == "summary session identity is unsafe"


def test_work_context_integrity_mismatch_returns_only_the_stored_redacted_excerpt(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text(
        "immutable expansion original marker with stable historical evidence",
        encoding="utf-8",
    )
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli(
            "work",
            "start",
            "immutable expansion original marker",
            "--json",
            cwd=repo,
        ).stdout
    )
    pack_id = started["context_pack"]["pack_id"]
    source_ref = displayed_source_ref(started)
    source_id = started["context_briefing"]["source_ids"][int(source_ref) - 1]
    source = next(
        item for item in started["context_pack"]["sources"] if item["source_id"] == source_id
    )
    envelope = repo / ".agentdir" / source["file_path"]
    raw = envelope.read_text(encoding="utf-8")
    assert "immutable expansion original marker" in raw
    envelope.write_text(
        raw.replace("immutable expansion original marker", "immutable expansion changed marker"),
        encoding="utf-8",
    )

    expanded = json.loads(
        run_cli(
            "work",
            "context",
            "--pack",
            pack_id,
            "--expand",
            source_ref,
            "--json",
            cwd=repo,
        ).stdout
    )
    audit = json.loads(
        run_cli("audit", "context", "--pack", pack_id, "--json", cwd=repo).stdout
    )

    assert expanded["integrity"] == "changed"
    assert expanded["basis"] == "manifest_excerpt"
    assert expanded["extent"] == "stored_excerpt"
    assert "immutable expansion original marker" in expanded["content"]
    assert "immutable expansion changed marker" not in expanded["content"]
    assert expanded["receipt"]["status"] == "not_recorded"
    assert expanded["receipt"]["reason"] == "canonical_source_unavailable"
    assert audit["expansion"]["receipt_event_count"] == 0


def test_context_expansion_labels_derived_summary_drift_instead_of_rewriting_history(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text(
        "derived summary context expansion drift marker",
        encoding="utf-8",
    )
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli(
            "work",
            "start",
            "derived summary context expansion drift marker",
            "--memory-limit",
            "1",
            "--recent-limit",
            "5",
            "--json",
            cwd=repo,
        ).stdout
    )
    pack_id = started["context_pack"]["pack_id"]
    summary_ref = displayed_source_ref(started, event_type="summary.compacted")
    original = json.loads(
        run_cli(
            "work",
            "context",
            "--pack",
            pack_id,
            "--expand",
            summary_ref,
            "--json",
            cwd=repo,
        ).stdout
    )
    drift = tmp_path / "drift.txt"
    drift.write_text("new derived summary material that was not captured", encoding="utf-8")
    run_cli(
        "emit",
        "--session",
        "prior-context",
        "--type",
        "agent.message",
        "--body",
        str(drift),
        cwd=repo,
    )

    changed = json.loads(
        run_cli(
            "work",
            "context",
            "--pack",
            pack_id,
            "--expand",
            summary_ref,
            "--json",
            cwd=repo,
        ).stdout
    )

    assert original["integrity"] == "verified"
    assert original["basis"] == "canonical_derived_summary"
    assert changed["integrity"] == "changed"
    assert changed["basis"] == "manifest_excerpt"
    assert "drifted or its session identity was replaced" in changed["integrity_reason"]
    assert "new derived summary material" not in changed["content"]
    assert changed["receipt"]["status"] == "not_recorded"


def test_expansion_after_use_is_observable_without_changing_the_terminal_decision(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text(
        "late context expansion decision stability marker",
        encoding="utf-8",
    )
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli(
            "work",
            "start",
            "late context expansion decision stability marker",
            "--json",
            cwd=repo,
        ).stdout
    )
    pack_id = started["context_pack"]["pack_id"]
    source_ref = displayed_source_ref(started)
    run_cli(
        "work",
        "context",
        "--pack",
        pack_id,
        "--use",
        source_ref,
        "--reason",
        "the historical pattern constrains the implementation",
        cwd=repo,
    )
    before = json.loads(
        run_cli("audit", "context", "--pack", pack_id, "--json", cwd=repo).stdout
    )

    expanded = json.loads(
        run_cli(
            "work",
            "context",
            "--pack",
            pack_id,
            "--expand",
            source_ref,
            "--json",
            cwd=repo,
        ).stdout
    )
    after = json.loads(
        run_cli("audit", "context", "--pack", pack_id, "--json", cwd=repo).stdout
    )

    for field in ("decision_id", "decision", "review_status", "finish_allowed", "lineage_valid"):
        assert after[field] == before[field]
    assert expanded["decision"]["phase"] == "after_decision"
    assert after["expansion"]["expanded_before_decision_count"] == 0
    assert after["expansion"]["expanded_after_decision_count"] == 1
    assert after["expansion"]["used_without_prior_expansion_count"] == 1
    assert all(event["event_type"] != "context.sources.expanded" for event in after["events"])
    run_cli("work", "finish", "--json", cwd=repo)


def test_terminal_context_show_is_decision_aware_and_historical_expand_is_read_only(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text(
        "terminal context expansion historical read marker",
        encoding="utf-8",
    )
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli(
            "work",
            "start",
            "terminal context expansion historical read marker",
            "--json",
            cwd=repo,
        ).stdout
    )
    pack_id = started["context_pack"]["pack_id"]
    source_ref = displayed_source_ref(started)
    reason = "the historical record is not relevant to the current decision"
    run_cli(
        "work",
        "context",
        "--pack",
        pack_id,
        "--none-relevant",
        "--reason",
        reason,
        cwd=repo,
    )

    shown = run_cli("work", "context", "--show", "--pack", pack_id, cwd=repo)
    assert "context_review_status=complete" in shown.stdout
    assert "context_decision=no_relevant" in shown.stdout
    assert f"context_reason={reason}" in shown.stdout
    assert "context_use=" not in shown.stdout
    assert "context_none=" not in shown.stdout
    assert "context_skip=" not in shown.stdout
    assert (
        f"context_expand={scoped_work_context_invocation(repo / '.agentdir')} "
        f"--pack {pack_id} --expand <number>"
        in shown.stdout
    )

    run_cli("work", "finish", "--json", cwd=repo)
    historical = json.loads(
        run_cli(
            "work",
            "context",
            "--pack",
            pack_id,
            "--expand",
            source_ref,
            "--json",
            cwd=repo,
        ).stdout
    )
    run_cli("index", "update", cwd=repo)
    receipts = query_rows(repo / ".agentdir", "context.sources.expanded")

    assert historical["integrity"] == "verified"
    assert "historical read marker" in historical["content"]
    assert historical["receipt"]["status"] == "not_recorded"
    assert historical["receipt"]["reason"] == "session_not_active"
    assert receipts == []


def test_archived_pack_and_source_receipt_remain_directly_readable_but_not_searchable(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text(
        "archived context expansion retained canonical marker",
        encoding="utf-8",
    )
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli(
            "work",
            "start",
            "archived context expansion retained canonical marker",
            "--json",
            cwd=repo,
        ).stdout
    )
    pack_id = started["context_pack"]["pack_id"]
    session_id = started["session"]["session_id"]
    source_ref = displayed_source_ref(started)
    active_read = json.loads(
        run_cli(
            "work",
            "context",
            "--pack",
            pack_id,
            "--expand",
            source_ref,
            "--json",
            cwd=repo,
        ).stdout
    )
    run_cli(
        "work",
        "context",
        "--pack",
        pack_id,
        "--use",
        source_ref,
        "--reason",
        "the retained canonical record establishes the historical behavior",
        cwd=repo,
    )
    run_cli("work", "finish", "--json", cwd=repo)
    run_cli(
        "archive",
        "--session",
        session_id,
        "--session",
        "prior-context",
        "--apply",
        cwd=repo,
    )

    archived_read = json.loads(
        run_cli(
            "work",
            "context",
            "--pack",
            pack_id,
            "--expand",
            source_ref,
            "--json",
            cwd=repo,
        ).stdout
    )
    audit = json.loads(
        run_cli("audit", "context", "--pack", pack_id, "--json", cwd=repo).stdout
    )
    rows = query_rows(repo / ".agentdir")

    assert active_read["receipt"]["status"] == "recorded"
    assert archived_read["integrity"] == "verified"
    assert "retained canonical marker" in archived_read["content"]
    assert archived_read["receipt"]["status"] == "not_recorded"
    assert archived_read["receipt"]["reason"] == "session_not_active"
    assert audit["review_status"] == "complete"
    assert audit["expansion"]["receipt_event_count"] == 1
    assert audit["expansion"]["expanded_before_decision_count"] == 1
    assert all(row["session_id"] != session_id for row in rows)
    assert all(row["event_type"] != "context.sources.expanded" for row in rows)


def test_work_start_blocks_a_second_pack_until_the_first_is_decided(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect pending briefing marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)

    first = json.loads(
        run_cli("work", "start", "checkout redirect pending briefing marker", "--json", cwd=repo).stdout
    )
    blocked = run_cli(
        "work",
        "start",
        "second task cannot hide the first pack",
        "--json",
        cwd=repo,
        expected_returncode=3,
    )

    assert first["context_pack"]["pack_id"] in blocked.stderr
    assert f"work context --root {repo / '.agentdir'} --pack" in blocked.stderr
    assert f"work context --root {repo / '.agentdir'} --show --pack" in blocked.stderr


def test_concurrent_work_starts_create_only_one_pending_pack(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect concurrent start marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    run_cli("session", "start", "--id", "active-context", cwd=repo)

    def start() -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC_ROOT)
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "agentdir",
                "work",
                "start",
                "checkout redirect concurrent start marker",
                "--json",
            ],
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: start(), range(2)))

    assert sorted(result.returncode for result in results) == [0, 3]
    successful = next(result for result in results if result.returncode == 0)
    pack_id = json.loads(successful.stdout)["context_pack"]["pack_id"]
    run_cli("status", "--json", cwd=repo)
    packs = [
        row
        for row in query_rows(repo / ".agentdir", "context.pack.created")
        if row["session_id"] == "active-context"
    ]
    assert len(packs) == 1
    assert pack_id in next(result.stderr for result in results if result.returncode == 3)


def test_context_pack_emission_linearizes_before_finish_audit(tmp_path: Path, monkeypatch) -> None:
    import fcntl
    import hashlib
    from contextlib import contextmanager
    from threading import current_thread

    import agentdir.context as context
    import agentdir.control as control
    from agentdir.store import AgentDirStateError

    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect finish race marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli("work", "start", "finish race baseline", "--no-context", "--json", cwd=repo).stdout
    )
    store = repo / ".agentdir"
    pack = context.build_context_pack(
        store,
        "checkout redirect finish race marker",
        session_id=started["session"]["session_id"],
        exclude_session_from_memory=True,
    )
    assert context.brief_context_manifest(context.build_context_manifest(pack))["review_required"]

    emitter_inside = Event()
    release_emitter = Event()
    finish_lock_probe = Event()
    finish_scan = Event()
    finisher_ident: dict[str, int | None] = {"value": None}
    probe_blocked: dict[str, bool | None] = {"value": None}
    real_context_emit = context.emit_event
    real_build_report = control.build_final_report
    real_control_lock = control.lifecycle_lock

    def paused_context_emit(*args, **kwargs):
        if kwargs.get("event_type") == "context.pack.created":
            emitter_inside.set()
            if not release_emitter.wait(5):
                raise AssertionError("timed out waiting to release context emission")
        return real_context_emit(*args, **kwargs)

    def observed_build_report(*args, **kwargs):
        finish_scan.set()
        return real_build_report(*args, **kwargs)

    @contextmanager
    def observed_control_lock(root, key):
        expected_key = f"session:{started['session']['session_id']}"
        if current_thread().ident == finisher_ident["value"] and key == expected_key:
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
            lock_path = store / "state" / f".lifecycle-{digest}.lock"
            with lock_path.open("a+", encoding="utf-8") as probe:
                try:
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    probe_blocked["value"] = True
                else:
                    probe_blocked["value"] = False
                    fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            finish_lock_probe.set()
        with real_control_lock(root, key):
            yield

    def finish() -> dict[str, object]:
        finisher_ident["value"] = current_thread().ident
        return control.finish_work(store, run_health_check=False)

    monkeypatch.setattr(context, "emit_event", paused_context_emit)
    monkeypatch.setattr(control, "build_final_report", observed_build_report)
    monkeypatch.setattr(control, "lifecycle_lock", observed_control_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        emitted_future = pool.submit(context.emit_context_pack, store, pack)
        assert emitter_inside.wait(5)
        finish_future = pool.submit(finish)
        assert finish_lock_probe.wait(5)
        try:
            assert probe_blocked["value"] is True
        finally:
            release_emitter.set()
        emitted = emitted_future.result(timeout=5)
        try:
            finish_future.result(timeout=5)
        except AgentDirStateError as exc:
            assert "Context review is pending" in str(exc)
        else:
            raise AssertionError("finish unexpectedly ignored the newly emitted pending pack")

    audit = json.loads(
        run_cli("audit", "context", "--pack", emitted.manifest["pack_id"], "--json", cwd=repo).stdout
    )
    current = json.loads(run_cli("session", "current", "--json", cwd=repo).stdout)
    assert finish_scan.is_set()
    assert audit["review_status"] == "pending"
    assert current["session_id"] == started["session"]["session_id"]


def test_session_start_reservation_orders_context_creation(tmp_path: Path, monkeypatch) -> None:
    import fcntl
    import hashlib
    from contextlib import contextmanager
    from threading import current_thread

    import agentdir.context as context
    import agentdir.sessions as sessions

    repo = init_repo(tmp_path / "repo")
    store = repo / ".agentdir"
    run_cli("init", str(store))
    session_id = "ordered-session"
    pack = context.build_context_pack(store, "ordered context creation", session_id=session_id)
    start_paused = Event()
    release_start = Event()
    emitter_probe_complete = Event()
    emitter_ident: dict[str, int | None] = {"value": None}
    probe_blocked: dict[str, bool | None] = {"value": None}
    real_write_session_state = sessions.write_session_state
    real_context_lock = context.lifecycle_lock

    def paused_write_session_state(*args, **kwargs):
        start_paused.set()
        if not release_start.wait(5):
            raise AssertionError("timed out waiting to release session start")
        return real_write_session_state(*args, **kwargs)

    @contextmanager
    def observed_context_lock(root, key):
        if current_thread().ident == emitter_ident["value"] and key == f"session:{session_id}":
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
            lock_path = store / "state" / f".lifecycle-{digest}.lock"
            with lock_path.open("a+", encoding="utf-8") as probe:
                try:
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    probe_blocked["value"] = True
                else:
                    probe_blocked["value"] = False
                    fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            emitter_probe_complete.set()
        with real_context_lock(root, key):
            yield

    def start():
        return sessions.start_session(store, session_id=session_id, cwd=repo)

    def emit():
        emitter_ident["value"] = current_thread().ident
        return context.emit_context_pack(store, pack)

    monkeypatch.setattr(sessions, "write_session_state", paused_write_session_state)
    monkeypatch.setattr(context, "lifecycle_lock", observed_context_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        start_future = pool.submit(start)
        assert start_paused.wait(5)
        emit_future = pool.submit(emit)
        assert emitter_probe_complete.wait(5)
        try:
            assert probe_blocked["value"] is True
        finally:
            release_start.set()
        started = start_future.result(timeout=5)
        emitted = emit_future.result(timeout=5)

    run_cli("index", "update", "--root", str(store))
    lifecycle_events = [
        row["event_type"]
        for row in query_rows(store)
        if row["session_id"] == session_id
        and row["event_type"] in {"session.started", "context.pack.created"}
    ]
    assert started.session_id == session_id
    assert emitted.manifest["session_id"] == session_id
    assert lifecycle_events == ["session.started", "context.pack.created"]


def test_context_pack_emission_is_rejected_after_finish_wins(tmp_path: Path, monkeypatch) -> None:
    import fcntl
    import hashlib
    from contextlib import contextmanager
    from threading import current_thread

    import agentdir.context as context
    import agentdir.control as control
    from agentdir.store import AgentDirStateError

    repo = init_repo(tmp_path / "repo")
    started = json.loads(
        run_cli("work", "start", "finish wins baseline", "--no-context", "--json", cwd=repo).stdout
    )
    store = repo / ".agentdir"
    pack = context.build_context_pack(
        store,
        "late context pack",
        session_id=started["session"]["session_id"],
    )
    finish_scanned = Event()
    release_finish = Event()
    emitter_lock_probe = Event()
    creation_reached = Event()
    emitter_ident: dict[str, int | None] = {"value": None}
    probe_blocked: dict[str, bool | None] = {"value": None}
    real_build_report = control.build_final_report
    real_context_emit = context.emit_event
    real_context_lock = context.lifecycle_lock

    def paused_build_report(*args, **kwargs):
        report = real_build_report(*args, **kwargs)
        finish_scanned.set()
        if not release_finish.wait(5):
            raise AssertionError("timed out waiting to release finish")
        return report

    def observed_context_emit(*args, **kwargs):
        if kwargs.get("event_type") == "context.pack.created":
            creation_reached.set()
        return real_context_emit(*args, **kwargs)

    @contextmanager
    def observed_context_lock(root, key):
        expected_key = f"session:{started['session']['session_id']}"
        if current_thread().ident == emitter_ident["value"] and key == expected_key:
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
            lock_path = store / "state" / f".lifecycle-{digest}.lock"
            with lock_path.open("a+", encoding="utf-8") as probe:
                try:
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    probe_blocked["value"] = True
                else:
                    probe_blocked["value"] = False
                    fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            emitter_lock_probe.set()
        with real_context_lock(root, key):
            yield

    def emit_late_pack():
        emitter_ident["value"] = current_thread().ident
        return context.emit_context_pack(store, pack)

    monkeypatch.setattr(control, "build_final_report", paused_build_report)
    monkeypatch.setattr(context, "emit_event", observed_context_emit)
    monkeypatch.setattr(context, "lifecycle_lock", observed_context_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        finish_future = pool.submit(control.finish_work, store, run_health_check=False)
        assert finish_scanned.wait(5)
        emitted_future = pool.submit(emit_late_pack)
        assert emitter_lock_probe.wait(5)
        try:
            assert probe_blocked["value"] is True
        finally:
            release_finish.set()
        finished = finish_future.result(timeout=5)
        try:
            emitted_future.result(timeout=5)
        except AgentDirStateError as exc:
            assert "ended session" in str(exc)
        else:
            raise AssertionError("a context pack was emitted after the session ended")

    run_cli("index", "update", cwd=repo)
    created = [
        row
        for row in query_rows(store, "context.pack.created")
        if row["session_id"] == started["session"]["session_id"]
    ]
    assert finished["ended_session"]["session_id"] == started["session"]["session_id"]
    assert len(created) == 1
    assert not creation_reached.is_set()


def test_finish_cannot_end_a_concurrently_started_replacement_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import fcntl
    import hashlib
    from contextlib import contextmanager
    from threading import current_thread

    import agentdir.control as control
    import agentdir.sessions as sessions

    repo = init_repo(tmp_path / "repo")
    first = json.loads(
        run_cli("work", "start", "session A", "--no-context", "--json", cwd=repo).stdout
    )
    first_session_id = first["session"]["session_id"]
    store = repo / ".agentdir"
    finish_paused = Event()
    release_finish = Event()
    pointer_probe_complete = Event()
    starter_ident: dict[str, int | None] = {"value": None}
    probe_blocked: dict[str, bool | None] = {"value": None}
    real_control_emit = control.emit_event
    real_pointer_lock = sessions.session_pointer_lock

    def paused_finish_emit(*args, **kwargs):
        if kwargs.get("event_type") == "work.report.final":
            finish_paused.set()
            if not release_finish.wait(5):
                raise AssertionError("timed out waiting to release finish")
        return real_control_emit(*args, **kwargs)

    @contextmanager
    def observed_pointer_lock(root):
        if current_thread().ident == starter_ident["value"]:
            digest = hashlib.sha256(b"active-session-pointer").hexdigest()[:24]
            lock_path = store / "state" / f".lifecycle-{digest}.lock"
            with lock_path.open("a+", encoding="utf-8") as probe:
                try:
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    probe_blocked["value"] = True
                else:
                    probe_blocked["value"] = False
                    fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            pointer_probe_complete.set()
        with real_pointer_lock(root):
            yield

    def start_replacement():
        starter_ident["value"] = current_thread().ident
        return sessions.start_session(
            store,
            session_id="session-B",
            title="session B",
            cwd=repo,
        )

    monkeypatch.setattr(control, "emit_event", paused_finish_emit)
    monkeypatch.setattr(sessions, "session_pointer_lock", observed_pointer_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        finish_future = pool.submit(control.finish_work, store, run_health_check=False)
        assert finish_paused.wait(5)
        start_future = pool.submit(start_replacement)
        assert pointer_probe_complete.wait(5)
        try:
            assert probe_blocked["value"] is True
        finally:
            release_finish.set()
        finished = finish_future.result(timeout=5)
        replacement = start_future.result(timeout=5)

    run_cli("index", "update", cwd=repo)
    ended = query_rows(store, "session.ended")
    current = json.loads(run_cli("session", "current", "--json", cwd=repo).stdout)
    assert finished["ended_session"]["session_id"] == first_session_id
    assert replacement.session_id == "session-B"
    assert current["session_id"] == "session-B"
    assert [row["session_id"] for row in ended].count(first_session_id) == 1
    assert [row["session_id"] for row in ended].count("session-B") == 0


def test_work_start_cannot_partially_mutate_a_session_being_finished(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import fcntl
    import hashlib
    from contextlib import contextmanager
    from threading import current_thread

    import agentdir.control as control
    import agentdir.sessions as sessions

    repo = init_repo(tmp_path / "repo")
    first = json.loads(
        run_cli("work", "start", "first task", "--no-context", "--json", cwd=repo).stdout
    )
    first_session_id = first["session"]["session_id"]
    store = repo / ".agentdir"
    finish_scanned = Event()
    release_finish = Event()
    start_probe_complete = Event()
    starter_ident: dict[str, int | None] = {"value": None}
    probe_blocked: dict[str, bool | None] = {"value": None}
    real_build_report = control.build_final_report
    real_lifecycle_lock = control.lifecycle_lock

    def paused_build_report(*args, **kwargs):
        report = real_build_report(*args, **kwargs)
        finish_scanned.set()
        if not release_finish.wait(5):
            raise AssertionError("timed out waiting to release finish")
        return report

    @contextmanager
    def observed_lifecycle_lock(root, key):
        if current_thread().ident == starter_ident["value"] and key == "work-start":
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
            lock_path = store / "state" / f".lifecycle-{digest}.lock"
            with lock_path.open("a+", encoding="utf-8") as probe:
                try:
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    probe_blocked["value"] = True
                else:
                    probe_blocked["value"] = False
                    fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            start_probe_complete.set()
        with real_lifecycle_lock(root, key):
            yield

    def start_replacement_work():
        starter_ident["value"] = current_thread().ident
        return control.start_work(store, "replacement task", emit_context=False)

    monkeypatch.setattr(control, "build_final_report", paused_build_report)
    monkeypatch.setattr(control, "lifecycle_lock", observed_lifecycle_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        finish_future = pool.submit(control.finish_work, store, run_health_check=False)
        assert finish_scanned.wait(5)
        start_future = pool.submit(start_replacement_work)
        assert start_probe_complete.wait(5)
        try:
            assert probe_blocked["value"] is True
        finally:
            release_finish.set()
        finished = finish_future.result(timeout=5)
        replacement = start_future.result(timeout=5)

    run_cli("index", "update", cwd=repo)
    work_starts = query_rows(store, "work.started")
    current = sessions.read_current_session(store)
    replacement_session_id = replacement["session"]["session_id"]
    assert finished["ended_session"]["session_id"] == first_session_id
    assert replacement_session_id != first_session_id
    assert current is not None and current.session_id == replacement_session_id
    assert [row["session_id"] for row in work_starts].count(first_session_id) == 1
    assert [row["session_id"] for row in work_starts].count(replacement_session_id) == 1


def test_work_start_serializes_direct_active_session_replacement(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import fcntl
    import hashlib
    from contextlib import contextmanager
    from threading import current_thread

    import agentdir.control as control
    import agentdir.sessions as sessions

    repo = init_repo(tmp_path / "repo")
    store = repo / ".agentdir"
    start_paused = Event()
    release_start = Event()
    pointer_probe_complete = Event()
    replacement_ident: dict[str, int | None] = {"value": None}
    probe_blocked: dict[str, bool | None] = {"value": None}
    real_emit_pack = control.emit_context_pack
    real_pointer_lock = sessions.session_pointer_lock

    def paused_emit_pack(*args, **kwargs):
        start_paused.set()
        if not release_start.wait(5):
            raise AssertionError("timed out waiting to release work start")
        return real_emit_pack(*args, **kwargs)

    @contextmanager
    def observed_pointer_lock(root):
        if current_thread().ident == replacement_ident["value"]:
            digest = hashlib.sha256(b"active-session-pointer").hexdigest()[:24]
            lock_path = store / "state" / f".lifecycle-{digest}.lock"
            with lock_path.open("a+", encoding="utf-8") as probe:
                try:
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    probe_blocked["value"] = True
                else:
                    probe_blocked["value"] = False
                    fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            pointer_probe_complete.set()
        with real_pointer_lock(root):
            yield

    def replace_session():
        replacement_ident["value"] = current_thread().ident
        return sessions.start_session(
            store,
            session_id="replacement-session",
            cwd=repo,
        )

    monkeypatch.setattr(control, "emit_context_pack", paused_emit_pack)
    monkeypatch.setattr(sessions, "session_pointer_lock", observed_pointer_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        work_future = pool.submit(control.start_work, store, "serialized start", emit_context=False)
        assert start_paused.wait(5)
        replacement_future = pool.submit(replace_session)
        assert pointer_probe_complete.wait(5)
        try:
            assert probe_blocked["value"] is True
        finally:
            release_start.set()
        started = work_future.result(timeout=5)
        replacement = replacement_future.result(timeout=5)

    run_cli("index", "update", cwd=repo)
    created = query_rows(store, "context.pack.created")
    work_starts = query_rows(store, "work.started")
    current = json.loads(run_cli("session", "current", "--json", cwd=repo).stdout)
    assert len(created) == 1
    assert [row["session_id"] for row in work_starts] == [started["session"]["session_id"]]
    assert replacement.session_id == "replacement-session"
    assert current["session_id"] == "replacement-session"


def test_work_context_show_reopens_the_persisted_numbered_briefing(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect persisted briefing marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli("work", "start", "checkout redirect persisted briefing marker", "--json", cwd=repo).stdout
    )

    shown = run_cli(
        "work",
        "context",
        "--show",
        "--pack",
        started["context_pack"]["pack_id"],
        cwd=repo,
    )

    assert "[1]" in shown.stdout
    assert "persisted briefing marker" in shown.stdout
    pack_id = started["context_pack"]["pack_id"]
    assert (
        f"context_use={scoped_work_context_invocation(repo / '.agentdir')} "
        f"--pack {pack_id} --use <number>"
        in shown.stdout
    )


def test_reopened_briefing_commands_remain_bound_to_the_displayed_pack(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect recovered pack target marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    first = json.loads(
        run_cli(
            "work",
            "start",
            "checkout redirect recovered pack target marker",
            "--json",
            cwd=repo,
        ).stdout
    )
    first_pack_id = first["context_pack"]["pack_id"]
    second = json.loads(
        run_cli(
            "context",
            "build",
            "checkout redirect newer pack target marker",
            "--emit",
            "--json",
            cwd=repo,
        ).stdout
    )
    second_pack_id = second["manifest"]["pack_id"]

    shown = run_cli("work", "context", "--show", "--pack", first_pack_id, cwd=repo)
    assert (
        f"context_use={scoped_work_context_invocation(repo / '.agentdir')} "
        f"--pack {first_pack_id} --use <number>"
        in shown.stdout
    )
    run_cli(
        "work",
        "context",
        "--pack",
        first_pack_id,
        "--use",
        "1",
        "--reason",
        "the recovered source constrains the active repair",
        cwd=repo,
    )

    first_audit = json.loads(
        run_cli("audit", "context", "--pack", first_pack_id, "--json", cwd=repo).stdout
    )
    second_audit = json.loads(
        run_cli("audit", "context", "--pack", second_pack_id, "--json", cwd=repo).stdout
    )
    assert first_audit["review_status"] == "complete"
    assert first_audit["used_count"] == 1
    assert second_audit["review_status"] == "pending"


def test_finish_cannot_hide_an_older_pending_pack_behind_a_newer_pack(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect hidden pending marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    first = json.loads(
        run_cli("work", "start", "checkout redirect hidden pending marker", "--json", cwd=repo).stdout
    )
    second = json.loads(
        run_cli(
            "context",
            "build",
            "checkout redirect replacement pack",
            "--emit",
            "--json",
            cwd=repo,
        ).stdout
    )
    run_cli(
        "work",
        "context",
        "--pack",
        second["manifest"]["pack_id"],
        "--none-relevant",
        "--reason",
        "the replacement briefing does not constrain this task",
        cwd=repo,
    )

    status = json.loads(run_cli("status", "--json", cwd=repo).stdout)
    strict_audit = run_cli("audit", "session", "--strict", "--json", cwd=repo, expected_returncode=1)
    blocked = run_cli("work", "finish", "--json", cwd=repo, expected_returncode=3)

    assert first["context_pack"]["pack_id"] in status["context"]["blocking_packs"]
    assert first["context_pack"]["pack_id"] in strict_audit.stdout
    assert first["context_pack"]["pack_id"] in blocked.stderr


def test_no_context_records_a_visible_marker_for_the_latest_task(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect stale attribution marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    run_cli("work", "start", "first attributed task", cwd=repo)
    run_cli(
        "work",
        "context",
        "--none-relevant",
        "--reason",
        "the prior record does not constrain the next task",
        cwd=repo,
    )

    second = json.loads(
        run_cli("work", "start", "second explicit opt-out task", "--no-context", "--json", cwd=repo).stdout
    )
    status = json.loads(run_cli("status", "--json", cwd=repo).stdout)
    finished = json.loads(run_cli("work", "finish", "--json", cwd=repo).stdout)

    assert second["context_pack"] is not None
    assert second["context_briefing"]["match_state"] == "context_disabled"
    assert second["context_pack"]["selection_policy"]["context_enabled"] is False
    assert status["session"]["current"]["title"] == "second explicit opt-out task"
    assert status["context"]["audit"]["task"] == "second explicit opt-out task"
    assert status["context"]["audit"]["retrieved_count"] == 0
    assert finished["report"]["task"] == "second explicit opt-out task"


def test_low_level_consume_of_all_presented_sources_completes_compatibility_review(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect compatibility consume marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli("work", "start", "checkout redirect compatibility consume marker", "--json", cwd=repo).stdout
    )
    pack_id = started["context_pack"]["pack_id"]
    source_ids = started["context_briefing"]["source_ids"]
    source_args = [part for source_id in source_ids for part in ("--source", source_id)]

    run_cli(
        "context",
        "consume",
        "--pack",
        pack_id,
        *source_args,
        "--purpose",
        "plan",
        cwd=repo,
    )
    audit = json.loads(run_cli("audit", "context", "--pack", pack_id, "--json", cwd=repo).stdout)

    assert audit["review_status"] == "complete"
    assert audit["decision"] == "legacy_used"
    assert audit["reviewed_count"] == audit["presented_count"]
    run_cli("work", "finish", "--json", cwd=repo)


def test_partial_low_level_consume_can_finish_with_a_visible_compatibility_warning(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    for index in range(2):
        prior = tmp_path / f"prior-{index}.txt"
        prior.write_text(
            f"checkout redirect partial compatibility marker {index}",
            encoding="utf-8",
        )
        run_cli("session", "start", "--id", f"prior-context-{index}", cwd=repo)
        run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
        run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli("work", "start", "checkout redirect partial compatibility marker", "--json", cwd=repo).stdout
    )
    assert started["context_briefing"]["presented_count"] >= 2
    pack_id = started["context_pack"]["pack_id"]
    source_id = started["context_briefing"]["source_ids"][0]

    run_cli(
        "context",
        "consume",
        "--pack",
        pack_id,
        "--source",
        source_id,
        "--purpose",
        "plan",
        cwd=repo,
    )
    audit = json.loads(run_cli("audit", "context", "--pack", pack_id, "--json", cwd=repo).stdout)
    finished = json.loads(run_cli("work", "finish", "--json", cwd=repo).stdout)

    assert audit["review_status"] == "compatibility_partial"
    assert audit["decision"] == "legacy_partial"
    assert audit["finish_allowed"] is True
    assert audit["lineage_valid"] is False
    assert finished["report"]["agent_handoff"]["status"] == "needs_attention"


def test_terminal_review_rejects_later_low_level_consumption(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect terminal review marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli("work", "start", "checkout redirect terminal review marker", "--json", cwd=repo).stdout
    )
    pack_id = started["context_pack"]["pack_id"]
    source_id = started["context_briefing"]["source_ids"][0]
    run_cli(
        "work",
        "context",
        "--none-relevant",
        "--reason",
        "the prior marker does not constrain this task",
        cwd=repo,
    )

    blocked = run_cli(
        "context",
        "consume",
        "--pack",
        pack_id,
        "--source",
        source_id,
        "--purpose",
        "plan",
        cwd=repo,
        expected_returncode=3,
    )
    audit = json.loads(run_cli("audit", "context", "--pack", pack_id, "--json", cwd=repo).stdout)

    assert "already terminal" in blocked.stderr
    assert audit["decision"] == "no_relevant"
    assert audit["used_count"] == 0
    assert audit["transition_conflict"] is False


def test_concurrent_identical_context_decisions_are_idempotent(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect concurrent decision marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    run_cli("work", "start", "checkout redirect concurrent decision marker", cwd=repo)

    def decide() -> dict[str, object]:
        result = run_cli(
            "work",
            "context",
            "--use",
            "1",
            "--reason",
            "the prior redirect marker constrains the plan",
            "--json",
            cwd=repo,
        )
        return json.loads(result.stdout)

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(lambda _: decide(), range(2)))

    assert sorted(decision["recorded"] for decision in decisions) == [False, True]
    assert len({decision["decision_id"] for decision in decisions}) == 1


def test_reordered_context_selectors_resolve_to_the_same_terminal_decision(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect selector order marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli("work", "start", "checkout redirect selector order marker", "--json", cwd=repo).stdout
    )
    assert started["context_briefing"]["presented_count"] >= 2

    first = json.loads(
        run_cli(
            "work",
            "context",
            "--use",
            "2",
            "--use",
            "1",
            "--reason",
            "both prior records constrain the plan",
            "--json",
            cwd=repo,
        ).stdout
    )
    repeated = json.loads(
        run_cli(
            "work",
            "context",
            "--use",
            "1",
            "--use",
            "2",
            "--reason",
            "both prior records constrain the plan",
            "--json",
            cwd=repo,
        ).stdout
    )

    assert first["recorded"] is True
    assert repeated["recorded"] is False
    assert repeated["decision_id"] == first["decision_id"]


def test_work_context_rejects_a_retrieved_source_omitted_from_the_briefing(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    for index in range(6):
        body = tmp_path / f"prior-{index}.txt"
        body.write_text(
            f"checkout redirect omitted source marker variant {index}",
            encoding="utf-8",
        )
        run_cli("session", "start", "--id", f"prior-{index}", cwd=repo)
        run_cli("emit", "--type", "agent.message", "--body", str(body), cwd=repo)
        run_cli("session", "end", "--summary", str(body), cwd=repo)
    started = json.loads(
        run_cli(
            "work",
            "start",
            "checkout redirect omitted source marker",
            "--memory-limit",
            "8",
            "--json",
            cwd=repo,
        ).stdout
    )
    presented = set(started["context_briefing"]["source_ids"])
    omitted = next(
        source["source_id"]
        for source in started["context_pack"]["sources"]
        if source["source_id"] not in presented
    )

    blocked = run_cli(
        "work",
        "context",
        "--use",
        omitted,
        "--reason",
        "attempt to bypass the bounded briefing",
        cwd=repo,
        expected_returncode=2,
    )

    assert "not presented" in blocked.stderr

    all_source_ids = [source["source_id"] for source in started["context_pack"]["sources"]]
    source_args = [part for source_id in all_source_ids for part in ("--source", source_id)]
    run_cli(
        "context",
        "consume",
        "--pack",
        started["context_pack"]["pack_id"],
        *source_args,
        "--purpose",
        "plan",
        cwd=repo,
    )
    audit = json.loads(
        run_cli(
            "audit",
            "context",
            "--pack",
            started["context_pack"]["pack_id"],
            "--json",
            cwd=repo,
        ).stdout
    )

    assert audit["used_count"] == audit["presented_count"]
    assert audit["consumed_count"] == audit["retrieved_count"]
    assert audit["additional_consumed_count"] == audit["retrieved_count"] - audit["presented_count"]
    assert audit["lineage_valid"] is True


def test_context_review_can_target_an_older_session_without_ending_the_current_one(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect older session marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli("work", "start", "checkout redirect older session marker", "--json", cwd=repo).stdout
    )
    older_session = started["session"]["session_id"]
    run_cli("session", "start", "--id", "newer-active", cwd=repo)

    run_cli(
        "work",
        "context",
        "--session",
        older_session,
        "--none-relevant",
        "--reason",
        "the older marker does not constrain the current implementation",
        cwd=repo,
    )
    blocked = run_cli(
        "work",
        "finish",
        "--session",
        older_session,
        "--json",
        cwd=repo,
        expected_returncode=3,
    )
    current = json.loads(run_cli("session", "current", "--json", cwd=repo).stdout)
    finished = json.loads(
        run_cli(
            "work",
            "finish",
            "--session",
            older_session,
            "--keep-session",
            "--json",
            cwd=repo,
        ).stdout
    )

    assert "--keep-session" in blocked.stderr
    assert current["session_id"] == "newer-active"
    assert finished["ended_session"] is None


def test_status_does_not_attribute_an_older_pack_to_a_new_active_session(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    started = json.loads(run_cli("work", "start", "first context pack", "--json", cwd=repo).stdout)
    assert started["context_pack"] is not None
    run_cli("work", "finish", "--json", cwd=repo)
    run_cli("session", "start", "--id", "new-session-without-pack", cwd=repo)

    status = json.loads(run_cli("status", "--json", cwd=repo).stdout)

    assert status["session"]["current"]["session_id"] == "new-session-without-pack"
    assert status["context"]["latest_pack"] is None
    assert status["context"]["audit"] is None


def test_work_finish_blocks_pending_context_but_allows_visible_skip(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect review gate marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    run_cli("work", "start", "checkout redirect review gate marker", "--emit-context", cwd=repo)

    blocked = run_cli("work", "finish", "--json", cwd=repo, expected_returncode=3)
    assert "Context review is pending" in blocked.stderr
    assert json.loads(run_cli("session", "current", "--json", cwd=repo).stdout)["status"] == "active"

    run_cli(
        "work",
        "context",
        "--skip",
        "--reason",
        "the context artifact could not be reviewed",
        cwd=repo,
    )
    finished = json.loads(run_cli("work", "finish", "--json", cwd=repo).stdout)
    assert finished["report"]["agent_handoff"]["status"] == "needs_attention"
    assert finished["report"]["agent_handoff"]["context_lineage"]["review_status"] == "skipped"


def test_later_clean_pack_does_not_hide_an_earlier_context_warning(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect durable skip marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    first = json.loads(
        run_cli("work", "start", "checkout redirect durable skip marker", "--json", cwd=repo).stdout
    )
    first_pack_id = first["context_pack"]["pack_id"]
    run_cli(
        "work",
        "context",
        "--pack",
        first_pack_id,
        "--skip",
        "--reason",
        "the briefing could not be reviewed in this environment",
        cwd=repo,
    )
    run_cli("work", "start", "later explicit context opt out", "--no-context", cwd=repo)

    status = json.loads(run_cli("status", "--json", cwd=repo).stdout)
    strict = run_cli("audit", "session", "--strict", "--json", cwd=repo, expected_returncode=1)
    report = json.loads(
        run_cli("report", "final", "--format", "json", "--no-doctor", cwd=repo).stdout
    )
    finished = json.loads(run_cli("work", "finish", "--no-doctor", "--json", cwd=repo).stdout)

    assert first_pack_id in status["context"]["attention_packs"]
    assert first_pack_id in strict.stdout
    assert report["agent_handoff"]["status"] == "needs_attention"
    assert report["agent_handoff"]["context_lineage"]["pack_id"] == first_pack_id
    assert report["agent_handoff"]["context_lineage"]["review_status"] == "skipped"
    assert finished["report"]["agent_handoff"]["status"] == "needs_attention"


def test_tampered_context_manifest_is_visible_and_blocks_finish(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "corrupt context manifest", cwd=repo)
    blobs = list((repo / ".agentdir" / "artifacts" / "blobs" / "sha256").glob("*/*/*"))
    assert len(blobs) == 1
    manifest = json.loads(blobs[0].read_text(encoding="utf-8"))
    manifest["briefing"]["review_required"] = False
    blobs[0].write_text(json.dumps(manifest), encoding="utf-8")

    status = run_cli("status", cwd=repo, expected_returncode=1)
    report = json.loads(
        run_cli("report", "final", "--format", "json", "--no-doctor", cwd=repo).stdout
    )
    blocked = run_cli("work", "finish", "--no-doctor", "--json", cwd=repo, expected_returncode=3)

    assert "review_status" in status.stdout
    assert "audit_error" in status.stdout
    assert "error" in status.stdout
    assert report["agent_handoff"]["status"] == "needs_attention"
    assert report["agent_handoff"]["context_lineage"]["review_status"] == "error"
    assert "digest does not match" in report["agent_handoff"]["context_lineage"]["error"]
    assert "cannot be certified" in blocked.stderr


def test_malformed_context_manifests_use_the_typed_audit_error_path(tmp_path: Path) -> None:
    from agentdir.events import emit_event

    variants: tuple[tuple[str, str, str], ...] = (
        ("invalid-json", "{not json", "invalid JSON"),
        ("non-object", "[]", "must be a JSON object"),
        (
            "missing-sources",
            json.dumps(
                {
                    "protocol": "agentdir.context-pack.v1",
                    "pack_id": "ctx-missing-sources",
                    "task": "missing sources",
                    "session_id": "malformed-missing-sources",
                }
            ),
            "sources must be a list",
        ),
        (
            "disabled-review",
            json.dumps(
                {
                    "protocol": "agentdir.context-pack.v1",
                    "pack_id": "ctx-disabled-review",
                    "task": "disabled review",
                    "session_id": "malformed-disabled-review",
                    "sources": [{"source_id": "src-presented"}],
                    "briefing": {
                        "protocol": "agentdir.context-briefing.v1",
                        "source_ids": ["src-presented"],
                        "presented_count": 1,
                        "omitted_count": 0,
                        "review_required": False,
                    },
                }
            ),
            "review requirement is inconsistent",
        ),
        (
            "session-mismatch",
            json.dumps(
                {
                    "protocol": "agentdir.context-pack.v1",
                    "pack_id": "ctx-session-mismatch",
                    "task": "session mismatch",
                    "session_id": "claimed-session",
                    "sources": [],
                    "briefing": {
                        "protocol": "agentdir.context-briefing.v1",
                        "source_ids": [],
                        "presented_count": 0,
                        "omitted_count": 0,
                        "review_required": False,
                    },
                }
            ),
            "session does not match",
        ),
    )
    for name, content, expected_error in variants:
        repo = init_repo(tmp_path / name)
        session_id = f"malformed-{name}"
        pack_id = "ctx-missing-sources" if name == "missing-sources" else f"ctx-{name}"
        run_cli("session", "start", "--id", session_id, cwd=repo)
        artifact = tmp_path / f"{name}.json"
        artifact.write_text(content, encoding="utf-8")
        emit_event(
            repo / ".agentdir",
            session_id=session_id,
            event_type="context.pack.created",
            subject=f"malformed manifest {name}",
            body=f"pack_id={pack_id}\nprotocol=agentdir.context-pack.v1",
            artifact=artifact,
            extra_headers={
                "X-AgentDir-Protocol": "agentdir.context-pack.v1",
                "X-AgentDir-Pack-Id": pack_id,
            },
        )

        status = json.loads(run_cli("status", "--json", cwd=repo).stdout)
        strict = run_cli(
            "audit",
            "session",
            "--strict",
            "--json",
            cwd=repo,
            expected_returncode=1,
        )
        report = json.loads(
            run_cli("report", "final", "--format", "json", "--no-doctor", cwd=repo).stdout
        )
        blocked = run_cli(
            "work",
            "finish",
            "--no-doctor",
            "--json",
            cwd=repo,
            expected_returncode=3,
        )

        assert expected_error in status["context"]["audit"]["error"]
        assert pack_id in status["context"]["blocking_packs"]
        assert expected_error in strict.stdout
        assert report["agent_handoff"]["status"] == "needs_attention"
        assert report["agent_handoff"]["context_lineage"]["review_status"] == "error"
        assert "cannot be certified" in blocked.stderr
        assert json.loads(run_cli("session", "current", "--json", cwd=repo).stdout)[
            "session_id"
        ] == session_id


def test_malformed_context_creation_identities_remain_visible_blockers(tmp_path: Path) -> None:
    from agentdir.events import emit_event

    variants = (
        ("header-only", ["ctx-header-only"], "no pack id in this body", "body field, found 0"),
        ("body-only", [], "pack_id=ctx-body-only", "header, found 0"),
        (
            "mismatch",
            ["ctx-mismatch-header"],
            "pack_id=ctx-mismatch-body",
            "does not match body pack id",
        ),
        (
            "duplicate-header",
            ["ctx-duplicate-header", "ctx-duplicate-header"],
            "pack_id=ctx-duplicate-header",
            "header, found 2",
        ),
        (
            "duplicate-body",
            ["ctx-duplicate-body"],
            "pack_id=ctx-duplicate-body\npack_id=ctx-duplicate-body",
            "body field, found 2",
        ),
    )
    for name, header_ids, body, expected_error in variants:
        repo = init_repo(tmp_path / name)
        session_id = f"identity-{name}"
        run_cli("session", "start", "--id", session_id, cwd=repo)
        extra_headers: dict[str, str | list[str]] = {
            "X-AgentDir-Protocol": "agentdir.context-pack.v1"
        }
        if header_ids:
            extra_headers["X-AgentDir-Pack-Id"] = header_ids
        emit_event(
            repo / ".agentdir",
            session_id=session_id,
            event_type="context.pack.created",
            subject=f"malformed identity {name}",
            body=body,
            extra_headers=extra_headers,
        )
        pack_id = header_ids[0] if header_ids else f"ctx-{name}"

        status = json.loads(run_cli("status", "--json", cwd=repo).stdout)
        strict = run_cli(
            "audit",
            "session",
            "--strict",
            "--json",
            cwd=repo,
            expected_returncode=1,
        )
        report = json.loads(
            run_cli("report", "final", "--format", "json", "--no-doctor", cwd=repo).stdout
        )
        blocked = run_cli(
            "work",
            "finish",
            "--no-doctor",
            "--json",
            cwd=repo,
            expected_returncode=3,
        )

        assert pack_id in status["context"]["blocking_packs"]
        assert expected_error in status["context"]["audit"]["error"]
        assert expected_error in strict.stdout
        assert report["agent_handoff"]["status"] == "needs_attention"
        assert "cannot be certified" in blocked.stderr
        direct = run_cli(
            "audit",
            "context",
            "--pack",
            pack_id,
            "--json",
            cwd=repo,
            expected_returncode=2,
        )
        assert expected_error in direct.stdout + direct.stderr

    repo = init_repo(tmp_path / "duplicate-creation")
    run_cli("session", "start", "--id", "identity-duplicate-creation", cwd=repo)
    for index in range(2):
        headers: dict[str, str] = {
            "X-AgentDir-Protocol": "agentdir.context-pack.v1",
        }
        if index == 0:
            headers["X-AgentDir-Pack-Id"] = "ctx-duplicate-creation"
        emit_event(
            repo / ".agentdir",
            session_id="identity-duplicate-creation",
            event_type="context.pack.created",
            subject=f"duplicate creation {index}",
            body="pack_id=ctx-duplicate-creation",
            extra_headers=headers,
        )
    status = json.loads(run_cli("status", "--json", cwd=repo).stdout)
    blocked = run_cli(
        "work",
        "finish",
        "--no-doctor",
        "--json",
        cwd=repo,
        expected_returncode=3,
    )
    assert "ctx-duplicate-creation" in status["context"]["blocking_packs"]
    assert "multiple creation events" in status["context"]["audit"]["error"]
    assert "cannot be certified" in blocked.stderr
    direct = run_cli(
        "audit",
        "context",
        "--pack",
        "ctx-duplicate-creation",
        "--json",
        cwd=repo,
        expected_returncode=2,
    )
    assert "multiple creation events" in direct.stdout + direct.stderr


def test_build_final_report_rebuilds_index_once(tmp_path: Path, monkeypatch) -> None:
    import agentdir.context as context
    import agentdir.control as control
    import agentdir.review as review

    repo = init_repo(tmp_path / "repo")

    started = run_cli("work", "start", "single rebuild final report", "--emit-context", "--json", cwd=repo)
    session_id = json.loads(started.stdout)["session"]["session_id"]
    run_cli(
        "run",
        "--name",
        "python",
        "--",
        sys.executable,
        "-c",
        "print('tests passed')",
        cwd=repo,
    )

    calls = 0
    real_rebuild = control.update_index

    def counted_rebuild(root: str | Path) -> None:
        nonlocal calls
        calls += 1
        real_rebuild(root)

    monkeypatch.setattr(control, "update_index", counted_rebuild)
    monkeypatch.setattr(context, "update_index", counted_rebuild)
    monkeypatch.setattr(review, "update_index", counted_rebuild)

    report = control.build_final_report(repo / ".agentdir")

    assert calls == 1
    assert report["summary"]["session_id"] == session_id
    assert report["context"]["latest_pack"]
    assert any(row["tool"] == "python" and row["tool_exit_code"] == 0 for row in report["evidence"])

    calls = 0
    claims_report = control.build_final_report(repo / ".agentdir", claims_text="Tests passed.")

    assert calls == 1
    assert claims_report["claim_support"]["claims"][0]["family"] == "test"
    assert claims_report["claim_support"]["claims"][0]["status"] == "supported"


def test_status_context_pack_and_audit_rebuild_index_once(tmp_path: Path, monkeypatch) -> None:
    import agentdir.audit as audit
    import agentdir.context as context
    import agentdir.control as control
    import agentdir.review as review

    repo = init_repo(tmp_path / "repo")

    run_cli("work", "start", "single rebuild status", "--emit-context", cwd=repo)
    run_cli(
        "run",
        "--name",
        "python",
        "--",
        sys.executable,
        "-c",
        "print('tests passed')",
        cwd=repo,
    )

    calls = 0
    real_rebuild = control.update_index

    def counted_rebuild(root: str | Path) -> None:
        nonlocal calls
        calls += 1
        real_rebuild(root)

    monkeypatch.setattr(control, "update_index", counted_rebuild)
    monkeypatch.setattr(context, "update_index", counted_rebuild)
    monkeypatch.setattr(review, "update_index", counted_rebuild)

    root = repo / ".agentdir"

    status = control.build_status(root)
    assert calls == 1
    assert status["session"]["active"]

    calls = 0
    pack = context.build_context_pack(root, "single rebuild status")
    assert calls == 1
    assert pack["evidence"]

    calls = 0
    session_audit = audit.audit_session(root)
    assert calls == 1
    assert session_audit["checks"]


def test_summarize_and_evidence_use_current_session(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("session", "start", "--id", "summary-session", cwd=repo)
    run_cli(
        "run",
        "--name",
        "python",
        "--",
        sys.executable,
        "-c",
        "print('summary evidence marker')",
        cwd=repo,
    )

    summary = run_cli("summarize", "--json", cwd=repo)
    evidence = run_cli("evidence", cwd=repo)
    payload = json.loads(summary.stdout)

    assert payload["session_id"] == "summary-session"
    assert payload["event_counts"]["tool.call"] == 1
    assert payload["event_counts"]["tool.result"] == 1
    assert "tool.result python exit=0" in evidence.stdout


def test_review_and_memory_commands_can_rebuild_concurrently(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("session", "ensure", "--id", "concurrent-review", "--title", "Concurrent Review", cwd=repo)
    for index in range(6):
        body = tmp_path / f"body-{index}.txt"
        body.write_text(f"concurrent rebuild marker {index}", encoding="utf-8")
        run_cli("emit", "--type", "agent.message", "--body", str(body), cwd=repo)

    db_path = repo / ".agentdir" / "indexes" / "agentdir.sqlite3"
    if db_path.exists():
        db_path.unlink()

    commands = [
        ("summarize", "--json"),
        ("evidence",),
        ("memory", "search", "concurrent rebuild marker", "--json"),
    ]

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(lambda args: run_cli(*args, cwd=repo), commands))

    assert all(result.returncode == 0 for result in results)
    assert db_path.is_file()
