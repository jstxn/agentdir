from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from email.parser import BytesParser
from email.policy import default
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


def test_index_rebuild_creates_builtin_vector_memory_documents(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "body.txt", "database migration failure in account auth")

    run_cli("emit", "--root", str(root), "--session", "session-1", "--type", "agent.message", "--body", str(body))
    run_cli("index", "rebuild", "--root", str(root))

    with sqlite3.connect(index_path(root)) as conn:
        assert conn.execute("select value from metadata where key = 'vector_memory'").fetchone()[0] == "yes"
        assert conn.execute("select value from metadata where key = 'hybrid_passages'").fetchone()[0] == "yes"
        assert conn.execute("select count(*) from memory_documents").fetchone()[0] == 2
        assert conn.execute("select count(*) from memory_passages").fetchone()[0] == 2
        assert conn.execute("select count(*) from memory_terms").fetchone()[0] > 0
        assert conn.execute(
            "select count(*) from memory_documents where source_kind = 'message'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "select count(*) from memory_documents where source_kind = 'session_summary'"
        ).fetchone()[0] == 1
        row = conn.execute(
            "select vector_dim, token_count, length(vector_json) from memory_documents"
        ).fetchone()

    assert row[0] == 256
    assert row[1] > 0
    assert row[2] > 0


def test_hybrid_memory_search_returns_best_passage_for_long_records(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    long_text = " ".join(
        [
            *["frontend spacing hover polish"] * 80,
            "sqlite wal checkpoint corruption recovery marker",
            *["button color padding layout"] * 80,
        ]
    )
    other_text = "sqlite migration completed in the unrelated service"
    long_body = write_body(tmp_path / "long.txt", long_text)
    other_body = write_body(tmp_path / "other.txt", other_text)

    run_cli("emit", "--root", str(root), "--session", "long-session", "--type", "agent.message", "--body", str(long_body))
    run_cli("emit", "--root", str(root), "--session", "other-session", "--type", "agent.message", "--body", str(other_body))

    result = run_cli(
        "memory",
        "search",
        "--root",
        str(root),
        "--json",
        "checkpoint corruption recovery",
    )
    rows = json.loads(result.stdout)

    assert rows[0]["session_id"] == "long-session"
    assert rows[0]["retrieval_mode"] == "hybrid"
    assert rows[0]["passage_id"]
    assert "checkpoint corruption recovery" in rows[0]["passage_body_text"]


def test_memory_backend_list_reports_optional_vector_extras(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "body.txt", "agentdir backend status marker")
    run_cli("emit", "--root", str(root), "--session", "backend-session", "--type", "agent.message", "--body", str(body))

    result = run_cli("memory", "backend", "list", "--root", str(root), "--json")
    payload = json.loads(result.stdout)

    assert payload["active"] == "local-hybrid"
    assert payload["source_of_truth"] == "immutable envelopes"
    assert payload["backends"][0]["enabled"] is True
    assert payload["backends"][0]["passages"] > 0
    assert any(backend["name"] == "sqlite-vec" and backend["enabled"] is False for backend in payload["backends"])


def test_memory_optional_backends_can_be_configured_without_becoming_truth(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "body.txt", "semantic backend configuration marker")
    run_cli("emit", "--root", str(root), "--session", "backend-session", "--type", "agent.message", "--body", str(body))

    vector = run_cli("memory", "backend", "configure", "--root", str(root), "sqlite-vec", "--json")
    embeddings = run_cli(
        "memory",
        "embeddings",
        "configure",
        "--root",
        str(root),
        "fastembed",
        "--model",
        "BAAI/bge-small-en-v1.5",
        "--json",
    )
    team = run_cli("memory", "team", "configure", "--root", str(root), "qdrant", "--json")
    semantic = run_cli(
        "memory",
        "search",
        "--root",
        str(root),
        "--retrieval",
        "semantic",
        "configuration marker",
        expected_returncode=2,
    )

    vector_payload = json.loads(vector.stdout)
    embeddings_payload = json.loads(embeddings.stdout)
    team_payload = json.loads(team.stdout)

    assert vector_payload["source_of_truth"] == "immutable envelopes"
    assert vector_payload["config"]["vector_backend"] == "sqlite-vec"
    assert embeddings_payload["config"]["embeddings"]["provider"] == "fastembed"
    assert embeddings_payload["config"]["embeddings"]["model"] == "BAAI/bge-small-en-v1.5"
    assert team_payload["config"]["team_backend"] == "qdrant"
    assert "fastembed" in semantic.stderr


def test_memory_daemon_run_once_and_start_status_stop(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "body.txt", "daemon refresh marker")
    run_cli("emit", "--root", str(root), "--session", "daemon-session", "--type", "agent.message", "--body", str(body))

    once = run_cli("memory", "daemon", "run", "--root", str(root), "--once", "--json")
    started = run_cli("memory", "daemon", "start", "--root", str(root), "--interval", "10", "--force", "--json")
    status = run_cli("memory", "daemon", "status", "--root", str(root), "--json")
    stopped = run_cli("memory", "daemon", "stop", "--root", str(root), "--json")

    once_payload = json.loads(once.stdout)
    started_payload = json.loads(started.stdout)
    status_payload = json.loads(status.stdout)
    stopped_payload = json.loads(stopped.stdout)

    assert once_payload["last_refresh_ok"] is True
    assert started_payload["started"] is True
    assert status_payload["running"] is True
    assert status_payload["watch_backend"] in {"poll", "watchfiles"}
    assert stopped_payload["running"] is False


def test_memory_search_auto_rebuilds_and_ranks_relevant_records(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    auth_body = write_body(
        tmp_path / "auth.txt",
        "The agent fixed an auth token database migration failure in the login service.",
    )
    ui_body = write_body(
        tmp_path / "ui.txt",
        "The agent adjusted button spacing and hover color on the dashboard.",
    )

    run_cli("emit", "--root", str(root), "--session", "auth-session", "--type", "agent.message", "--body", str(auth_body))
    run_cli("emit", "--root", str(root), "--session", "ui-session", "--type", "agent.message", "--body", str(ui_body))

    result = run_cli(
        "memory",
        "search",
        "--root",
        str(root),
        "--json",
        "auth token database migration",
    )
    rows = json.loads(result.stdout)

    assert rows[0]["session_id"] == "auth-session"
    assert rows[0]["memory_score"] > 0
    assert "auth token database migration" in rows[0]["body_text"]


def test_memory_search_can_return_derived_session_summaries(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    first = write_body(tmp_path / "first.txt", "checkout flow failed after auth redirect")
    second = write_body(tmp_path / "second.txt", "pytest checkout redirect regression passed")

    run_cli("emit", "--root", str(root), "--session", "checkout-session", "--type", "agent.message", "--body", str(first))
    run_cli("emit", "--root", str(root), "--session", "checkout-session", "--type", "tool.result", "--body", str(second))

    result = run_cli(
        "memory",
        "search",
        "--root",
        str(root),
        "--type",
        "summary.compacted",
        "--json",
        "checkout auth redirect regression",
    )
    rows = json.loads(result.stdout)

    assert rows[0]["source_kind"] == "session_summary"
    assert rows[0]["session_id"] == "checkout-session"
    assert "Session summary: checkout-session" in rows[0]["body_text"]


def test_memory_explain_reports_why_a_hit_matched(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "body.txt", "auth token database migration failure")
    run_cli("emit", "--root", str(root), "--session", "auth-session", "--type", "agent.message", "--body", str(body))

    search = run_cli("memory", "search", "--root", str(root), "--json", "auth database")
    source_id = json.loads(search.stdout)[0]["source_id"]
    explain = run_cli("memory", "explain", "--root", str(root), "--source", source_id, "--json", "auth database")
    payload = json.loads(explain.stdout)

    assert payload["source_id"] == source_id
    assert payload["memory_score"] > 0
    assert set(payload["overlap_terms"]) >= {"auth", "database"}
    assert "auth token database migration failure" in payload["excerpt"]


def test_query_semantic_uses_vector_memory_with_existing_filters(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    first = write_body(tmp_path / "first.txt", "pytest failure caused by sqlite index rebuild")
    second = write_body(tmp_path / "second.txt", "pytest failure caused by frontend route state")

    run_cli("emit", "--root", str(root), "--session", "backend", "--type", "tool.result", "--body", str(first))
    run_cli("emit", "--root", str(root), "--session", "frontend", "--type", "tool.result", "--body", str(second))

    result = run_cli(
        "query",
        "--root",
        str(root),
        "--session",
        "backend",
        "--type",
        "tool.result",
        "--semantic",
        "sqlite index failure",
        "--json",
    )
    rows = json.loads(result.stdout)

    assert [row["session_id"] for row in rows] == ["backend"]
    assert "sqlite index rebuild" in rows[0]["body_text"]
    assert rows[0]["memory_score"] > 0


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
        "auth login",
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
        "--source",
        evidence_source["source_id"],
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
    body = write_body(tmp_path / "child.txt", "federated context source marker")

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
    federated_source = next(source for source in manifest["sources"] if source.get("source_root_name") == "child")

    assert manifest["federated"] is True
    assert manifest["retrieval_mode"] == "hybrid"
    assert federated_source["source_id"].startswith(f"{federated_source['source_root_id']}:message:")
    assert federated_source["source_id_original"].startswith("message:")
    assert event_message["X-AgentDir-Context-Scope"] == "federated"
