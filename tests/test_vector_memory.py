from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
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
    expected_returncode: int = 0,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_ROOT)
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


def write_body(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def index_path(root: Path) -> Path:
    return root / "indexes" / "agentdir.sqlite3"


def test_index_rebuild_creates_builtin_vector_memory_documents(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "body.txt", "database migration failure in account auth")

    run_cli("emit", "--root", str(root), "--session", "session-1", "--type", "agent.message", "--body", str(body))
    run_cli("index", "rebuild", "--root", str(root))

    with sqlite3.connect(index_path(root)) as conn:
        assert conn.execute("select value from metadata where key = 'vector_memory'").fetchone()[0] == "yes"
        assert conn.execute("select count(*) from memory_documents").fetchone()[0] == 2
        assert conn.execute(
            "select count(*) from memory_documents where source_kind = 'message'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "select count(*) from memory_documents where source_kind = 'session_summary'"
        ).fetchone()[0] == 1
        row = conn.execute(
            "select vector_dim, token_count, length(vector_json) from memory_documents"
        ).fetchone()

    assert row[0] == 256
    assert row[1] > 0
    assert row[2] > 0


def test_memory_search_auto_rebuilds_and_ranks_relevant_records(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    auth_body = write_body(
        tmp_path / "auth.txt",
        "The agent fixed an auth token database migration failure in the login service.",
    )
    ui_body = write_body(
        tmp_path / "ui.txt",
        "The agent adjusted button spacing and hover color on the dashboard.",
    )

    run_cli("emit", "--root", str(root), "--session", "auth-session", "--type", "agent.message", "--body", str(auth_body))
    run_cli("emit", "--root", str(root), "--session", "ui-session", "--type", "agent.message", "--body", str(ui_body))

    result = run_cli(
        "memory",
        "search",
        "--root",
        str(root),
        "--json",
        "auth token database migration",
    )
    rows = json.loads(result.stdout)

    assert rows[0]["session_id"] == "auth-session"
    assert rows[0]["memory_score"] > 0
    assert "auth token database migration" in rows[0]["body_text"]


def test_memory_search_can_return_derived_session_summaries(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    first = write_body(tmp_path / "first.txt", "checkout flow failed after auth redirect")
    second = write_body(tmp_path / "second.txt", "pytest checkout redirect regression passed")

    run_cli("emit", "--root", str(root), "--session", "checkout-session", "--type", "agent.message", "--body", str(first))
    run_cli("emit", "--root", str(root), "--session", "checkout-session", "--type", "tool.result", "--body", str(second))

    result = run_cli(
        "memory",
        "search",
        "--root",
        str(root),
        "--type",
        "summary.compacted",
        "--json",
        "checkout auth redirect regression",
    )
    rows = json.loads(result.stdout)

    assert rows[0]["source_kind"] == "session_summary"
    assert rows[0]["session_id"] == "checkout-session"
    assert "Session summary: checkout-session" in rows[0]["body_text"]


def test_memory_explain_reports_why_a_hit_matched(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "body.txt", "auth token database migration failure")
    run_cli("emit", "--root", str(root), "--session", "auth-session", "--type", "agent.message", "--body", str(body))

    search = run_cli("memory", "search", "--root", str(root), "--json", "auth database")
    source_id = json.loads(search.stdout)[0]["source_id"]
    explain = run_cli("memory", "explain", "--root", str(root), "--source", source_id, "--json", "auth database")
    payload = json.loads(explain.stdout)

    assert payload["source_id"] == source_id
    assert payload["memory_score"] > 0
    assert set(payload["overlap_terms"]) >= {"auth", "database"}
    assert "auth token database migration failure" in payload["excerpt"]


def test_query_semantic_uses_vector_memory_with_existing_filters(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    first = write_body(tmp_path / "first.txt", "pytest failure caused by sqlite index rebuild")
    second = write_body(tmp_path / "second.txt", "pytest failure caused by frontend route state")

    run_cli("emit", "--root", str(root), "--session", "backend", "--type", "tool.result", "--body", str(first))
    run_cli("emit", "--root", str(root), "--session", "frontend", "--type", "tool.result", "--body", str(second))

    result = run_cli(
        "query",
        "--root",
        str(root),
        "--session",
        "backend",
        "--type",
        "tool.result",
        "--semantic",
        "sqlite index failure",
        "--json",
    )
    rows = json.loads(result.stdout)

    assert [row["session_id"] for row in rows] == ["backend"]
    assert "sqlite index rebuild" in rows[0]["body_text"]
    assert rows[0]["memory_score"] > 0


def test_context_build_creates_agent_ready_context_pack(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    message = write_body(tmp_path / "message.txt", "auth login task context marker")
    evidence = write_body(tmp_path / "evidence.txt", "pytest auth login evidence passed")

    run_cli("session", "start", "--root", str(root), "--id", "context-session")
    run_cli("emit", "--root", str(root), "--session", "context-session", "--type", "agent.message", "--body", str(message))
    run_cli(
        "emit",
        "--root",
        str(root),
        "--session",
        "context-session",
        "--type",
        "tool.result",
        "--tool",
        "pytest",
        "--tool-exit-code",
        "0",
        "--body",
        str(evidence),
    )

    result = run_cli("context", "build", "--root", str(root), "--session", "context-session", "auth login")
    output = tmp_path / "context.md"
    written = run_cli(
        "context",
        "build",
        "--root",
        str(root),
        "--session",
        "context-session",
        "--output",
        str(output),
        "auth login",
    )

    assert "# AgentDir Context Pack" in result.stdout
    assert "auth login task context marker" in result.stdout
    assert "tool.result pytest exit=0" in result.stdout
    assert "Session summary: context-session" in result.stdout
    assert written.stdout.strip() == str(output)
    assert "# AgentDir Context Pack" in output.read_text(encoding="utf-8")
