from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def find_project_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError("could not locate project root")


PROJECT_ROOT = find_project_root()
SRC_ROOT = PROJECT_ROOT / "src"


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


def test_cli_version_reports_package_version() -> None:
    result = run_cli("--version")

    assert result.stdout.strip() == "agentdir 0.7.6"


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
    assert 'agentdir work start "<short task>" --emit-context' in skill_text
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
    real_rebuild = control.rebuild_index

    def counted_rebuild(root: str | Path) -> None:
        nonlocal calls
        calls += 1
        real_rebuild(root)

    monkeypatch.setattr(control, "rebuild_index", counted_rebuild)
    monkeypatch.setattr(context, "rebuild_index", counted_rebuild)
    monkeypatch.setattr(review, "rebuild_index", counted_rebuild)

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
    real_rebuild = control.rebuild_index

    def counted_rebuild(root: str | Path) -> None:
        nonlocal calls
        calls += 1
        real_rebuild(root)

    monkeypatch.setattr(control, "rebuild_index", counted_rebuild)
    monkeypatch.setattr(context, "rebuild_index", counted_rebuild)
    monkeypatch.setattr(review, "rebuild_index", counted_rebuild)

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
