from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from test_agent_first import SRC_ROOT, init_repo, query_rows, run_cli


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


def hook_test_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    wrapper = bin_dir / "agentdir"
    wrapper.write_text(
        f"#!/bin/sh\nexec {sys.executable} -m agentdir \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["PYTHONPATH"] = str(SRC_ROOT)
    env.update(extra)
    return env


def test_linked_worktree_shares_the_main_working_tree_store(tmp_path: Path) -> None:
    repo = adopted_repo(tmp_path)
    worktree = add_worktree(repo, tmp_path / "feature", "feature")

    resolved = run_cli("root", cwd=worktree).stdout.strip()

    assert Path(resolved) == (repo / ".agentdir").resolve()
    assert not (worktree / ".agentdir").exists()


def test_agent_bootstrap_probe_reuses_shared_store_without_adoption(tmp_path: Path) -> None:
    repo = adopted_repo(tmp_path)
    worktree = add_worktree(repo, tmp_path / "feature", "feature")
    before = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    probe = run_cli("root", "--require", "--quiet", cwd=worktree)
    started = json.loads(run_cli("work", "start", "linked checkout task", "--json", cwd=worktree).stdout)
    after = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert probe.stdout == ""
    assert started["session"]["title"] == "linked checkout task"
    assert before == after
    assert not (worktree / ".agentdir").exists()


def test_root_require_does_not_initialize_a_missing_store(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")

    result = run_cli("root", "--require", "--quiet", cwd=repo, expected_returncode=3)

    assert "Not an AgentDir root" in result.stderr
    assert not (repo / ".agentdir").exists()


def test_adopt_if_needed_reuses_shared_store_without_mutation(tmp_path: Path) -> None:
    repo = adopted_repo(tmp_path)
    worktree = add_worktree(repo, tmp_path / "feature", "feature")
    home = tmp_path / "home"
    home.mkdir()
    before = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    hook_before = (repo / ".git" / "hooks" / "pre-commit").read_bytes()

    planned = json.loads(
        run_cli(
            "adopt",
            "--if-needed",
            "--dry-run",
            "--json",
            cwd=worktree,
            env_extra={"HOME": str(home)},
        ).stdout
    )
    result = run_cli(
        "adopt",
        "--if-needed",
        "--json",
        cwd=worktree,
        env_extra={"HOME": str(home)},
    )
    payload = json.loads(result.stdout)
    after = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert payload["root"] == str((repo / ".agentdir").resolve())
    assert planned["already_adopted"] is True
    assert planned["hooks"] == []
    assert planned["integrations"] == []
    assert payload["already_adopted"] is True
    assert payload["changed"] is False
    assert before == after
    assert (repo / ".git" / "hooks" / "pre-commit").read_bytes() == hook_before
    assert not (worktree / ".agentdir").exists()
    assert not (home / ".codex" / "skills" / "agentdir" / "SKILL.md").exists()


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


def test_shared_hooks_path_routes_events_to_the_invoking_repository(tmp_path: Path) -> None:
    shared_hooks = tmp_path / "shared-hooks"
    shared_hooks.mkdir()
    repo_a = init_repo(tmp_path / "repo-a")
    repo_b = init_repo(tmp_path / "repo-b")
    for repo in (repo_a, repo_b):
        commit_initial(repo)
        subprocess.run(
            ["git", "config", "core.hooksPath", str(shared_hooks)],
            cwd=repo,
            check=True,
        )
        run_cli("adopt", "--gitignore", "none", "--install-skill", "store", cwd=repo)
    run_cli("work", "start", "repo A hook routing", "--no-context", cwd=repo_a)
    (repo_a / "routed.txt").write_text("route to repo A\n", encoding="utf-8")
    subprocess.run(["git", "add", "routed.txt"], cwd=repo_a, check=True)
    subprocess.run(
        ["git", "commit", "-m", "route hook"],
        cwd=repo_a,
        env=hook_test_env(tmp_path),
        check=True,
        capture_output=True,
        text=True,
    )
    run_cli("index", "update", cwd=repo_a)
    run_cli("index", "update", cwd=repo_b)

    events_a = query_rows(repo_a / ".agentdir", "git.hook.post-commit")
    events_b = query_rows(repo_b / ".agentdir", "git.hook.post-commit")
    assert len(events_a) == 1
    assert events_b == []


def test_shared_hooks_path_skips_an_unadopted_invoking_repository(tmp_path: Path) -> None:
    shared_hooks = tmp_path / "shared-hooks"
    shared_hooks.mkdir()
    repo_a = init_repo(tmp_path / "repo-a")
    repo_b = init_repo(tmp_path / "repo-b")
    for repo in (repo_a, repo_b):
        commit_initial(repo)
        subprocess.run(
            ["git", "config", "core.hooksPath", str(shared_hooks)],
            cwd=repo,
            check=True,
        )
    run_cli("adopt", "--gitignore", "none", "--install-skill", "store", cwd=repo_b)

    (repo_a / "unadopted.txt").write_text("do not cross stores\n", encoding="utf-8")
    subprocess.run(["git", "add", "unadopted.txt"], cwd=repo_a, check=True)
    subprocess.run(
        ["git", "commit", "-m", "unadopted commit"],
        cwd=repo_a,
        env=hook_test_env(tmp_path),
        check=True,
        capture_output=True,
        text=True,
    )
    run_cli("index", "update", cwd=repo_b)

    assert not (repo_a / ".agentdir").exists()
    assert query_rows(repo_b / ".agentdir", "git.hook.pre-commit") == []
    assert query_rows(repo_b / ".agentdir", "git.hook.post-commit") == []


def test_hook_respects_local_worktree_store_mode(tmp_path: Path) -> None:
    repo = adopted_repo(tmp_path)
    worktree = add_worktree(repo, tmp_path / "feature", "feature")
    local_env = {"AGENTDIR_WORKTREE_STORE": "local"}
    run_cli("init", cwd=worktree, env_extra=local_env)
    run_cli("work", "start", "local worktree hook routing", "--no-context", cwd=worktree, env_extra=local_env)
    (worktree / "local.txt").write_text("local worktree event\n", encoding="utf-8")
    subprocess.run(["git", "add", "local.txt"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-m", "local hook"],
        cwd=worktree,
        env=hook_test_env(tmp_path, AGENTDIR_WORKTREE_STORE="local"),
        check=True,
        capture_output=True,
        text=True,
    )
    run_cli("index", "update", cwd=worktree, env_extra=local_env)
    run_cli("index", "update", cwd=repo)

    local_events = query_rows(worktree / ".agentdir", "git.hook.post-commit")
    main_events = query_rows(repo / ".agentdir", "git.hook.post-commit")
    assert len(local_events) == 1
    assert main_events == []


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
