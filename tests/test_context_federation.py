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


def test_roots_register_and_federated_memory_search(tmp_path: Path) -> None:
    controller = tmp_path / "controller"
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    run_cli("init", str(controller))
    run_cli("init", str(alpha))
    run_cli("init", str(beta))
    alpha_body = write_body(tmp_path / "alpha.txt", "federated checkout memory marker in registered root")
    beta_body = write_body(tmp_path / "beta.txt", "federated checkout memory marker in unregistered root")

    run_cli("emit", "--root", str(alpha), "--session", "alpha-session", "--type", "agent.message", "--body", str(alpha_body))
    run_cli("emit", "--root", str(beta), "--session", "beta-session", "--type", "agent.message", "--body", str(beta_body))
    registered = run_cli(
        "roots",
        "register",
        "--root",
        str(controller),
        str(alpha),
        "--name",
        "alpha",
        "--json",
    )
    listed = run_cli("roots", "list", "--root", str(controller), "--json")
    result = run_cli(
        "memory",
        "search",
        "--root",
        str(controller),
        "--federated",
        "--type",
        "agent.message",
        "--json",
        "federated checkout marker",
    )

    entry = json.loads(registered.stdout)
    roots = json.loads(listed.stdout)
    rows = json.loads(result.stdout)

    assert entry["name"] == "alpha"
    assert roots[0]["available"] is True
    assert rows[0]["source_root_name"] == "alpha"
    assert rows[0]["session_id"] == "alpha-session"
    assert rows[0]["source_id"].startswith(f"{entry['root_id']}:message:")
    assert rows[0]["source_id_original"].startswith("message:")
    assert all(row["session_id"] != "beta-session" for row in rows)


def test_root_suggest_doctor_and_groups_scope_federated_search(tmp_path: Path) -> None:
    controller = tmp_path / "controller"
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    run_cli("init", str(controller))
    run_cli("init", str(alpha))
    run_cli("init", str(beta))
    alpha_body = write_body(tmp_path / "alpha.txt", "grouped memory marker from alpha root")
    beta_body = write_body(tmp_path / "beta.txt", "grouped memory marker from beta root")

    run_cli("emit", "--root", str(alpha), "--session", "alpha-session", "--type", "agent.message", "--body", str(alpha_body))
    run_cli("emit", "--root", str(beta), "--session", "beta-session", "--type", "agent.message", "--body", str(beta_body))
    suggestions = run_cli("roots", "suggest", "--root", str(controller), "--near", str(tmp_path), "--json")
    listed_before_register = run_cli("roots", "list", "--root", str(controller), "--json")

    alpha_entry = json.loads(
        run_cli("roots", "register", "--root", str(controller), str(alpha), "--name", "alpha", "--json").stdout
    )
    beta_entry = json.loads(
        run_cli("roots", "register", "--root", str(controller), str(beta), "--name", "beta", "--json").stdout
    )
    group = run_cli(
        "roots",
        "group",
        "create",
        "--root",
        str(controller),
        "product",
        "--member",
        alpha_entry["root_id"],
        "--json",
    )
    grouped_search = run_cli(
        "memory",
        "search",
        "--root",
        str(controller),
        "--group",
        "product",
        "--json",
        "grouped memory marker",
    )
    doctor = run_cli("roots", "doctor", "--root", str(controller), "--group", "product", "--json")
    run_cli(
        "roots",
        "group",
        "add",
        "--root",
        str(controller),
        "product",
        beta_entry["root_id"],
    )
    groups = run_cli("roots", "group", "list", "--root", str(controller), "--json")

    suggestion_payload = json.loads(suggestions.stdout)
    rows = json.loads(grouped_search.stdout)
    doctor_payload = json.loads(doctor.stdout)
    group_payload = json.loads(group.stdout)
    groups_payload = json.loads(groups.stdout)

    assert json.loads(listed_before_register.stdout) == []
    assert {item["name"] for item in suggestion_payload} >= {"alpha", "beta"}
    assert group_payload["root_ids"] == [alpha_entry["root_id"]]
    assert {row["session_id"] for row in rows} == {"alpha-session"}
    assert doctor_payload[0]["root_id"] == alpha_entry["root_id"]
    assert "stale" in doctor_payload[0]
    assert set(groups_payload[0]["root_ids"]) == {alpha_entry["root_id"], beta_entry["root_id"]}


def test_federated_context_build_emits_root_qualified_manifest(tmp_path: Path) -> None:
    controller = tmp_path / "controller"
    child = tmp_path / "child"
    run_cli("init", str(controller))
    run_cli("init", str(child))
    body = write_body(
        tmp_path / "child.txt",
        "federated context source marker\n"
        + ("canonical child detail " * 60)
        + "\ndeep federated tail sentinel",
    )

    run_cli("session", "start", "--root", str(controller), "--id", "controller-session")
    run_cli("emit", "--root", str(child), "--session", "child-session", "--type", "agent.message", "--body", str(body))
    run_cli("roots", "register", "--root", str(controller), str(child), "--name", "child")
    result = run_cli(
        "context",
        "build",
        "--root",
        str(controller),
        "--federated",
        "--emit",
        "--json",
        "federated context marker",
    )

    payload = json.loads(result.stdout)
    manifest = payload["manifest"]
    event_message = parse_message(Path(payload["event_path"]))
    federated_source = next(
        source
        for source in manifest["sources"]
        if source.get("source_root_name") == "child"
        and source.get("event_type") == "agent.message"
    )
    briefing = brief_context_manifest(manifest)
    source_ref = next(
        source["ref"]
        for source in briefing["sources"]
        if source["source_id"] == federated_source["source_id"]
    )
    expanded = json.loads(
        run_cli(
            "work",
            "context",
            "--root",
            str(controller),
            "--pack",
            manifest["pack_id"],
            "--expand",
            source_ref,
            "--json",
        ).stdout
    )

    assert manifest["federated"] is True
    assert manifest["retrieval_mode"] == "hybrid"
    assert federated_source["source_id"].startswith(f"{federated_source['source_root_id']}:message:")
    assert federated_source["source_id_original"].startswith("message:")
    assert event_message["X-AgentDir-Context-Scope"] == "federated"
    assert expanded["integrity"] == "verified"
    assert expanded["basis"] == "canonical_envelope"
    assert expanded["source"]["root_id"] == federated_source["source_root_id"]
    assert "deep federated tail sentinel" in expanded["content"]

    version = child / "VERSION"
    unavailable_version = child / "VERSION.unavailable"
    version.rename(unavailable_version)
    try:
        unavailable = json.loads(
            run_cli(
                "work",
                "context",
                "--root",
                str(controller),
                "--pack",
                manifest["pack_id"],
                "--expand",
                source_ref,
                "--json",
            ).stdout
        )
    finally:
        unavailable_version.rename(version)

    assert unavailable["integrity"] == "unavailable"
    assert unavailable["basis"] == "manifest_excerpt"
    assert unavailable["extent"] == "stored_excerpt"
    assert "deep federated tail sentinel" not in unavailable["content"]
    assert unavailable["receipt"]["status"] == "not_recorded"


def test_malformed_expansion_receipt_is_visible_but_never_blocks_a_valid_decision(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    prior = write_body(tmp_path / "prior.txt", "optional expansion receipt validation marker")
    run_cli("session", "start", "--root", str(root), "--id", "prior-session")
    run_cli(
        "emit",
        "--root",
        str(root),
        "--type",
        "agent.message",
        "--body",
        str(prior),
    )
    run_cli("session", "end", "--root", str(root), "--summary", str(prior))
    started = json.loads(
        run_cli(
            "work",
            "start",
            "--root",
            str(root),
            "--json",
            "optional expansion receipt validation marker",
        ).stdout
    )
    pack_id = started["context_pack"]["pack_id"]
    session_id = started["session"]["session_id"]
    run_cli(
        "work",
        "context",
        "--root",
        str(root),
        "--pack",
        pack_id,
        "--none-relevant",
        "--reason",
        "the historical record does not constrain this task",
    )
    clean = json.loads(
        run_cli("audit", "context", "--root", str(root), "--pack", pack_id, "--json").stdout
    )

    emit_event(
        root,
        session_id=session_id,
        event_type="context.sources.expanded",
        subject="malformed optional context expansion receipt",
        body="receipt intentionally missing required metadata",
        extra_headers={"X-AgentDir-Context-View-Pack-Id": pack_id},
    )
    emit_event(
        root,
        session_id=session_id,
        event_type="context.sources.expanded",
        subject="orphan optional context expansion receipt",
        body="receipt intentionally claims an unknown pack",
        extra_headers={"X-AgentDir-Context-View-Pack-Id": "ctx-does-not-exist"},
    )
    wrong_session = emit_event(
        root,
        session_id=session_id,
        event_type="context.sources.expanded",
        subject="physically owned receipt with wrong session header",
        body="routing header intentionally changed before indexing",
        extra_headers={"X-AgentDir-Context-View-Pack-Id": "ctx-wrong-session"},
    )
    wrong_session.path.write_text(
        wrong_session.path.read_text(encoding="utf-8").replace(
            f"X-AgentDir-Session: {session_id}",
            "X-AgentDir-Session: spoofed-session",
        ),
        encoding="utf-8",
    )
    wrong_type = emit_event(
        root,
        session_id=session_id,
        event_type="context.sources.expanded",
        subject="physically owned receipt with missing event type",
        body="event header intentionally changed before indexing",
        extra_headers={"X-AgentDir-Context-View-Pack-Id": "ctx-wrong-type"},
    )
    wrong_type.path.write_text(
        wrong_type.path.read_text(encoding="utf-8").replace(
            "X-AgentDir-Event-Type: context.sources.expanded",
            "X-AgentDir-Event-Typo: context.sources.expanded",
        ),
        encoding="utf-8",
    )
    audit = json.loads(
        run_cli("audit", "context", "--root", str(root), "--pack", pack_id, "--json").stdout
    )
    status = json.loads(run_cli("status", "--root", str(root), "--json").stdout)
    finished = json.loads(
        run_cli("work", "finish", "--root", str(root), "--json").stdout
    )

    for field in ("review_status", "decision_id", "finish_allowed", "lineage_valid"):
        assert audit[field] == clean[field]
    assert audit["expansion"]["receipts_valid"] is False
    assert audit["expansion"]["receipt_event_count"] == 1
    assert audit["expansion"]["valid_receipt_count"] == 0
    assert audit["expansion"]["validation_errors"]
    assert pack_id in status["context"]["attention_packs"]
    assert pack_id not in status["context"]["blocking_packs"]
    assert status["health"]["ok"] is True
    assert any(
        "optional context expansion receipt" in warning
        for warning in status["health"]["warnings"]
    )
    inventory = status["context"]["expansion_receipt_inventory"]
    assert inventory["event_count"] == 4
    assert inventory["orphan_event_count"] == 3
    assert inventory["receipts_valid"] is False
    with sqlite3.connect(index_path(root)) as conn:
        direct_receipt_documents = conn.execute(
            """
            select count(distinct md.id)
            from memory_documents md
            join headers h on h.message_rowid = md.message_rowid
            where lower(h.name) in (
              'x-agentdir-context-view-pack-id',
              'x-agentdir-context-view-id',
              'x-agentdir-context-view-source-id'
            )
            """
        ).fetchone()[0]
        receipt_summary_leaks = conn.execute(
            """
            select count(*) from memory_documents
            where source_kind = 'session_summary'
              and (
                body_text like '%routing header intentionally changed%'
                or body_text like '%event header intentionally changed%'
              )
            """
        ).fetchone()[0]
    assert direct_receipt_documents == 0
    assert receipt_summary_leaks == 0
    lineage = finished["report"]["agent_handoff"]["context_lineage"]
    assert lineage["ok"] is True
    assert lineage["expansion"]["receipts_valid"] is False
    assert finished["report"]["agent_handoff"]["status"] == "needs_attention"
    inventory_check = next(
        check
        for check in finished["report"]["session_audit"]["checks"]
        if check["id"] == "context_expansion_receipt_inventory"
    )
    assert inventory_check["status"] == "warn"
    assert finished["report"]["health"]["ok"] is True
