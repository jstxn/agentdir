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
        assert conn.execute("select count(*) from memory_documents").fetchone()[0] == 1
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
