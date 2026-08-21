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


def test_same_named_worktrees_keep_separate_active_and_completed_sessions(
    tmp_path: Path,
) -> None:
    main_parent = tmp_path / "main-parent"
    linked_parent = tmp_path / "linked-parent"
    main_parent.mkdir()
    linked_parent.mkdir()
    repo = adopted_repo(main_parent, name="agentdir")
    worktree = add_worktree(repo, linked_parent / "agentdir", "feature")

    main_started = json.loads(
        run_cli("work", "start", "main same-name task", "--no-context", "--json", cwd=repo).stdout
    )
    linked_started = json.loads(
        run_cli(
            "work",
            "start",
            "linked same-name task",
            "--no-context",
            "--json",
            cwd=worktree,
        ).stdout
    )

    main_current = json.loads(run_cli("session", "current", "--json", cwd=repo).stdout)
    linked_current = json.loads(
        run_cli("session", "current", "--json", cwd=worktree).stdout
    )
    assert main_current["session_id"] == main_started["session"]["session_id"]
    assert linked_current["session_id"] == linked_started["session"]["session_id"]

    run_cli("work", "finish", "--json", cwd=repo)
    run_cli("work", "finish", "--json", cwd=worktree)

    main_status = json.loads(run_cli("status", "--json", cwd=repo).stdout)
    linked_status = json.loads(run_cli("status", "--json", cwd=worktree).stdout)
    assert main_status["session"]["latest"]["session_id"] == main_started["session"]["session_id"]
    assert linked_status["session"]["latest"]["session_id"] == linked_started["session"]["session_id"]


def test_linked_worktree_move_preserves_current_final_head_and_last_session(
    tmp_path: Path,
) -> None:
    repo = adopted_repo(tmp_path)
    worktree = add_worktree(repo, tmp_path / "before", "feature")
    started = json.loads(
        run_cli("work", "start", "moved worktree task", "--no-context", "--json", cwd=worktree).stdout
    )
    moved = tmp_path / "after"
    subprocess.run(
        ["git", "worktree", "move", str(worktree), str(moved)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    current = json.loads(run_cli("session", "current", "--json", cwd=moved).stdout)
    tracked = moved / "moved.txt"
    tracked.write_text("moved worktree\n", encoding="utf-8")
    subprocess.run(["git", "add", "moved.txt"], cwd=moved, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Move worktree"],
        cwd=moved,
        check=True,
        capture_output=True,
    )
    moved_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=moved,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    caller = tmp_path / "caller"
    caller.mkdir()
    finished = json.loads(
        run_cli(
            "work",
            "finish",
            "--root",
            str(repo / ".agentdir"),
            "--json",
            cwd=caller,
        ).stdout
    )
    latest = json.loads(run_cli("status", "--json", cwd=moved).stdout)["session"]["latest"]

    assert current["session_id"] == started["session"]["session_id"]
    assert finished["report"]["git"]["head"] == moved_head
    assert finished["ended_session"]["git_head"] == moved_head
    assert latest["session_id"] == started["session"]["session_id"]


def test_long_same_named_repositories_keep_separate_user_scope_sessions(tmp_path: Path) -> None:
    first_parent = tmp_path / "first-parent"
    second_parent = tmp_path / "second-parent"
    first_parent.mkdir()
    second_parent.mkdir()
    name = "same-" + ("x" * 145)
    first = init_repo(first_parent / name)
    second = init_repo(second_parent / name)
    commit_initial(first)
    commit_initial(second)
    env = {"AGENTDIR_USER_ROOT": str(tmp_path / "user-store")}

    first_started = json.loads(
        run_cli(
            "work",
            "start",
            "first user task",
            "--scope",
            "user",
            "--no-context",
            "--json",
            cwd=first,
            env_extra=env,
        ).stdout
    )
    second_started = json.loads(
        run_cli(
            "work",
            "start",
            "second user task",
            "--scope",
            "user",
            "--no-context",
            "--json",
            cwd=second,
            env_extra=env,
        ).stdout
    )
    first_current = json.loads(
        run_cli("session", "current", "--scope", "user", "--json", cwd=first, env_extra=env).stdout
    )
    second_current = json.loads(
        run_cli(
            "session",
            "current",
            "--scope",
            "user",
            "--json",
            cwd=second,
            env_extra=env,
        ).stdout
    )

    assert first_started["session"]["session_id"] != second_started["session"]["session_id"]
    assert first_current["session_id"] == first_started["session"]["session_id"]
    assert second_current["session_id"] == second_started["session"]["session_id"]


def test_legacy_basename_pointer_is_used_only_when_unambiguous(tmp_path: Path) -> None:
    main_parent = tmp_path / "main-parent"
    linked_parent = tmp_path / "linked-parent"
    main_parent.mkdir()
    linked_parent.mkdir()
    repo = adopted_repo(main_parent, name="same")
    started = json.loads(
        run_cli("work", "start", "legacy pointer task", "--no-context", "--json", cwd=repo).stdout
    )
    workspaces = repo / ".agentdir" / "state" / "workspaces"
    canonical = next(workspaces.glob("checkout-main-*/current-session.json"))
    canonical.unlink()

    current = json.loads(run_cli("session", "current", "--json", cwd=repo).stdout)
    worktree = add_worktree(repo, linked_parent / "same", "feature")
    ambiguous = run_cli(
        "session",
        "current",
        "--json",
        cwd=worktree,
        expected_returncode=2,
    )

    assert current["session_id"] == started["session"]["session_id"]
    assert "No active AgentDir session" in ambiguous.stderr


def test_explicit_root_refuses_to_guess_between_active_worktrees(tmp_path: Path) -> None:
    repo = adopted_repo(tmp_path)
    worktree = add_worktree(repo, tmp_path / "feature", "feature")
    caller = tmp_path / "caller"
    caller.mkdir()

    run_cli("work", "start", "main ambiguous task", "--no-context", cwd=repo)
    run_cli("work", "start", "linked ambiguous task", "--no-context", cwd=worktree)

    result = run_cli(
        "session",
        "current",
        "--root",
        str(repo / ".agentdir"),
        cwd=caller,
        expected_returncode=3,
    )

    assert "Multiple active sessions" in result.stderr
    assert "owning checkout" in result.stderr


def test_explicit_root_finish_uses_the_linked_worktree_checkout(tmp_path: Path) -> None:
    main_parent = tmp_path / "main-parent"
    linked_parent = tmp_path / "linked-parent"
    main_parent.mkdir()
    linked_parent.mkdir()
    repo = adopted_repo(main_parent, name="agentdir")
    worktree = add_worktree(repo, linked_parent / "agentdir", "feature")
    caller = tmp_path / "caller"
    caller.mkdir()

    run_cli("work", "start", "linked provenance task", "--no-context", cwd=worktree)
    tracked = worktree / "linked.txt"
    tracked.write_text("linked implementation\n", encoding="utf-8")
    subprocess.run(["git", "add", "linked.txt"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Linked implementation"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    linked_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    main_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    finished = json.loads(
        run_cli(
            "work",
            "finish",
            "--root",
            str(repo / ".agentdir"),
            "--json",
            cwd=caller,
        ).stdout
    )

    assert linked_sha != main_sha
    assert finished["report"]["git"]["head"] == linked_sha
    assert finished["ended_session"]["git_head"] == linked_sha


def test_post_finish_latest_session_reads_stay_scoped_to_each_worktree(tmp_path: Path) -> None:
    repo = adopted_repo(tmp_path)
    worktree = add_worktree(repo, tmp_path / "feature", "feature")

    main_started = json.loads(
        run_cli("work", "start", "main completed task", "--no-context", "--json", cwd=repo).stdout
    )
    run_cli(
        "run",
        "--name",
        "pytest-main",
        "--",
        sys.executable,
        "-c",
        "print('main passed')",
        cwd=repo,
    )
    run_cli("work", "finish", "--json", cwd=repo)

    feature_started = json.loads(
        run_cli(
            "work",
            "start",
            "feature completed task",
            "--no-context",
            "--json",
            cwd=worktree,
        ).stdout
    )
    run_cli(
        "run",
        "--name",
        "pytest-feature",
        "--",
        sys.executable,
        "-c",
        "print('feature passed')",
        cwd=worktree,
    )
    run_cli("work", "finish", "--json", cwd=worktree)

    main_status = json.loads(run_cli("status", "--json", cwd=repo).stdout)
    feature_status = json.loads(run_cli("status", "--json", cwd=worktree).stdout)
    main_brief = json.loads(run_cli("evidence", "--brief", "--json", cwd=repo).stdout)
    feature_brief = json.loads(
        run_cli("evidence", "--brief", "--json", cwd=worktree).stdout
    )

    assert main_status["session"]["latest"]["session_id"] == main_started["session"]["session_id"]
    assert feature_status["session"]["latest"]["session_id"] == feature_started["session"]["session_id"]
    assert main_brief["latest_by_family"]["test"]["tool"] == "pytest-main"
    assert feature_brief["latest_by_family"]["test"]["tool"] == "pytest-feature"


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
