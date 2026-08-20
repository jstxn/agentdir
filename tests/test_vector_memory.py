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

    assert row[0] == 1024
    assert row[1] > 0
    assert row[2] > 0


def test_incremental_index_cleans_receipt_memory_written_by_an_older_cli(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    emit_event(
        root,
        session_id="receipt-session",
        event_type="context.sources.expanded",
        body="old cli receipt memory noise marker",
        extra_headers={"X-AgentDir-Context-View-Pack-Id": "ctx-old-cli"},
    )
    run_cli("index", "rebuild", "--root", str(root))

    with sqlite3.connect(index_path(root)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "select * from messages where event_type = 'context.sources.expanded'"
        ).fetchone()
        assert row is not None
        index_memory_document(
            conn,
            message_rowid=row["id"],
            message_id=row["message_id"],
            session_id=row["session_id"],
            event_type=row["event_type"],
            subject=row["subject"],
            from_actor=row["from_actor"],
            to_actor=row["to_actor"],
            task_id=row["task_id"],
            tool=row["tool"],
            tool_exit_code=row["tool_exit_code"],
            workspace=row["workspace"],
            git_head=row["git_head"],
            date_header=row["date_header"],
            date_utc=row["date_utc"],
            file_path=row["file_path"],
            body_text=row["body_text"],
            indexed_at=row["indexed_at"],
        )
        conn.execute(
            """
            update memory_documents
            set body_text = body_text || '\n- context.sources.expanded: old cli receipt memory noise marker'
            where source_kind = 'session_summary' and session_id = 'receipt-session'
            """
        )
        conn.commit()

    run_cli("index", "update", "--root", str(root))
    stats = json.loads(run_cli("memory", "stats", "--root", str(root), "--json").stdout)
    with sqlite3.connect(index_path(root)) as conn:
        direct = conn.execute(
            "select count(*) from memory_documents where event_type = 'context.sources.expanded'"
        ).fetchone()[0]
        summary = conn.execute(
            """
            select body_text from memory_documents
            where source_kind = 'session_summary' and session_id = 'receipt-session'
            """
        ).fetchone()[0]

    assert direct == 0
    assert "- context.sources.expanded:" not in summary
    assert "context.sources.expanded=1" in summary
    assert stats["coverage"] == 1.0


def test_index_rebuild_preserves_semantic_embedding_cache(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "body.txt", "semantic embedding cache survival marker")

    run_cli("emit", "--root", str(root), "--session", "cache-session", "--type", "agent.message", "--body", str(body))
    run_cli("index", "rebuild", "--root", str(root))

    with sqlite3.connect(index_path(root)) as conn:
        source_id, text_sha256 = conn.execute(
            "select source_id, text_sha256 from memory_documents where source_kind = 'message'"
        ).fetchone()
        conn.execute(
            """
            insert into semantic_embeddings(
              source_id, model, text_sha256, dimensions, vector_json, indexed_at
            ) values (?, ?, ?, ?, ?, ?)
            """,
            (source_id, "test-model", text_sha256, 3, "[0.1, 0.2, 0.3]", "2026-06-09T00:00:00+00:00"),
        )
        conn.execute(
            """
            insert into semantic_embeddings(
              source_id, model, text_sha256, dimensions, vector_json, indexed_at
            ) values (?, ?, ?, ?, ?, ?)
            """,
            ("message:gone/stale", "test-model", "0" * 64, 3, "[0.0, 0.0, 0.0]", "2026-06-09T00:00:00+00:00"),
        )
        conn.commit()

    run_cli("index", "rebuild", "--root", str(root))

    with sqlite3.connect(index_path(root)) as conn:
        rows = conn.execute(
            "select source_id, text_sha256, vector_json from semantic_embeddings"
        ).fetchall()

    assert rows == [(source_id, text_sha256, "[0.1, 0.2, 0.3]")]


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
    automatic = run_cli(
        "memory",
        "search",
        "--root",
        str(root),
        "--json",
        "configuration marker",
    )

    vector_payload = json.loads(vector.stdout)
    embeddings_payload = json.loads(embeddings.stdout)
    team_payload = json.loads(team.stdout)

    assert vector_payload["source_of_truth"] == "immutable envelopes"
    assert vector_payload["config"]["vector_backend"] == "sqlite-vec"
    assert embeddings_payload["config"]["embeddings"]["provider"] == "fastembed"
    assert embeddings_payload["config"]["embeddings"]["model"] == "BAAI/bge-small-en-v1.5"
    assert embeddings_payload["active"] == "local-hybrid"
    assert json.loads(automatic.stdout)[0]["requested_retrieval_mode"] == "auto"
    assert json.loads(automatic.stdout)[0]["retrieval_mode"] == "hybrid"
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


def test_memory_explain_preserves_the_actual_semantic_score_and_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(
        tmp_path / "decision.txt",
        "Tenant-scoped webhook callback backoff uses an ephemeral failure ledger.",
    )
    run_cli(
        "emit",
        "--root",
        str(root),
        "--session",
        "retry-decision",
        "--type",
        "decision.recorded",
        "--body",
        str(body),
    )
    run_cli("index", "update", "--root", str(root))

    query = "stabilize outbound retries following temporary upstream interruptions"
    monkeypatch.setattr(memory, "_module_available", lambda module: module == "fastembed")
    memory.configure_embeddings(root, "fastembed", model="test-semantic-model")

    def fake_vectors(_root: Path, _model: str, texts: list[str]) -> list[list[float]]:
        return [
            [1.0, 0.0]
            if text == query
            else ([0.8, 0.6] if "webhook callback backoff" in text else [0.0, 1.0])
            for text in texts
        ]

    monkeypatch.setattr(memory, "_fastembed_vectors", fake_vectors)
    search = memory.search_memory(root, query, retrieval_mode="semantic")
    hit = next(row for row in search if row["event_type"] == "decision.recorded")

    explanation = memory.explain_memory_match(
        root,
        query,
        source_id=hit["source_id"],
        retrieval_mode="semantic",
    )

    assert explanation["memory_score"] == hit["memory_score"] == 0.8
    assert explanation["semantic_score"] == 0.8
    assert explanation["retrieval_mode"] == "semantic"
    assert explanation["requested_retrieval_mode"] == "semantic"
    assert explanation["overlap_terms"] == []


def test_automatic_retrieval_fuses_semantic_and_lexical_signals_when_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(
        tmp_path / "decision.txt",
        "Tenant-scoped webhook callback backoff uses an ephemeral failure ledger.",
    )
    run_cli(
        "emit",
        "--root",
        str(root),
        "--session",
        "retry-decision",
        "--type",
        "decision.recorded",
        "--body",
        str(body),
    )
    run_cli("index", "update", "--root", str(root))

    query = "stabilize outbound retries following ephemeral failure upstream interruptions"
    monkeypatch.setattr(memory, "_module_available", lambda module: module == "fastembed")
    backend = memory.configure_embeddings(root, "fastembed", model="test-semantic-model")

    def fake_vectors(_root: Path, _model: str, texts: list[str]) -> list[list[float]]:
        return [
            [1.0, 0.0]
            if text == query
            else ([0.8, 0.6] if "webhook callback backoff" in text else [0.0, 1.0])
            for text in texts
        ]

    monkeypatch.setattr(memory, "_fastembed_vectors", fake_vectors)
    rows = memory.search_memory(root, query, retrieval_mode="auto")
    hit = next(row for row in rows if row["event_type"] == "decision.recorded")

    assert backend["active"] == "semantic-hybrid"
    assert hit["retrieval_mode"] == "semantic-hybrid"
    assert hit["requested_retrieval_mode"] == "auto"
    assert hit["semantic_score"] == 0.8
    assert hit["hybrid_score"] > 0
    assert hit["memory_score"] >= hit["semantic_score"]

    automatic_pack = build_context_pack(
        root,
        query,
        memory_limit=5,
        recent_limit=0,
        rebuild=False,
    )
    assert automatic_pack["requested_retrieval_mode"] == "auto"
    assert automatic_pack["retrieval_mode"] == "semantic-hybrid"
    assert automatic_pack["memory_hits"][0]["event_type"] == "decision.recorded"


def test_automatic_retrieval_defaults_are_used_by_agent_entry_points() -> None:
    parser = build_parser()

    assert parser.parse_args(["work", "start", "test task"]).retrieval == "auto"
    assert parser.parse_args(["context", "build", "test task"]).retrieval == "auto"
    assert parser.parse_args(["memory", "search", "test task"]).retrieval == "auto"
    assert parser.parse_args(["memory", "explain", "test task"]).retrieval == "auto"


def test_context_pack_candidate_selection_does_not_spend_slots_on_lifecycle_noise(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    run_cli("index", "update", "--root", str(root))
    task = "tenant callback retry policy"
    rows = [
        {
            "source_id": "message:started-noise",
            "session_id": "noise-session",
            "event_type": "work.started",
            "body_text": task,
            "memory_score": 0.99,
        },
        {
            "source_id": "message:report-noise",
            "session_id": "report-session",
            "event_type": "work.report.final",
            "body_text": task,
            "memory_score": 0.98,
        },
        *[
            {
                "source_id": f"message:decision-{index}",
                "session_id": f"decision-session-{index}",
                "event_type": "decision.recorded",
                "body_text": f"{task} decision {index}",
                "memory_score": 0.90 - index / 100,
            }
            for index in range(5)
        ],
    ]
    monkeypatch.setattr(context_module, "search_memory", lambda *_args, **_kwargs: rows)

    pack = build_context_pack(
        root,
        task,
        memory_limit=5,
        recent_limit=0,
        rebuild=False,
    )

    assert [row["event_type"] for row in pack["memory_hits"]] == [
        "decision.recorded",
    ] * 5


def test_fastembed_runtime_disables_telemetry_and_uses_store_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "store"
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    run_cli("init", str(root))
    events: list[str] = []

    fake_onnxruntime = types.ModuleType("onnxruntime")

    def disable_telemetry_events() -> None:
        events.append("telemetry-disabled")

    fake_onnxruntime.disable_telemetry_events = disable_telemetry_events  # type: ignore[attr-defined]
    fake_fastembed = types.ModuleType("fastembed")

    class FakeTextEmbedding:
        def __init__(self, *, model_name: str, cache_dir: str) -> None:
            events.append("model-created")
            self.model_name = model_name
            self.cache_dir = cache_dir
            if os.environ.get("ORT_DISABLE_TELEMETRY") != "1":
                (Path.cwd() / ":memory:.ses").write_text("telemetry", encoding="utf-8")

        def embed(self, texts: list[str]):
            return [[1.0, 0.0] for _ in texts]

    fake_fastembed.TextEmbedding = FakeTextEmbedding  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_onnxruntime)
    monkeypatch.setitem(sys.modules, "fastembed", fake_fastembed)
    monkeypatch.delenv("ORT_DISABLE_TELEMETRY", raising=False)
    monkeypatch.setenv("AGENTDIR_CACHE_DIR", str(tmp_path / "runtime-cache"))
    monkeypatch.chdir(checkout)
    memory._FASTEMBED_MODELS.clear()

    assert memory._fastembed_vectors(root, "test-model", ["semantic text"]) == [[1.0, 0.0]]

    model = next(iter(memory._FASTEMBED_MODELS.values()))
    assert events == ["telemetry-disabled", "model-created"]
    assert os.environ["ORT_DISABLE_TELEMETRY"] == "1"
    assert Path(model.cache_dir) == tmp_path / "runtime-cache" / "fastembed"
    assert not Path(model.cache_dir).is_relative_to(checkout)
    assert not (checkout / ":memory:.ses").exists()


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
