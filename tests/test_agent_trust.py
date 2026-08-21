from __future__ import annotations

import json
import os
import subprocess
import sys
import time
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
    input_text: str | None = None,
    expected_returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_ROOT)
    result = subprocess.run(
        [sys.executable, "-m", "agentdir", *args],
        cwd=cwd or PROJECT_ROOT,
        env=env,
        input=input_text,
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


# --- exit-code taxonomy -----------------------------------------------------


def test_missing_root_exits_3_with_recovery_hint(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")

    result = run_cli("evidence", "--session", "sess-x", cwd=repo, expected_returncode=3)

    assert "Not an AgentDir root" in result.stderr
    assert "agentdir adopt" in result.stderr
    assert "agentdir init" in result.stderr


def test_missing_session_exits_3_with_recovery_hint(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("init", cwd=repo)

    result = run_cli("summarize", cwd=repo, expected_returncode=3)

    assert "No active AgentDir session" in result.stderr
    assert "agentdir work start" in result.stderr


def test_unsupported_store_version_exits_5(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("init", cwd=repo)
    (repo / ".agentdir" / "VERSION").write_text("99.0\n", encoding="utf-8")

    result = run_cli("session", "start", cwd=repo, expected_returncode=5)

    assert "Unsupported AgentDir root version" in result.stderr


def test_user_error_still_exits_2(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")

    result = run_cli("run", cwd=repo, expected_returncode=2)

    assert "Usage: agentdir run" in result.stderr


def test_json_error_envelope_on_failure(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")

    result = run_cli("evidence", "--session", "sess-x", "--json", cwd=repo, expected_returncode=3)
    payload = json.loads(result.stdout)

    assert payload["success"] is False
    assert payload["exit_code"] == 3
    assert payload["error_code"] == "AgentDirStateError"
    assert "Not an AgentDir root" in payload["error"]


# --- --quiet ----------------------------------------------------------------


def test_quiet_suppresses_stdout_keeps_exit_code(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "quiet check", cwd=repo)

    ok = run_cli("status", "--quiet", "--no-rebuild", cwd=repo)
    assert ok.stdout == ""

    missing = init_repo(tmp_path / "empty")
    failed = run_cli("evidence", "--session", "sess-x", "--quiet", cwd=missing, expected_returncode=3)
    assert failed.stdout == ""
    assert "Not an AgentDir root" in failed.stderr


# --- --json coverage --------------------------------------------------------


def test_emit_actor_send_hooks_record_json(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "json coverage", cwd=repo)

    emitted = json.loads(
        run_cli("emit", "--type", "note.test", "--body", "-", "--json", cwd=repo, input_text="hello").stdout
    )
    assert emitted["event_type"] == "note.test"
    assert emitted["session_id"]
    assert Path(emitted["path"]).is_file()

    actor = json.loads(run_cli("actor", "create", "engineer", "--json", cwd=repo).stdout)
    assert actor["actor_id"] == "engineer"
    assert Path(actor["inbox"]).is_dir()

    run_cli("actor", "create", "codex", cwd=repo)
    sent = json.loads(
        run_cli(
            "send",
            "--from",
            "codex",
            "--to",
            "engineer",
            "--type",
            "approval.requested",
            "--body",
            "-",
            "--json",
            cwd=repo,
            input_text="please review",
        ).stdout
    )
    assert sent["from"] == "codex"
    assert sent["to"] == "engineer"
    assert Path(sent["inbox"]).is_file()

    recorded = json.loads(
        run_cli(
            "hooks",
            "record",
            "--hook",
            "post-commit",
            "--original-exit-code",
            "0",
            "--json",
            cwd=repo,
        ).stdout
    )
    assert recorded == {"recorded": True, "hook": "post-commit", "original_exit_code": 0}


def test_replay_json(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "replay json", cwd=repo)
    run_cli("run", "--name", "pytest", "--", sys.executable, "-c", "print('ok')", cwd=repo)
    session_id = run_cli("session", "current", cwd=repo).stdout.strip()

    rows = json.loads(run_cli("replay", "--session", session_id, "--json", cwd=repo).stdout)

    assert isinstance(rows, list) and rows
    types = {row["event_type"] for row in rows}
    assert {"tool.call", "tool.result"} <= types


def parse_trailing_json(stdout: str) -> dict[str, object]:
    lines = stdout.splitlines()
    start = lines.index("{")
    return json.loads("\n".join(lines[start:]))


def test_run_json_summary(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "run json", cwd=repo)

    result = run_cli(
        "run",
        "--name",
        "pytest",
        "--json",
        "--",
        sys.executable,
        "-c",
        "print('tests passed')",
        cwd=repo,
    )
    assert result.stdout.startswith("tests passed")
    summary = parse_trailing_json(result.stdout)

    assert summary["exit_code"] == 0
    assert summary["tool"] == "pytest"
    assert summary["timed_out"] is False
    assert summary["truncated_streams"] == []
    assert Path(summary["event_path"]).is_file()


def test_context_cite_json(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "cite json", cwd=repo)
    run_cli("run", "--name", "pytest", "--", sys.executable, "-c", "print('ok')", cwd=repo)
    emitted = json.loads(run_cli("context", "build", "cite json", "--emit", "--json", cwd=repo).stdout)
    pack_id = emitted["manifest"]["pack_id"]
    source_id = emitted["manifest"]["sources"][0]["source_id"]
    run_cli(
        "context",
        "consume",
        "--pack",
        pack_id,
        "--source",
        source_id,
        "--purpose",
        "answer",
        cwd=repo,
    )

    citation = json.loads(
        run_cli("context", "cite", "--pack", pack_id, "--source", source_id, "--json", cwd=repo).stdout
    )

    assert citation["pack_id"] == pack_id
    assert citation["rendered"]


# --- run --timeout ----------------------------------------------------------


def test_run_timeout_kills_and_records(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "timeout check", cwd=repo)

    start = time.monotonic()
    result = run_cli(
        "run",
        "--name",
        "sleeper",
        "--timeout",
        "1",
        "--json",
        "--",
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        cwd=repo,
        expected_returncode=124,
    )
    elapsed = time.monotonic() - start

    assert elapsed < 15, f"timeout did not kill promptly ({elapsed:.1f}s)"
    assert "timed out after 1.0s" in result.stderr
    summary = parse_trailing_json(result.stdout)
    assert summary["timed_out"] is True
    assert summary["exit_code"] == 124

    rows = json.loads(run_cli("evidence", "--json", cwd=repo).stdout)
    timed_out_rows = [row for row in rows if row.get("tool") == "sleeper" and row["event_type"] == "tool.result"]
    assert timed_out_rows and timed_out_rows[-1]["tool_exit_code"] == 124


# --- run --session modes ----------------------------------------------------


def test_run_session_require_fails_without_session(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("init", cwd=repo)

    result = run_cli(
        "run",
        "--session",
        "require",
        "--",
        sys.executable,
        "-c",
        "print('nope')",
        cwd=repo,
        expected_returncode=3,
    )
    assert "No active AgentDir session" in result.stderr


def test_run_session_require_uses_active_session(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "require mode", cwd=repo)
    session_id = run_cli("session", "current", cwd=repo).stdout.strip()

    result = run_cli(
        "run",
        "--session",
        "require",
        "--json",
        "--",
        sys.executable,
        "-c",
        "print('ok')",
        cwd=repo,
    )
    summary = parse_trailing_json(result.stdout)
    assert summary["session_id"] == session_id


def test_run_session_create_starts_fresh_session(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "create mode", cwd=repo)
    before = run_cli("session", "current", cwd=repo).stdout.strip()

    result = run_cli(
        "run",
        "--session",
        "create",
        "--json",
        "--",
        sys.executable,
        "-c",
        "print('ok')",
        cwd=repo,
    )
    summary = parse_trailing_json(result.stdout)
    assert summary["session_id"] != before


# --- incremental index ------------------------------------------------------


def test_index_update_is_incremental(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "incremental", cwd=repo)
    run_cli("index", "rebuild", cwd=repo)

    unchanged = json.loads(run_cli("index", "update", "--json", cwd=repo).stdout)
    assert unchanged["indexed"] == 0

    run_cli("emit", "--type", "note.test", "--body", "-", cwd=repo, input_text="fresh note")
    first = json.loads(run_cli("index", "update", "--json", cwd=repo).stdout)
    assert first["indexed"] == 1

    again = json.loads(run_cli("index", "update", "--json", cwd=repo).stdout)
    assert again["indexed"] == 0


def test_incremental_update_serves_memory_search(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "memory freshness", cwd=repo)
    run_cli("index", "rebuild", cwd=repo)
    run_cli(
        "emit",
        "--type",
        "note.test",
        "--body",
        "-",
        cwd=repo,
        input_text="zanzibar checkout regression reproduced",
    )

    hits = json.loads(run_cli("memory", "search", "zanzibar checkout regression", "--json", cwd=repo).stdout)
    assert any("zanzibar" in (hit.get("body_text") or "") for hit in hits)


def test_incremental_update_drops_pruned_sessions(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("session", "start", "--id", "old-session", cwd=repo)
    run_cli("emit", "--type", "note.test", "--body", "-", "--session", "old-session", cwd=repo, input_text="old data")
    run_cli("session", "end", cwd=repo)
    run_cli("session", "start", "--id", "new-session", cwd=repo)
    run_cli("index", "rebuild", cwd=repo)

    run_cli("prune", "--session", "old-session", "--include-live-sessions", "--apply", "--json", cwd=repo)

    rows = json.loads(
        run_cli("query", "--session", "old-session", "--json", cwd=repo).stdout
    )
    assert rows == []


# --- federation registry lock ----------------------------------------------


def test_concurrent_root_registration_loses_nothing(tmp_path: Path) -> None:
    controller = init_repo(tmp_path / "controller")
    run_cli("init", cwd=controller)
    children = []
    for index in range(6):
        child = init_repo(tmp_path / f"child-{index}")
        run_cli("init", cwd=child)
        children.append(child)

    def register(child: Path) -> None:
        run_cli("roots", "register", str(child / ".agentdir"), "--name", child.name, cwd=controller)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(register, children))

    roots = json.loads(run_cli("roots", "list", "--json", cwd=controller).stdout)
    assert len(roots) == 6
