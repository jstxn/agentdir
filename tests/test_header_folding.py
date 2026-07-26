"""Header values must survive the fold, on every supported Python.

A header longer than the fold width is written across continuation lines.
Python 3.12 and newer strip the continuation's leading whitespace when reading
it back; 3.11 does not. `X-AgentDir-Blob-SHA256` is 89 characters including its
name, so it always folds, and on 3.11 every artifact lookup was made with a
leading space and never matched. Context packs were broken on the project's own
declared minimum Python until CI ran the suite there.
"""

from __future__ import annotations

import email
import json
import sqlite3
import sys
from email import policy
from pathlib import Path

from agentdir.envelope import build_envelope, envelope_bytes, header_value, parse_envelope
from test_audit import init_repo, run_cli

LONG_SHA = "d6d3e7efcb0546e36e6802638af0664d9c2c54d5181848fbab3792e573a93bfd"


def test_header_value_strips_fold_whitespace() -> None:
    assert header_value(f" {LONG_SHA}") == LONG_SHA
    assert header_value(f"{LONG_SHA}\n ") == LONG_SHA
    assert header_value(LONG_SHA) == LONG_SHA


def test_folded_header_round_trips_through_the_email_parser() -> None:
    message = build_envelope(
        event_type="context.pack.created",
        body="body",
        artifact_headers={"X-AgentDir-Blob-SHA256": LONG_SHA},
    )
    parsed = email.message_from_bytes(envelope_bytes(message), policy=policy.default)

    raw = parsed.get("X-AgentDir-Blob-SHA256")
    if sys.version_info < (3, 12):
        # Guard the assumption this whole module rests on.
        assert str(raw) != LONG_SHA, "3.11 no longer folds; the normalisation may be removable"
    assert header_value(raw) == LONG_SHA


def test_parsed_envelope_returns_normalised_headers(tmp_path: Path) -> None:
    message = build_envelope(
        event_type="context.pack.created",
        body="body",
        artifact_headers={"X-AgentDir-Blob-SHA256": LONG_SHA},
    )
    path = tmp_path / "envelope.eml"
    path.write_bytes(envelope_bytes(message))

    parsed = parse_envelope(path)

    assert parsed.headers("X-AgentDir-Blob-SHA256") == [LONG_SHA]
    assert parsed.header("X-AgentDir-Event-Type") == "context.pack.created"


def test_doctor_flags_an_index_that_kept_fold_whitespace(tmp_path: Path) -> None:
    """Upgrading does not rewrite already-indexed rows.

    A store indexed by an affected 3.11 install keeps padded values and fails
    artifact lookups against them silently, so doctor has to name the condition
    and the command that clears it.
    """
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "stale index", cwd=repo)
    run_cli("status", cwd=repo)

    index = repo / ".agentdir" / "indexes" / "agentdir.sqlite3"
    with sqlite3.connect(index) as conn:
        rowid = conn.execute("select id from messages limit 1").fetchone()[0]
        conn.execute(
            "insert into headers(message_rowid, name, value) values (?, ?, ?)",
            (rowid, "X-AgentDir-Blob-SHA256", f" {LONG_SHA}"),
        )

    before = json.loads(run_cli("doctor", "--json", cwd=repo).stdout)
    assert any("folded header" in warning for warning in before["warnings"])

    run_cli("index", "rebuild", cwd=repo)

    after = json.loads(run_cli("doctor", "--json", cwd=repo).stdout)
    assert not any("folded header" in warning for warning in after["warnings"])
