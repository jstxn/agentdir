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
    assert (
        parser.parse_args(
            ["memory", "explain", "test task", "--retrieval", "semantic-hybrid"]
        ).retrieval
        == "semantic-hybrid"
    )


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


def test_context_pack_omits_untargeted_operational_noise_and_duplicate_reports(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    run_cli("index", "update", "--root", str(root))
    task = "start deployment profile inheritance rollback policy"
    rows = [
        {
            "source_id": "message:decision",
            "session_id": "relevant-session",
            "event_type": "decision.recorded",
            "body_text": f"{task}: nested maps merge and lists replace",
            "memory_score": 0.82,
        },
        {
            "source_id": "message:verification",
            "session_id": "verification-session",
            "event_type": "tool.result",
            "body_text": f"verified {task} with focused tests",
            "memory_score": 0.79,
        },
        {
            "source_id": "message:duplicate-report",
            "session_id": "relevant-session",
            "event_type": "work.report.final",
            "body_text": f"final report repeats {task}",
            "memory_score": 0.99,
        },
        {
            "source_id": "message:standalone-report",
            "session_id": "standalone-report-session",
            "event_type": "work.report.final",
            "body_text": f"another final report repeats {task}",
            "memory_score": 0.995,
        },
        {
            "source_id": "message:hook-noise",
            "session_id": "hook-session",
            "event_type": "git.hook.pre-commit",
            "subject": "pre-commit hook passed",
            "body_text": task,
            "memory_score": 0.98,
        },
        {
            "source_id": "message:claim-noise",
            "session_id": "claim-session",
            "event_type": "claim.recorded",
            "subject": "test claim passed",
            "body_text": task,
            "memory_score": 0.97,
        },
        {
            "source_id": "message:lifecycle-noise",
            "session_id": "lifecycle-session",
            "event_type": "work.started",
            "body_text": task,
            "memory_score": 0.96,
        },
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
        "tool.result",
    ]


def test_context_pack_uses_final_report_only_as_last_resort(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    run_cli("index", "update", "--root", str(root))
    task = "deployment profile inheritance rollback policy"
    rows = [
        {
            "source_id": "message:only-report",
            "session_id": "report-session",
            "event_type": "work.report.final",
            "subject": "deployment profile implementation handoff",
            "body_text": f"{task}: nested mappings merge and lists replace",
            "memory_score": 0.88,
        },
        {
            "source_id": "message:hook-noise",
            "session_id": "hook-session",
            "event_type": "git.hook.pre-commit",
            "body_text": task,
            "memory_score": 0.99,
        },
        {
            "source_id": "message:claim-noise",
            "session_id": "claim-session",
            "event_type": "claim.recorded",
            "body_text": task,
            "memory_score": 0.98,
        },
        {
            "source_id": "message:lifecycle-noise",
            "session_id": "lifecycle-session",
            "event_type": "work.started",
            "body_text": task,
            "memory_score": 0.97,
        },
    ]
    monkeypatch.setattr(context_module, "search_memory", lambda *_args, **_kwargs: rows)

    pack = build_context_pack(
        root,
        task,
        memory_limit=5,
        recent_limit=0,
        rebuild=False,
    )
    manifest = build_context_manifest(pack)

    assert [row["source_id"] for row in pack["memory_hits"]] == [
        "message:only-report"
    ]
    assert manifest["briefing"]["source_ids"] == ["message:only-report"]
    assert manifest["sources"][0]["source_class"] == "retrieval_hint"
    assert manifest["sources"][0]["source_role"] == "final_report"


def test_strong_final_report_fallback_beats_weak_recent_summary() -> None:
    task = "deployment profile inheritance rollback policy"
    manifest = build_context_manifest(
        {
            "task": task,
            "retrieval_query": task,
            "retrieval_query_state": "specific_terms",
            "session_id": "current-session",
            "retrieval_mode": "semantic-hybrid",
            "memory_hits": [
                {
                    "source_id": "message:strong-report",
                    "session_id": "report-session",
                    "event_type": "work.report.final",
                    "body_text": f"{task}: nested mappings merge and lists replace",
                    "memory_score": 0.82,
                    "retrieval_mode": "semantic-hybrid",
                }
            ],
            "recent_session_summaries": [
                {
                    "source_id": "session:recent:summary",
                    "source_kind": "session_summary",
                    "session_id": "recent",
                    "event_type": "summary.compacted",
                    "body_text": "unrelated dashboard color palette cleanup",
                }
            ],
            "evidence": [],
        }
    )

    assert manifest["briefing"]["source_ids"] == ["message:strong-report"]
    assert sum(
        source["source_role"] == "final_report"
        for source in manifest["sources"]
        if source["source_id"] in manifest["briefing"]["source_ids"]
    ) == 1


def test_briefing_does_not_backfill_weak_summaries_after_strong_decision() -> None:
    task = "deployment profile inheritance rollback policy"
    manifest = build_context_manifest(
        {
            "task": task,
            "retrieval_query": task,
            "retrieval_query_state": "specific_terms",
            "session_id": "current-session",
            "retrieval_mode": "semantic-hybrid",
            "memory_hits": [
                {
                    "source_id": "message:strong-decision",
                    "session_id": "decision-session",
                    "event_type": "decision.recorded",
                    "body_text": f"{task}: nested mappings merge and lists replace",
                    "memory_score": 0.82,
                    "retrieval_mode": "semantic-hybrid",
                }
            ],
            "recent_session_summaries": [
                {
                    "source_id": f"session:weak-{index}:summary",
                    "source_kind": "session_summary",
                    "session_id": f"weak-{index}",
                    "event_type": "summary.compacted",
                    "body_text": f"unrelated dashboard palette cleanup {index}",
                }
                for index in range(5)
            ],
            "evidence": [],
        }
    )

    assert manifest["briefing"]["source_ids"] == ["message:strong-decision"]


def test_lifecycle_only_session_summary_cannot_reenter_the_briefing() -> None:
    task = "ordered multi parent profile inheritance leaf provenance explain api"
    manifest = build_context_manifest(
        {
            "task": task,
            "retrieval_query": task,
            "retrieval_query_state": "specific_terms",
            "session_id": "current-session",
            "retrieval_mode": "semantic-hybrid",
            "memory_hits": [
                {
                    "source_id": "message:strong-decision",
                    "session_id": "decision-session",
                    "event_type": "decision.recorded",
                    "body_text": f"{task}: lists replace and mappings merge",
                    "memory_score": 0.82,
                },
                {
                    "source_id": "session:hook-only:summary",
                    "source_kind": "session_summary",
                    "session_id": "hook-only",
                    "event_type": "summary.compacted",
                    "body_text": (
                        "Session summary: hook-only\n"
                        "Events: 3\n"
                        "Event counts: git.hook.post-commit=1, session.ended=1, "
                        "session.started=1\n"
                        "Key records:\n- session.started: Git hook post-commit"
                    ),
                    "memory_score": 0.426,
                },
            ],
            "recent_session_summaries": [],
            "evidence": [],
        }
    )

    source_by_id = {source["source_id"]: source for source in manifest["sources"]}
    assert manifest["briefing"]["source_ids"] == ["message:strong-decision"]
    assert source_by_id["session:hook-only:summary"]["source_class"] == "summary"
    assert source_by_id["session:hook-only:summary"]["source_role"] == "lifecycle"


def test_session_summary_with_substantive_events_remains_eligible() -> None:
    task = "ordered multi parent profile inheritance leaf provenance explain api"
    manifest = build_context_manifest(
        {
            "task": task,
            "retrieval_query": task,
            "retrieval_query_state": "specific_terms",
            "session_id": "current-session",
            "retrieval_mode": "semantic-hybrid",
            "memory_hits": [
                {
                    "source_id": "session:substantive:summary",
                    "source_kind": "session_summary",
                    "session_id": "substantive",
                    "event_type": "summary.compacted",
                    "body_text": (
                        "Session summary: substantive\n"
                        "Events: 3\n"
                        "Event counts: agent.message=1, session.ended=1, "
                        "session.started=1\n"
                        f"Key records:\n- agent.message: {task}"
                    ),
                    "memory_score": 0.72,
                }
            ],
            "recent_session_summaries": [],
            "evidence": [],
        }
    )

    assert manifest["briefing"]["source_ids"] == ["session:substantive:summary"]
    assert manifest["sources"][0]["source_class"] == "summary"
    assert manifest["sources"][0]["source_role"] == "summary"


def test_malformed_zero_count_summary_fails_open_as_substantive() -> None:
    task = "ordered multi parent profile inheritance leaf provenance explain api"
    manifest = build_context_manifest(
        {
            "task": task,
            "retrieval_query": task,
            "retrieval_query_state": "specific_terms",
            "session_id": "current-session",
            "retrieval_mode": "semantic-hybrid",
            "memory_hits": [
                {
                    "source_id": "session:legacy-malformed:summary",
                    "source_kind": "session_summary",
                    "session_id": "legacy-malformed",
                    "event_type": "summary.compacted",
                    "body_text": (
                        "Session summary: legacy-malformed\n"
                        "Events: 2\n"
                        "Event counts: session.started=0, session.ended=0\n"
                        f"Key records:\n- agent.message: {task}"
                    ),
                    "memory_score": 0.72,
                }
            ],
            "recent_session_summaries": [],
            "evidence": [],
        }
    )

    assert manifest["briefing"]["source_ids"] == ["session:legacy-malformed:summary"]
    assert manifest["sources"][0]["source_class"] == "summary"
    assert manifest["sources"][0]["source_role"] == "summary"


def test_malformed_summary_count_structure_fails_open_as_substantive() -> None:
    task = "ordered multi parent profile inheritance leaf provenance explain api"
    oversized_count = "9" * 5000
    malformed_bodies = (
        (
            "Session summary: inconsistent-total\n"
            "Events: 3\n"
            "Event counts: session.started=1, session.ended=1\n"
            f"Key records:\n- agent.message: {task}"
        ),
        (
            "Session summary: duplicate-type\n"
            "Events: 2\n"
            "Event counts: session.started=1, session.started=1\n"
            f"Key records:\n- agent.message: {task}"
        ),
        (
            "Session summary: unicode-total\n"
            "Events: ²\n"
            "Event counts: session.started=1\n"
            f"Key records:\n- agent.message: {task}"
        ),
        (
            "Session summary: unicode-count\n"
            "Events: 1\n"
            "Event counts: session.started=²\n"
            f"Key records:\n- agent.message: {task}"
        ),
        (
            "Session summary: oversized-total\n"
            f"Events: {oversized_count}\n"
            "Event counts: session.started=1\n"
            f"Key records:\n- agent.message: {task}"
        ),
        (
            "Session summary: oversized-count\n"
            "Events: 1\n"
            f"Event counts: session.started={oversized_count}\n"
            f"Key records:\n- agent.message: {task}"
        ),
    )

    for index, body in enumerate(malformed_bodies):
        source_id = f"session:legacy-malformed-{index}:summary"
        manifest = build_context_manifest(
            {
                "task": task,
                "retrieval_query": task,
                "retrieval_query_state": "specific_terms",
                "session_id": "current-session",
                "retrieval_mode": "semantic-hybrid",
                "memory_hits": [
                    {
                        "source_id": source_id,
                        "source_kind": "session_summary",
                        "session_id": f"legacy-malformed-{index}",
                        "event_type": "summary.compacted",
                        "body_text": body,
                        "memory_score": 0.72,
                    }
                ],
                "recent_session_summaries": [],
                "evidence": [],
            }
        )

        assert manifest["briefing"]["source_ids"] == [source_id]
        assert manifest["sources"][0]["source_role"] == "summary"


def test_briefing_does_not_let_unmatched_historical_tool_evidence_displace_hints() -> None:
    task = "explainable multi base environment composition"
    manifest = build_context_manifest(
        {
            "task": task,
            "retrieval_query": task,
            "retrieval_query_state": "specific_terms",
            "session_id": "current-session",
            "retrieval_mode": "semantic-hybrid",
            "memory_hits": [
                {
                    "source_id": "message:dashboard-decision",
                    "session_id": "dashboard-session",
                    "event_type": "decision.recorded",
                    "body_text": "dashboard decision for environment composition",
                    "memory_score": 0.628,
                },
                {
                    "source_id": "message:merge-decision",
                    "session_id": "merge-session",
                    "event_type": "decision.recorded",
                    "body_text": "nested mappings merge while arrays replace",
                    "memory_score": 0.609,
                },
                {
                    "source_id": "message:pytest-call",
                    "session_id": "pytest-session",
                    "event_type": "tool.call",
                    "subject": "tool.call pytest",
                    "body_text": "tool=pytest argv=['tests/test_context.py']",
                    "memory_score": 0.699,
                },
                {
                    "source_id": "message:error-hint",
                    "session_id": "error-session",
                    "event_type": "agent.message",
                    "body_text": "environment errors include the resolved layer path",
                    "memory_score": 0.668,
                },
                {
                    "source_id": "message:loader-hint",
                    "session_id": "loader-session",
                    "event_type": "agent.message",
                    "body_text": "environment loaders resolve the ordered inputs",
                    "memory_score": 0.646,
                },
                {
                    "source_id": "message:cache-hint",
                    "session_id": "cache-session",
                    "event_type": "agent.message",
                    "body_text": "environment cache keys include every resolved input",
                    "memory_score": 0.637,
                },
            ],
            "recent_session_summaries": [],
            "evidence": [],
        }
    )

    assert manifest["briefing"]["source_ids"] == [
        "message:dashboard-decision",
        "message:merge-decision",
        "message:error-hint",
        "message:loader-hint",
        "message:cache-hint",
    ]


def test_context_pack_retains_unmatched_tool_evidence_outside_the_briefing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    run_cli("index", "update", "--root", str(root))
    task = "explainable multi base environment composition"
    rows = [
        {
            "source_id": "message:pytest-call",
            "session_id": "pytest-session",
            "event_type": "tool.call",
            "subject": "tool.call pytest",
            "body_text": "tool=pytest argv=['tests/test_context.py']",
            "memory_score": 0.699,
        },
        {
            "source_id": "message:cache-hint",
            "session_id": "cache-session",
            "event_type": "agent.message",
            "body_text": "environment cache keys include every resolved input",
            "memory_score": 0.637,
        },
    ]
    monkeypatch.setattr(context_module, "search_memory", lambda *_args, **_kwargs: rows)

    pack = build_context_pack(
        root,
        task,
        memory_limit=2,
        evidence_limit=0,
        recent_limit=0,
        rebuild=False,
    )
    manifest = build_context_manifest(pack)

    assert {source["source_id"] for source in manifest["sources"]} == {
        "message:pytest-call",
        "message:cache-hint",
    }
    assert manifest["briefing"]["source_ids"] == ["message:cache-hint"]


def test_context_pack_fills_candidate_budget_before_historical_tool_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    run_cli("index", "update", "--root", str(root))
    task = "explainable multi base environment composition"
    rows = [
        {
            "source_id": "message:dashboard-decision",
            "session_id": "dashboard-session",
            "event_type": "decision.recorded",
            "body_text": "dashboard decision for environment composition",
            "memory_score": 0.628,
        },
        {
            "source_id": "message:merge-decision",
            "session_id": "merge-session",
            "event_type": "decision.recorded",
            "body_text": "nested mappings merge while arrays replace",
            "memory_score": 0.609,
        },
        {
            "source_id": "message:pytest-call",
            "session_id": "pytest-session",
            "event_type": "tool.call",
            "body_text": "tool=pytest argv=['tests/test_context.py']",
            "memory_score": 0.699,
        },
        *[
            {
                "source_id": f"message:{name}-hint",
                "session_id": f"{name}-session",
                "event_type": "agent.message",
                "body_text": f"environment {name} preserves every resolved input",
                "memory_score": score,
            }
            for name, score in (("error", 0.668), ("loader", 0.646), ("cache", 0.637))
        ],
    ]
    monkeypatch.setattr(context_module, "search_memory", lambda *_args, **_kwargs: rows)

    pack = build_context_pack(
        root,
        task,
        memory_limit=5,
        evidence_limit=0,
        recent_limit=0,
        rebuild=False,
    )
    manifest = build_context_manifest(pack)
    expected = [
        "message:dashboard-decision",
        "message:merge-decision",
        "message:error-hint",
        "message:loader-hint",
        "message:cache-hint",
    ]

    assert [row["source_id"] for row in pack["memory_hits"]] == expected
    assert manifest["briefing"]["source_ids"] == expected


def test_briefing_keeps_current_and_strong_paraphrased_tool_evidence_privileged() -> None:
    task = "explainable multi base environment composition"
    manifest = build_context_manifest(
        {
            "task": task,
            "retrieval_query": task,
            "retrieval_query_state": "specific_terms",
            "session_id": "current-session",
            "retrieval_mode": "semantic-hybrid",
            "memory_hits": [
                {
                    "source_id": "message:paraphrased-result",
                    "session_id": "prior-session",
                    "event_type": "tool.result",
                    "body_text": "resolved layers retain provenance across inherited inputs",
                    "memory_score": 0.75,
                },
                {
                    "source_id": "message:high-score-hint",
                    "session_id": "hint-session",
                    "event_type": "agent.message",
                    "body_text": f"{task} cache hint",
                    "memory_score": 0.95,
                },
            ],
            "recent_session_summaries": [],
            "evidence": [
                {
                    "source_id": "message:current-pytest",
                    "session_id": "current-session",
                    "event_type": "tool.call",
                    "body_text": "tool=pytest argv=['tests/test_context.py']",
                }
            ],
        }
    )

    assert manifest["briefing"]["source_ids"] == [
        "message:current-pytest",
        "message:paraphrased-result",
        "message:high-score-hint",
    ]
    assert manifest["briefing"]["quality_policy"]["historical_tool_evidence"] == {
        "evidence_tier_requires": "specific_task_overlap_or_strong_match",
        "unmatched_manifest_tier": "historical_tool_fallback",
        "unmatched_briefing_eligibility": "omitted",
        "current_session_tier": "current_evidence",
    }


def test_briefing_caps_weak_only_exploration_with_deterministic_diversity() -> None:
    task = "deployment profile inheritance rollback policy"
    pack = {
        "task": task,
        "retrieval_query": task,
        "retrieval_query_state": "specific_terms",
        "session_id": "current-session",
        "retrieval_mode": "document",
        "memory_hits": [
            {
                "source_id": f"message:weak-{index}",
                "session_id": f"memory-{index}",
                "event_type": "agent.message",
                "body_text": f"unrelated dashboard palette cleanup {index}",
                "memory_score": 0.2 - index / 100,
            }
            for index in range(4)
        ],
        "recent_session_summaries": [
            {
                "source_id": f"session:summary-{index}:summary",
                "source_kind": "session_summary",
                "session_id": f"summary-{index}",
                "event_type": "summary.compacted",
                "body_text": f"unrelated widget color cleanup {index}",
            }
            for index in range(3)
        ],
        "evidence": [],
    }

    first = build_context_manifest(pack)
    second = build_context_manifest(pack)

    assert first["briefing"]["source_ids"] == [
        "message:weak-0",
        "session:summary-0:summary",
    ]
    assert second["briefing"]["source_ids"] == first["briefing"]["source_ids"]


def test_context_pack_retains_only_explicitly_targeted_work_lifecycle_event(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    run_cli("index", "update", "--root", str(root))
    rows = [
        {
            "source_id": "message:work-started",
            "session_id": "start-session",
            "event_type": "work.started",
            "body_text": "work start context recovery",
            "memory_score": 0.91,
        },
        {
            "source_id": "message:work-finished",
            "session_id": "finish-session",
            "event_type": "work.finished",
            "body_text": "work finish context recovery",
            "memory_score": 0.90,
        },
    ]
    monkeypatch.setattr(context_module, "search_memory", lambda *_args, **_kwargs: rows)

    start_pack = build_context_pack(
        root,
        "diagnose work start context recovery",
        memory_limit=5,
        recent_limit=0,
        rebuild=False,
    )
    finish_pack = build_context_pack(
        root,
        "diagnose work finish context recovery",
        memory_limit=5,
        recent_limit=0,
        rebuild=False,
    )

    assert [row["event_type"] for row in start_pack["memory_hits"]] == ["work.started"]
    assert [row["event_type"] for row in finish_pack["memory_hits"]] == [
        "work.finished"
    ]
    assert build_context_manifest(start_pack)["briefing"]["source_ids"] == [
        "message:work-started"
    ]
    assert build_context_manifest(finish_pack)["briefing"]["source_ids"] == [
        "message:work-finished"
    ]


def test_context_pack_does_not_promote_ordinary_start_or_finish_tasks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    run_cli("index", "update", "--root", str(root))
    rows = [
        {
            "source_id": "message:work-started",
            "session_id": "start-session",
            "event_type": "work.started",
            "body_text": "start server parser",
            "memory_score": 0.91,
        },
        {
            "source_id": "message:work-finished",
            "session_id": "finish-session",
            "event_type": "work.finished",
            "body_text": "finish parser implementation",
            "memory_score": 0.90,
        },
    ]
    monkeypatch.setattr(context_module, "search_memory", lambda *_args, **_kwargs: rows)

    start_pack = build_context_pack(
        root,
        "start server parser",
        memory_limit=5,
        recent_limit=0,
        rebuild=False,
    )
    finish_pack = build_context_pack(
        root,
        "finish parser implementation",
        memory_limit=5,
        recent_limit=0,
        rebuild=False,
    )
    start_work_pack = build_context_pack(
        root,
        "start work on parser implementation",
        memory_limit=5,
        recent_limit=0,
        rebuild=False,
    )
    finish_work_pack = build_context_pack(
        root,
        "finish work on parser implementation",
        memory_limit=5,
        recent_limit=0,
        rebuild=False,
    )

    assert start_pack["memory_hits"] == []
    assert finish_pack["memory_hits"] == []
    assert start_work_pack["memory_hits"] == []
    assert finish_work_pack["memory_hits"] == []


def test_context_pack_retains_operational_source_when_task_targets_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    run_cli("index", "update", "--root", str(root))
    task = "diagnose pre-commit hook capture"
    rows = [
        {
            "source_id": "message:hook",
            "session_id": "hook-session",
            "event_type": "git.hook.pre-commit",
            "subject": "pre-commit hook capture",
            "body_text": "hook capture failed before commit",
            "memory_score": 0.91,
        }
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
        "git.hook.pre-commit"
    ]
    manifest = build_context_manifest(pack)
    assert manifest["briefing"]["source_ids"] == ["message:hook"]
    assert manifest["sources"][0]["source_class"] == "evidence"
    assert manifest["sources"][0]["source_role"] == "operational"
    assert manifest["briefing"]["quality_policy"][
        "low_signal_omitted_unless_task_targeted"
    ] == ["lifecycle", "operational"]
    assert manifest["briefing"]["quality_policy"]["final_report_fallback"] == (
        "only_when_no_higher_signal_source_survives"
    )


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
