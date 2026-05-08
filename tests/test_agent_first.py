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
    assert "agentdir session ensure" in skill_text
    assert "agentdir run -- <command>" in skill_text
    assert "Do not wrap routine exploration commands" in skill_text
    assert (repo / ".git" / "hooks" / "pre-commit").is_file()


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
