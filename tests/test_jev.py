from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agentdir import context, jev
from agentdir.context_repository import read_context_manifest
from agentdir.control import format_work_start, show_work_context, start_work
from agentdir.events import emit_event
from agentdir.index import rebuild_index
from agentdir.memory import configure_context_reranker
from agentdir.store import init_root


def response(scores):
    return {"model": jev.MODEL,
            "answers": {f"p{i}": {"type": "noul", "noul": score} for i, score in enumerate(scores)},
            "usage": {"input_tokens": 42, "output_tokens": 8}}


def hits(count=3):
    return [
        {"source_id": f"message:{i}", "session_id": f"prior-{i}",
         "source_kind": "message", "event_type": "decision.recorded",
         "subject": "rollback migration", "body_text": "Use a transaction.",
         "memory_score": 0.9 - i / 100}
        for i in range(count)
    ]


def test_opt_in_and_export_boundaries(tmp_path, monkeypatch):
    init_root(tmp_path)
    rows = hits()
    monkeypatch.setenv("JEV_API_KEY", "synthetic-api-key")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(jev.subprocess, "run", lambda *a, **k: pytest.fail("unexpected export"))
    assert jev.filter_context_hits(tmp_path, "migration", rows)[1]["status"] == "disabled"
    configure_context_reranker(tmp_path, "jev")
    assert jev.filter_context_hits(tmp_path, "migration", rows, federated=True)[1]["reason"] == "federated_context"
    foreign = [{**rows[0], "source_root_path": "/another/store"}]
    assert jev.filter_context_hits(tmp_path, "migration", foreign)[1]["reason"] == "federated_context"
    assert jev.filter_context_hits(tmp_path, "migration", [])[1]["reason"] == "no_candidates"
    monkeypatch.delenv("JEV_API_KEY")
    returned, record = jev.filter_context_hits(tmp_path, "migration", rows)
    assert returned is rows and record["reason"] == "missing_api_key"
    configure_context_reranker(tmp_path, "none")
    assert jev.filter_context_hits(tmp_path, "migration", rows)[1]["status"] == "disabled"


def test_bounded_redacted_export_and_scores_survive_both_selection_stages(tmp_path, monkeypatch):
    init_root(tmp_path)
    configure_context_reranker(tmp_path, "jev")
    monkeypatch.setenv("JEV_API_KEY", "synthetic-api-key")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    rows = hits(25)
    rows[0]["body_text"] = "password=abcdefghijklmnop " + "é" * 2000
    rows[1]["body_text"] = "synthetic-api-key"
    rows[2]["body_text"] = "-----BEGIN PRIVATE KEY-----\nprivate material"
    original = deepcopy(rows)
    captured = {}

    def run(command, **kwargs):
        captured.update(json.loads(kwargs["input"]))
        assert command == [sys.executable, "-m", "agentdir.jev"]
        assert kwargs["timeout"] == 3
        return SimpleNamespace(stdout=json.dumps(response([0.7, 0.99] + [0.1] * 18)))

    monkeypatch.setattr(jev.subprocess, "run", run)
    selected, record = jev.filter_context_hits(tmp_path, "migration token=abcdefghijklmnop", rows)
    assert len(captured["questions"]) == 20
    assert all(len(text.encode()) <= 1024 for text in captured["state"]["candidates"].values())
    exported = json.dumps(captured)
    assert all(secret not in exported for secret in ("abcdefghijklmnop", "synthetic-api-key", "private material", "prior-0"))
    assert rows == original
    assert record["status"] == "applied" and len(record["candidates"]) == 20
    assert len(record["request_sha256"]) == 64
    selected = context._diversify_memory_hits(selected, 8, retrieval_query="migration")
    manifest = context.build_context_manifest({"task": "migration", "memory_hits": selected, "reranking": record})
    assert manifest["briefing"]["source_ids"] == ["message:1", "message:0"]
    assert manifest["sources"][0]["memory_score"] == original[1]["memory_score"]
    assert manifest["sources"][0]["rerank_score"] == 0.99
    assert "advisory" in manifest["sources"][0]["match_reasons"][-1]


@pytest.mark.parametrize("bad", [
    None, {}, {**response([0.9]), "model": "jev-latest"}, response([True]),
    response([float("nan")]), response([float("inf")]), response([1.01]),
    response([-0.1]), response([10 ** 1000]), response(["0.9"]), response([0.9, 0.8]),
    {**response([0.9]), "answers": {"p0": {"type": "score", "noul": 0.9}}},
    {**response([0.9]), "usage": {"input_tokens": -1, "output_tokens": 8}},
    {**response([0.9]), "usage": {"input_tokens": True, "output_tokens": 8}},
    {"error": "secret server body"}, {"error": "rate_limited"}, {"error": {"message": "secret server body"}},
])
def test_invalid_or_failed_response_preserves_local_candidates(tmp_path, monkeypatch, bad):
    init_root(tmp_path)
    configure_context_reranker(tmp_path, "jev")
    monkeypatch.setenv("JEV_API_KEY", "synthetic-api-key")
    monkeypatch.setattr(jev.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(bad)))
    rows = hits(1)
    returned, record = jev.filter_context_hits(tmp_path, "migration", rows)
    assert returned is rows and record["status"] == "fallback"
    assert "secret server body" not in json.dumps(record)
    assert record["reason"] in {"invalid_response", "rate_limited"}


def test_deadline_terminates_stalled_worker(tmp_path, monkeypatch):
    init_root(tmp_path)
    configure_context_reranker(tmp_path, "jev")
    monkeypatch.setenv("JEV_API_KEY", "synthetic-api-key")
    real_run = subprocess.run
    monkeypatch.setattr(jev, "TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(jev.subprocess, "run", lambda command, **kwargs: real_run(
        [sys.executable, "-c", "import time; time.sleep(30)"], **kwargs,
    ))
    rows = hits()
    returned, record = jev.filter_context_hits(tmp_path, "migration", rows)
    assert returned is rows and record["reason"] == "timeout"
    assert record["latency_ms"] < 2000


@pytest.mark.parametrize("status,body,reason", [
    (302, b"", "http_error"), (429, b"", "rate_limited"),
    (503, b"", "http_error"), (200, b"bad json", "invalid_response"),
    (200, b"x" * (jev.MAX_RESPONSE_BYTES + 1), "invalid_response"),
])
def test_http_errors_do_not_follow_redirects_or_log_bodies(monkeypatch, status, body, reason):
    calls = []
    connection = SimpleNamespace(
        request=lambda *a, **k: calls.append(a),
        getresponse=lambda: SimpleNamespace(status=status, read=lambda limit: body[:limit]),
        close=lambda: calls.append("closed"),
    )
    monkeypatch.setattr(jev.http.client, "HTTPSConnection", lambda host, **k: connection)
    assert jev._request(b"{}") == {"error": reason}
    assert len(calls) == 2 and calls[-1] == "closed"


def test_work_start_persists_scores_and_review_replays_without_api(tmp_path, monkeypatch):
    init_root(tmp_path)
    for i in range(2):
        emit_event(tmp_path, session_id=f"prior-{i}", event_type="decision.recorded",
                   subject="rollback migration", body="Use a transaction for rollback migration.")
    configure_context_reranker(tmp_path, "jev")
    monkeypatch.setenv("JEV_API_KEY", "synthetic-api-key")

    def score(command, **kwargs):
        payload = json.loads(kwargs["input"])
        return SimpleNamespace(stdout=json.dumps(response([0.9] * len(payload["questions"]))))

    # Patch only the worker, leaving git and session subprocesses operational.
    real_run = subprocess.run
    monkeypatch.setattr(jev.subprocess, "run", lambda command, **kwargs:
                        score(command, **kwargs) if command[:3] == [sys.executable, "-m", "agentdir.jev"]
                        else real_run(command, **kwargs))
    started = start_work(tmp_path, "rollback migration")
    manifest = started["context_pack"]
    assert manifest["reranking"]["status"] == "applied"
    assert manifest["recent_summaries"] == []
    assert manifest["briefing"]["review_required"]
    assert "context_reranker=jev status=applied" in format_work_start(started)
    monkeypatch.setattr(context, "filter_context_hits", lambda *a, **k: pytest.fail("replay called Jev"))
    rebuild_index(tmp_path)
    stored = read_context_manifest(tmp_path, manifest["pack_id"])
    assert stored["reranking"] == manifest["reranking"]
    shown = show_work_context(tmp_path)
    assert shown["context_pack"]["reranking"] == manifest["reranking"]
    assert shown["context_audit"]["review_status"] == "pending"


def test_empty_filter_cannot_backfill_recent_summaries_and_keeps_current_evidence(tmp_path, monkeypatch):
    init_root(tmp_path)
    configure_context_reranker(tmp_path, "jev")
    monkeypatch.setenv("JEV_API_KEY", "synthetic-api-key")
    monkeypatch.setattr(context, "search_memory", lambda *a, **k: hits())
    monkeypatch.setattr(context, "recent_session_summaries", lambda *a, **k: [
        {"source_id": "summary:prior", "subject": "rollback migration", "body_text": "rollback migration"},
    ])
    evidence = [{"source_id": "current:tool", "event_type": "tool.result", "body_text": "exit=0"}]
    monkeypatch.setattr(context, "evidence_rows", lambda *a, **k: evidence)
    monkeypatch.setattr(context, "summarize_session", lambda *a, **k: {})
    monkeypatch.setattr(jev.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(response([0.1] * 3))))
    pack = context.build_context_pack(tmp_path, "rollback migration", session_id="current")
    assert pack["memory_hits"] == [] and pack["recent_session_summaries"] == []
    manifest = context.build_context_manifest(pack)
    assert manifest["briefing"]["source_ids"] == ["current:tool"]
    assert manifest["sources"][0]["match_quality"] == "current"


def test_cli_configuration_and_visible_missing_key_fallback(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    for name in ("TYPESAFE_API_KEY", "JEV_API_KEY"):
        env.pop(name, None)

    def cli(*args):
        return subprocess.run([sys.executable, "-m", "agentdir", *args], env=env,
                              text=True, capture_output=True, check=True).stdout

    cli("init", str(tmp_path))
    configured = cli("memory", "reranker", "configure", "jev", "--root", str(tmp_path))
    assert "api.typesafe.ai" in configured
    emit_event(tmp_path, session_id="prior", event_type="decision.recorded",
               subject="rollback migration", body="Use a transaction for rollback migration.")
    built = json.loads(cli("context", "build", "rollback migration", "--root", str(tmp_path), "--json"))
    assert built["reranking"]["reason"] == "missing_api_key"
    assert built["memory_hits"]
    assert "fallback missing_api_key" in cli("context", "build", "rollback migration", "--root", str(tmp_path))
    assert json.loads(cli("memory", "reranker", "configure", "none", "--root", str(tmp_path), "--json")) == {"provider": "none"}
