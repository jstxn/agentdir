from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import types
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

import agentdir.memory as memory
import agentdir.context as context_module
from agentdir.cli import build_parser
from agentdir.context import (
    brief_context_manifest,
    build_context_manifest,
    build_context_pack,
    review_context_pack,
)
from agentdir.events import emit_event
from agentdir.memory import index_memory_document
from agentdir.store import AgentDirError


def find_project_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError("could not locate project root")


PROJECT_ROOT = find_project_root()
SRC_ROOT = PROJECT_ROOT / "src"


def run_cli(
    *args: str,
    expected_returncode: int = 0,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_ROOT)
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


def write_body(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def index_path(root: Path) -> Path:
    return root / "indexes" / "agentdir.sqlite3"


def artifact_blob(root: Path, sha256: str) -> Path:
    return root / "artifacts" / "blobs" / "sha256" / sha256[:2] / sha256[2:4] / sha256


def parse_message(path: Path):
    with path.open("rb") as handle:
        return BytesParser(policy=default).parse(handle)


def test_context_build_creates_agent_ready_context_pack(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    message = write_body(tmp_path / "message.txt", "auth login task context marker")
    evidence = write_body(tmp_path / "evidence.txt", "pytest auth login evidence passed")

    run_cli("session", "start", "--root", str(root), "--id", "context-session")
    run_cli("emit", "--root", str(root), "--session", "context-session", "--type", "agent.message", "--body", str(message))
    run_cli(
        "emit",
        "--root",
        str(root),
        "--session",
        "context-session",
        "--type",
        "tool.result",
        "--tool",
        "pytest",
        "--tool-exit-code",
        "0",
        "--body",
        str(evidence),
    )

    result = run_cli("context", "build", "--root", str(root), "--session", "context-session", "auth login")
    output = tmp_path / "context.md"
    written = run_cli(
        "context",
        "build",
        "--root",
        str(root),
        "--session",
        "context-session",
        "--output",
        str(output),
        "auth login",
    )

    assert "# AgentDir Context Pack" in result.stdout
    assert "auth login task context marker" in result.stdout
    assert "tool.result pytest exit=0" in result.stdout
    assert "Session summary: context-session" in result.stdout
    assert written.stdout.strip() == str(output)
    assert "# AgentDir Context Pack" in output.read_text(encoding="utf-8")


def test_context_build_emit_creates_manifest_artifact_and_event(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    message = write_body(tmp_path / "message.txt", "auth login context marker")
    evidence = write_body(tmp_path / "evidence.txt", "pytest auth login evidence passed")

    run_cli("session", "start", "--root", str(root), "--id", "context-session")
    run_cli("emit", "--root", str(root), "--session", "context-session", "--type", "agent.message", "--body", str(message))
    run_cli(
        "emit",
        "--root",
        str(root),
        "--session",
        "context-session",
        "--type",
        "tool.result",
        "--tool",
        "pytest",
        "--tool-exit-code",
        "0",
        "--body",
        str(evidence),
    )

    result = run_cli(
        "context",
        "build",
        "--root",
        str(root),
        "--session",
        "context-session",
        "--emit",
        "--json",
        "work on AgentDir auth login",
    )
    payload = json.loads(result.stdout)
    manifest = payload["manifest"]
    event_path = Path(payload["event_path"])
    artifact_path = artifact_blob(root, payload["artifact"]["sha256"])
    event_message = parse_message(event_path)
    stored_manifest = json.loads(artifact_path.read_text(encoding="utf-8"))

    assert manifest["protocol"] == "agentdir.context-pack.v1"
    assert manifest["pack_id"].startswith("ctx-")
    assert manifest["enforcement_boundary"].startswith("advisory:")
    assert manifest["source_counts"]["evidence"] >= 1
    assert stored_manifest["pack_id"] == manifest["pack_id"]
    assert event_message["X-AgentDir-Event-Type"] == "context.pack.created"
    assert event_message["X-AgentDir-Protocol"] == "agentdir.context-pack.v1"
    assert event_message["X-AgentDir-Pack-Id"] == manifest["pack_id"]
    assert event_message["X-AgentDir-Context-Query"] == manifest["retrieval_query"]
    assert event_message["X-AgentDir-Context-Query"] != manifest["task"]
    assert event_message["X-AgentDir-Enforcement-Mode"] == "advisory"
    assert event_message.get_all("X-AgentDir-Source-Id")


def test_context_consume_cite_and_audit_track_source_lineage(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    message = write_body(tmp_path / "message.txt", "checkout auth redirect context")
    evidence = write_body(tmp_path / "evidence.txt", "pytest checkout auth redirect passed")

    run_cli("session", "start", "--root", str(root), "--id", "context-session")
    run_cli("emit", "--root", str(root), "--session", "context-session", "--type", "agent.message", "--body", str(message))
    run_cli(
        "emit",
        "--root",
        str(root),
        "--session",
        "context-session",
        "--type",
        "tool.result",
        "--tool",
        "pytest",
        "--tool-exit-code",
        "0",
        "--body",
        str(evidence),
    )
    pack_result = run_cli(
        "context",
        "build",
        "--root",
        str(root),
        "--session",
        "context-session",
        "--emit",
        "--json",
        "checkout auth redirect",
    )
    manifest = json.loads(pack_result.stdout)["manifest"]
    pack_id = manifest["pack_id"]
    evidence_source = next(source for source in manifest["sources"] if source["source_class"] == "evidence")

    consumed = run_cli(
        "context",
        "consume",
        "--root",
        str(root),
        "--pack",
        pack_id,
        "--source",
        evidence_source["source_id"],
        "--purpose",
        "answer",
        "--json",
    )
    cited = run_cli(
        "context",
        "cite",
        "--root",
        str(root),
        "--pack",
        pack_id,
        "--format",
        "json",
    )
    audit = run_cli("audit", "context", "--root", str(root), "--pack", pack_id, "--json")

    consumed_payload = json.loads(consumed.stdout)
    cited_payload = json.loads(cited.stdout)
    audit_payload = json.loads(audit.stdout)

    assert consumed_payload["source_ids"] == [evidence_source["source_id"]]
    assert consumed_payload["purpose"] == "answer"
    assert cited_payload["sources"][0]["source_class"] == "evidence"
    assert cited_payload["source_counts"]["evidence"] == 1
    assert audit_payload["retrieved_count"] == len(manifest["sources"])
    assert audit_payload["consumed_count"] == 1
    assert audit_payload["cited_count"] == 1
    assert audit_payload["evidence_backed_count"] == 1
    assert audit_payload["consumed_source_ids"] == [evidence_source["source_id"]]
    assert audit_payload["cited_source_ids"] == [evidence_source["source_id"]]


def test_context_cite_without_used_or_explicit_sources_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "message.txt", "checkout auth redirect context")
    run_cli("session", "start", "--root", str(root), "--id", "context-session")
    run_cli("emit", "--root", str(root), "--session", "context-session", "--type", "agent.message", "--body", str(body))
    emitted = json.loads(
        run_cli(
            "context",
            "build",
            "--root",
            str(root),
            "--session",
            "context-session",
            "--emit",
            "--json",
            "checkout auth redirect",
        ).stdout
    )

    rejected = run_cli(
        "context",
        "cite",
        "--root",
        str(root),
        "--pack",
        emitted["manifest"]["pack_id"],
        expected_returncode=3,
    )

    assert "No used context sources to cite" in rejected.stderr


def test_context_cite_rejects_an_explicit_source_before_use_without_emitting(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "message.txt", "checkout auth redirect context")
    run_cli("session", "start", "--root", str(root), "--id", "context-session")
    run_cli("emit", "--root", str(root), "--session", "context-session", "--type", "agent.message", "--body", str(body))
    emitted = json.loads(
        run_cli(
            "context",
            "build",
            "--root",
            str(root),
            "--session",
            "context-session",
            "--emit",
            "--json",
            "checkout auth redirect",
        ).stdout
    )
    pack_id = emitted["manifest"]["pack_id"]
    source_id = emitted["manifest"]["briefing"]["source_ids"][0]

    rejected = run_cli(
        "context",
        "cite",
        "--root",
        str(root),
        "--pack",
        pack_id,
        "--source",
        source_id,
        expected_returncode=3,
    )
    audit = json.loads(
        run_cli("audit", "context", "--root", str(root), "--pack", pack_id, "--json").stdout
    )

    assert "Cannot cite context before use" in rejected.stderr
    assert audit["cited_count"] == 0
    assert audit["cited_without_use_count"] == 0


def test_legacy_v1_pack_citation_remains_compatible_and_is_not_strictly_enforced(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "message.txt", "checkout auth redirect legacy citation")
    run_cli("session", "start", "--root", str(root), "--id", "legacy-context")
    run_cli("emit", "--root", str(root), "--session", "legacy-context", "--type", "agent.message", "--body", str(body))
    pack = build_context_pack(
        root,
        "checkout auth redirect",
        session_id="legacy-context",
    )
    legacy_manifest = build_context_manifest(pack)
    legacy_manifest.pop("briefing")
    legacy_manifest.pop("retrieval_query_state", None)
    for source in legacy_manifest["sources"]:
        source.pop("text_sha256", None)
        source.pop("body_sha256", None)
    legacy_path = tmp_path / "legacy-context-manifest.json"
    legacy_path.write_text(
        json.dumps(legacy_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    pack_id = legacy_manifest["pack_id"]
    emit_event(
        root,
        session_id="legacy-context",
        event_type="context.pack.created",
        subject=f"context pack: {legacy_manifest['task']}",
        body=(
            f"pack_id={pack_id}\n"
            f"protocol={legacy_manifest['protocol']}\n"
            f"task={legacy_manifest['task']}\n"
            "session_id=legacy-context\n"
            f"sources={len(legacy_manifest['sources'])}"
        ),
        artifact=legacy_path,
        extra_headers={
            "X-AgentDir-Protocol": legacy_manifest["protocol"],
            "X-AgentDir-Pack-Id": pack_id,
            "X-AgentDir-Context-Query": legacy_manifest["retrieval_query"],
            "X-AgentDir-Source-Id": [
                source["source_id"] for source in legacy_manifest["sources"]
            ],
        },
    )

    legacy_briefing = brief_context_manifest(legacy_manifest)
    message_ref = next(
        source["ref"]
        for source in legacy_briefing["sources"]
        if source.get("event_type") == "agent.message"
    )
    expanded = json.loads(
        run_cli(
            "work",
            "context",
            "--root",
            str(root),
            "--pack",
            pack_id,
            "--expand",
            message_ref,
            "--json",
        ).stdout
    )

    citation = json.loads(
        run_cli(
            "context",
            "cite",
            "--root",
            str(root),
            "--pack",
            pack_id,
            "--format",
            "json",
        ).stdout
    )
    audit = json.loads(
        run_cli("audit", "context", "--root", str(root), "--pack", pack_id, "--json").stdout
    )
    session_audit = json.loads(
        run_cli(
            "audit",
            "session",
            "--root",
            str(root),
            "--session",
            "legacy-context",
            "--strict",
            "--json",
        ).stdout
    )
    citation_check = next(
        check for check in session_audit["checks"] if check["id"] == "context_sources_cited"
    )

    assert len(citation["sources"]) == len(legacy_manifest["sources"])
    assert expanded["integrity"] == "legacy_unverified"
    assert expanded["receipt"]["status"] == "recorded"
    assert audit["review_status"] == "legacy"
    assert audit["expansion"]["expanded_source_count"] == 1
    assert audit["expansion"]["receipts_valid"] is True
    assert audit["cited_without_use_count"] == len(legacy_manifest["sources"])
    assert audit["cited_without_use_enforced"] is False
    assert citation_check["status"] == "not_applicable"


def test_legacy_v1_cross_session_actions_remain_compatible(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "message.txt", "checkout auth legacy session attribution")
    run_cli("session", "start", "--root", str(root), "--id", "legacy-owner")
    run_cli(
        "emit",
        "--root",
        str(root),
        "--session",
        "legacy-owner",
        "--type",
        "agent.message",
        "--body",
        str(body),
    )
    legacy_manifest = build_context_manifest(
        build_context_pack(
            root,
            "checkout auth legacy session attribution",
            session_id="legacy-owner",
        )
    )
    legacy_manifest.pop("briefing")
    legacy_manifest.pop("retrieval_query_state", None)
    legacy_path = tmp_path / "legacy-cross-session-manifest.json"
    legacy_path.write_text(
        json.dumps(legacy_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    pack_id = legacy_manifest["pack_id"]
    source_ids = [source["source_id"] for source in legacy_manifest["sources"]]
    assert source_ids
    emit_event(
        root,
        session_id="legacy-owner",
        event_type="context.pack.created",
        subject=f"context pack: {legacy_manifest['task']}",
        body=(
            f"pack_id={pack_id}\n"
            f"protocol={legacy_manifest['protocol']}\n"
            f"task={legacy_manifest['task']}\n"
            "session_id=legacy-owner\n"
            f"sources={len(source_ids)}"
        ),
        artifact=legacy_path,
        extra_headers={
            "X-AgentDir-Protocol": legacy_manifest["protocol"],
            "X-AgentDir-Pack-Id": pack_id,
            "X-AgentDir-Context-Query": legacy_manifest["retrieval_query"],
            "X-AgentDir-Source-Id": source_ids,
        },
    )
    run_cli("session", "start", "--root", str(root), "--id", "legacy-action-owner")
    emit_event(
        root,
        session_id="legacy-action-owner",
        event_type="context.pack.consumed",
        subject="legacy cross-session context consume",
        body=f"action=context_consumed\npack_id={pack_id}\npurpose=plan",
        extra_headers={
            "X-AgentDir-Protocol": legacy_manifest["protocol"],
            "X-AgentDir-Pack-Id": pack_id,
            "X-AgentDir-Source-Id": source_ids,
            "X-AgentDir-Consumption-Purpose": "plan",
        },
    )

    audit = json.loads(
        run_cli("audit", "context", "--root", str(root), "--pack", pack_id, "--json").stdout
    )
    status = json.loads(run_cli("status", "--root", str(root), "--json").stdout)
    finished = json.loads(
        run_cli(
            "work",
            "finish",
            "--root",
            str(root),
            "--no-doctor",
            "--json",
        ).stdout
    )

    assert audit["review_status"] == "legacy"
    assert audit["finish_allowed"] is True
    assert audit["lineage_valid"] is True
    assert audit["session_attribution_enforced"] is False
    assert audit["session_validation_errors"] == []
    assert any("legacy-action-owner" in warning for warning in audit["legacy_session_mismatches"])
    assert status["context"]["blocking_packs"] == []
    assert finished["ended_session"]["session_id"] == "legacy-action-owner"


def test_context_audit_rejects_a_malformed_terminal_decision(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "message.txt", "checkout auth malformed decision")
    run_cli("session", "start", "--root", str(root), "--id", "malformed-context")
    run_cli(
        "emit",
        "--root",
        str(root),
        "--session",
        "malformed-context",
        "--type",
        "agent.message",
        "--body",
        str(body),
    )
    emitted = json.loads(
        run_cli(
            "context",
            "build",
            "--root",
            str(root),
            "--session",
            "malformed-context",
            "--emit",
            "--json",
            "checkout auth malformed decision",
        ).stdout
    )
    pack_id = emitted["manifest"]["pack_id"]
    presented = emitted["manifest"]["briefing"]["source_ids"]
    emit_event(
        root,
        session_id="malformed-context",
        event_type="context.pack.consumed",
        subject="malformed context decision",
        body=f"action=context_review\npack_id={pack_id}\ndisposition=used\nreason=tampered payload",
        extra_headers={
            "X-AgentDir-Protocol": "agentdir.context-pack.v1",
            "X-AgentDir-Pack-Id": pack_id,
            "X-AgentDir-Reviewed-Source-Id": presented,
            "X-AgentDir-Dismissed-Source-Id": presented,
            "X-AgentDir-Context-Disposition": "used",
            "X-AgentDir-Context-Decision-Id": "ctxd-tampered",
            "X-AgentDir-Context-Decision-Revision": "1",
            "X-AgentDir-Consumption-Purpose": "plan",
        },
    )

    audit = json.loads(
        run_cli("audit", "context", "--root", str(root), "--pack", pack_id, "--json").stdout
    )
    strict = run_cli(
        "audit",
        "session",
        "--root",
        str(root),
        "--session",
        "malformed-context",
        "--strict",
        "--json",
        expected_returncode=1,
    )

    assert audit["review_status"] == "conflict"
    assert audit["lineage_valid"] is False
    assert audit["finish_allowed"] is False
    assert any("decision id" in error for error in audit["decision_validation_errors"])
    assert any("at least one used source" in error for error in audit["decision_validation_errors"])
    assert "malformed decision" in strict.stdout


def test_context_audit_requires_terminal_decision_integrity_headers(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "message.txt", "checkout auth missing integrity headers")
    run_cli("session", "start", "--root", str(root), "--id", "missing-integrity")
    run_cli(
        "emit",
        "--root",
        str(root),
        "--session",
        "missing-integrity",
        "--type",
        "agent.message",
        "--body",
        str(body),
    )
    emitted = json.loads(
        run_cli(
            "context",
            "build",
            "--root",
            str(root),
            "--session",
            "missing-integrity",
            "--emit",
            "--json",
            "checkout auth missing integrity headers",
        ).stdout
    )
    pack_id = emitted["manifest"]["pack_id"]
    presented = emitted["manifest"]["briefing"]["source_ids"]
    used = [presented[0]]
    dismissed = [source_id for source_id in presented if source_id not in set(used)]
    emit_event(
        root,
        session_id="missing-integrity",
        event_type="context.pack.consumed",
        subject="decision missing integrity headers",
        body=(
            f"action=context_review\npack_id={pack_id}\ndisposition=used\n"
            "reason=valid payload without integrity headers"
        ),
        extra_headers={
            "X-AgentDir-Protocol": "agentdir.context-pack.v1",
            "X-AgentDir-Pack-Id": pack_id,
            "X-AgentDir-Source-Id": used,
            "X-AgentDir-Reviewed-Source-Id": presented,
            "X-AgentDir-Used-Source-Id": used,
            "X-AgentDir-Dismissed-Source-Id": dismissed,
            "X-AgentDir-Context-Disposition": "used",
            "X-AgentDir-Consumption-Purpose": "plan",
        },
    )

    audit = json.loads(
        run_cli("audit", "context", "--root", str(root), "--pack", pack_id, "--json").stdout
    )

    assert audit["review_status"] == "conflict"
    assert audit["finish_allowed"] is False
    assert any("decision id is missing" in error for error in audit["decision_validation_errors"])
    assert any(
        "decision revision is missing" in error for error in audit["decision_validation_errors"]
    )


def test_unknown_low_level_context_references_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "message.txt", "checkout auth unknown source guard")
    run_cli("session", "start", "--root", str(root), "--id", "unknown-source")
    run_cli(
        "emit",
        "--root",
        str(root),
        "--session",
        "unknown-source",
        "--type",
        "agent.message",
        "--body",
        str(body),
    )
    emitted = json.loads(
        run_cli(
            "context",
            "build",
            "--root",
            str(root),
            "--session",
            "unknown-source",
            "--emit",
            "--json",
            "checkout auth unknown source guard",
        ).stdout
    )
    pack_id = emitted["manifest"]["pack_id"]
    emit_event(
        root,
        session_id="unknown-source",
        event_type="context.pack.consumed",
        subject="unknown context consume",
        body=f"action=context_consumed\npack_id={pack_id}\npurpose=plan",
        extra_headers={
            "X-AgentDir-Protocol": "agentdir.context-pack.v1",
            "X-AgentDir-Pack-Id": pack_id,
            "X-AgentDir-Source-Id": "src-does-not-exist",
            "X-AgentDir-Consumption-Purpose": "plan",
        },
    )
    emit_event(
        root,
        session_id="unknown-source",
        event_type="context.sources.cited",
        subject="unknown context citation",
        body=f"pack_id={pack_id}\nsource=src-citation-does-not-exist",
        extra_headers={
            "X-AgentDir-Protocol": "agentdir.context-pack.v1",
            "X-AgentDir-Pack-Id": pack_id,
            "X-AgentDir-Source-Id": "src-citation-does-not-exist",
        },
    )

    audit = json.loads(
        run_cli("audit", "context", "--root", str(root), "--pack", pack_id, "--json").stdout
    )
    blocked = run_cli(
        "work",
        "finish",
        "--root",
        str(root),
        "--no-doctor",
        "--json",
        expected_returncode=3,
    )

    assert audit["review_status"] == "conflict"
    assert audit["finish_allowed"] is False
    assert any("consumption references unknown" in error for error in audit["source_validation_errors"])
    assert any("citation references unknown" in error for error in audit["source_validation_errors"])
    assert "cannot be certified" in blocked.stderr


def test_context_actions_reject_a_different_session_than_the_pack(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "message.txt", "checkout auth session attribution guard")
    run_cli("session", "start", "--root", str(root), "--id", "actual-session")
    run_cli(
        "emit",
        "--root",
        str(root),
        "--session",
        "actual-session",
        "--type",
        "agent.message",
        "--body",
        str(body),
    )
    emitted = json.loads(
        run_cli(
            "context",
            "build",
            "--root",
            str(root),
            "--session",
            "actual-session",
            "--emit",
            "--json",
            "checkout auth session attribution guard",
        ).stdout
    )
    pack_id = emitted["manifest"]["pack_id"]
    presented = emitted["manifest"]["briefing"]["source_ids"]
    assert presented

    rejected_consume = run_cli(
        "context",
        "consume",
        "--root",
        str(root),
        "--pack",
        pack_id,
        "--source",
        presented[0],
        "--purpose",
        "plan",
        "--session",
        "wrong-session",
        expected_returncode=2,
    )
    try:
        review_context_pack(
            root,
            pack_id=pack_id,
            disposition="used",
            reason="apply the retrieved session-attribution evidence",
            source_selectors=["1"],
            session_id="wrong-session",
        )
    except AgentDirError as exc:
        review_error = str(exc)
    else:
        raise AssertionError("context review accepted a different session")

    consume_args: list[str] = []
    for source_id in presented:
        consume_args.extend(("--source", source_id))
    run_cli(
        "context",
        "consume",
        "--root",
        str(root),
        "--pack",
        pack_id,
        *consume_args,
        "--purpose",
        "plan",
    )
    rejected_cite = run_cli(
        "context",
        "cite",
        "--root",
        str(root),
        "--pack",
        pack_id,
        "--source",
        presented[0],
        "--session",
        "wrong-session",
        expected_returncode=2,
    )

    assert "belongs to session actual-session, not wrong-session" in rejected_consume.stderr
    assert "belongs to session actual-session, not wrong-session" in review_error
    assert "belongs to session actual-session, not wrong-session" in rejected_cite.stderr


def test_context_event_from_a_different_session_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "message.txt", "checkout auth event attribution guard")
    run_cli("session", "start", "--root", str(root), "--id", "actual-session")
    run_cli(
        "emit",
        "--root",
        str(root),
        "--session",
        "actual-session",
        "--type",
        "agent.message",
        "--body",
        str(body),
    )
    emitted = json.loads(
        run_cli(
            "context",
            "build",
            "--root",
            str(root),
            "--session",
            "actual-session",
            "--emit",
            "--json",
            "checkout auth event attribution guard",
        ).stdout
    )
    pack_id = emitted["manifest"]["pack_id"]
    presented = emitted["manifest"]["briefing"]["source_ids"]
    assert presented
    run_cli("session", "start", "--root", str(root), "--id", "wrong-session")
    emit_event(
        root,
        session_id="wrong-session",
        event_type="context.pack.consumed",
        subject="misattributed context consume",
        body=f"action=context_consumed\npack_id={pack_id}\npurpose=plan",
        extra_headers={
            "X-AgentDir-Protocol": "agentdir.context-pack.v1",
            "X-AgentDir-Pack-Id": pack_id,
            "X-AgentDir-Source-Id": presented,
            "X-AgentDir-Consumption-Purpose": "plan",
        },
    )

    audit = json.loads(
        run_cli("audit", "context", "--root", str(root), "--pack", pack_id, "--json").stdout
    )
    status = json.loads(run_cli("status", "--root", str(root), "--json").stdout)
    blocked = run_cli(
        "work",
        "finish",
        "--root",
        str(root),
        "--no-doctor",
        "--json",
        expected_returncode=3,
    )
    current = json.loads(run_cli("session", "current", "--root", str(root), "--json").stdout)

    assert audit["review_status"] == "conflict"
    assert audit["finish_allowed"] is False
    assert audit["lineage_valid"] is False
    assert any("event session 'wrong-session'" in error for error in audit["session_validation_errors"])
    assert pack_id in status["context"]["blocking_packs"]
    assert "cannot be certified" in blocked.stderr
    assert current["session_id"] == "wrong-session"


def test_context_consume_rejects_sources_outside_the_pack(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "message.txt", "auth login context marker")

    run_cli("session", "start", "--root", str(root), "--id", "context-session")
    run_cli("emit", "--root", str(root), "--session", "context-session", "--type", "agent.message", "--body", str(body))
    pack_result = run_cli(
        "context",
        "build",
        "--root",
        str(root),
        "--session",
        "context-session",
        "--emit",
        "--json",
        "auth login",
    )
    pack_id = json.loads(pack_result.stdout)["manifest"]["pack_id"]

    rejected = run_cli(
        "context",
        "consume",
        "--root",
        str(root),
        "--pack",
        pack_id,
        "--source",
        "message:sessions/not-in-pack/Maildir/new/missing",
        "--purpose",
        "plan",
        expected_returncode=2,
    )

    assert "Unknown context source" in rejected.stderr
