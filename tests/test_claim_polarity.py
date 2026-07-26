"""Polarity matrix for `agentdir audit claims`.

Every bug this check has had came from wording the patterns understood in only
one direction: text that hid a failure passed green, then honest failure reports
were flagged, then failure words asserting success passed green again.

The fix is structural rather than another example test. Each case below names
the polarity class it belongs to, and `test_matrix_covers_every_cell` fails
unless every class is exercised against both passing and failing evidence. A new
pattern cannot be added without filling in what it does in all four directions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from test_audit import init_repo, run_cli

# Polarity classes.
SUCCESS = "success-claim"  # "tests passed"
NEGATED_FAILURE = "negated-failure-claim"  # "no test failures" -- success, in failure words
HONEST_FAILURE = "honest-failure-report"  # "tests failed"
NO_CLAIM = "no-checkable-claim"  # "everything works"

POLARITY_CLASSES = (SUCCESS, NEGATED_FAILURE, HONEST_FAILURE, NO_CLAIM)

# Evidence states.
PASSING = "passing"
FAILING = "failing"

EVIDENCE_STATES = (PASSING, FAILING)

# (evidence, polarity, text, expected status or None, expected ok, strict exit code)
CASES: tuple[tuple[str, str, str, str | None, bool, int], ...] = (
    # -- failing evidence -------------------------------------------------
    (FAILING, SUCCESS, "Tests passed.", "contradicted", False, 1),
    (FAILING, SUCCESS, "The test suite is green.", "contradicted", False, 1),
    (FAILING, NEGATED_FAILURE, "No test failures.", "contradicted", False, 1),
    (FAILING, NEGATED_FAILURE, "There are no failing tests.", "contradicted", False, 1),
    (FAILING, NEGATED_FAILURE, "Tests are not failing.", "contradicted", False, 1),
    (FAILING, HONEST_FAILURE, "Tests failed.", "acknowledged", True, 0),
    (FAILING, HONEST_FAILURE, "The test suite is still failing.", "acknowledged", True, 0),
    (FAILING, HONEST_FAILURE, "Two tests fail; I am still debugging.", "acknowledged", True, 0),
    (FAILING, HONEST_FAILURE, "I did not run the tests.", "acknowledged", True, 0),
    (FAILING, NO_CLAIM, "Everything works.", "unreviewed", False, 1),
    (FAILING, NO_CLAIM, "Verified locally.", "unreviewed", False, 1),
    (FAILING, NO_CLAIM, "No regressions.", "unreviewed", False, 1),
    (FAILING, NO_CLAIM, "I refactored the parser.", "unreviewed", False, 1),
    # -- passing evidence -------------------------------------------------
    (PASSING, SUCCESS, "Tests passed.", "supported", True, 0),
    (PASSING, SUCCESS, "The test suite is green.", "supported", True, 0),
    (PASSING, NEGATED_FAILURE, "No test failures.", "supported", True, 0),
    (PASSING, NEGATED_FAILURE, "There are no failing tests.", "supported", True, 0),
    # Known gap: understating a passing run makes no claim to check. Recorded
    # here so the behaviour is visible rather than silently assumed.
    (PASSING, HONEST_FAILURE, "Tests failed.", None, True, 0),
    (PASSING, HONEST_FAILURE, "I did not run the tests.", None, True, 0),
    (PASSING, NO_CLAIM, "Everything works.", None, True, 0),
    (PASSING, NO_CLAIM, "I refactored the parser.", None, True, 0),
)


def _make_repo(base: Path, name: str, *, failing: bool) -> Path:
    repo = init_repo(base / name)
    run_cli("work", "start", f"polarity {name}", cwd=repo)
    script = (
        "import sys; print('FAILED test_x'); sys.exit(1)"
        if failing
        else "print('5 tests passed')"
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
    base = tmp_path_factory.mktemp("polarity")
    return {
        FAILING: _make_repo(base, "failing", failing=True),
        PASSING: _make_repo(base, "passing", failing=False),
    }


@pytest.mark.parametrize(
    ("evidence", "polarity", "text", "expected_status", "expected_ok", "strict_code"),
    CASES,
    ids=[f"{evidence}-{polarity}-{text}" for evidence, polarity, text, *_ in CASES],
)
def test_claim_polarity(
    repos: dict[str, Path],
    evidence: str,
    polarity: str,
    text: str,
    expected_status: str | None,
    expected_ok: bool,
    strict_code: int,
) -> None:
    repo = repos[evidence]
    payload = json.loads(
        run_cli("audit", "claims", "--text", "-", "--json", cwd=repo, input_text=text).stdout
    )

    statuses = {claim["family"]: claim["status"] for claim in payload["claims"]}
    assert statuses.get("test") == expected_status
    assert payload["ok"] is expected_ok

    run_cli(
        "audit",
        "claims",
        "--text",
        "-",
        "--json",
        "--strict",
        cwd=repo,
        input_text=text,
        expected_returncode=strict_code,
    )


def test_matrix_covers_every_cell() -> None:
    covered = {(evidence, polarity) for evidence, polarity, *_ in CASES}
    missing = {
        (evidence, polarity)
        for evidence in EVIDENCE_STATES
        for polarity in POLARITY_CLASSES
    } - covered
    assert not missing, f"polarity matrix has untested cells: {sorted(missing)}"
