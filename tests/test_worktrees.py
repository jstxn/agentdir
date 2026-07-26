from __future__ import annotations

import json
import subprocess
from pathlib import Path

from test_agent_first import init_repo, run_cli


def commit_initial(repo: Path) -> None:
    (repo / "file.txt").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)


def add_worktree(repo: Path, path: Path, branch: str) -> Path:
    subprocess.run(
        ["git", "worktree", "add", str(path), "-b", branch],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return path


def adopted_repo(tmp_path: Path, name: str = "main") -> Path:
    repo = init_repo(tmp_path / name)
    commit_initial(repo)
    run_cli("adopt", "--gitignore", "none", "--install-skill", "store", cwd=repo)
    return repo


def test_linked_worktree_shares_the_main_working_tree_store(tmp_path: Path) -> None:
    repo = adopted_repo(tmp_path)
    worktree = add_worktree(repo, tmp_path / "feature", "feature")

    resolved = run_cli("root", cwd=worktree).stdout.strip()

    assert Path(resolved) == (repo / ".agentdir").resolve()
    assert not (worktree / ".agentdir").exists()


def test_worktree_memory_search_sees_main_working_tree_sessions(tmp_path: Path) -> None:
    repo = adopted_repo(tmp_path)
    run_cli("work", "start", "checkout latency regression", cwd=repo)
    worktree = add_worktree(repo, tmp_path / "feature", "feature")

    found = run_cli("memory", "search", "checkout latency regression", cwd=worktree).stdout

    assert "checkout latency regression" in found


def test_parallel_worktree_sessions_do_not_overwrite_each_other(tmp_path: Path) -> None:
    repo = adopted_repo(tmp_path)
    worktree = add_worktree(repo, tmp_path / "feature", "feature")

    run_cli("work", "start", "main tree task", cwd=repo)
    run_cli("work", "start", "feature branch task", cwd=worktree)

    main_session = json.loads(run_cli("session", "current", "--json", cwd=repo).stdout)
    worktree_session = json.loads(run_cli("session", "current", "--json", cwd=worktree).stdout)

    assert main_session["title"] == "main tree task"
    assert worktree_session["title"] == "feature branch task"
    assert main_session["session_id"] != worktree_session["session_id"]


def test_worktree_store_mode_local_keeps_a_separate_store(tmp_path: Path) -> None:
    repo = adopted_repo(tmp_path)
    worktree = add_worktree(repo, tmp_path / "feature", "feature")

    resolved = run_cli(
        "root",
        cwd=worktree,
        env_extra={"AGENTDIR_WORKTREE_STORE": "local"},
    ).stdout.strip()

    assert Path(resolved) == (worktree / ".agentdir").resolve()


def test_existing_worktree_store_still_wins_over_the_main_one(tmp_path: Path) -> None:
    repo = adopted_repo(tmp_path)
    worktree = add_worktree(repo, tmp_path / "feature", "feature")
    run_cli("init", cwd=worktree, env_extra={"AGENTDIR_WORKTREE_STORE": "local"})

    resolved = run_cli("root", cwd=worktree).stdout.strip()

    assert Path(resolved) == (worktree / ".agentdir").resolve()


def test_doctor_warns_when_a_worktree_store_is_split_from_the_main_one(tmp_path: Path) -> None:
    repo = adopted_repo(tmp_path)
    worktree = add_worktree(repo, tmp_path / "feature", "feature")
    run_cli("init", cwd=worktree, env_extra={"AGENTDIR_WORKTREE_STORE": "local"})

    report = json.loads(run_cli("doctor", "--json", cwd=worktree).stdout)

    assert any("separate from the main working tree store" in w for w in report["warnings"])


def test_status_renders_once(tmp_path: Path) -> None:
    repo = adopted_repo(tmp_path)

    output = run_cli("status", cwd=repo).stdout

    assert output.count("AgentDir Status") == 1


def test_doctor_renders_once(tmp_path: Path) -> None:
    repo = adopted_repo(tmp_path)

    output = run_cli("doctor", cwd=repo).stdout

    assert output.count("AgentDir Doctor") == 1


def test_invalid_worktree_store_mode_is_reported(tmp_path: Path) -> None:
    repo = adopted_repo(tmp_path)

    result = run_cli(
        "status",
        cwd=repo,
        env_extra={"AGENTDIR_WORKTREE_STORE": "locl"},
        expected_returncode=5,
    )

    assert "AGENTDIR_WORKTREE_STORE" in result.stderr


def test_worktree_ignores_a_main_directory_that_is_not_a_store(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "main")
    commit_initial(repo)
    (repo / ".agentdir").mkdir()
    worktree = add_worktree(repo, tmp_path / "feature", "feature")

    resolved = run_cli("root", cwd=worktree).stdout.strip()

    assert Path(resolved) == (worktree / ".agentdir").resolve()
