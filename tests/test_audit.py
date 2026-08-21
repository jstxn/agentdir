from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from agentdir.context import build_context_manifest


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
    assert check(payload, "context_pack_created")["status"] == "pass"


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


def test_post_finish_status_and_read_commands_project_latest_session(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    started = json.loads(
        run_cli("work", "start", "post-finish evidence projection", "--json", cwd=repo).stdout
    )
    session_id = started["session"]["session_id"]
    run_cli(
        "run",
        "--name",
        "pytest",
        "--",
        sys.executable,
        "-c",
        "print('post-finish evidence passed')",
        cwd=repo,
    )
    run_cli("claim", "test", "--passed", cwd=repo)
    run_cli("work", "finish", "--json", cwd=repo)

    status = json.loads(run_cli("status", "--json", cwd=repo).stdout)
    brief = json.loads(run_cli("evidence", "--brief", "--json", cwd=repo).stdout)
    timeline = json.loads(run_cli("timeline", "--json", cwd=repo).stdout)

    assert status["session"]["active"] is False
    assert status["session"]["current"] is None
    assert status["session"]["latest"]["session_id"] == session_id
    assert status["session"]["summary"]["session_id"] == session_id
    assert status["evidence"]["session_id"] == session_id
    assert status["evidence"]["count"] >= 2
    assert brief["latest_by_family"]["test"]["exit_code"] == 0
    assert timeline[0]["event_type"] == "session.started"
    assert timeline[-1]["event_type"] == "session.ended"


def test_newer_passing_evidence_resolves_prior_failure_without_erasing_history(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "repair a transient test failure", cwd=repo)
    run_cli(
        "run",
        "--name",
        "pytest",
        "--",
        sys.executable,
        "-c",
        "import sys; sys.exit(3)",
        cwd=repo,
        expected_returncode=3,
    )
    run_cli(
        "run",
        "--name",
        "pytest",
        "--",
        sys.executable,
        "-c",
        "print('repaired test passed')",
        cwd=repo,
    )
    run_cli("claim", "test", "--passed", cwd=repo)

    audit = json.loads(run_cli("audit", "session", "--strict", "--json", cwd=repo).stdout)
    report = json.loads(run_cli("report", "final", "--format", "json", cwd=repo).stdout)
    handoff = report["agent_handoff"]
    test_verification = next(
        item for item in handoff["verification"] if item["family"] == "test"
    )

    assert check(audit, "failed_tool_results")["status"] == "pass"
    assert check(audit, "resolved_failed_tool_results")["status"] == "pass"
    assert handoff["status"] == "ok"
    assert handoff["failed_evidence"] == []
    assert handoff["unresolved_failed_evidence"] == []
    assert len(handoff["resolved_failed_evidence"]) == 1
    assert len(handoff["historical_failed_evidence"]) == 1
    assert test_verification["failed"] == 1
    assert test_verification["resolved_failed"] == 1
    assert test_verification["unresolved_failed"] == 0
    assert test_verification["currently_failing"] is False


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


def test_work_start_does_not_treat_workspace_name_only_as_relevant_memory(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "agentdir")
    body = tmp_path / "body.txt"
    body.write_text("agentdir agentdir generic wheel build log", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(body), cwd=repo)
    run_cli("session", "end", "--summary", str(body), cwd=repo)

    started = run_cli(
        "work",
        "start",
        "work on AgentDir",
        "--json",
        cwd=repo,
    )
    payload = json.loads(started.stdout)

    assert payload["context"]["retrieval_query"] == ""
    assert payload["context_pack"]["retrieval_query_state"] == "no_specific_terms"
    assert payload["context"]["memory_hits"] == 0
    assert payload["context_briefing"]["match_state"] == "no_strong_prior_context"
    assert all(source["match_quality"] != "strong" for source in payload["context_briefing"]["sources"])


def test_quality_policy_caps_score_only_document_matches_below_strong() -> None:
    manifest = build_context_manifest(
        {
            "task": "checkout redirect callback",
            "retrieval_query": "checkout redirect callback",
            "retrieval_query_state": "specific_terms",
            "session_id": "quality-test",
            "retrieval_mode": "document",
            "memory_hits": [
                {
                    "source_id": "message:unrelated",
                    "body_text": "generic build output with no matching vocabulary",
                    "memory_score": 0.99,
                }
            ],
            "recent_session_summaries": [],
            "evidence": [],
        }
    )

    source = manifest["sources"][0]
    policy = manifest["briefing"]["quality_policy"]
    assert source["match_quality"] == "possible"
    assert source["overlap_terms"] == []
    assert policy["id"] == "agentdir.balanced.v6"
    assert policy["retrieval_mode"] == "document"
    assert policy["score_thresholds"]["semantic_only_strong"] is None
    assert policy["briefing_limit"] == 5
    assert policy["quality_order"][0] == "strong"


def test_briefing_enforces_the_persisted_per_session_cap() -> None:
    manifest = build_context_manifest(
        {
            "task": "checkout redirect callback",
            "retrieval_query": "checkout redirect callback",
            "retrieval_query_state": "specific_terms",
            "session_id": "quality-test",
            "retrieval_mode": "document",
            "memory_hits": [
                {
                    "source_id": f"message:same-session-{index}",
                    "session_id": "same-session",
                    "body_text": f"checkout redirect callback record {index}",
                    "memory_score": 0.9 - index / 100,
                }
                for index in range(5)
            ],
            "recent_session_summaries": [],
            "evidence": [],
        }
    )

    briefing = manifest["briefing"]
    assert briefing["quality_policy"]["max_per_session"] == 2
    assert briefing["presented_count"] == 2


def test_briefing_prefers_substantive_sources_over_lifecycle_and_duplicate_reports() -> None:
    task = "tenant callback retry policy"
    same_session = "relevant-session"
    rows = [
        {
            "source_id": "message:work-started",
            "session_id": same_session,
            "event_type": "work.started",
            "body_text": f"{task} work lifecycle metadata",
            "memory_score": 0.99,
        },
        {
            "source_id": "message:final-report",
            "session_id": same_session,
            "event_type": "work.report.final",
            "body_text": f"{task} redundant generated final report",
            "memory_score": 0.98,
        },
        {
            "source_id": "session:relevant-session:summary",
            "source_kind": "session_summary",
            "session_id": same_session,
            "event_type": "summary.compacted",
            "body_text": f"{task} derived session summary",
            "memory_score": 0.97,
        },
        {
            "source_id": "message:relevant-decision",
            "session_id": same_session,
            "event_type": "decision.recorded",
            "body_text": f"{task} use tenant scoped delivery identity",
            "memory_score": 0.90,
        },
        *[
            {
                "source_id": f"message:substantive-{index}",
                "session_id": f"other-session-{index}",
                "event_type": "decision.recorded" if index % 2 else "agent.message",
                "body_text": f"{task} substantive alternative {index}",
                "memory_score": 0.89 - index / 100,
            }
            for index in range(1, 5)
        ],
    ]

    manifest = build_context_manifest(
        {
            "task": task,
            "retrieval_query": task,
            "retrieval_query_state": "specific_terms",
            "session_id": "quality-test",
            "retrieval_mode": "semantic-hybrid",
            "memory_hits": rows,
            "recent_session_summaries": [],
            "evidence": [],
        }
    )

    source_ids = manifest["briefing"]["source_ids"]
    policy = manifest["briefing"]["quality_policy"]
    assert "message:relevant-decision" in source_ids
    assert "message:work-started" not in source_ids
    assert "message:final-report" not in source_ids
    assert "session:relevant-session:summary" not in source_ids
    assert policy["source_preference_order"][:3] == [
        "current_evidence",
        "decision",
        "evidence",
    ]
    assert policy["source_selection_tiers"][1] == ["decision", "evidence"]
    assert policy["redundant_with_decision_or_evidence_session"] == [
        "lifecycle",
        "final_report",
        "summary",
    ]


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


def test_dogfood_session_can_force_source_when_agentdir_is_on_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    installed = bin_dir / "agentdir"
    installed.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    installed.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "AGENTDIR_FORCE_SOURCE": "1",
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


def _session_with_failed_tests(repo: Path) -> None:
    run_cli("work", "start", "unreviewed claims", cwd=repo)
    run_cli(
        "run",
        "--name",
        "pytest",
        "--",
        sys.executable,
        "-c",
        "import sys; print('FAILED test_x'); sys.exit(1)",
        cwd=repo,
        expected_returncode=1,
    )


def test_audit_claims_flags_failures_outside_a_partial_acknowledgement(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    _session_with_failed_tests(repo)

    payload = json.loads(
        run_cli(
            "audit",
            "claims",
            "--text",
            "-",
            "--json",
            cwd=repo,
            input_text="Lint passed.",
        ).stdout
    )

    assert payload["claims_detected"] == 1
    assert claim(payload, "lint")["status"] == "unsupported"
    assert claim(payload, "test")["status"] == "unreviewed"


def test_audit_claims_does_not_guess_families_for_unreviewed_failures(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "noisy failure", cwd=repo)
    # A realistic failure log mentions many family keywords. Only the family the
    # evidence was actually classified into may be flagged, or one failed command
    # reports itself under every family and buries the real signal.
    run_cli(
        "run",
        "--name",
        "pytest",
        "--",
        sys.executable,
        "-c",
        "import sys; print('FAILED: build step, lint config, typecheck, doctor, release'); sys.exit(1)",
        cwd=repo,
        expected_returncode=1,
    )

    payload = json.loads(
        run_cli(
            "audit",
            "claims",
            "--text",
            "-",
            "--json",
            cwd=repo,
            input_text="Everything works.",
        ).stdout
    )

    unreviewed = [c for c in payload["claims"] if c["status"] == "unreviewed"]
    assert [c["family"] for c in unreviewed] == ["test"]


def test_audit_claims_still_matches_stated_claims_by_keyword(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "keyword fallback", cwd=repo)
    run_cli(
        "run",
        "--name",
        "check",
        "--",
        sys.executable,
        "-c",
        "import sys; print('build failed'); sys.exit(1)",
        cwd=repo,
        expected_returncode=1,
    )

    payload = json.loads(
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

    assert claim(payload, "build")["status"] == "contradicted"
