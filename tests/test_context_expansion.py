from __future__ import annotations

import json
import os
import shlex
import sqlite3
import subprocess
import sys
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from agentdir.control import format_work_start
from agentdir.context_expansion import (
    HEADER_VIEW_ID,
    HEADER_VIEW_SOURCE_SHA256,
    _expansion_events,
    _receipt_body,
    _resolve_session_summary,
    _validate_receipt,
    _view_id,
    _view_payload,
    expand_context_source,
)


def find_project_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError("could not locate project root")


PROJECT_ROOT = find_project_root()
SRC_ROOT = PROJECT_ROOT / "src"
CLI_INVOCATION = shlex.join((sys.executable, "-m", "agentdir"))


def scoped_work_context_invocation(root: Path) -> str:
    return f"{CLI_INVOCATION} work context --root {shlex.quote(str(root.resolve()))}"


def run_cli(
    *args: str,
    cwd: Path | None = None,
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


def query_rows(root: Path, event_type: str | None = None) -> list[dict[str, object]]:
    db = root / "indexes" / "agentdir.sqlite3"
    sql = "select * from messages"
    params: tuple[object, ...] = ()
    if event_type:
        sql += " where event_type = ?"
        params = (event_type,)
    sql += " order by id"
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def displayed_source_ref(payload: dict[str, object], *, event_type: str = "agent.message") -> str:
    manifest = payload["context_pack"]
    briefing = payload["context_briefing"]
    assert isinstance(manifest, dict)
    assert isinstance(briefing, dict)
    source_by_id = {
        source["source_id"]: source
        for source in manifest["sources"]
        if isinstance(source, dict)
    }
    for index, source_id in enumerate(briefing["source_ids"], start=1):
        if source_by_id[source_id].get("event_type") == event_type:
            return str(index)
    raise AssertionError(f"no displayed {event_type} source in context pack")


def test_five_source_briefing_adds_one_explain_action_without_growing_line_cap() -> None:
    query = "deployment profile inheritance"
    sources = [
        {
            "ref": str(index),
            "source_id": f"message:decision-{index}",
            "source_class": "retrieval_hint",
            "source_role": "decision",
            "match_quality": "strong",
            "subject": f"profile decision {index}",
            "excerpt": "Nested mappings merge recursively while lists replace.",
            "next_actions": {
                "explain": [
                    "memory",
                    "explain",
                    query,
                    "--source",
                    f"message:decision-{index}",
                    "--no-rebuild",
                ]
            },
        }
        for index in range(1, 6)
    ]
    result = {
        "session": {"session_id": "briefing-session"},
        "task": query,
        "context": {
            "memory_hits": 5,
            "evidence": 0,
            "recent_session_summaries": 0,
            "federated": False,
        },
        "context_pack": {"pack_id": "ctx-briefing"},
        "context_briefing": {
            "match_state": "strong_prior_context",
            "presented_count": 5,
            "omitted_count": 0,
            "review_required": True,
            "sources": sources,
        },
    }

    rendered = format_work_start(result, command_root="/tmp/agentdir-store")

    assert len(rendered.splitlines()) <= 25
    assert rendered.count("context_explain_") == 1
    assert "context_explain_1=" in rendered


def test_decision_context_stays_typed_and_uses_body_first_preview_through_expansion(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "decision.txt"
    decision_body = (
        "Deployment profiles merge nested mappings recursively and replace lists "
        "so rollback order stays deterministic."
    )
    prior.write_text(decision_body, encoding="utf-8")
    run_cli("session", "start", "--id", "profile-decision", cwd=repo)
    run_cli(
        "emit",
        "--type",
        "decision.recorded",
        "--subject",
        "deployment profile inheritance decision",
        "--body",
        str(prior),
        cwd=repo,
    )
    run_cli("session", "end", cwd=repo)

    started = json.loads(
        run_cli(
            "work",
            "start",
            "implement deterministic deployment profile rollback inheritance",
            "--json",
            cwd=repo,
        ).stdout
    )
    source_ref = displayed_source_ref(started, event_type="decision.recorded")
    briefing_source = started["context_briefing"]["sources"][int(source_ref) - 1]
    source_id = briefing_source["source_id"]
    retrieval_query = started["context"]["retrieval_query"]

    assert started["context_pack"]["protocol"] == "agentdir.context-pack.v1"
    assert briefing_source["source_class"] == "retrieval_hint"
    assert briefing_source["source_role"] == "decision"
    assert briefing_source["excerpt"].startswith("Deployment profiles merge")
    assert "message:" not in briefing_source["excerpt"]
    assert "event: decision.recorded" not in briefing_source["excerpt"]
    assert briefing_source["next_actions"]["explain"] == [
        "memory",
        "explain",
        retrieval_query,
        "--source",
        source_id,
        "--no-rebuild",
    ]

    shown = run_cli(
        "work",
        "context",
        "--show",
        "--pack",
        started["context_pack"]["pack_id"],
        cwd=repo,
    )
    explain_line = next(
        line
        for line in shown.stdout.splitlines()
        if line.startswith(f"context_explain_{source_ref}=")
    )
    explain_command = explain_line.split("=", 1)[1]
    copied = subprocess.run(
        shlex.split(explain_command),
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(SRC_ROOT)},
        text=True,
        capture_output=True,
    )

    assert copied.returncode == 0, copied.stderr
    assert f"query={retrieval_query}" in copied.stdout
    assert f"source_id={source_id}" in copied.stdout

    expanded = json.loads(
        run_cli(
            "work",
            "context",
            "--pack",
            started["context_pack"]["pack_id"],
            "--expand",
            source_ref,
            "--json",
            cwd=repo,
        ).stdout
    )

    assert expanded["source"]["source_class"] == "retrieval_hint"
    assert expanded["source"]["source_role"] == "decision"
    assert expanded["content"].strip() == decision_body


def test_work_context_expands_clean_bounded_pages_and_records_read_before_use(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior_text = "context expansion canonical marker\n" + "".join(
        f"line-{index:04d} preserves canonical context expansion detail {index}\n"
        for index in range(220)
    )
    prior.write_text(prior_text, encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli(
            "work",
            "start",
            "canonical context expansion detail",
            "--json",
            cwd=repo,
        ).stdout
    )
    pack_id = started["context_pack"]["pack_id"]
    source_ref = displayed_source_ref(started)

    first = json.loads(
        run_cli(
            "work",
            "context",
            "--pack",
            pack_id,
            "--expand",
            source_ref,
            "--page",
            "1",
            "--json",
            cwd=repo,
        ).stdout
    )
    second = json.loads(
        run_cli(
            "work",
            "context",
            "--pack",
            pack_id,
            "--expand",
            source_ref,
            "--page",
            "2",
            "--json",
            cwd=repo,
        ).stdout
    )
    repeated = run_cli(
        "work",
        "context",
        "--pack",
        pack_id,
        "--expand",
        source_ref,
        "--page",
        "1",
        cwd=repo,
    )

    assert first["pack_id"] == pack_id
    assert first["source"]["ref"] == source_ref
    assert first["source"]["event_type"] == "agent.message"
    assert first["source"]["match_quality"] in {"strong", "possible", "weak"}
    assert first["integrity"] == "verified"
    assert first["basis"] == "canonical_envelope"
    assert first["extent"] == "bounded"
    assert first["page"] == 1
    assert first["page_count"] >= 2
    assert first["byte_start"] == 0
    assert first["returned_bytes"] <= 4096
    assert first["truncated"] is True
    assert "context expansion canonical marker" in first["content"]
    assert first["receipt"]["status"] == "recorded"
    assert first["receipt"]["recorded"] is True
    assert second["byte_start"] == first["byte_end"]
    assert second["content"] != first["content"]
    assert second["receipt"]["status"] == "recorded"
    assert "receipt=existing" in repeated.stdout
    assert (
        f"next_page={scoped_work_context_invocation(repo / '.agentdir')} --pack {pack_id} "
        f"--expand {source_ref} --page 2"
        in repeated.stdout
    )

    run_cli(
        "work",
        "context",
        "--pack",
        pack_id,
        "--use",
        source_ref,
        "--reason",
        "the expanded canonical record supplies the implementation details",
        cwd=repo,
    )
    status = json.loads(run_cli("status", "--json", cwd=repo).stdout)
    expansion = status["context"]["audit"]["expansion"]
    lineage = json.loads(
        run_cli("report", "final", "--format", "json", cwd=repo).stdout
    )["agent_handoff"]["context_lineage"]

    assert expansion["expanded_source_count"] == 1
    assert expansion["expanded_before_decision_count"] == 1
    assert expansion["expanded_after_decision_count"] == 0
    assert expansion["used_without_prior_expansion_count"] == 0
    assert expansion["receipt_event_count"] == 2
    assert expansion["receipts_valid"] is True
    assert lineage["expansion"] == expansion

    root = repo / ".agentdir"
    with sqlite3.connect(root / "indexes" / "agentdir.sqlite3") as conn:
        direct_receipts = conn.execute(
            "select count(*) from memory_documents where event_type = 'context.sources.expanded'"
        ).fetchone()[0]
        summaries = [
            row[0]
            for row in conn.execute(
                "select body_text from memory_documents where source_kind = 'session_summary'"
            ).fetchall()
        ]
    assert direct_receipts == 0
    assert all("- context.sources.expanded:" not in summary for summary in summaries)
    assert status["memory"]["coverage"] == 1.0


def test_context_expansion_pages_unicode_and_deduplicates_concurrent_receipts(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior_text = (
        "unicode context expansion boundary marker\n"
        + ("unicode context expansion boundary marker " * 110)
        + "🙂é漢字"
        + (" unicode context expansion boundary marker" * 110)
        + "\nunicode context expansion tail"
    )
    prior.write_text(prior_text, encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli(
            "work",
            "start",
            "unicode context expansion boundary marker",
            "--json",
            cwd=repo,
        ).stdout
    )
    pack_id = started["context_pack"]["pack_id"]
    source_ref = displayed_source_ref(started)

    def expand_first_page() -> dict[str, object]:
        return json.loads(
            run_cli(
                "work",
                "context",
                "--pack",
                pack_id,
                "--expand",
                source_ref,
                "--page",
                "1",
                "--json",
                cwd=repo,
            ).stdout
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(expand_first_page), pool.submit(expand_first_page))
        concurrent = [future.result() for future in futures]

    assert {item["receipt"]["status"] for item in concurrent} == {"recorded", "existing"}
    first = concurrent[0]
    content = [str(first["content"])]
    for page in range(2, int(first["page_count"]) + 1):
        expanded = json.loads(
            run_cli(
                "work",
                "context",
                "--pack",
                pack_id,
                "--expand",
                source_ref,
                "--page",
                str(page),
                "--json",
                cwd=repo,
            ).stdout
        )
        assert expanded["returned_bytes"] <= 4096
        content.append(expanded["content"])
    assert "".join(content).strip() == prior_text.strip()

    run_cli(
        "work",
        "context",
        "--pack",
        pack_id,
        "--expand",
        source_ref,
        "--page",
        "0",
        "--json",
        cwd=repo,
        expected_returncode=2,
    )
    run_cli(
        "work",
        "context",
        "--pack",
        pack_id,
        "--expand",
        source_ref,
        "--page",
        str(int(first["page_count"]) + 1),
        "--json",
        cwd=repo,
        expected_returncode=2,
    )
    run_cli("index", "update", cwd=repo)
    root = repo / ".agentdir"
    with sqlite3.connect(root / "indexes" / "agentdir.sqlite3") as conn:
        receipt_rows = conn.execute(
            "select id from messages where event_type = 'context.sources.expanded' order by id"
        ).fetchall()
        header_names = {
            row[0]
            for row in conn.execute(
                "select name from headers where message_rowid = ?",
                (receipt_rows[0][0],),
            ).fetchall()
        }
    assert len(receipt_rows) == int(first["page_count"])
    assert "X-AgentDir-Context-View-Pack-Id" in header_names
    assert "X-AgentDir-Pack-Id" not in header_names


def test_context_expansion_returns_content_when_optional_receipt_write_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("optional receipt failure must not hide expanded content", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli(
            "work",
            "start",
            "optional receipt failure expanded content",
            "--json",
            cwd=repo,
        ).stdout
    )
    pack_id = started["context_pack"]["pack_id"]
    source_ref = displayed_source_ref(started)

    def fail_receipt(*_args, **_kwargs):
        raise OSError("simulated receipt fsync failure")

    monkeypatch.setattr("agentdir.context_expansion.emit_event", fail_receipt)
    expanded = expand_context_source(
        repo / ".agentdir",
        pack_id=pack_id,
        source_selector=source_ref,
    )

    assert "optional receipt failure" in expanded["content"]
    assert expanded["receipt"]["status"] == "failed"
    assert expanded["receipt"]["reason"] == "receipt_write_failed"
    assert "simulated receipt fsync failure" in expanded["receipt"]["error"]
    assert any("optional receipt failed" in warning for warning in expanded["warnings"])


def test_context_expansion_stdout_failure_emits_no_receipt(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("stdout delivery failure receipt boundary marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli("work", "start", "stdout delivery failure receipt boundary", "--json", cwd=repo).stdout
    )
    env = {**os.environ, "PYTHONPATH": str(SRC_ROOT)}
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agentdir",
            "work",
            "context",
            "--pack",
            started["context_pack"]["pack_id"],
            "--expand",
            displayed_source_ref(started),
            "--json",
        ],
        cwd=repo,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    process.stdout.close()
    returncode = process.wait(timeout=10)
    stderr = process.stderr.read() if process.stderr is not None else ""
    run_cli("index", "update", cwd=repo)

    assert returncode != 0, stderr
    assert query_rows(repo / ".agentdir", "context.sources.expanded") == []


def test_context_expansion_receipt_binds_verified_content_to_manifest_digest(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("receipt manifest digest binding marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli("work", "start", "receipt manifest digest binding marker", "--json", cwd=repo).stdout
    )
    manifest = started["context_pack"]
    pack_id = manifest["pack_id"]
    session_id = manifest["session_id"]
    source_ref = displayed_source_ref(started)
    expanded = expand_context_source(
        repo / ".agentdir",
        pack_id=pack_id,
        source_selector=source_ref,
    )
    run_cli("index", "update", cwd=repo)
    source_id = expanded["source"]["source_id"]
    event = deepcopy(
        _expansion_events(repo / ".agentdir", pack_id, session_id=session_id)[0]
    )
    forged_payload = _view_payload(expanded)
    forged_payload["source_sha256"] = "0" * 64
    forged_view_id = _view_id(forged_payload)
    event["header_values"][HEADER_VIEW_SOURCE_SHA256] = ["0" * 64]
    event["header_values"][HEADER_VIEW_ID] = [forged_view_id]
    event["message_id"] = f"<{forged_view_id}@agentdir.local>"
    event["body_text"] = _receipt_body(
        forged_payload,
        view_id=forged_view_id,
        decision_phase="before_decision",
    )

    payload, errors = _validate_receipt(
        repo / ".agentdir",
        event,
        manifest,
        {source_id: source_ref},
        {source["source_id"]: source for source in manifest["sources"]},
    )

    assert payload is not None
    assert any("source_sha256 does not match retained content" in error for error in errors), errors


def test_context_expansion_rejects_symlinked_source_outside_the_store(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("outside symlink containment expansion marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli(
            "work",
            "start",
            "outside symlink containment expansion marker",
            "--json",
            cwd=repo,
        ).stdout
    )
    pack_id = started["context_pack"]["pack_id"]
    source_ref = displayed_source_ref(started)
    source_id = started["context_briefing"]["source_ids"][int(source_ref) - 1]
    source = next(
        item for item in started["context_pack"]["sources"] if item["source_id"] == source_id
    )
    envelope = repo / ".agentdir" / source["file_path"]
    outside = tmp_path / "outside-source.eml"
    envelope.rename(outside)
    envelope.symlink_to(outside)

    expanded = json.loads(
        run_cli(
            "work",
            "context",
            "--pack",
            pack_id,
            "--expand",
            source_ref,
            "--json",
            cwd=repo,
        ).stdout
    )

    assert expanded["integrity"] == "unavailable"
    assert expanded["basis"] == "manifest_excerpt"
    assert expanded["receipt"]["status"] == "not_recorded"


def test_context_expansion_normalizes_retained_header_whitespace(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("header whitespace expansion marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli(
        "emit",
        "--type",
        "agent.message",
        "--subject",
        "subject  with   spacing",
        "--body",
        str(prior),
        cwd=repo,
    )
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli("work", "start", "header whitespace expansion marker", "--json", cwd=repo).stdout
    )
    expanded = json.loads(
        run_cli(
            "work",
            "context",
            "--pack",
            started["context_pack"]["pack_id"],
            "--expand",
            displayed_source_ref(started),
            "--json",
            cwd=repo,
        ).stdout
    )

    assert expanded["integrity"] == "verified"
    assert expanded["basis"] == "canonical_envelope"


def test_context_expansion_rejects_unsafe_summary_session_identity(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("init", str(repo / ".agentdir"))
    outside_session = tmp_path / "outside-session"

    resolved = _resolve_session_summary(
        repo / ".agentdir",
        {
            "session_id": str(outside_session),
            "source_kind": "session_summary",
            "excerpt": "bounded captured summary preview",
        },
    )

    assert resolved["integrity"] == "unavailable"
    assert resolved["basis"] == "manifest_excerpt"
    assert resolved["integrity_reason"] == "summary session identity is unsafe"


def test_work_context_integrity_mismatch_returns_only_the_stored_redacted_excerpt(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text(
        "immutable expansion original marker with stable historical evidence",
        encoding="utf-8",
    )
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli(
            "work",
            "start",
            "immutable expansion original marker",
            "--json",
            cwd=repo,
        ).stdout
    )
    pack_id = started["context_pack"]["pack_id"]
    source_ref = displayed_source_ref(started)
    source_id = started["context_briefing"]["source_ids"][int(source_ref) - 1]
    source = next(
        item for item in started["context_pack"]["sources"] if item["source_id"] == source_id
    )
    envelope = repo / ".agentdir" / source["file_path"]
    raw = envelope.read_text(encoding="utf-8")
    assert "immutable expansion original marker" in raw
    envelope.write_text(
        raw.replace("immutable expansion original marker", "immutable expansion changed marker"),
        encoding="utf-8",
    )

    expanded = json.loads(
        run_cli(
            "work",
            "context",
            "--pack",
            pack_id,
            "--expand",
            source_ref,
            "--json",
            cwd=repo,
        ).stdout
    )
    audit = json.loads(
        run_cli("audit", "context", "--pack", pack_id, "--json", cwd=repo).stdout
    )

    assert expanded["integrity"] == "changed"
    assert expanded["basis"] == "manifest_excerpt"
    assert expanded["extent"] == "stored_excerpt"
    assert "immutable expansion original marker" in expanded["content"]
    assert "immutable expansion changed marker" not in expanded["content"]
    assert expanded["receipt"]["status"] == "not_recorded"
    assert expanded["receipt"]["reason"] == "canonical_source_unavailable"
    assert audit["expansion"]["receipt_event_count"] == 0


def test_context_expansion_labels_derived_summary_drift_instead_of_rewriting_history(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text(
        "derived summary context expansion drift marker",
        encoding="utf-8",
    )
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli(
            "work",
            "start",
            "derived summary context expansion drift marker",
            "--memory-limit",
            "1",
            "--recent-limit",
            "5",
            "--json",
            cwd=repo,
        ).stdout
    )
    pack_id = started["context_pack"]["pack_id"]
    summary_ref = displayed_source_ref(started, event_type="summary.compacted")
    original = json.loads(
        run_cli(
            "work",
            "context",
            "--pack",
            pack_id,
            "--expand",
            summary_ref,
            "--json",
            cwd=repo,
        ).stdout
    )
    drift = tmp_path / "drift.txt"
    drift.write_text("new derived summary material that was not captured", encoding="utf-8")
    run_cli(
        "emit",
        "--session",
        "prior-context",
        "--type",
        "agent.message",
        "--body",
        str(drift),
        cwd=repo,
    )

    changed = json.loads(
        run_cli(
            "work",
            "context",
            "--pack",
            pack_id,
            "--expand",
            summary_ref,
            "--json",
            cwd=repo,
        ).stdout
    )

    assert original["integrity"] == "verified"
    assert original["basis"] == "canonical_derived_summary"
    assert changed["integrity"] == "changed"
    assert changed["basis"] == "manifest_excerpt"
    assert "drifted or its session identity was replaced" in changed["integrity_reason"]
    assert "new derived summary material" not in changed["content"]
    assert changed["receipt"]["status"] == "not_recorded"


def test_expansion_after_use_is_observable_without_changing_the_terminal_decision(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text(
        "late context expansion decision stability marker",
        encoding="utf-8",
    )
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli(
            "work",
            "start",
            "late context expansion decision stability marker",
            "--json",
            cwd=repo,
        ).stdout
    )
    pack_id = started["context_pack"]["pack_id"]
    source_ref = displayed_source_ref(started)
    run_cli(
        "work",
        "context",
        "--pack",
        pack_id,
        "--use",
        source_ref,
        "--reason",
        "the historical pattern constrains the implementation",
        cwd=repo,
    )
    before = json.loads(
        run_cli("audit", "context", "--pack", pack_id, "--json", cwd=repo).stdout
    )

    expanded = json.loads(
        run_cli(
            "work",
            "context",
            "--pack",
            pack_id,
            "--expand",
            source_ref,
            "--json",
            cwd=repo,
        ).stdout
    )
    after = json.loads(
        run_cli("audit", "context", "--pack", pack_id, "--json", cwd=repo).stdout
    )

    for field in ("decision_id", "decision", "review_status", "finish_allowed", "lineage_valid"):
        assert after[field] == before[field]
    assert expanded["decision"]["phase"] == "after_decision"
    assert after["expansion"]["expanded_before_decision_count"] == 0
    assert after["expansion"]["expanded_after_decision_count"] == 1
    assert after["expansion"]["used_without_prior_expansion_count"] == 1
    assert all(event["event_type"] != "context.sources.expanded" for event in after["events"])
    run_cli("work", "finish", "--json", cwd=repo)


def test_terminal_context_show_is_decision_aware_and_historical_expand_is_read_only(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text(
        "terminal context expansion historical read marker",
        encoding="utf-8",
    )
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli(
            "work",
            "start",
            "terminal context expansion historical read marker",
            "--json",
            cwd=repo,
        ).stdout
    )
    pack_id = started["context_pack"]["pack_id"]
    source_ref = displayed_source_ref(started)
    reason = "the historical record is not relevant to the current decision"
    run_cli(
        "work",
        "context",
        "--pack",
        pack_id,
        "--none-relevant",
        "--reason",
        reason,
        cwd=repo,
    )

    shown = run_cli("work", "context", "--show", "--pack", pack_id, cwd=repo)
    assert "context_review_status=complete" in shown.stdout
    assert "context_decision=no_relevant" in shown.stdout
    assert f"context_reason={reason}" in shown.stdout
    assert "context_use=" not in shown.stdout
    assert "context_none=" not in shown.stdout
    assert "context_skip=" not in shown.stdout
    assert (
        f"context_expand={scoped_work_context_invocation(repo / '.agentdir')} "
        f"--pack {pack_id} --expand <number>"
        in shown.stdout
    )

    run_cli("work", "finish", "--json", cwd=repo)
    historical = json.loads(
        run_cli(
            "work",
            "context",
            "--pack",
            pack_id,
            "--expand",
            source_ref,
            "--json",
            cwd=repo,
        ).stdout
    )
    run_cli("index", "update", cwd=repo)
    receipts = query_rows(repo / ".agentdir", "context.sources.expanded")

    assert historical["integrity"] == "verified"
    assert "historical read marker" in historical["content"]
    assert historical["receipt"]["status"] == "not_recorded"
    assert historical["receipt"]["reason"] == "session_not_active"
    assert receipts == []


def test_archived_pack_and_source_receipt_remain_directly_readable_but_not_searchable(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text(
        "archived context expansion retained canonical marker",
        encoding="utf-8",
    )
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli(
            "work",
            "start",
            "archived context expansion retained canonical marker",
            "--json",
            cwd=repo,
        ).stdout
    )
    pack_id = started["context_pack"]["pack_id"]
    session_id = started["session"]["session_id"]
    source_ref = displayed_source_ref(started)
    active_read = json.loads(
        run_cli(
            "work",
            "context",
            "--pack",
            pack_id,
            "--expand",
            source_ref,
            "--json",
            cwd=repo,
        ).stdout
    )
    run_cli(
        "work",
        "context",
        "--pack",
        pack_id,
        "--use",
        source_ref,
        "--reason",
        "the retained canonical record establishes the historical behavior",
        cwd=repo,
    )
    run_cli("work", "finish", "--json", cwd=repo)
    run_cli(
        "archive",
        "--session",
        session_id,
        "--session",
        "prior-context",
        "--apply",
        cwd=repo,
    )

    archived_read = json.loads(
        run_cli(
            "work",
            "context",
            "--pack",
            pack_id,
            "--expand",
            source_ref,
            "--json",
            cwd=repo,
        ).stdout
    )
    audit = json.loads(
        run_cli("audit", "context", "--pack", pack_id, "--json", cwd=repo).stdout
    )
    rows = query_rows(repo / ".agentdir")

    assert active_read["receipt"]["status"] == "recorded"
    assert archived_read["integrity"] == "verified"
    assert "retained canonical marker" in archived_read["content"]
    assert archived_read["receipt"]["status"] == "not_recorded"
    assert archived_read["receipt"]["reason"] == "session_not_active"
    assert audit["review_status"] == "complete"
    assert audit["expansion"]["receipt_event_count"] == 1
    assert audit["expansion"]["expanded_before_decision_count"] == 1
    assert all(row["session_id"] != session_id for row in rows)
    assert all(row["event_type"] != "context.sources.expanded" for row in rows)
