from __future__ import annotations

import json
import os
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


def check(payload: dict[str, object], check_id: str) -> dict[str, object]:
    for item in payload["checks"]:  # type: ignore[index]
        if item["id"] == check_id:
            return item
    raise AssertionError(f"missing check {check_id}")


def claim(payload: dict[str, object], family: str) -> dict[str, object]:
    for item in payload["claims"]:  # type: ignore[index]
        if item["family"] == family:
            return item
    raise AssertionError(f"missing claim family {family}")


def run_truncated_pytest(repo: Path) -> subprocess.CompletedProcess[str]:
    return run_cli(
        "run",
        "--name",
        "pytest",
        "--max-capture-bytes",
        "64",
        "--",
        sys.executable,
        "-c",
        "print('tests passed ' * 50)",
        cwd=repo,
    )


def test_run_warns_on_truncated_output(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "truncation warning", cwd=repo)

    result = run_truncated_pytest(repo)

    assert "agentdir: warning: captured stdout truncated at 64 bytes" in result.stderr
    assert "recorded evidence is partial" in result.stderr


def test_run_does_not_warn_within_limit(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "no truncation", cwd=repo)

    result = run_cli(
        "run",
        "--name",
        "pytest",
        "--",
        sys.executable,
        "-c",
        "print('tests passed')",
        cwd=repo,
    )

    assert "truncated" not in result.stderr


def test_truncated_result_records_header_and_evidence_flag(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "truncation evidence", cwd=repo)
    run_truncated_pytest(repo)

    envelopes = [path for path in (repo / ".agentdir" / "sessions").rglob("*") if path.is_file()]
    result_envelopes = [
        path
        for path in envelopes
        if "X-AgentDir-Truncated: stdout" in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert result_envelopes, "expected a tool.result envelope with X-AgentDir-Truncated header"

    rows = json.loads(run_cli("evidence", "--json", cwd=repo).stdout)
    results = [row for row in rows if row.get("event_type") == "tool.result"]
    assert results and all(row["truncated"] is True for row in results)

    brief = json.loads(run_cli("evidence", "--brief", "--json", cwd=repo).stdout)
    assert brief["latest_by_family"]["test"]["truncated"] is True


def test_untruncated_result_has_no_flag(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "clean evidence", cwd=repo)
    run_cli(
        "run",
        "--name",
        "pytest",
        "--",
        sys.executable,
        "-c",
        "print('tests passed')",
        cwd=repo,
    )

    rows = json.loads(run_cli("evidence", "--json", cwd=repo).stdout)
    results = [row for row in rows if row.get("event_type") == "tool.result"]
    assert results and all(row["truncated"] is False for row in results)


def test_tool_output_cannot_spoof_truncation_flag(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "spoof attempt", cwd=repo)
    run_cli(
        "run",
        "--name",
        "pytest",
        "--",
        sys.executable,
        "-c",
        "print('stdout_truncated=true')",
        cwd=repo,
    )

    rows = json.loads(run_cli("evidence", "--json", cwd=repo).stdout)
    results = [row for row in rows if row.get("event_type") == "tool.result"]
    assert results and all(row["truncated"] is False for row in results)


def test_audit_session_warns_on_truncated_evidence(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "session truncation audit", cwd=repo)
    run_truncated_pytest(repo)

    payload = json.loads(run_cli("audit", "session", "--json", cwd=repo).stdout)

    assert payload["ok"] is True
    item = check(payload, "truncated_evidence")
    assert item["status"] == "warn"
    assert "truncated" in item["message"]


def test_audit_session_passes_without_truncation(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "session clean audit", cwd=repo)
    run_cli(
        "run",
        "--name",
        "pytest",
        "--",
        sys.executable,
        "-c",
        "print('tests passed')",
        cwd=repo,
    )

    payload = json.loads(run_cli("audit", "session", "--json", cwd=repo).stdout)
    assert check(payload, "truncated_evidence")["status"] == "pass"


def test_audit_claims_partial_on_truncated_success(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "claims truncation audit", cwd=repo)
    run_truncated_pytest(repo)

    text = "Tests passed."
    payload = json.loads(
        run_cli("audit", "claims", "--text", "-", "--json", cwd=repo, input_text=text).stdout
    )
    strict = run_cli(
        "audit",
        "claims",
        "--text",
        "-",
        "--json",
        "--strict",
        cwd=repo,
        input_text=text,
        expected_returncode=1,
    )
    strict_payload = json.loads(strict.stdout)

    test_claim = claim(payload, "test")
    assert test_claim["status"] == "partial"
    assert "truncated" in test_claim["message"]
    assert test_claim["evidence"]["truncated"] is True
    # Advisory mode stays ok; strict mode refuses partial evidence.
    assert payload["ok"] is True
    assert strict_payload["strict"] is True
    assert claim(strict_payload, "test")["status"] == "partial"


def test_audit_claims_supported_when_not_truncated(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "claims clean audit", cwd=repo)
    run_cli(
        "run",
        "--name",
        "pytest",
        "--",
        sys.executable,
        "-c",
        "print('tests passed')",
        cwd=repo,
    )

    payload = json.loads(
        run_cli("audit", "claims", "--text", "-", "--json", cwd=repo, input_text="Tests passed.").stdout
    )
    test_claim = claim(payload, "test")
    assert test_claim["status"] == "supported"
    assert test_claim["evidence"]["truncated"] is False
