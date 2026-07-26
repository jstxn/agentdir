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
import sys
from email import policy
from pathlib import Path

from agentdir.envelope import build_envelope, envelope_bytes, header_value, parse_envelope

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
