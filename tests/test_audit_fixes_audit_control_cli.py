from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

from agentdir import audit, claims, cli, control, rendering, review
from agentdir.events import emit_event
from agentdir.store import AgentDirError, AgentDirStateError, init_root


def find_project_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError("could not locate project root")


PROJECT_ROOT = find_project_root()
SRC_ROOT = PROJECT_ROOT / "src"


def _isolated_env(env_extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_ROOT)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    if env_extra:
        env.update(env_extra)
    return env


def run_cli(
    *args: str,
    cwd: Path | None = None,
    input_text: str | None = None,
    env_extra: dict[str, str] | None = None,
    expected_returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    env = _isolated_env(env_extra)
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


def init_repo(path: Path, *, commit: bool = False) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    env = _isolated_env()
    subprocess.run(["git", "init"], cwd=path, env=env, check=True, text=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=path, env=env, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, env=env, check=True)
    if commit:
        (path / "README.md").write_text("initial\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=path, env=env, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=path, env=env, check=True, capture_output=True)
    return path


def _claim_by_family(payload: dict[str, object], family: str) -> dict[str, object]:
    for item in payload["claims"]:  # type: ignore[index]
        if item["family"] == family:
            return item
    raise AssertionError(f"missing claim for family {family}: {payload['claims']!r}")


def _check_by_id(payload: dict[str, object], check_id: str) -> dict[str, object]:
    for item in payload["checks"]:  # type: ignore[index]
        if item["id"] == check_id:
            return item
    raise AssertionError(f"missing check {check_id}")


# --- 1. --root propagation and dry-run isolation ----------------------------


def test_adopt_and_setup_dry_run_store_targets_do_not_create_store(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", commit=True)

    adopt_plan = json.loads(
        run_cli(
            "adopt",
            "--dry-run",
            "--install-skill",
            "store",
            "--install-generic",
            "store",
            "--integration-target",
            "store",
            "--json",
            cwd=repo,
        ).stdout
    )
    setup_plan = json.loads(
        run_cli(
            "setup",
            "--dry-run",
            "--codex-skill",
            "store",
            "--install-generic",
            "store",
            "--integration-target",
            "store",
            "--json",
            cwd=repo,
        ).stdout
    )

    assert adopt_plan["would_create_root"] is True
    assert setup_plan["would_create_root"] is True
    assert adopt_plan["codex_skill"]["target"] == "store"
    assert setup_plan["codex_skill"]["target"] == "store"
    assert not (repo / ".agentdir").exists()


def test_explicit_root_propagates_across_adopt_hooks_integrations_status_and_unadopt(
    tmp_path: Path,
) -> None:
    target = init_repo(tmp_path / "target-repo", commit=True)
    caller = init_repo(tmp_path / "caller-repo", commit=True)
    env = _isolated_env()
    subprocess.run(["git", "checkout", "-b", "target-branch"], cwd=target, env=env, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "caller-branch"], cwd=caller, env=env, check=True, capture_output=True)
    (target / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    target_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=target, check=True, text=True, capture_output=True
    ).stdout.strip()
    target_root = target / ".agentdir"

    plan = json.loads(
        run_cli(
            "adopt",
            "--root",
            str(target_root),
            "--dry-run",
            "--install-skill",
            "store",
            "--gitignore",
            "project",
            "--json",
            cwd=caller,
        ).stdout
    )
    assert not target_root.exists()
    assert plan["gitignore"]["path"] == str(target / ".gitignore")
    assert all(str(target) in item["path"] for item in plan["hooks"])
    assert all(str(target) in item["path"] for item in plan["integrations"])

    adopted = json.loads(
        run_cli(
            "adopt",
            "--root",
            str(target_root),
            "--install-skill",
            "store",
            "--gitignore",
            "project",
            "--json",
            cwd=caller,
        ).stdout
    )
    assert adopted["root"] == str(target_root)
    assert (target / ".git" / "hooks" / "pre-commit").is_file()
    assert (target / "AGENTS.md").is_file()
    assert (target / ".gitignore").is_file()
    assert not (caller / ".agentdir").exists()
    assert not (caller / ".git" / "hooks" / "pre-commit").exists()
    assert not (caller / "AGENTS.md").exists()
    assert not (caller / ".gitignore").exists()

    hooks_state = json.loads(
        run_cli(
            "hooks",
            "status",
            "--root",
            str(target_root),
            "--hook",
            "pre-commit",
            "--json",
            cwd=caller,
        ).stdout
    )
    assert hooks_state[0]["managed"] is True
    assert hooks_state[0]["path"] == str(target / ".git" / "hooks" / "pre-commit")

    doctor = json.loads(
        run_cli("integrations", "doctor", "--root", str(target_root), "--json", cwd=caller).stdout
    )
    assert doctor["ok"] is True
    assert all(str(target) in item["path"] for item in doctor["checks"])

    status = json.loads(
        run_cli("status", "--root", str(target_root), "--json", cwd=caller).stdout
    )
    assert status["git"]["workspace"] == target.name
    assert status["git"]["branch"] == "target-branch"
    assert status["git"]["head"] == target_head
    assert status["git"]["dirty"] is True

    unadopted = json.loads(
        run_cli("unadopt", "--root", str(target_root), "--apply", "--json", cwd=caller).stdout
    )
    assert unadopted["applied"] is True
    assert not (target / ".git" / "hooks" / "pre-commit").exists()
    assert not (target / "AGENTS.md").exists()
    assert target_root.is_dir()


def test_explicit_root_propagates_across_setup_hooks_and_integrations_install(
    tmp_path: Path,
) -> None:
    target = init_repo(tmp_path / "target", commit=True)
    caller = init_repo(tmp_path / "caller", commit=True)
    target_root = target / ".agentdir"

    run_cli(
        "setup",
        "--root",
        str(target_root),
        "--codex-skill",
        "store",
        "--install-integrations",
        "none",
        "--install-generic",
        "none",
        "--gitignore",
        "project",
        "--json",
        cwd=caller,
    )
    assert (target / ".git" / "hooks" / "pre-commit").is_file()
    assert (target / ".gitignore").is_file()
    assert not (caller / ".git" / "hooks" / "pre-commit").exists()
    assert not (caller / ".gitignore").exists()

    run_cli(
        "hooks",
        "uninstall",
        "--root",
        str(target_root),
        "--hook",
        "pre-commit",
        cwd=caller,
    )
    assert not (target / ".git" / "hooks" / "pre-commit").exists()

    run_cli(
        "hooks",
        "install",
        "--root",
        str(target_root),
        "--hook",
        "pre-commit",
        cwd=caller,
    )
    assert (target / ".git" / "hooks" / "pre-commit").is_file()
    assert not (caller / ".git" / "hooks" / "pre-commit").exists()

    run_cli(
        "integrations",
        "install",
        "claude",
        "--target",
        "project",
        "--root",
        str(target_root),
        cwd=caller,
    )
    assert (target / "CLAUDE.md").is_file()
    assert not (caller / "CLAUDE.md").exists()


def test_hooks_record_explicit_root_targets_other_repo_but_preserves_linked_worktree(
    tmp_path: Path,
) -> None:
    main = init_repo(tmp_path / "main", commit=True)
    caller = init_repo(tmp_path / "caller", commit=True)
    env = _isolated_env()
    subprocess.run(["git", "checkout", "-b", "main-branch"], cwd=main, env=env, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "caller-branch"], cwd=caller, env=env, check=True, capture_output=True)
    worktree = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", str(worktree), "-b", "linked-branch"],
        cwd=main,
        env=env,
        check=True,
        capture_output=True,
    )
    main_root = main / ".agentdir"
    init_root(main_root)

    run_cli(
        "hooks",
        "record",
        "--root",
        str(main_root),
        "--hook",
        "pre-commit",
        "--original-exit-code",
        "0",
        cwd=caller,
    )
    run_cli(
        "hooks",
        "record",
        "--root",
        str(main_root),
        "--hook",
        "post-commit",
        "--original-exit-code",
        "0",
        cwd=worktree,
    )
    run_cli("index", "update", "--root", str(main_root), cwd=caller)
    rows = json.loads(run_cli("query", "--root", str(main_root), "--json", cwd=caller).stdout)
    pre = next(row for row in rows if row["event_type"] == "git.hook.pre-commit")
    post = next(row for row in rows if row["event_type"] == "git.hook.post-commit")

    assert "branch=main-branch" in pre["body_text"]
    assert "branch=linked-branch" in post["body_text"]


# --- 2. Uninitialized status & Rich markup/errors ---------------------------


def test_uninitialized_status_does_not_crash_and_shows_doctor_errors(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")

    json_result = run_cli("status", "--json", cwd=repo, expected_returncode=1)
    payload = json.loads(json_result.stdout)
    assert payload["root"]["initialized"] is False
    assert payload["federation"] == {
        "registered_roots": 0,
        "stale_roots": 0,
        "roots": [],
        "groups": [],
    }
    assert payload["health"]["ok"] is False
    assert payload["health"]["errors"]

    rendered = run_cli("status", cwd=repo, expected_returncode=1)
    assert "Not an AgentDir root" in rendered.stdout


def test_rich_status_disables_markup_and_renders_warnings_and_errors(tmp_path: Path) -> None:
    status_payload = {
        "root": {"path": "/tmp/[/not-a-tag]/[bold]repo/.agentdir", "scope": "default", "initialized": True, "version": "0.1"},
        "session": {"active": False, "current": None, "latest": None},
        "context": {"latest_pack": None},
        "evidence": {"session_id": None, "count": 0},
        "memory": {"memory_documents": 0, "passages": 0},
        "federation": {},
        "health": {
            "ok": False,
            "warnings": ["split store at [/tmp/a] [red]warn[/red]"],
            "errors": ["corrupt blob [/tmp/b] [bold]err[/bold]"],
        },
    }

    rich_out = rendering.rich_status(status_payload)
    if rich_out is not None:
        assert "[/not-a-tag]/[bold]repo" in rich_out
        assert "warning" in rich_out
        assert "[/tmp/a] [red]warn[/red]" in rich_out
        assert "error" in rich_out
        assert "[/tmp/b] [bold]err[/bold]" in rich_out

    with mock.patch.object(control, "rich_status", return_value=None):
        plain_out = control.format_status(status_payload)
    assert "registered_roots=0" in plain_out
    assert "stale_roots=0" in plain_out
    assert "warning: split store at [/tmp/a] [red]warn[/red]" in plain_out
    assert "error: corrupt blob [/tmp/b] [bold]err[/bold]" in plain_out


# --- 3. Evidence classification & claim auditing ----------------------------


def test_python_command_family_scans_only_leading_flags() -> None:
    assert review.classify_evidence_command(["python", "-m", "pytest", "-c", "pyproject.toml"]) == "test"
    assert review.classify_evidence_command(["python3", "-u", "-B", "-W", "ignore", "-X", "utf8", "-m", "pytest", "-c", "pyproject.toml"]) == "test"
    assert review.classify_evidence_command(["python", "-um", "pytest", "-c", "pyproject.toml"]) == "test"
    assert review.classify_evidence_command(["python", "-mpytest", "-c", "pyproject.toml"]) == "test"
    assert review.classify_evidence_command(["python", "scripts/notify.py", "-m", "test msg"]) == "diagnostic"
    assert review.classify_evidence_command(["python", "-u", "-c", "import pytest"]) == "diagnostic"
    assert review.classify_evidence_command(["python", "--", "pytest"]) == "diagnostic"
    assert review.classify_evidence_command(["python", "--check-hash-based-pycs", "default", "-m", "ruff", "check"]) == "lint"


def test_uv_command_family_preserves_short_flag_case() -> None:
    assert review.classify_evidence_command(["uv", "run", "-C", "/tmp", "pytest"]) == "test"
    assert review.classify_evidence_command(["uv", "-C", "/tmp", "run", "pytest"]) == "test"
    assert review.classify_evidence_command(["uv", "run", "-U", "pytest"]) == "test"
    assert review.classify_evidence_command(["uv", "run", "-P", "pkg", "pytest"]) == "test"
    assert review.classify_evidence_command(["uv", "RUN", "--WITH", "ruff", "pytest"]) == "test"
    assert review.classify_evidence_command(["uv", "run", "python", "-W", "ignore", "-m", "pytest", "-c", "pyproject.toml"]) == "test"
    assert review.classify_evidence_command(["uv", "run", "-c", "/tmp", "pytest"]) == "diagnostic"
    assert review.classify_evidence_command(["uv", "run", "-u", "pytest"]) == "diagnostic"


def test_env_assignment_classifies_redacted_secret_tokens(tmp_path: Path) -> None:
    assert review.classify_evidence_command(["env", "<redacted:key-value-secret>", "pytest"]) == "test"
    assert review.classify_evidence_command(["env", "MY_<redacted:key-value-secret>", "pytest"]) == "test"
    assert review.classify_evidence_command(["env", "<redacted:key-value-secret>", "echo", "pytest"]) == "diagnostic"

    repo = init_repo(tmp_path / "repo")
    (repo / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    run_cli("work", "start", "redacted env assignment", "--no-context", cwd=repo)
    run_cli(
        "run",
        "--",
        "env",
        "API_KEY=abcdef1234567890",
        sys.executable,
        "-m",
        "py_compile",
        "sample.py",
        cwd=repo,
    )
    brief = json.loads(run_cli("evidence", "--brief", "--json", cwd=repo).stdout)
    assert brief["latest_by_family"]["build"]["tool"] == "env"


def test_review_and_claims_query_without_row_cap(tmp_path: Path) -> None:
    root = tmp_path / ".agentdir"
    init_root(root)
    with (
        mock.patch.object(review, "query_messages", return_value=[]) as review_query,
        mock.patch.object(claims, "query_messages", return_value=[]) as claims_query,
    ):
        review.summarize_session(root, "sess-1", rebuild=False)
        review.evidence_rows(root, "sess-1", rebuild=False)
        claims.recorded_claims(root, "sess-1", rebuild=False)

    assert review_query.call_args_list == [
        mock.call(root, session_id="sess-1", limit=None),
        mock.call(root, session_id="sess-1", limit=None),
    ]
    assert claims_query.call_args_list == [
        mock.call(root, session_id="sess-1", event_type=claims.CLAIM_EVENT_TYPE, limit=None),
    ]


def test_audit_claims_fallback_blocks_passing_diagnostic_and_cross_family_matches(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "fallback guardrails", "--no-context", cwd=repo)
    run_cli(
        "run",
        "--name",
        "echo",
        "--",
        sys.executable,
        "-c",
        "print('tests passed and build succeeded')",
        cwd=repo,
    )
    passing_diag = json.loads(
        run_cli(
            "audit",
            "claims",
            "--text",
            "-",
            "--json",
            cwd=repo,
            input_text="Tests passed. Build passed.",
        ).stdout
    )
    assert _claim_by_family(passing_diag, "test")["status"] == "unsupported"
    assert _claim_by_family(passing_diag, "build")["status"] == "unsupported"

    # A failing test command mentioning "build" must not be used as build evidence.
    run_cli(
        "run",
        "--name",
        "pytest",
        "--",
        sys.executable,
        "-c",
        "import sys; print('build failed inside test'); sys.exit(1)",
        cwd=repo,
        expected_returncode=1,
    )
    cross_family = json.loads(
        run_cli(
            "audit",
            "claims",
            "--text",
            "-",
            "--json",
            cwd=repo,
            input_text="Build passed.",
        ).stdout
    )
    assert _claim_by_family(cross_family, "build")["status"] == "unsupported"
    assert _claim_by_family(cross_family, "test")["status"] == "unreviewed"

    # A failing diagnostic command with only substring "rebuilding" must not match "build".
    run_cli(
        "run",
        "--name",
        "custom-wrapper",
        "--",
        sys.executable,
        "-c",
        "import sys; print('rebuilding index failed'); sys.exit(1)",
        cwd=repo,
        expected_returncode=1,
    )
    word_boundary = json.loads(
        run_cli(
            "audit",
            "claims",
            "--text",
            "-",
            "--json",
            cwd=repo,
            input_text="Build passed.",
        ).stdout
    )
    assert _claim_by_family(word_boundary, "build")["status"] == "unsupported"

    # A failing diagnostic wrapper with whole-word "build" still contradicts "Build passed.".
    run_cli(
        "run",
        "--name",
        "custom-wrapper",
        "--",
        sys.executable,
        "-c",
        "import sys; print('build failed'); sys.exit(1)",
        cwd=repo,
        expected_returncode=1,
    )
    wrapper_contradiction = json.loads(
        run_cli(
            "audit",
            "claims",
            "--text",
            "-",
            "--json",
            cwd=repo,
            input_text="Build passed.",
        ).stdout
    )
    assert _claim_by_family(wrapper_contradiction, "build")["status"] == "contradicted"


def test_audit_session_handles_multiple_worktrees_and_session_git_cwd(tmp_path: Path) -> None:
    main = init_repo(tmp_path / "main", commit=True)
    env = _isolated_env()
    run_cli(
        "adopt",
        "--gitignore",
        "project",
        "--install-skill",
        "none",
        "--install-generic",
        "none",
        "--install-integrations",
        "none",
        "--no-hooks",
        cwd=main,
    )
    subprocess.run(["git", "add", ".gitignore"], cwd=main, env=env, check=True)
    subprocess.run(["git", "commit", "-m", "ignore store"], cwd=main, env=env, check=True, capture_output=True)
    worktree = tmp_path / "feature"
    subprocess.run(
        ["git", "worktree", "add", str(worktree), "-b", "feature"],
        cwd=main,
        env=env,
        check=True,
        capture_output=True,
    )
    caller = tmp_path / "caller"
    caller.mkdir()

    main_session = json.loads(
        run_cli("work", "start", "main clean task", "--no-context", "--json", cwd=main).stdout
    )["session"]["session_id"]
    wt_session = json.loads(
        run_cli("work", "start", "worktree dirty task", "--no-context", "--json", cwd=worktree).stdout
    )["session"]["session_id"]
    (worktree / "uncommitted.txt").write_text("dirty in linked worktree\n", encoding="utf-8")

    # With two active sessions, explicit --session must not fail on read_current_session()
    # and must check git_dirty in each session's own checkout.
    main_audit = json.loads(
        run_cli(
            "audit",
            "session",
            "--root",
            str(main / ".agentdir"),
            "--session",
            main_session,
            "--json",
            cwd=caller,
        ).stdout
    )
    wt_audit = json.loads(
        run_cli(
            "audit",
            "session",
            "--root",
            str(main / ".agentdir"),
            "--session",
            wt_session,
            "--json",
            cwd=caller,
        ).stdout
    )

    assert _check_by_id(main_audit, "session_finished")["message"] == "session is still active"
    assert _check_by_id(wt_audit, "session_finished")["message"] == "session is still active"
    assert _check_by_id(main_audit, "git_dirty")["status"] == "pass"
    assert _check_by_id(wt_audit, "git_dirty")["status"] == "warn"


def test_audit_claims_clause_splitting_and_negation_boundaries(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "clause polarity", "--no-context", cwd=repo)
    run_cli(
        "run",
        "--name",
        "pytest",
        "--",
        sys.executable,
        "-c",
        "import sys; print('FAILED'); sys.exit(1)",
        cwd=repo,
        expected_returncode=1,
    )
    run_cli(
        "run",
        "--name",
        "ruff",
        "--",
        sys.executable,
        "-c",
        "print('ruff ok')",
        cwd=repo,
    )

    # Negated pass phrases and unfixed-failure prose must acknowledge, never claim pass.
    for prose in (
        "Tests did not pass.",
        "The test suite does not pass.",
        "Tests are not passing.",
        "Tests never passed.",
        "Tests failed to pass.",
        "I did not fix the test failure.",
        "Tests failed after a green run.",
    ):
        payload = json.loads(
            run_cli("audit", "claims", "--text", "-", "--json", cwd=repo, input_text=prose).stdout
        )
        assert payload["claims_detected"] == 0, prose
        assert _claim_by_family(payload, "test")["status"] == "acknowledged", prose
        assert payload["ok"] is True, prose

    # Contrast clauses must isolate pass claims from failures and not-run statements.
    for prose in (
        "Lint passed, but tests failed.",
        "Tests failed, but lint passed.",
        "Tests failed while lint passed.",
        "I did not run tests; lint passed.",
        "Although tests failed, lint passed.",
    ):
        payload = json.loads(
            run_cli("audit", "claims", "--text", "-", "--json", cwd=repo, input_text=prose).stdout
        )
        assert payload["claims_detected"] == 1, prose
        assert _claim_by_family(payload, "lint")["status"] == "supported", prose
        assert _claim_by_family(payload, "test")["status"] == "acknowledged", prose
        assert payload["ok"] is True, prose

    # Genuine negated-failure claims still contradict failing tests.
    for prose in (
        "No test failures.",
        "There are no failing tests.",
        "Tests are not failing.",
        "Tests did not fail.",
    ):
        payload = json.loads(
            run_cli("audit", "claims", "--text", "-", "--json", cwd=repo, input_text=prose).stdout
        )
        assert _claim_by_family(payload, "test")["status"] == "contradicted", prose
        assert payload["ok"] is False, prose


def test_strict_claims_audit_marks_partial_evidence_not_ok(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "strict partial", "--no-context", cwd=repo)
    run_cli(
        "run",
        "--name",
        "pytest",
        "--max-capture-bytes",
        "40",
        "--",
        sys.executable,
        "-c",
        "print('tests passed ' * 40)",
        cwd=repo,
    )
    run_cli("claim", "test", "--passed", cwd=repo)

    text_advisory = json.loads(
        run_cli("audit", "claims", "--text", "-", "--json", cwd=repo, input_text="Tests passed.").stdout
    )
    text_strict = json.loads(
        run_cli(
            "audit",
            "claims",
            "--text",
            "-",
            "--json",
            "--strict",
            cwd=repo,
            input_text="Tests passed.",
            expected_returncode=1,
        ).stdout
    )
    rec_advisory = json.loads(run_cli("audit", "claims", "--json", cwd=repo).stdout)
    rec_strict = json.loads(
        run_cli("audit", "claims", "--json", "--strict", cwd=repo, expected_returncode=1).stdout
    )

    assert text_advisory["ok"] is True
    assert text_strict["ok"] is False
    assert _claim_by_family(text_strict, "test")["status"] == "partial"
    assert rec_advisory["ok"] is True
    assert rec_strict["ok"] is False
    assert _claim_by_family(rec_strict, "test")["status"] == "partial"


# --- 4. Control plane locks, validation order & unknown sessions ------------


def test_finish_work_runs_doctor_outside_store_locks(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", commit=True)
    started = json.loads(
        run_cli("work", "start", "doctor outside locks", "--no-context", "--json", cwd=repo).stdout
    )
    session_id = started["session"]["session_id"]
    store = repo / ".agentdir"
    observed_blocked: dict[str, bool] = {}
    real_doctor = control.run_doctor

    def probe_lock(key: str) -> bool:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        lock_path = store / "state" / f".lifecycle-{digest}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False

    def observed_doctor(root: str | Path):
        for key in ("work-start", "active-session-pointer", f"session:{session_id}"):
            observed_blocked[key] = probe_lock(key)
        return real_doctor(root)

    with mock.patch.object(control, "run_doctor", side_effect=observed_doctor):
        finished = control.finish_work(store, run_health_check=True)

    assert finished["ended_session"]["session_id"] == session_id
    assert observed_blocked == {
        "work-start": False,
        "active-session-pointer": False,
        f"session:{session_id}": False,
    }


def test_start_work_validates_inputs_before_mutating_session_state(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", commit=True)
    store = repo / ".agentdir"
    init_root(store)

    # 1. Fresh store: invalid federation group must not create a session or emit work.started.
    run_cli(
        "work",
        "start",
        "checkout redirect repair",
        "--group",
        "missing-group",
        cwd=repo,
        expected_returncode=2,
    )
    assert list((store / "sessions").iterdir()) == []
    run_cli("session", "current", cwd=repo, expected_returncode=2)

    # 2. Fresh store: unconfigured semantic retrieval must not create a session.
    run_cli(
        "work",
        "start",
        "checkout redirect repair",
        "--retrieval",
        "semantic",
        cwd=repo,
        expected_returncode=2,
    )
    assert list((store / "sessions").iterdir()) == []

    # 3. Fresh store: invalid retrieval mode with --no-context must not create a session.
    try:
        control.start_work(store, "checkout redirect", emit_context=False, retrieval_mode="invalid")
    except AgentDirError:
        pass
    else:
        raise AssertionError("expected invalid retrieval mode to fail")
    assert list((store / "sessions").iterdir()) == []

    # 4. Existing active session: failed start must not overwrite session.title or emit work.started.
    started = json.loads(
        run_cli("work", "start", "original task", "--no-context", "--json", cwd=repo).stdout
    )
    session_id = started["session"]["session_id"]
    run_cli(
        "work",
        "start",
        "replacement task that fails",
        "--group",
        "missing-group",
        cwd=repo,
        expected_returncode=2,
    )
    current = json.loads(run_cli("session", "current", "--json", cwd=repo).stdout)
    assert current["session_id"] == session_id
    assert current["title"] == "original task"
    run_cli("index", "update", cwd=repo)
    work_started = json.loads(
        run_cli("query", "--session", session_id, "--type", "work.started", "--json", cwd=repo).stdout
    )
    assert len(work_started) == 1

    # 5. Active session with pending briefing must still block with pending-review error first.
    prior_repo = init_repo(tmp_path / "pending-repo", commit=True)
    prior_body = tmp_path / "prior.txt"
    prior_body.write_text("checkout redirect callback state marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior", cwd=prior_repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior_body), cwd=prior_repo)
    run_cli("session", "end", "--summary", str(prior_body), cwd=prior_repo)
    run_cli("work", "start", "checkout redirect callback state marker", cwd=prior_repo)

    blocked = run_cli(
        "work",
        "start",
        "second task",
        "--group",
        "missing-group",
        cwd=prior_repo,
        expected_returncode=3,
    )
    assert "Context review is still pending" in blocked.stderr


def test_control_avoids_redundant_index_updates(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", commit=True)
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect index efficiency marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-session", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)

    store = repo / ".agentdir"
    real_update = control.update_index
    calls = 0

    def counted_update(root: str | Path):
        nonlocal calls
        calls += 1
        return real_update(root)

    # Fresh start_work with emit_context=False should not call control.update_index itself.
    with mock.patch.object(control, "update_index", side_effect=counted_update):
        control.start_work(store, "no-context fresh start", emit_context=False)
    assert calls == 0

    run_cli("work", "finish", "--no-doctor", cwd=repo)

    # Older explicit session with a context pack: build_final_report must rebuild once.
    started = json.loads(
        run_cli("work", "start", "checkout redirect index efficiency marker", "--json", cwd=repo).stdout
    )
    older_session = started["session"]["session_id"]
    run_cli("session", "start", "--id", "newer-session", cwd=repo)

    calls = 0
    with (
        mock.patch.object(control, "update_index", side_effect=counted_update),
        mock.patch.object(review, "update_index", side_effect=counted_update),
    ):
        control.build_final_report(store, session_id=older_session, run_health_check=False)
    assert calls == 1

    calls = 0
    with (
        mock.patch.object(control, "update_index", side_effect=counted_update),
        mock.patch.object(review, "update_index", side_effect=counted_update),
        mock.patch.object(control, "expand_context_source", return_value={"ok": True}),
    ):
        control.expand_work_context(store, source_selector="1", session_id=older_session)
    assert calls == 1

    calls = 0
    with (
        mock.patch.object(control, "update_index", side_effect=counted_update),
        mock.patch.object(review, "update_index", side_effect=counted_update),
        mock.patch.object(control, "review_context_pack", return_value={"ok": True}),
    ):
        control.review_work_context(
            store,
            disposition="no_relevant",
            reason="not needed",
            session_id=older_session,
        )
    assert calls == 1


def test_resolve_session_rejects_unknown_and_invalid_session_ids(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    store = repo / ".agentdir"
    init_root(store)

    unknown_report = run_cli(
        "report",
        "final",
        "--session",
        "does-not-exist",
        "--no-doctor",
        cwd=repo,
        expected_returncode=3,
    )
    assert "Unknown session: does-not-exist" in unknown_report.stderr
    assert not (store / "sessions" / "does-not-exist").exists()

    unknown_finish = run_cli(
        "work",
        "finish",
        "--session",
        "does-not-exist",
        "--keep-session",
        "--no-doctor",
        cwd=repo,
        expected_returncode=3,
    )
    assert "Unknown session: does-not-exist" in unknown_finish.stderr
    assert not (store / "sessions" / "does-not-exist").exists()

    invalid_report = run_cli(
        "report",
        "final",
        "--session",
        "../escape",
        "--no-doctor",
        cwd=repo,
        expected_returncode=2,
    )
    assert "Invalid session id" in invalid_report.stderr

    # Existing session with events still resolves even without a session pointer file.
    emit_event(store, session_id="manual-only", event_type="agent.message", body="hello")
    resolved = control._resolve_session(store, "manual-only")
    assert resolved.session_id == "manual-only"


# --- 5. CLI --quiet, --format json, and read_body() -------------------------


def test_format_json_propagates_to_error_envelope_and_quiet_mode(tmp_path: Path) -> None:
    empty_repo = init_repo(tmp_path / "empty")
    failed = run_cli(
        "report",
        "final",
        "--format",
        "json",
        cwd=empty_repo,
        expected_returncode=3,
    )
    error_payload = json.loads(failed.stdout)
    assert error_payload["success"] is False
    assert error_payload["exit_code"] == 3
    assert error_payload["error_code"] == "AgentDirStateError"

    repo = init_repo(tmp_path / "active")
    run_cli("work", "start", "format json quiet", "--no-context", cwd=repo)
    quiet_json = run_cli(
        "report",
        "final",
        "--format",
        "json",
        "--quiet",
        "--no-doctor",
        cwd=repo,
    )
    report_payload = json.loads(quiet_json.stdout)
    assert report_payload["task"] == "format json quiet"


def test_cli_quiet_restores_stdout_in_process(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    store = repo / ".agentdir"
    init_root(store)
    original_stdout = sys.stdout
    rc = cli.main(["status", "--quiet", "--no-rebuild", "--root", str(store)])
    assert rc == 0
    assert sys.stdout is original_stdout


def test_work_context_expand_rejects_quiet_without_json(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", commit=True)
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect quiet expand marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli("work", "start", "checkout redirect quiet expand marker", "--json", cwd=repo).stdout
    )
    pack_id = started["context_pack"]["pack_id"]

    rejected = run_cli(
        "work",
        "context",
        "--pack",
        pack_id,
        "--expand",
        "1",
        "--quiet",
        cwd=repo,
        expected_returncode=2,
    )
    assert "--quiet cannot be combined with --expand without --json" in rejected.stderr

    run_cli("index", "update", cwd=repo)
    receipts = json.loads(
        run_cli("query", "--type", "context.sources.expanded", "--json", cwd=repo).stdout
    )
    assert receipts == []

    # --quiet combined with --json still emits JSON output and records the receipt.
    allowed = json.loads(
        run_cli(
            "work",
            "context",
            "--pack",
            pack_id,
            "--expand",
            "1",
            "--quiet",
            "--json",
            cwd=repo,
        ).stdout
    )
    assert allowed["pack_id"] == pack_id
    assert allowed["receipt"]["recorded"] is True


def test_read_body_rejects_empty_missing_and_invalid_utf8_and_clarifies_help(
    tmp_path: Path,
) -> None:
    try:
        cli.read_body("")
    except AgentDirError as exc:
        assert "empty" in str(exc).lower()
    else:
        raise AssertionError("read_body('') should raise AgentDirError")

    missing = tmp_path / "missing.txt"
    try:
        cli.read_body(str(missing))
    except AgentDirError as exc:
        assert "Could not read file" in str(exc)
    else:
        raise AssertionError("read_body(missing) should raise AgentDirError")

    bad_utf8 = tmp_path / "bad.bin"
    bad_utf8.write_bytes(b"\xff\xfe\xfa")
    try:
        cli.read_body(str(bad_utf8))
    except AgentDirError as exc:
        assert "UTF-8" in str(exc)
    else:
        raise AssertionError("read_body(bad_utf8) should raise AgentDirError")

    with mock.patch.object(sys, "stdin", io.StringIO("stdin content")):
        assert cli.read_body(None) == "stdin content"
    with mock.patch.object(sys, "stdin", io.StringIO("stdin content")):
        assert cli.read_body("-") == "stdin content"

    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "read body cli", "--no-context", cwd=repo)

    empty_cli = run_cli("audit", "claims", "--text", "", cwd=repo, expected_returncode=2)
    assert "empty" in empty_cli.stderr.lower()
    assert "Traceback" not in empty_cli.stderr

    missing_cli = run_cli("audit", "claims", "--text", str(missing), cwd=repo, expected_returncode=2)
    assert "Could not read file" in missing_cli.stderr
    assert "Traceback" not in missing_cli.stderr

    help_out = run_cli("audit", "claims", "--help", cwd=repo).stdout
    assert "path to prose file, or '-' for stdin" in help_out
