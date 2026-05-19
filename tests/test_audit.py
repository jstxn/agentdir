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
    env_extra: dict[str, str] | None = None,
    expected_returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_ROOT)
    if env_extra:
        env.update(env_extra)
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


def test_audit_session_reports_advisory_gaps_without_strict_failure(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "audit without evidence", cwd=repo)

    result = run_cli("audit", "session", "--json", cwd=repo)
    payload = json.loads(result.stdout)

    assert payload["ok"] is True
    assert check(payload, "session_started")["status"] == "pass"
    assert check(payload, "session_finished")["status"] == "warn"
    assert check(payload, "evidence_present")["status"] == "warn"
    assert check(payload, "context_pack_created")["status"] == "warn"


def test_audit_session_strict_fails_on_failed_tool_result(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "audit failed evidence", cwd=repo)
    run_cli(
        "run",
        "--name",
        "pytest",
        "--",
        sys.executable,
        "-c",
        "import sys; sys.exit(7)",
        cwd=repo,
        expected_returncode=7,
    )

    advisory = run_cli("audit", "session", "--json", cwd=repo)
    strict = run_cli("audit", "session", "--json", "--strict", cwd=repo, expected_returncode=1)
    advisory_payload = json.loads(advisory.stdout)
    strict_payload = json.loads(strict.stdout)

    assert advisory_payload["ok"] is False
    assert strict_payload["strict"] is True
    assert check(strict_payload, "failed_tool_results")["status"] == "fail"


def test_audit_claims_supports_contradicts_and_flags_unsupported_families(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "claims audit", cwd=repo)
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
    run_cli(
        "run",
        "--name",
        "build",
        "--",
        sys.executable,
        "-c",
        "import sys; sys.exit(2)",
        cwd=repo,
        expected_returncode=2,
    )

    text = "Tests passed. Build passed. Lint passed."
    result = run_cli("audit", "claims", "--text", "-", "--json", cwd=repo, input_text=text)
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
    payload = json.loads(result.stdout)
    strict_payload = json.loads(strict.stdout)

    assert payload["ok"] is False
    assert strict_payload["strict"] is True
    assert claim(payload, "test")["status"] == "supported"
    assert claim(payload, "build")["status"] == "contradicted"
    assert claim(payload, "lint")["status"] == "unsupported"


def test_report_final_can_include_claim_support(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    claims = tmp_path / "claims.txt"
    claims.write_text("Tests passed.", encoding="utf-8")
    run_cli("work", "start", "claim report", cwd=repo)
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

    report = run_cli("report", "final", "--format", "json", "--claims", str(claims), cwd=repo)
    payload = json.loads(report.stdout)

    assert payload["session_audit"]["summary"]["session_id"] == payload["summary"]["session_id"]
    assert payload["claim_support"]["claims"][0]["family"] == "test"
    assert payload["claim_support"]["claims"][0]["status"] == "supported"
    assert payload["agent_handoff"]["status"] == "ok"
    assert payload["agent_handoff"]["claim_support"]["claims"][0]["status"] == "supported"
    assert payload["agent_handoff"]["verification"][0]["family"] == "test"


def test_agent_handoff_surfaces_failed_evidence_and_claim_gaps(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    claims = tmp_path / "claims.txt"
    claims.write_text("Build passed. Typecheck passed.", encoding="utf-8")
    run_cli("work", "start", "handoff failed evidence", cwd=repo)
    run_cli(
        "run",
        "--name",
        "build",
        "--",
        sys.executable,
        "-c",
        "import sys; sys.exit(4)",
        cwd=repo,
        expected_returncode=4,
    )

    report = run_cli("report", "final", "--format", "json", "--claims", str(claims), cwd=repo)
    payload = json.loads(report.stdout)
    handoff = payload["agent_handoff"]

    assert handoff["status"] == "needs_attention"
    assert handoff["failed_evidence"][0]["family"] == "build"
    assert any(claim["status"] == "contradicted" for claim in handoff["claim_support"]["claims"])
    assert any(claim["status"] == "unsupported" for claim in handoff["claim_support"]["claims"])
    assert handoff["recommended_agent_actions"]


def test_evidence_brief_filters_and_timeline_json(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "evidence skim", cwd=repo)
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
    run_cli(
        "run",
        "--name",
        "build",
        "--",
        sys.executable,
        "-c",
        "import sys; sys.exit(2)",
        cwd=repo,
        expected_returncode=2,
    )

    brief = json.loads(run_cli("evidence", "--brief", "--json", cwd=repo).stdout)
    failed = json.loads(run_cli("evidence", "--failed", "--json", cwd=repo).stdout)
    tests = json.loads(run_cli("evidence", "--family", "test", "--json", cwd=repo).stdout)
    timeline = json.loads(run_cli("timeline", "--json", "--limit", "10", cwd=repo).stdout)

    assert brief["latest_by_family"]["test"]["exit_code"] == 0
    assert brief["latest_by_family"]["build"]["exit_code"] == 2
    assert brief["failed_evidence"][0]["family"] == "build"
    assert {row["family"] for row in failed} == {"build"}
    assert {row["family"] for row in tests} == {"test"}
    assert timeline[0]["event_type"] == "session.started"
    assert any(row["event_type"] == "tool.result" and row.get("family") == "build" for row in timeline)


def test_index_rebuild_json_flag_and_replay_recover_deleted_index(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "replay recovery", cwd=repo)
    run_cli(
        "run",
        "--name",
        "pytest",
        "--",
        sys.executable,
        "-c",
        "print('replay recovery evidence')",
        cwd=repo,
    )
    session_id = json.loads(run_cli("session", "current", "--json", cwd=repo).stdout)["session_id"]
    db_path = repo / ".agentdir" / "indexes" / "agentdir.sqlite3"

    rebuilt = run_cli("index", "rebuild", "--json", cwd=repo)
    assert json.loads(rebuilt.stdout)["malformed"] == 0
    db_path.unlink()
    replay = run_cli("replay", "--session", session_id, cwd=repo)

    assert db_path.is_file()
    assert "tool.result pytest exit 0" in replay.stdout


def test_work_start_context_excludes_current_session_self_hits(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")

    started = run_cli("work", "start", "fresh self hit check", "--emit-context", "--json", cwd=repo)
    payload = json.loads(started.stdout)

    assert payload["context"]["memory_hits"] == 0
    assert payload["context"]["recent_session_summaries"] == 0
    assert payload["context_pack"]["source_counts"] == {
        "evidence": 0,
        "retrieval_hint": 0,
        "summary": 0,
    }


def test_work_finish_keeps_git_dirty_visible_but_out_of_known_gaps(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "dirty final report", cwd=repo)
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
    (repo / "uncommitted.txt").write_text("dirty worktree marker\n", encoding="utf-8")

    finished = run_cli("work", "finish", "--json", cwd=repo)
    payload = json.loads(finished.stdout)
    run_cli("index", "rebuild", cwd=repo)
    rendered = run_cli("replay", "--session", payload["ended_session"]["session_id"], cwd=repo).stdout

    assert any(check["id"] == "git_dirty" and check["status"] == "warn" for check in payload["report"]["session_audit"]["checks"])
    assert not any(gap.startswith("git_dirty:") for gap in payload["report"]["known_gaps"])
    assert "events_before_final_report" in rendered


def test_audit_session_git_dirty_uses_requested_root_not_process_cwd(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    other = init_repo(tmp_path / "other")
    run_cli("work", "start", "dirty root audit", cwd=repo)
    (repo / "uncommitted.txt").write_text("dirty worktree marker\n", encoding="utf-8")

    result = run_cli("audit", "session", "--root", str(repo / ".agentdir"), "--json", cwd=other)
    payload = json.loads(result.stdout)

    assert check(payload, "git_dirty")["status"] == "warn"


def test_generic_guidance_store_and_project_targets_preserve_existing_agents_file(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    agents = repo / "AGENTS.md"
    agents.write_text("# Local Notes\n\nKeep this project note.\n", encoding="utf-8")

    store = run_cli("skills", "install", "generic", "--target", "store", "--json", cwd=repo)
    project = run_cli("skills", "install", "generic", "--target", "project", "--json", cwd=repo)
    rerun = run_cli("skills", "install", "generic", "--target", "project", "--json", cwd=repo)
    adopt = run_cli(
        "adopt",
        "--install-skill",
        "store",
        "--install-generic",
        "store",
        "--json",
        cwd=repo,
    )

    store_payload = json.loads(store.stdout)
    project_payload = json.loads(project.stdout)
    rerun_payload = json.loads(rerun.stdout)
    adopt_payload = json.loads(adopt.stdout)
    text = agents.read_text(encoding="utf-8")

    assert store_payload["path"].endswith(".agentdir/integrations/generic/AGENTS.md")
    assert project_payload["path"] == str(agents)
    assert rerun_payload["path"] == str(agents)
    assert "Keep this project note." in text
    assert text.count("agentdir-managed-generic:start") == 1
    assert adopt_payload["generic_guidance"].endswith(".agentdir/integrations/generic/AGENTS.md")


def test_dogfood_session_respects_agentdir_python_for_source_fallback(tmp_path: Path) -> None:
    root = tmp_path / "root"
    env = {
        **os.environ,
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "AGENTDIR_PYTHON": sys.executable,
    }
    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "examples" / "dogfood-session.sh"), str(root)],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert "agentdir index rebuild" in result.stdout


def test_dogfood_session_rejects_unsupported_agentdir_python(tmp_path: Path) -> None:
    unsupported = tmp_path / "unsupported-python"
    unsupported.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    unsupported.chmod(0o755)
    env = {
        **os.environ,
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "AGENTDIR_PYTHON": str(unsupported),
    }
    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "examples" / "dogfood-session.sh"), str(tmp_path / "root")],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "AGENTDIR_PYTHON must be Python 3.11 or newer" in result.stderr
