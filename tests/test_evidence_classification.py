from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from agentdir.review import classify_evidence_command, evidence_failure_state


def find_project_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError("could not locate project root")


PROJECT_ROOT = find_project_root()
SRC_ROOT = PROJECT_ROOT / "src"


def run_cli(
    *args: str,
    cwd: Path,
    expected_returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_ROOT)
    result = subprocess.run(
        [sys.executable, "-m", "agentdir", *args],
        cwd=cwd,
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
    run_cli("init", cwd=path)
    return path


def parse_trailing_json(stdout: str) -> dict[str, object]:
    lines = stdout.splitlines()
    start = lines.index("{")
    return json.loads("\n".join(lines[start:]))


def test_git_diff_check_ignores_release_word_in_cwd(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "release-candidate")

    run_cli("run", "--", "git", "diff", "--check", cwd=repo)

    brief = json.loads(run_cli("evidence", "--brief", "--json", cwd=repo).stdout)
    assert brief["latest_by_family"]["diagnostic"]["tool"] == "git"
    assert "release" not in brief["latest_by_family"]


def test_compileall_is_build_evidence_even_when_tests_is_an_argument(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")

    run_cli("run", "--", sys.executable, "-m", "compileall", "-q", "src", "tests", cwd=repo)

    brief = json.loads(run_cli("evidence", "--brief", "--json", cwd=repo).stdout)
    assert brief["latest_by_family"]["build"]["tool"] == Path(sys.executable).name
    assert "test" not in brief["latest_by_family"]


def test_run_reports_recorded_family_without_polluting_child_stdout(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")

    result = run_cli(
        "run",
        "--name",
        "pytest",
        "--",
        sys.executable,
        "-c",
        "print('child output')",
        cwd=repo,
    )

    assert result.stdout == "child output\n"
    assert result.stderr == "agentdir: recorded evidence family=test\n"


def test_run_separates_footer_from_non_newline_child_stderr(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")

    result = run_cli(
        "run",
        "--name",
        "pytest",
        "--",
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('child error')",
        cwd=repo,
    )

    assert result.stderr == "child error\nagentdir: recorded evidence family=test\n"

    terminated = run_cli(
        "run",
        "--name",
        "pytest",
        "--",
        sys.executable,
        "-c",
        "import sys; print('child error', file=sys.stderr)",
        cwd=repo,
    )
    assert terminated.stderr == "child error\nagentdir: recorded evidence family=test\n"


def test_run_json_reports_family_in_summary_without_text_footer(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")

    result = run_cli(
        "run",
        "--name",
        "pytest",
        "--json",
        "--",
        sys.executable,
        "-c",
        "print('child output')",
        cwd=repo,
    )
    summary = parse_trailing_json(result.stdout)

    assert summary["evidence_family"] == "test"
    assert result.stderr == ""


def test_posix_test_utility_is_diagnostic_evidence(tmp_path: Path) -> None:
    assert classify_evidence_command(["test", "-d", "src"]) == "diagnostic"
    assert classify_evidence_command(["/usr/bin/test", "-f", "artifact"]) == "diagnostic"

    repo = init_repo(tmp_path / "repo")
    run_cli("run", "--", "test", "-d", ".", cwd=repo)

    brief = json.loads(run_cli("evidence", "--brief", "--json", cwd=repo).stdout)
    assert brief["latest_by_family"]["diagnostic"]["tool"] == "test"
    assert "test" not in brief["latest_by_family"]


def test_python_module_runner_outranks_tests_operand() -> None:
    assert classify_evidence_command(
        ["python", "-m", "ruff", "check", "src", "tests"]
    ) == "lint"
    assert classify_evidence_command(
        ["python", "-m", "mypy", "src", "tests"]
    ) == "typecheck"
    assert classify_evidence_command(
        ["python", "-m", "py_compile", "src/sample.py"]
    ) == "build"


def test_unknown_python_module_does_not_promote_an_operand_identity() -> None:
    assert classify_evidence_command(
        ["python", "-m", "pydoc", "py_compile"]
    ) == "diagnostic"
    assert classify_evidence_command(
        ["uv", "run", "python", "-m", "pydoc", "py_compile"]
    ) == "diagnostic"


def test_env_prefix_classifies_the_underlying_command_not_assignments(tmp_path: Path) -> None:
    assert classify_evidence_command(
        ["env", "PYTHONPYCACHEPREFIX=/tmp/cache", "python", "-m", "py_compile", "src/a.py"]
    ) == "build"
    assert classify_evidence_command(
        ["env", "-i", "TEST_RUNNER=ruff", "python", "scripts/validate.py"]
    ) == "diagnostic"
    assert classify_evidence_command(
        ["env", "--unset=PYTHONPATH", "uv", "run", "pytest"]
    ) == "test"
    assert classify_evidence_command(
        ["env", "--", "COVERAGE_FILE=/tmp/coverage", "python", "-m", "pytest"]
    ) == "test"
    assert classify_evidence_command(
        ["env", "A-B=value", "python", "-m", "py_compile", "src/a.py"]
    ) == "build"
    assert classify_evidence_command(
        ["env", "-C/tmp", "-P", "/usr/bin", "-uPATH", "python", "-m", "pytest"]
    ) == "test"
    assert classify_evidence_command(
        ["env", "-ivC/tmp", "python", "-m", "py_compile", "src/a.py"]
    ) == "build"
    assert classify_evidence_command(["env", "-iSpytest -q", "tests"]) == "test"
    assert classify_evidence_command(["env", "-Spytest -q", "tests"]) == "test"
    assert classify_evidence_command(
        ["env", "--split-string=python -m py_compile", "src/a.py"]
    ) == "build"
    assert classify_evidence_command(["env", "--", "-i", "pytest"]) == "diagnostic"
    assert classify_evidence_command(
        ["env", "SETTING=value", "-i", "pytest"]
    ) == "diagnostic"
    assert classify_evidence_command(["env", "--unknown", "pytest"]) == "diagnostic"
    assert classify_evidence_command(
        ["env", "--definitely-unknown=value", "pytest"]
    ) == "diagnostic"
    assert classify_evidence_command(["env", "-Q=value", "pytest"]) == "diagnostic"
    assert classify_evidence_command(["env", "-0", "pytest"]) == "diagnostic"
    assert classify_evidence_command(["env", "--null", "pytest"]) == "diagnostic"
    assert classify_evidence_command(["echo", "pytest"]) == "diagnostic"
    assert classify_evidence_command(["env", "echo", "pytest"]) == "diagnostic"
    assert classify_evidence_command(["env", "-S", "echo pytest"]) == "diagnostic"
    assert classify_evidence_command(
        ["env", "-S", r"\pytest --version"]
    ) == "diagnostic"
    assert classify_evidence_command(
        ["env", "-S", r"\c pytest --version"]
    ) == "diagnostic"
    assert classify_evidence_command(
        ["env", "-S", "echo\u00a0pytest"]
    ) == "diagnostic"
    assert classify_evidence_command(
        ["env", "-S", "-i '#literal'", "pytest", "--version"]
    ) == "diagnostic"
    assert classify_evidence_command(
        ["env", "-S", "'pytest --version'"]
    ) == "diagnostic"

    repo = init_repo(tmp_path / "repo")
    (repo / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    run_cli(
        "run",
        "--",
        "env",
        f"PYTHONPYCACHEPREFIX={tmp_path / 'pycache'}",
        sys.executable,
        "-m",
        "py_compile",
        "sample.py",
        cwd=repo,
    )

    brief = json.loads(run_cli("evidence", "--brief", "--json", cwd=repo).stdout)
    assert brief["latest_by_family"]["build"]["tool"] == "env"
    assert "diagnostic" not in brief["latest_by_family"]


def test_package_script_outranks_tests_operand() -> None:
    assert classify_evidence_command(["npm", "run", "lint", "--", "tests"]) == "lint"
    assert classify_evidence_command(["npx", "ruff", "check", "tests"]) == "lint"


def test_compiler_subcommand_outranks_path_like_test_operands() -> None:
    assert classify_evidence_command(["cargo", "build", "--target-dir", "tests"]) == "build"
    assert classify_evidence_command(["go", "build", "./tests"]) == "build"
    assert classify_evidence_command(["make", "lint", "TESTS=tests"]) == "lint"


def test_agentdir_doctor_is_explicit_doctor_evidence_without_operand_promotion() -> None:
    assert classify_evidence_command(["agentdir", "doctor", "--json"]) == "doctor"
    assert classify_evidence_command(
        ["/usr/local/bin/agentdir", "doctor"]
    ) == "doctor"
    assert classify_evidence_command(
        ["agentdir", "status", "doctor"]
    ) == "diagnostic"


def test_common_structured_runners_keep_explicit_subcommand_families() -> None:
    expected = {
        ("just", "test"): "test",
        ("just", "lint"): "lint",
        ("swift", "test"): "test",
        ("swift", "build"): "build",
        ("dotnet", "test"): "test",
        ("dotnet", "build"): "build",
        ("deno", "test"): "test",
        ("deno", "lint"): "lint",
        ("gradle", "test"): "test",
        ("bazel", "test"): "test",
        ("xcodebuild", "test"): "test",
        ("xcodebuild", "build-for-testing"): "build",
        ("xcodebuild", "test-without-building"): "test",
        ("gradle", ":app:test"): "test",
        ("gradlew", ":app:build"): "build",
        ("mvn", "integration-test"): "test",
        ("mvnw", "integration-test"): "test",
    }
    for command, family in expected.items():
        assert classify_evidence_command(command) == family
    assert classify_evidence_command(
        ["npm", "--unknown-option", "test", "run", "build"]
    ) == "diagnostic"
    assert classify_evidence_command(
        ["mvn", "--unknown-option", "test"]
    ) == "diagnostic"
    assert classify_evidence_command(["go", "-cover", "test"]) == "diagnostic"
    assert classify_evidence_command(["make", "-check", "test"]) == "diagnostic"
    assert classify_evidence_command(
        ["make", "../test=value", "build"]
    ) == "diagnostic"

    assert classify_evidence_command(
        ["just", "--working-directory", "test", "build"]
    ) == "diagnostic"
    assert classify_evidence_command(
        ["dotnet", "--info", "test"]
    ) == "diagnostic"
    assert classify_evidence_command(["mvn", "clean", "test"]) == "test"
    assert classify_evidence_command(["gradle", "clean", "test"]) == "test"
    assert classify_evidence_command(["xcodebuild", "clean", "test"]) == "test"
    for phase in (
        "test-compile",
        "generate-test-sources",
        "process-test-resources",
        "pre-integration-test",
        "post-integration-test",
    ):
        assert classify_evidence_command(["mvn", phase]) == "diagnostic"
    assert classify_evidence_command(
        ["dotnet", "build-server", "shutdown"]
    ) == "diagnostic"


def test_runner_options_do_not_hide_or_impersonate_the_executed_action() -> None:
    expected = {
        ("mvn", "-q", "test"): "test",
        ("gradle", "--no-daemon", "test"): "test",
        ("xcodebuild", "-scheme", "App", "test"): "test",
        ("swift", "--package-path", ".", "test"): "test",
        ("bazel", "--output_base=/tmp/b", "test", "//..."): "test",
        ("dotnet", "--verbosity", "quiet", "test"): "test",
        ("cargo", "--target-dir", "test", "build"): "build",
        ("npm", "--prefix", "test", "run", "build"): "build",
        ("pnpm", "--dir", "test", "run", "build"): "build",
        ("make", "--directory", "test", "build"): "build",
        ("make", "TEST=unit", "build"): "build",
        ("make", "TEST=unit"): "build",
        ("just", "TEST=unit", "build"): "build",
        ("go", "-C", "test", "build"): "build",
    }
    for command, family in expected.items():
        assert classify_evidence_command(command) == family


def test_uv_run_classifies_the_wrapped_command_not_dependency_options() -> None:
    assert classify_evidence_command(
        ["uv", "run", "--with", "ruff", "pytest"]
    ) == "test"
    assert classify_evidence_command(["uv", "run", "--", "pytest"]) == "test"
    assert classify_evidence_command(
        ["uv", "run", "python", "-m", "pytest", "tests"]
    ) == "test"
    assert classify_evidence_command(
        ["uv", "run", "--with", "pytest", "python", "-m", "compileall", "src", "tests"]
    ) == "build"
    assert classify_evidence_command(["uv", "run", "ruff", "check", "tests"]) == "lint"
    assert classify_evidence_command(
        ["uv", "run", "--fork-strategy", "fewest", "pytest"]
    ) == "test"


def test_generic_validation_ignores_release_word_in_path_operand() -> None:
    assert classify_evidence_command(
        ["python", "scripts/validate.py", "/tmp/release-candidate/config.json"]
    ) == "diagnostic"


def test_failed_brief_keeps_current_family_state_after_a_passing_rerun(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli(
        "run",
        "--name",
        "pytest",
        "--",
        sys.executable,
        "-c",
        "import sys; sys.exit(1)",
        cwd=repo,
        expected_returncode=1,
    )
    run_cli(
        "run",
        "--name",
        "pytest",
        "--",
        sys.executable,
        "-c",
        "print('repaired')",
        cwd=repo,
    )

    brief = json.loads(
        run_cli("evidence", "--brief", "--failed", "--json", cwd=repo).stdout
    )
    family = next(item for item in brief["families"] if item["family"] == "test")

    assert brief["counts"]["resolved_failed"] == 1
    assert brief["counts"]["unresolved_failed"] == 0
    assert brief["latest_by_family"]["test"]["exit_code"] == 0
    assert family["resolved_failed"] == 1
    assert family["unresolved_failed"] == 0
    assert family["currently_failing"] is False


def test_unknown_result_does_not_resolve_an_earlier_failure() -> None:
    failure = {
        "event_type": "tool.result",
        "tool": "pytest",
        "tool_exit_code": 1,
    }
    unknown = {
        "event_type": "tool.result",
        "tool": "pytest",
        "tool_exit_code": None,
    }

    state = evidence_failure_state([failure, unknown])

    assert state["historical"] == [failure]
    assert state["resolved"] == []
    assert state["unresolved"] == [failure]
