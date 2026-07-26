"""Matrix for structured claims.

Recorded claims name their family and outcome outright, so this check is a
comparison rather than an interpretation of prose. The matrix mirrors
`test_claim_polarity` so both paths are held to the same outcomes, and
`test_matrix_covers_every_cell` fails unless every claimed-outcome/evidence
combination is exercised.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from test_audit import init_repo, run_cli

PASSED = "passed"
FAILED = "failed"
CLAIM_OUTCOMES = (PASSED, FAILED)

PASSING = "passing"
FAILING = "failing"
MISSING = "missing"  # no evidence recorded for the family at all
EVIDENCE_STATES = (PASSING, FAILING, MISSING)

# (evidence, claimed outcome, expected status, expected ok, strict exit code)
CASES: tuple[tuple[str, str, str, bool, int], ...] = (
    (PASSING, PASSED, "supported", True, 0),
    (PASSING, FAILED, "contradicted", False, 1),
    (FAILING, PASSED, "contradicted", False, 1),
    (FAILING, FAILED, "acknowledged", True, 0),
    (MISSING, PASSED, "unsupported", False, 1),
    (MISSING, FAILED, "unsupported", False, 1),
)


def _repo_with_evidence(base: Path, name: str, evidence: str) -> Path:
    repo = init_repo(base / name)
    run_cli("work", "start", f"structured {name}", cwd=repo)
    if evidence == MISSING:
        return repo
    failing = evidence == FAILING
    script = (
        "import sys; print('FAILED test_x'); sys.exit(1)" if failing else "print('5 tests passed')"
    )
    run_cli(
        "run",
        "--name",
        "pytest",
        "--",
        sys.executable,
        "-c",
        script,
        cwd=repo,
        expected_returncode=1 if failing else 0,
    )
    return repo


@pytest.fixture(scope="module")
def repos(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    base = tmp_path_factory.mktemp("structured")
    return {state: _repo_with_evidence(base, state, state) for state in EVIDENCE_STATES}


@pytest.mark.parametrize(
    ("evidence", "outcome", "expected_status", "expected_ok", "strict_code"),
    CASES,
    ids=[f"{evidence}-claim-{outcome}" for evidence, outcome, *_ in CASES],
)
def test_structured_claim_against_evidence(
    repos: dict[str, Path],
    evidence: str,
    outcome: str,
    expected_status: str,
    expected_ok: bool,
    strict_code: int,
) -> None:
    repo = repos[evidence]
    run_cli("claim", "test", f"--{outcome}", cwd=repo)

    payload = json.loads(run_cli("audit", "claims", "--json", cwd=repo).stdout)

    assert payload["source"] == "recorded"
    statuses = {claim["family"]: claim["status"] for claim in payload["claims"]}
    assert statuses["test"] == expected_status
    assert payload["ok"] is expected_ok

    run_cli("audit", "claims", "--json", "--strict", cwd=repo, expected_returncode=strict_code)


def test_matrix_covers_every_cell() -> None:
    covered = {(evidence, outcome) for evidence, outcome, *_ in CASES}
    missing = {
        (evidence, outcome) for evidence in EVIDENCE_STATES for outcome in CLAIM_OUTCOMES
    } - covered
    assert not missing, f"structured claim matrix has untested cells: {sorted(missing)}"


def test_latest_claim_per_family_replaces_the_earlier_one(tmp_path: Path) -> None:
    repo = _repo_with_evidence(tmp_path, "replace", FAILING)

    run_cli("claim", "test", "--passed", cwd=repo)
    run_cli("claim", "test", "--failed", "--note", "parser regression", cwd=repo)

    payload = json.loads(run_cli("claim", "list", "--json", cwd=repo).stdout)

    assert [(c["family"], c["outcome"]) for c in payload["claims"]] == [("test", "failed")]
    assert payload["claims"][0]["note"] == "parser regression"


def test_failed_evidence_without_a_claim_is_unreviewed(tmp_path: Path) -> None:
    repo = _repo_with_evidence(tmp_path, "unclaimed", FAILING)

    payload = json.loads(run_cli("audit", "claims", "--json", cwd=repo).stdout)

    claim = next(c for c in payload["claims"] if c["family"] == "test")
    assert claim["status"] == "unreviewed"
    assert "no test claim was recorded" in claim["message"]
    assert payload["ok"] is False


def test_unknown_family_is_rejected(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "bad family", cwd=repo)

    # argparse rejects it as an unknown subcommand rather than recording it.
    run_cli("claim", "smoke", "--passed", cwd=repo, expected_returncode=2)


def test_recorded_claims_reach_the_handoff_without_final_text(tmp_path: Path) -> None:
    repo = _repo_with_evidence(tmp_path, "handoff", FAILING)
    run_cli("claim", "test", "--passed", cwd=repo)

    report = json.loads(run_cli("report", "final", "--format", "json", cwd=repo).stdout)

    support = report["claim_support"]
    assert support["source"] == "recorded"
    assert support["ok"] is False
    assert report["agent_handoff"]["status"] == "needs_attention"


def test_retracting_withdraws_a_claim(tmp_path: Path) -> None:
    repo = _repo_with_evidence(tmp_path, "retract", FAILING)
    run_cli("claim", "build", "--passed", cwd=repo)

    unsupported = json.loads(run_cli("audit", "claims", "--json", cwd=repo).stdout)
    assert any(c["family"] == "build" and c["status"] == "unsupported" for c in unsupported["claims"])

    run_cli("claim", "build", "--retract", cwd=repo)

    after = json.loads(run_cli("audit", "claims", "--json", cwd=repo).stdout)
    assert not any(c["family"] == "build" for c in after["claims"])
    assert json.loads(run_cli("claim", "list", "--json", cwd=repo).stdout)["claims"] == []


def test_retraction_leaves_the_original_claim_in_the_record(tmp_path: Path) -> None:
    repo = _repo_with_evidence(tmp_path, "record", FAILING)
    run_cli("claim", "test", "--passed", cwd=repo)
    run_cli("claim", "test", "--retract", cwd=repo)

    # Claims are append-only events; retraction supersedes rather than deletes.
    # Assert against the envelope store rather than the derived index, which
    # `agentdir query` does not refresh on its own.
    envelopes = [
        path.read_text(encoding="utf-8")
        for path in (repo / ".agentdir" / "sessions").glob("*/Maildir/new/*")
    ]
    claim_events = [body for body in envelopes if "claim.recorded" in body]
    outcomes = sorted(
        line.split("=", 1)[1].strip()
        for body in claim_events
        for line in body.splitlines()
        if line.startswith("outcome=")
    )
    assert outcomes == ["passed", "retracted"]
