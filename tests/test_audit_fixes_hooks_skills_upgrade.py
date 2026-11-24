from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import agentdir.git as git_module
import agentdir.hooks as hooks_module
import agentdir.skills as skills_module
import agentdir.upgrade as upgrade_module
from agentdir.gitignore import ensure_agentdir_ignored, gitignore_plan
from agentdir.hooks import (
    _hook_script,
    _validate_hook_name,
    hook_status,
    hooks_manifest_issues,
    hooks_manifest_path,
    install_hooks,
    preflight_install_hooks,
    record_hook_event,
    uninstall_hooks,
)
from agentdir.sessions import read_current_session, start_session
from agentdir.skills import (
    CODEX_SKILL,
    _ensure_within_project,
    _integration_managed,
    codex_skill_path_no_create,
    extract_managed_block,
    generic_guidance_path_no_create,
    install_codex_skill,
    install_generic_guidance,
    install_integrations,
    is_agentdir_managed_generic,
    merge_managed_block,
    remove_managed_block,
    uninstall_integrations,
)
from agentdir.store import AgentDirDependencyError, AgentDirError, init_root
from agentdir.upgrade import UpgradeOptions, _version_matches, format_upgrade_result, upgrade_agentdir


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"


def _clean_git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    return env


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    env = _clean_git_env()
    subprocess.run(["git", "init"], cwd=path, env=env, check=True, text=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=path, env=env, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, env=env, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=path, env=env, check=True)
    return path


def _run_cli(
    *args: str,
    cwd: Path,
    env_extra: dict[str, str] | None = None,
    expected_returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_ROOT)
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(
        [sys.executable, "-m", "agentdir", *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == expected_returncode, (
        f"expected {expected_returncode}, got {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def _expect_raises(exc_type: type[BaseException], fn, *args, **kwargs) -> BaseException:
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        return exc
    raise AssertionError(f"expected {exc_type.__name__} to be raised")


def test_hook_name_validation_rejects_traversal_and_shell_metacharacters(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    store = repo / ".agentdir"
    bad_names = [
        "",
        ".",
        "..",
        "../evil",
        "../../etc/passwd",
        "pre..commit",
        "a/b",
        "pre-commit; touch pwned",
        "pre-commit$(id)",
        'pre"commit',
        "pre`id`",
        "pre commit",
    ]
    for bad in bad_names:
        _expect_raises(AgentDirError, _validate_hook_name, bad)
        _expect_raises(AgentDirError, preflight_install_hooks, cwd=repo, hooks=[bad])
        _expect_raises(AgentDirError, install_hooks, store, cwd=repo, hooks=[bad])
        _expect_raises(AgentDirError, uninstall_hooks, cwd=repo, hooks=[bad], root=store)
        _expect_raises(AgentDirError, hook_status, cwd=repo, hooks=[bad])
        _expect_raises(
            AgentDirError,
            record_hook_event,
            store,
            hook=bad,
            original_exit_code=0,
            cwd=repo,
        )
    assert _validate_hook_name("pre-commit") == "pre-commit"
    assert _validate_hook_name("post_merge.v1") == "post_merge.v1"
    assert not (tmp_path / "evil").exists()


def test_hooks_manifest_issues_flags_and_skips_invalid_hook_names(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    store = repo / ".agentdir"
    install_hooks(store, cwd=repo, hooks=["pre-commit"])

    outside = tmp_path / "outside-secret.txt"
    outside.write_text("# AgentDir managed hook\n", encoding="utf-8")

    manifest_path = hooks_manifest_path(store)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["hooks"] = ["../../outside-secret.txt", "pre-commit;id", "pre-commit"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    issues = hooks_manifest_issues(store)
    assert any("invalid hook name" in issue and "outside-secret.txt" in issue for issue in issues)
    assert any("invalid hook name" in issue and "pre-commit;id" in issue for issue in issues)
    assert not any("pre-commit is missing" in issue for issue in issues)


def test_hook_script_quotes_name_and_original_path_against_shell_expansion(tmp_path: Path) -> None:
    pwned = tmp_path / "pwned"
    weird_dir = tmp_path / f"repo-$(touch {pwned.name})-`touch {pwned.name}`"
    repo = _init_repo(weird_dir)
    store = repo / ".agentdir"
    hooks_dir = repo / ".git" / "hooks"
    original = hooks_dir / "pre-commit"
    marker = repo / "original-ran"
    original.write_text(f"#!/bin/sh\necho ok > '{marker}'\nexit 0\n", encoding="utf-8")
    original.chmod(0o755)

    install_hooks(store, cwd=repo, hooks=["pre-commit"])
    script_path = hooks_dir / "pre-commit"
    script_text = script_path.read_text(encoding="utf-8")
    assert 'original="' not in script_text
    assert 'hook_name="' not in script_text

    # Execute the installed shim directly under /bin/sh with a clean PATH lacking agentdir.
    result = subprocess.run(
        ["/bin/sh", str(script_path)],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8").strip() == "ok"
    assert not pwned.exists()

    # Verify _hook_script rejects invalid hook names directly as well.
    _expect_raises(AgentDirError, _hook_script, 'bad"name', original, store)


def test_record_hook_event_does_not_end_concurrent_replacement_session(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    store = repo / ".agentdir"
    init_root(store)

    real_emit = hooks_module.emit_event
    replacement_id: dict[str, str] = {}

    def emit_with_concurrent_session(*args, **kwargs):
        result = real_emit(*args, **kwargs)
        replacement = start_session(
            store,
            session_id="concurrent-active-session",
            title="Concurrent work",
            cwd=repo,
        )
        replacement_id["id"] = replacement.session_id
        return result

    with patch.object(hooks_module, "emit_event", side_effect=emit_with_concurrent_session):
        record_hook_event(store, hook="pre-commit", original_exit_code=0, cwd=repo)

    current = read_current_session(store)
    assert current is not None
    assert current.session_id == replacement_id["id"] == "concurrent-active-session"
    assert current.status == "active"


def test_record_hook_event_passes_cwd_and_expected_session_id(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    store = repo / ".agentdir"
    init_root(store)

    seen_read_cwd: list[object] = []
    seen_end_kwargs: list[dict[str, object]] = []

    def fake_read(root, *, cwd=None):
        seen_read_cwd.append(cwd)
        return None

    def fake_end(root, **kwargs):
        seen_end_kwargs.append(kwargs)
        return None

    with (
        patch.object(hooks_module, "read_current_session", side_effect=fake_read),
        patch.object(hooks_module, "end_session", side_effect=fake_end),
    ):
        record_hook_event(store, hook="post-commit", original_exit_code=0, cwd=repo)

    assert seen_read_cwd == [repo]
    assert len(seen_end_kwargs) == 1
    assert seen_end_kwargs[0]["cwd"] == repo
    assert seen_end_kwargs[0]["expected_session_id"]


def test_record_hook_event_respects_worktree_cwd_from_different_directory(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    env = _clean_git_env()
    subprocess.run(["git", "add", "README.md"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, env=env, check=True, capture_output=True)

    worktree = tmp_path / "feature-wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature", str(worktree)],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
    )
    store = repo / ".agentdir"
    init_root(store)
    wt_session = start_session(store, session_id="wt-active", title="Worktree task", cwd=worktree)

    old_cwd = Path.cwd()
    os.chdir(repo)
    try:
        record_hook_event(store, hook="pre-commit", original_exit_code=0, cwd=worktree)
    finally:
        os.chdir(old_cwd)

    # Active session in the worktree must still be active and unchanged.
    assert hooks_module._read_current_session_for_cwd(store, cwd=worktree).session_id == wt_session.session_id  # type: ignore[union-attr]


def test_skills_reject_symlinks_across_leaf_parent_dangling_backup_and_uninstall(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    store = repo / ".agentdir"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_target = outside / "target.md"
    outside_target.write_text("do not modify\n", encoding="utf-8")

    # 1. Symlinked leaf for generic guidance.
    (repo / "AGENTS.md").symlink_to(outside_target)
    _expect_raises(AgentDirError, install_generic_guidance, store, target="project", cwd=repo)
    assert outside_target.read_text(encoding="utf-8") == "do not modify\n"
    (repo / "AGENTS.md").unlink()

    # 2. Dangling symlink for claude integration.
    dangling_target = outside / "dangling-claude.md"
    (repo / "CLAUDE.md").symlink_to(dangling_target)
    _expect_raises(AgentDirError, install_integrations, store, ["claude"], target="project", cwd=repo)
    assert not dangling_target.exists()
    (repo / "CLAUDE.md").unlink()

    # 3. Symlinked parent directory for cursor integration.
    (repo / ".cursor").symlink_to(outside)
    _expect_raises(AgentDirError, install_integrations, store, ["cursor"], target="project", cwd=repo)
    assert not (outside / "rules").exists()
    (repo / ".cursor").unlink()

    # 4. Symlinked leaf for project codex skill.
    skill_dir = repo / ".agents" / "skills" / "agentdir"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").symlink_to(outside_target)
    _expect_raises(AgentDirError, install_codex_skill, store, target="project", cwd=repo)
    assert outside_target.read_text(encoding="utf-8") == "do not modify\n"
    (skill_dir / "SKILL.md").unlink()

    # 5. Symlinked .bak backup file for generic guidance (force=True) and codex skill.
    (repo / "AGENTS.md").write_text("# Local notes\n", encoding="utf-8")
    (repo / "AGENTS.md.bak").symlink_to(outside_target)
    _expect_raises(
        AgentDirError,
        install_generic_guidance,
        store,
        target="project",
        force=True,
        cwd=repo,
    )
    assert outside_target.read_text(encoding="utf-8") == "do not modify\n"
    (repo / "AGENTS.md.bak").unlink()

    (skill_dir / "SKILL.md").write_text("---\nname: agentdir\n---\n# AgentDir\nold\n", encoding="utf-8")
    (skill_dir / "SKILL.md.bak").symlink_to(outside_target)
    _expect_raises(AgentDirError, install_codex_skill, store, target="project", cwd=repo)
    assert outside_target.read_text(encoding="utf-8") == "do not modify\n"
    (skill_dir / "SKILL.md.bak").unlink()

    # 6. Symlinked file on uninstall_integrations must refuse and never unlink target.
    outside_managed = outside / "managed-claude.md"
    outside_managed.write_text(
        "<!-- agentdir-managed-claude:start -->\nhi\n<!-- agentdir-managed-claude:end -->\n",
        encoding="utf-8",
    )
    (repo / "CLAUDE.md").symlink_to(outside_managed)
    _expect_raises(
        AgentDirError,
        uninstall_integrations,
        store,
        ["claude"],
        target="project",
        apply=True,
        cwd=repo,
    )
    assert outside_managed.is_file()
    (repo / "CLAUDE.md").unlink()

    # 7. Direct parent-directory traversal escape check.
    _expect_raises(AgentDirError, _ensure_within_project, repo, repo / ".." / "outside" / "target.md")


def test_gitignore_rejects_project_symlinks(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()

    existing_target = outside / "existing-ignore"
    existing_target.write_text("*.log\n", encoding="utf-8")
    (repo / ".gitignore").symlink_to(existing_target)
    _expect_raises(AgentDirError, gitignore_plan, target="project", cwd=repo)
    _expect_raises(AgentDirError, ensure_agentdir_ignored, target="project", cwd=repo)
    assert existing_target.read_text(encoding="utf-8") == "*.log\n"
    (repo / ".gitignore").unlink()

    dangling_target = outside / "dangling-ignore"
    (repo / ".gitignore").symlink_to(dangling_target)
    _expect_raises(AgentDirError, gitignore_plan, target="project", cwd=repo)
    _expect_raises(AgentDirError, ensure_agentdir_ignored, target="project", cwd=repo)
    assert not dangling_target.exists()


def test_no_create_path_helpers_default_false_and_dry_run_writes_nothing(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    store = repo / ".agentdir"

    skill_path = codex_skill_path_no_create(store, target="store", cwd=repo)
    generic_path = generic_guidance_path_no_create(store, target="store", cwd=repo)
    assert skill_path == store.resolve() / "integrations" / "codex" / "skills" / "agentdir" / "SKILL.md"
    assert generic_path == store.resolve() / "integrations" / "generic" / "AGENTS.md"
    assert not store.exists()

    home = tmp_path / "home"
    home.mkdir()
    result = _run_cli(
        "adopt",
        "--dry-run",
        "--install-skill",
        "store",
        "--install-generic",
        "store",
        "--integration-target",
        "store",
        "--gitignore",
        "none",
        "--json",
        cwd=repo,
        env_extra={"HOME": str(home)},
    )
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["would_create_root"] is True
    assert not store.exists()


def test_uninstall_integrations_skips_generated_files(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    store = repo / ".agentdir"
    init_root(store)

    claude = repo / "CLAUDE.md"
    generated_content = (
        "<!-- GENERATED BY rulesync. DO NOT EDIT. -->\n\n"
        "<!-- agentdir-managed-claude:start -->\n"
        "# AgentDir for Claude Code\n"
        "<!-- agentdir-managed-claude:end -->\n"
    )
    claude.write_text(generated_content, encoding="utf-8")

    install_generic_guidance(store, target="project", cwd=repo)
    assert (repo / "AGENTS.md").is_file()

    results = uninstall_integrations(store, ["claude", "generic"], target="project", apply=True, cwd=repo)
    by_name = {item["name"]: item for item in results}

    assert by_name["claude"]["action"] == "skip-generated"
    assert by_name["claude"]["removed"] is False
    assert "generated" in by_name["claude"]["skipped"]
    assert claude.read_text(encoding="utf-8") == generated_content

    assert by_name["generic"]["action"] == "remove"
    assert by_name["generic"]["removed"] is True
    assert not (repo / "AGENTS.md").exists()


def test_managed_block_handles_stray_earlier_end_marker(tmp_path: Path) -> None:
    del tmp_path
    start = "<!-- agentdir-managed-claude:start -->"
    end = "<!-- agentdir-managed-claude:end -->"
    stray_before = f"Intro with stray {end}\n\n{start}\nold content\n{end}\n\nFooter\n"

    extracted = extract_managed_block(stray_before, start, end)
    assert extracted == f"{start}\nold content\n{end}\n"
    assert _integration_managed("claude", stray_before) is True

    new_block = f"{start}\nnew content\n{end}\n"
    merged = merge_managed_block(stray_before, new_block, start, end)
    assert merged.count(start) == 1
    assert "new content" in merged
    assert "old content" not in merged
    assert f"Intro with stray {end}" in merged
    assert "Footer" in merged

    removed = remove_managed_block(stray_before, start, end)
    assert start not in removed
    assert "old content" not in removed
    assert f"Intro with stray {end}" in removed
    assert "Footer" in removed

    # Reversed-only markers (end before start, no closing end after start) are not managed.
    reversed_only = f"{end}\n\n{start}\nunclosed\n"
    assert extract_managed_block(reversed_only, start, end) is None
    assert _integration_managed("claude", reversed_only) is False

    generic_reversed = (
        f"{skills_module.GENERIC_GUIDANCE_END}\n\n{skills_module.GENERIC_GUIDANCE_START}\nunclosed\n"
    )
    assert is_agentdir_managed_generic(generic_reversed) is False


def test_upgrade_missing_gh_raises_dependency_error(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    old_cwd = Path.cwd()
    os.chdir(repo)
    try:
        with patch.object(upgrade_module.subprocess, "run", side_effect=FileNotFoundError("gh")):
            _expect_raises(
                AgentDirDependencyError,
                upgrade_agentdir,
                UpgradeOptions(version=None, dry_run=False),
            )
            _expect_raises(
                AgentDirDependencyError,
                upgrade_agentdir,
                UpgradeOptions(version="v0.10.0", dry_run=False),
            )
    finally:
        os.chdir(old_cwd)


def test_upgrade_version_matches_exact_token_boundaries() -> None:
    assert _version_matches("agentdir 0.10.0", "v0.10.0") is True
    assert _version_matches("agentdir 0.10.0", "0.10.0") is True
    assert _version_matches("agentdir v0.10.0", "v0.10.0") is True
    assert _version_matches("agentdir 0.10.0", "v0.1") is False
    assert _version_matches("agentdir 0.10.0-rc1", "v0.10.0") is False
    assert _version_matches("agentdir 0.10.0+local", "v0.10.0") is False
    assert _version_matches("agentdir 10.10.0", "v0.10.0") is False
    assert _version_matches("agentdir 0.10.0.1", "v0.10.0") is False
    assert _version_matches("", "v0.10.0") is False
    assert _version_matches(None, "v0.10.0") is False
    assert _version_matches("agentdir 0.10.0", "v") is False


def test_upgrade_only_adopts_already_adopted_repo(tmp_path: Path) -> None:
    unadopted = _init_repo(tmp_path / "unadopted")
    adopted = _init_repo(tmp_path / "adopted")
    init_root(adopted / ".agentdir")

    ok_proc = subprocess.CompletedProcess(args=["stub"], returncode=0, stdout='{"ok": true}\n', stderr="")

    old_cwd = Path.cwd()
    try:
        # Unadopted repo: dry-run and real upgrade must skip adopt & doctor.
        os.chdir(unadopted)
        dry_unadopted = upgrade_agentdir(UpgradeOptions(version="v0.10.0", dry_run=True))
        assert dry_unadopted["adopt"]["planned"] is False
        assert dry_unadopted["adopt"]["reason"] == "not_adopted"
        assert dry_unadopted["doctor"]["planned"] is False
        assert "not adopted" in format_upgrade_result(dry_unadopted)

        with (
            patch.object(upgrade_module, "_run_installer", return_value=ok_proc),
            patch.object(upgrade_module, "_command_version", return_value="agentdir 0.10.0"),
            patch.object(upgrade_module, "_run_adopt") as mock_adopt,
        ):
            real_unadopted = upgrade_agentdir(UpgradeOptions(version="v0.10.0", dry_run=False))
        assert real_unadopted["adopt"]["planned"] is False
        assert real_unadopted["doctor"]["planned"] is False
        assert mock_adopt.call_count == 0
        assert not (unadopted / ".agentdir").exists()

        # Adopted repo: dry-run plans adopt & doctor; real upgrade runs both.
        os.chdir(adopted)
        dry_adopted = upgrade_agentdir(UpgradeOptions(version="v0.10.0", dry_run=True))
        assert dry_adopted["adopt"]["planned"] is True
        assert dry_adopted["doctor"]["planned"] is True
        assert dry_adopted["adopt"]["command"].startswith("agentdir adopt ")

        with (
            patch.object(upgrade_module, "_run_installer", return_value=ok_proc),
            patch.object(upgrade_module, "_command_version", return_value="agentdir 0.10.0"),
            patch.object(upgrade_module, "_run_adopt", return_value=ok_proc) as mock_adopt,
            patch.object(upgrade_module.subprocess, "run", return_value=ok_proc),
        ):
            real_adopted = upgrade_agentdir(UpgradeOptions(version="v0.10.0", dry_run=False))
        assert real_adopted["adopt"]["planned"] is True
        assert real_adopted["adopt"]["ok"] is True
        assert real_adopted["doctor"]["ok"] is True
        assert mock_adopt.call_count == 1
    finally:
        os.chdir(old_cwd)


def test_install_sh_pipx_passes_python_and_verifies_sha256(tmp_path: Path) -> None:
    wheel = tmp_path / "agentdir_cli-0.10.0-py3-none-any.whl"
    wheel.write_bytes(b"fake-wheel-content-v0.10.0")
    wheel_sha = hashlib.sha256(wheel.read_bytes()).hexdigest()

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pipx_log = tmp_path / "pipx.log"
    pipx_stub = bin_dir / "pipx"
    pipx_stub.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> {pipx_log}\nexit 0\n",
        encoding="utf-8",
    )
    pipx_stub.chmod(0o755)

    agentdir_stub = bin_dir / "agentdir"
    agentdir_stub.write_text("#!/usr/bin/env bash\necho 'agentdir 0.10.0'\n", encoding="utf-8")
    agentdir_stub.chmod(0o755)

    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "AGENTDIR_WHEEL": str(wheel),
        "AGENTDIR_PYTHON": sys.executable,
        "AGENTDIR_SHA256": wheel_sha.upper(),
        "AGENTDIR_PREFIX": str(tmp_path / "prefix"),
        "AGENTDIR_HOME": str(tmp_path / "share"),
    }
    ok = subprocess.run(
        ["bash", str(PROJECT_ROOT / "scripts" / "install.sh")],
        env=env,
        text=True,
        capture_output=True,
    )
    assert ok.returncode == 0, ok.stderr
    logged = pipx_log.read_text(encoding="utf-8").splitlines()
    assert "uninstall agentdir" in logged
    assert f"install --force --python {sys.executable} {wheel}" in logged
    assert "verified SHA-256" in ok.stderr

    # Mismatched SHA-256 must fail before calling pipx.
    pipx_log.unlink()
    bad_env = {**env, "AGENTDIR_SHA256": "0" * 64}
    bad = subprocess.run(
        ["bash", str(PROJECT_ROOT / "scripts" / "install.sh")],
        env=bad_env,
        text=True,
        capture_output=True,
    )
    assert bad.returncode != 0
    assert "SHA-256 mismatch" in bad.stderr
    assert not pipx_log.exists()


def test_install_sh_venv_offline_skips_pip_upgrade_and_uninstalls_opposite_package(
    tmp_path: Path,
) -> None:
    def run_case(wheel_name: str, expected_uninstalled: str) -> list[str]:
        case_dir = tmp_path / wheel_name.replace(".", "_")
        case_dir.mkdir()
        wheel = case_dir / wheel_name
        wheel.write_bytes(b"fake-wheel-bytes")

        pip_log = case_dir / "pip.log"
        prefix = case_dir / "prefix"
        share = case_dir / "share"

        # Stub python that passes the 3.11+ version check, creates a fake venv, and logs pip calls.
        py_stub = case_dir / "python3-stub"
        py_stub.write_text(
            f"""#!/usr/bin/env bash
set -euo pipefail
if [ "$1" = "-" ]; then
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  venv_dir="$3"
  mkdir -p "$venv_dir/bin"
  cat > "$venv_dir/bin/python" <<'INNER'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> {pip_log}
if [ "$1" = "-m" ] && [ "$2" = "pip" ] && [ "$3" = "install" ]; then
  cat > "$(dirname "$0")/agentdir" <<'BIN'
#!/usr/bin/env bash
echo "agentdir 0.10.0"
BIN
  chmod +x "$(dirname "$0")/agentdir"
fi
INNER
  chmod +x "$venv_dir/bin/python"
  exit 0
fi
exit 0
""",
            encoding="utf-8",
        )
        py_stub.chmod(0o755)

        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(case_dir / "home"),
            "AGENTDIR_FORCE_VENV": "1",
            "AGENTDIR_WHEEL": str(wheel),
            "AGENTDIR_PYTHON": str(py_stub),
            "AGENTDIR_PREFIX": str(prefix),
            "AGENTDIR_HOME": str(share),
        }
        res = subprocess.run(
            ["bash", str(PROJECT_ROOT / "scripts" / "install.sh")],
            env=env,
            text=True,
            capture_output=True,
        )
        assert res.returncode == 0, res.stderr
        assert (prefix / "bin" / "agentdir").is_symlink()
        calls = pip_log.read_text(encoding="utf-8").splitlines()
        assert not any("--upgrade pip" in line for line in calls), calls
        assert any(f"-m pip uninstall -y {expected_uninstalled}" in line for line in calls), calls
        assert any(f"-m pip install --force-reinstall {wheel}" in line for line in calls), calls
        return calls

    run_case("agentdir_cli-0.10.0-py3-none-any.whl", "agentdir")
    run_case("agentdir-0.7.4-py3-none-any.whl", "agentdir-cli")


def test_install_sh_verifies_downloaded_sha256sums_and_fails_on_mismatch(tmp_path: Path) -> None:
    wheel_bytes = b"downloaded-release-wheel-payload"
    good_sha = hashlib.sha256(wheel_bytes).hexdigest()

    def run_download(sums_content: str) -> subprocess.CompletedProcess[str]:
        case_dir = tmp_path / ("good" if good_sha in sums_content else "bad")
        case_dir.mkdir()
        bin_dir = case_dir / "bin"
        bin_dir.mkdir()

        payload_file = case_dir / "wheel.bin"
        payload_file.write_bytes(wheel_bytes)
        sums_file = case_dir / "SHA256SUMS"
        sums_file.write_text(sums_content, encoding="utf-8")

        curl_stub = bin_dir / "curl"
        curl_stub.write_text(
            f"""#!/usr/bin/env bash
set -euo pipefail
out=""
url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
case "$url" in
  */SHA256SUMS) cp {sums_file} "$out" ;;
  *agentdir_cli-0.10.0-py3-none-any.whl) cp {payload_file} "$out" ;;
  *) exit 22 ;;
esac
""",
            encoding="utf-8",
        )
        curl_stub.chmod(0o755)

        pipx_stub = bin_dir / "pipx"
        pipx_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        pipx_stub.chmod(0o755)

        agentdir_stub = bin_dir / "agentdir"
        agentdir_stub.write_text("#!/usr/bin/env bash\necho 'agentdir 0.10.0'\n", encoding="utf-8")
        agentdir_stub.chmod(0o755)

        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(case_dir / "home"),
            "AGENTDIR_VERSION": "v0.10.0",
            "AGENTDIR_WHEEL": "",
            "AGENTDIR_PYTHON": sys.executable,
            "AGENTDIR_PREFIX": str(case_dir / "prefix"),
            "AGENTDIR_HOME": str(case_dir / "share"),
        }
        return subprocess.run(
            ["bash", str(PROJECT_ROOT / "scripts" / "install.sh")],
            env=env,
            text=True,
            capture_output=True,
        )

    ok = run_download(f"{good_sha}  agentdir_cli-0.10.0-py3-none-any.whl")
    assert ok.returncode == 0, ok.stderr
    assert "verified SHA-256 for agentdir_cli-0.10.0-py3-none-any.whl against SHA256SUMS" in ok.stderr

    bad = run_download(f"{'f' * 64}  agentdir_cli-0.10.0-py3-none-any.whl\n")
    assert bad.returncode != 0
    assert "SHA-256 mismatch" in bad.stderr


def test_rollback_sh_uses_sibling_installer_and_verifies_exact_version(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    rollback_copy = scripts_dir / "rollback.sh"
    shutil.copy2(PROJECT_ROOT / "scripts" / "rollback.sh", rollback_copy)

    install_log = tmp_path / "install.log"
    sibling_install = scripts_dir / "install.sh"
    sibling_install.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf 'version=%s\\n' "$AGENTDIR_VERSION" > {install_log}
mkdir -p "$AGENTDIR_PREFIX/bin"
cat > "$AGENTDIR_PREFIX/bin/agentdir" <<EOF
#!/usr/bin/env bash
echo "$FAKE_REPORTED_VERSION"
EOF
chmod +x "$AGENTDIR_PREFIX/bin/agentdir"
""",
        encoding="utf-8",
    )
    sibling_install.chmod(0o755)

    def run_rollback(target: str, reported: str) -> subprocess.CompletedProcess[str]:
        prefix = tmp_path / f"prefix-{target}-{reported.replace(' ', '_')}"
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path / "home"),
            "AGENTDIR_PREFIX": str(prefix),
            "FAKE_REPORTED_VERSION": reported,
        }
        return subprocess.run(
            ["bash", str(rollback_copy), target],
            env=env,
            text=True,
            capture_output=True,
        )

    ok = run_rollback("v0.9.0", "agentdir 0.9.0")
    assert ok.returncode == 0, ok.stderr
    assert install_log.read_text(encoding="utf-8").strip() == "version=v0.9.0"

    # Must reject prerelease suffix and prefix substring false positives.
    rc_mismatch = run_rollback("v0.9.0", "agentdir 0.9.0-rc1")
    assert rc_mismatch.returncode != 0
    assert "rollback version mismatch" in rc_mismatch.stderr

    prefix_mismatch = run_rollback("v0.1", "agentdir 0.10.0")
    assert prefix_mismatch.returncode != 0
    assert "rollback version mismatch" in prefix_mismatch.stderr

    # Verify fallback URL fetches installer from main rather than the old tag.
    rollback_text = (PROJECT_ROOT / "scripts" / "rollback.sh").read_text(encoding="utf-8")
    assert "$AGENTDIR_ROLLBACK_VERSION/scripts/install.sh" not in rollback_text
    assert 'installer_ref="${AGENTDIR_INSTALLER_REF:-main}"' in rollback_text


def test_publish_workflow_and_shipped_docs_manifest_checks() -> None:
    publish = (PROJECT_ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
    assert "contents: read" in publish
    assert "pyproject.toml" in publish
    assert "python -m pytest -q" in publish
    assert "python -m twine check dist/*" in publish
    assert "SHA256SUMS" in publish

    install_md = (PROJECT_ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    assert "agentdir memory backend list" in install_md
    assert "agentdir memory backend status" not in install_md
    assert "faster large stores" not in install_md
    assert "not-implemented" in install_md

    skill_md = (PROJECT_ROOT / "skills" / "agentdir" / "SKILL.md").read_text(encoding="utf-8")
    assert "agentdir audit claims --text" in skill_md

    assert "memory backend configure sqlite-vec" not in CODEX_SKILL

    manifest = (PROJECT_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "*.gif" in manifest


def test_git_module_removes_dead_helper_and_preserves_status_leading_space(tmp_path: Path) -> None:
    assert not hasattr(git_module, "is_linked_worktree")

    repo = _init_repo(tmp_path / "repo")
    tracked = repo / "tracked.txt"
    tracked.write_text("v1\n", encoding="utf-8")
    env = _clean_git_env()
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, env=env, check=True, capture_output=True)

    # Unstaged modification should keep its leading space (" M tracked.txt").
    tracked.write_text("v2\n", encoding="utf-8")
    status = git_module.git_status_short(repo)
    assert status.startswith(" M tracked.txt"), repr(status)

    # Verify git_output passes errors="replace" so non-UTF-8 output never crashes.
    recorded_kwargs: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        recorded_kwargs.update(kwargs)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=" M file\ufffd.txt\n", stderr="")

    with patch.object(git_module.subprocess, "run", side_effect=fake_run):
        out = git_module.git_output(["status", "--short"], repo)
    assert out == " M file\ufffd.txt"
    assert recorded_kwargs.get("errors") == "replace"
