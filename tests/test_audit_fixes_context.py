from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import agentdir.context as context_module
import agentdir.context_expansion as expansion_module
from agentdir.artifacts import artifact_path
from agentdir.context import (
    CONSUMPTION_PURPOSES,
    audit_context_pack,
    brief_context_manifest,
    build_context_manifest,
    build_context_pack,
    cite_context_sources,
    consume_context_sources,
    emit_context_pack,
    read_context_manifest,
    review_context_pack,
)
from agentdir.context_expansion import (
    _candidate_envelopes,
    _contained_file,
    _matches_source_metadata,
    _resolve_envelope_candidates,
    _resolve_envelope_source,
    _resolve_session_summary,
    _session_expansion_events,
    audit_context_expansion,
    expand_context_source,
)
from agentdir.context_repository import (
    CONTEXT_BRIEFING_PROTOCOL,
    CONTEXT_PROTOCOL,
    EVENT_CONTEXT_PACK_CONSUMED,
    EVENT_CONTEXT_PACK_CREATED,
    EVENT_CONTEXT_SOURCES_EXPANDED,
    HEADER_CONSUMPTION_PURPOSE,
    HEADER_CONTEXT_DECISION_ID,
    HEADER_CONTEXT_DECISION_REVISION,
    HEADER_CONTEXT_DISPOSITION,
    HEADER_PACK_ID,
    HEADER_PROTOCOL,
    HEADER_REVIEWED_SOURCE_ID,
    HEADER_SOURCE_ID,
    HEADER_USED_SOURCE_ID,
    context_events,
)
from agentdir.context_review import (
    context_decision_id,
    context_decision_validation_errors,
    fold_context_review,
    format_context_review_body,
)
from agentdir.envelope import parse_envelope
from agentdir.events import emit_event
from agentdir.index import connect_index, rebuild_index, update_index
from agentdir.memory import configure_context_reranker
from agentdir.retention import archive_sessions
from agentdir.sessions import end_session, start_session
from agentdir.store import AgentDirError, AgentDirStateError, init_root, paths_for


def _write_manifest_pack(
    root: Path,
    *,
    session_id: str,
    manifest: dict[str, object],
    extra_headers: dict[str, str | list[str]] | None = None,
) -> Path:
    manifest_file = root.parent / f"{manifest['pack_id']}.json"
    manifest_file.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    pack_id = str(manifest["pack_id"])
    headers: dict[str, str | list[str]] = {
        HEADER_PROTOCOL: CONTEXT_PROTOCOL,
        HEADER_PACK_ID: pack_id,
    }
    if extra_headers:
        headers.update(extra_headers)
    event = emit_event(
        root,
        session_id=session_id,
        event_type=EVENT_CONTEXT_PACK_CREATED,
        subject=f"context pack: {manifest.get('task', '')}",
        body=f"pack_id={pack_id}\nprotocol={CONTEXT_PROTOCOL}",
        artifact=manifest_file,
        extra_headers=headers,
    )
    return event.path


def _emit_two_source_pack(root: Path, session_id: str = "work-session") -> tuple[str, dict[str, object]]:
    init_root(root)
    start_session(root, session_id="prior-1")
    emit_event(
        root,
        session_id="prior-1",
        event_type="decision.recorded",
        subject="alpha migration policy",
        body="alpha migration policy details for checkout",
    )
    end_session(root, expected_session_id="prior-1")
    start_session(root, session_id="prior-2")
    emit_event(
        root,
        session_id="prior-2",
        event_type="decision.recorded",
        subject="beta migration policy",
        body="beta migration policy details for checkout",
    )
    end_session(root, expected_session_id="prior-2")
    start_session(root, session_id=session_id)
    pack = build_context_pack(
        root,
        "alpha beta migration policy details for checkout",
        session_id=session_id,
        exclude_session_from_memory=True,
    )
    emitted = emit_context_pack(root, pack)
    return emitted.manifest["pack_id"], emitted.manifest


def test_zero_presented_briefing_with_retrieved_sources_reads_cleanly(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    start_session(root, session_id="zero-briefing")
    pack = {
        "task": "explainable multi base environment composition",
        "retrieval_query": "explainable multi base environment composition",
        "retrieval_query_state": "specific_terms",
        "session_id": "zero-briefing",
        "retrieval_mode": "hybrid",
        "memory_hits": [
            {
                "source_id": "message:unmatched-tool",
                "session_id": "older-session",
                "event_type": "tool.call",
                "subject": "tool.call pytest",
                "body_text": "tool=pytest argv=['tests/test_other.py']",
                "memory_score": 0.65,
            }
        ],
        "recent_session_summaries": [],
        "evidence": [],
    }
    emitted = emit_context_pack(root, pack)
    assert len(emitted.manifest["sources"]) == 1
    assert emitted.manifest["briefing"]["source_ids"] == []
    assert emitted.manifest["briefing"]["presented_count"] == 0
    assert emitted.manifest["briefing"]["omitted_count"] == 1
    assert emitted.manifest["briefing"]["review_required"] is False

    loaded = read_context_manifest(root, emitted.manifest["pack_id"])
    audit = audit_context_pack(root, emitted.manifest["pack_id"], rebuild=False)
    assert loaded["pack_id"] == emitted.manifest["pack_id"]
    assert audit["review_status"] == "not_applicable"
    assert audit["finish_allowed"] is True
    assert audit["lineage_valid"] is True

    bad_manifest = {
        "protocol": CONTEXT_PROTOCOL,
        "pack_id": "ctx-unknown-in-empty",
        "task": "bad empty sources",
        "session_id": "zero-briefing",
        "sources": [],
        "briefing": {
            "protocol": CONTEXT_BRIEFING_PROTOCOL,
            "source_ids": ["message:ghost"],
            "presented_count": 1,
            "omitted_count": 0,
            "review_required": True,
        },
    }
    _write_manifest_pack(root, session_id="zero-briefing", manifest=bad_manifest)
    try:
        read_context_manifest(root, "ctx-unknown-in-empty")
    except AgentDirError as exc:
        assert "unknown sources" in str(exc)
    else:
        raise AssertionError("expected unknown briefing source to be rejected")


def test_read_context_manifest_rejects_duplicate_invalid_and_symlink_blob_headers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    init_root(root)
    start_session(root, session_id="blob-hardening")

    valid_manifest = {
        "protocol": CONTEXT_PROTOCOL,
        "pack_id": "ctx-dup-blob",
        "task": "blob hardening",
        "session_id": "blob-hardening",
        "sources": [],
        "briefing": {
            "protocol": CONTEXT_BRIEFING_PROTOCOL,
            "source_ids": [],
            "presented_count": 0,
            "omitted_count": 0,
            "review_required": False,
        },
    }
    event_path = _write_manifest_pack(root, session_id="blob-hardening", manifest=valid_manifest)
    raw = event_path.read_text(encoding="utf-8")
    sha_line = next(line for line in raw.splitlines() if line.lower().startswith("x-agentdir-blob-sha256:"))
    sha = sha_line.split(":", 1)[1].strip()
    event_path.write_text(raw.replace(sha_line, f"{sha_line}\nx-agentdir-blob-sha256: {sha}", 1), encoding="utf-8")
    rebuild_index(root)
    try:
        read_context_manifest(root, "ctx-dup-blob", rebuild=False)
    except AgentDirError as exc:
        assert "multiple manifest artifact headers" in str(exc)
    else:
        raise AssertionError("expected duplicate X-AgentDir-Blob-SHA256 to be rejected")

    for suffix, bad_sha in (
        ("traversal", "../../config.json"),
        ("uppercase", "A" * 64),
        ("short", "deadbeef"),
    ):
        pack_id = f"ctx-bad-{suffix}"
        emit_event(
            root,
            session_id="blob-hardening",
            event_type=EVENT_CONTEXT_PACK_CREATED,
            subject="bad sha",
            body=f"pack_id={pack_id}",
            extra_headers={
                HEADER_PROTOCOL: CONTEXT_PROTOCOL,
                HEADER_PACK_ID: pack_id,
                "X-AgentDir-Blob-SHA256": bad_sha,
            },
        )
        try:
            read_context_manifest(root, pack_id)
        except AgentDirError as exc:
            assert "digest is invalid" in str(exc)
        else:
            raise AssertionError(f"expected invalid digest {bad_sha!r} to be rejected")

    symlink_manifest = {**valid_manifest, "pack_id": "ctx-symlink-blob"}
    _write_manifest_pack(root, session_id="blob-hardening", manifest=symlink_manifest)
    symlink_bytes = (tmp_path / "ctx-symlink-blob.json").read_bytes()
    symlink_sha = hashlib.sha256(symlink_bytes).hexdigest()
    blob_file = artifact_path(root, symlink_sha)
    outside = tmp_path / "outside-manifest.json"
    blob_file.rename(outside)
    blob_file.symlink_to(outside)
    try:
        read_context_manifest(root, "ctx-symlink-blob")
    except AgentDirError as exc:
        assert "symlink" in str(exc)
    else:
        raise AssertionError("expected symlinked manifest blob to be rejected")

    dir_manifest = {**valid_manifest, "pack_id": "ctx-dir-blob"}
    _write_manifest_pack(root, session_id="blob-hardening", manifest=dir_manifest)
    dir_sha = hashlib.sha256((tmp_path / "ctx-dir-blob.json").read_bytes()).hexdigest()
    dir_blob = artifact_path(root, dir_sha)
    dir_blob.unlink()
    dir_blob.mkdir()
    try:
        read_context_manifest(root, "ctx-dir-blob")
    except AgentDirError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("expected directory manifest blob to be rejected")

    shard_manifest = {**valid_manifest, "pack_id": "ctx-shard-symlink"}
    _write_manifest_pack(root, session_id="blob-hardening", manifest=shard_manifest)
    shard_sha = hashlib.sha256((tmp_path / "ctx-shard-symlink.json").read_bytes()).hexdigest()
    shard_blob = artifact_path(root, shard_sha)
    outside_dir = tmp_path / "outside-shard"
    outside_dir.mkdir()
    shard_blob.rename(outside_dir / shard_sha)
    shard_parent = shard_blob.parent
    shard_parent.rmdir()
    shard_parent.symlink_to(outside_dir)
    try:
        read_context_manifest(root, "ctx-shard-symlink")
    except AgentDirError as exc:
        assert "symlink" in str(exc) or "outside" in str(exc)
    else:
        raise AssertionError("expected symlinked shard parent directory to be rejected")


def test_read_context_manifest_rejects_null_briefing_instead_of_legacy_downgrade(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    init_root(root)
    start_session(root, session_id="null-briefing")
    manifest = {
        "protocol": CONTEXT_PROTOCOL,
        "pack_id": "ctx-null-briefing",
        "task": "null briefing check",
        "session_id": "null-briefing",
        "sources": [{"source_id": "message:s1"}],
        "briefing": None,
    }
    _write_manifest_pack(root, session_id="null-briefing", manifest=manifest)
    try:
        read_context_manifest(root, "ctx-null-briefing")
    except AgentDirError as exc:
        assert "must be an object" in str(exc)
    else:
        raise AssertionError("expected 'briefing': null to fail validation")


def test_context_events_handles_mixed_case_headers_null_event_type_and_archived_duplicate_creation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    pack_id, manifest = _emit_two_source_pack(root, session_id="live-session")

    null_type_event = emit_event(
        root,
        session_id="live-session",
        event_type="agent.message",
        subject="tampered null event type",
        body=f"pack_id={pack_id}\ndisposition=no_relevant\nreason=tampered",
        extra_headers={
            "x-agentdir-pack-id": pack_id,
            "x-agentdir-context-disposition": "no_relevant",
        },
    )
    raw = null_type_event.path.read_text(encoding="utf-8")
    null_type_event.path.write_text(
        raw.replace("X-AgentDir-Event-Type: agent.message", "X-AgentDir-Event-Typo: agent.message"),
        encoding="utf-8",
    )
    rebuild_index(root)

    events = context_events(root, pack_id, rebuild=False, session_id="live-session")
    null_rows = [row for row in events if row.get("event_type") is None]
    assert len(null_rows) == 1
    assert null_rows[0]["pack_ids"] == [pack_id]
    assert null_rows[0]["header_values"][HEADER_CONTEXT_DISPOSITION] == ["no_relevant"]

    audit = audit_context_pack(root, pack_id, rebuild=False)
    assert audit["review_status"] == "conflict"
    assert audit["finish_allowed"] is False

    start_session(root, session_id="other-archived")
    _write_manifest_pack(root, session_id="other-archived", manifest={**manifest, "session_id": "other-archived"})
    end_session(root, expected_session_id="other-archived")
    archive_sessions(root, sessions=["other-archived"], apply=True)

    try:
        read_context_manifest(root, pack_id)
    except AgentDirError as exc:
        assert "multiple creation events" in str(exc)
    else:
        raise AssertionError("expected duplicate creation event in archived session to be detected")


def test_candidate_envelopes_pass_one_does_not_scan_session_or_bypass_id_validation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    init_root(root)
    start_session(root, session_id="scan-test")
    ev = emit_event(
        root,
        session_id="scan-test",
        event_type="agent.message",
        subject="direct lookup",
        body="direct lookup body",
    )
    rel = str(ev.path.relative_to(paths_for(root).root))

    unsafe_source = {"session_id": "../../outside", "file_path": ""}
    with patch.object(expansion_module, "iter_records", side_effect=AssertionError("unexpected scan")):
        assert _candidate_envelopes(root, unsafe_source, include_session_scan=False) == []

    direct_source = {"session_id": "scan-test", "file_path": rel}
    with patch.object(expansion_module, "iter_records", side_effect=AssertionError("unexpected Pass 1 scan")):
        found = _candidate_envelopes(root, direct_source, include_session_scan=False)
        assert len(found) == 1 and found[0] == ev.path.resolve()

    with patch.object(expansion_module, "iter_records", side_effect=OSError("unreadable")):
        assert _candidate_envelopes(root, {"session_id": "scan-test"}, include_session_scan=True) == []


def test_envelope_expansion_containment_and_metadata_guards(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    paths = paths_for(root)
    start_session(root, session_id="containment")
    valid_event = emit_event(
        root,
        session_id="containment",
        event_type="agent.message",
        subject="canonical subject",
        body="canonical body",
    )
    parsed_valid = parse_envelope(valid_event.path)
    valid_rel = str(valid_event.path.relative_to(paths.root))

    tmp_copy = paths.sessions / "containment" / "Maildir" / "tmp" / "draft.eml"
    tmp_copy.write_bytes(valid_event.path.read_bytes())
    dot_copy = paths.sessions / "containment" / "Maildir" / "new" / ".hidden.eml"
    dot_copy.write_bytes(valid_event.path.read_bytes())
    internal_link = paths.sessions / "containment" / "Maildir" / "new" / "internal-link.eml"
    internal_link.symlink_to(valid_event.path)

    for bad_path in (
        paths.root / "config.json",
        paths.state / "current-session.json",
        tmp_copy,
        dot_copy,
        internal_link,
    ):
        assert _contained_file(paths.root, bad_path) is None, f"should reject {bad_path}"

    assert _contained_file(paths.root, valid_event.path) == valid_event.path.resolve()

    queue_msg = paths.queues / "q1" / "Maildir" / "new" / "queued.eml"
    queue_msg.parent.mkdir(parents=True, exist_ok=True)
    queue_msg.write_bytes(valid_event.path.read_bytes())
    assert _contained_file(paths.root, queue_msg) == queue_msg.resolve()

    actor_msg = paths.actors / "alice" / "inbox" / "Maildir" / "cur" / "delivered.eml"
    actor_msg.parent.mkdir(parents=True, exist_ok=True)
    actor_msg.write_bytes(valid_event.path.read_bytes())
    assert _contained_file(paths.root, actor_msg) == actor_msg.resolve()

    config_source = {
        "source_id": "message:config.json",
        "file_path": "config.json",
        "event_type": "agent.message",
        "message_id": parsed_valid.message_id,
    }
    res = _resolve_envelope_source(paths.root, config_source)
    assert res["integrity"] == "unavailable"
    assert res["basis"] == "manifest_excerpt"

    missing_required = paths.sessions / "containment" / "Maildir" / "new" / "non-envelope.eml"
    missing_required.write_text("X-AgentDir-Event-Type: agent.message\n\njust text\n", encoding="utf-8")
    resolved, changed = _resolve_envelope_candidates(
        [missing_required],
        {"event_type": "agent.message", "file_path": valid_rel},
    )
    assert resolved is None and changed is False

    assert _matches_source_metadata(parsed_valid, {}) is False
    assert _matches_source_metadata(parsed_valid, {"message_id": "", "event_type": ""}) is False
    assert _matches_source_metadata(parsed_valid, {"event_type": "agent.message"}) is True


def test_derived_summary_expansion_excludes_corrupted_receipt_bodies_by_header(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    init_root(root)
    start_session(root, session_id="summary-session")
    emit_event(
        root,
        session_id="summary-session",
        event_type="agent.message",
        subject="real signal",
        body="important historical finding for derived summary",
    )
    corrupted = emit_event(
        root,
        session_id="summary-session",
        event_type=EVENT_CONTEXT_SOURCES_EXPANDED,
        subject="corrupted receipt",
        body="corrupted receipt body sentinel that must stay excluded",
        extra_headers={"X-AgentDir-Context-View-Pack-Id": "ctx-unrelated"},
    )
    raw = corrupted.path.read_text(encoding="utf-8")
    corrupted.path.write_text(
        raw.replace(
            f"X-AgentDir-Event-Type: {EVENT_CONTEXT_SOURCES_EXPANDED}",
            f"X-AgentDir-Event-Typo: {EVENT_CONTEXT_SOURCES_EXPANDED}",
        ),
        encoding="utf-8",
    )
    end_session(root, expected_session_id="summary-session")
    rebuild_index(root)

    with connect_index(root) as conn:
        row = conn.execute(
            "select text_sha256, body_text from memory_documents "
            "where source_kind = 'session_summary' and session_id = ?",
            ("summary-session",),
        ).fetchone()
    assert row is not None
    assert "corrupted receipt body sentinel" not in row["body_text"]

    resolved = _resolve_session_summary(
        paths_for(root).root,
        {
            "source_kind": "session_summary",
            "session_id": "summary-session",
            "text_sha256": row["text_sha256"],
            "excerpt": "fallback",
        },
    )
    assert resolved["integrity"] == "verified"
    assert resolved["basis"] == "canonical_derived_summary"
    assert "corrupted receipt body sentinel" not in resolved["body"]


def test_expansion_scale_query_plan_resolution_cache_and_single_index_update(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    init_root(root)
    start_session(root, session_id="prior-long")
    long_body = "scale expansion canonical detail\n" + ("word " * 1200)
    emit_event(
        root,
        session_id="prior-long",
        event_type="agent.message",
        subject="long historical record",
        body=long_body,
    )
    end_session(root, expected_session_id="prior-long")
    start_session(root, session_id="active-scale")
    pack = build_context_pack(
        root,
        "scale expansion canonical detail",
        session_id="active-scale",
        exclude_session_from_memory=True,
    )
    emitted = emit_context_pack(root, pack)
    pack_id = emitted.manifest["pack_id"]
    briefing = brief_context_manifest(emitted.manifest)
    source_ref = next(
        src["ref"] for src in briefing["sources"] if src.get("event_type") == "agent.message"
    )

    real_update = expansion_module.update_index
    update_calls = 0

    def counted_update(target):
        nonlocal update_calls
        update_calls += 1
        return real_update(target)

    with patch.object(expansion_module, "update_index", side_effect=counted_update):
        page1 = expand_context_source(root, pack_id=pack_id, source_selector=source_ref, page=1, page_bytes=512)
        assert update_calls == 1
        assert page1["page_count"] >= 3
        update_calls = 0
        page2 = expand_context_source(root, pack_id=pack_id, source_selector=source_ref, page=2, page_bytes=512)
        assert update_calls == 1
        assert page2["receipt"]["status"] == "recorded"

    expand_context_source(root, pack_id=pack_id, source_selector=source_ref, page=3, page_bytes=512)
    update_index(root)

    session_receipts = _session_expansion_events(root, "active-scale")
    assert len(session_receipts) == 3

    with connect_index(root) as conn:
        plan_rows = conn.execute(
            """
            explain query plan
            select m.*
            from messages m
            where m.mailbox_path = ?
              and (
                m.event_type = ?
                or m.id in (
                  select hv.message_rowid
                  from headers hv
                  where lower(hv.name) in (lower(?), lower(?), lower(?))
                )
              )
            """,
            (
                "sessions/active-scale/Maildir",
                EVENT_CONTEXT_SOURCES_EXPANDED,
                "X-AgentDir-Context-View-Pack-Id",
                "X-AgentDir-Context-View-Id",
                "X-AgentDir-Context-View-Source-Id",
            ),
        ).fetchall()
    plan_text = " ".join(str(row["detail"]) for row in plan_rows)
    assert "CORRELATED" not in plan_text.upper()

    manifest = read_context_manifest(root, pack_id, rebuild=False)
    events = context_events(root, pack_id, rebuild=False, session_id="active-scale")
    real_resolve = expansion_module._resolve_source
    resolve_calls = 0

    def counted_resolve(*args, **kwargs):
        nonlocal resolve_calls
        resolve_calls += 1
        return real_resolve(*args, **kwargs)

    with patch.object(expansion_module, "_resolve_source", side_effect=counted_resolve):
        exp_audit = audit_context_expansion(root, manifest, events, [])
    assert exp_audit["receipt_event_count"] == 3
    assert exp_audit["valid_receipt_count"] == 3
    assert resolve_calls == 1


def test_context_mutations_acquire_session_lock_before_pack_lock_and_reject_ended_or_archived_sessions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    pack_id, manifest = _emit_two_source_pack(root, session_id="lock-session")
    presented = manifest["briefing"]["source_ids"]
    real_lock = context_module.lifecycle_lock
    acquired: list[str] = []

    @contextmanager
    def recording_lock(lock_root, key):
        acquired.append(key)
        with real_lock(lock_root, key):
            yield

    with patch.object(context_module, "lifecycle_lock", side_effect=recording_lock):
        consume_context_sources(root, pack_id=pack_id, source_ids=[presented[0]], purpose="plan")
    assert acquired == ["session:lock-session", f"pack:{pack_id}"]

    acquired.clear()
    with patch.object(context_module, "lifecycle_lock", side_effect=recording_lock):
        review_context_pack(
            root,
            pack_id=pack_id,
            disposition="used",
            reason="constraints apply to the plan",
            source_selectors=["1"],
        )
    assert acquired == ["session:lock-session", f"pack:{pack_id}"]

    acquired.clear()
    with patch.object(context_module, "lifecycle_lock", side_effect=recording_lock):
        cite_context_sources(root, pack_id=pack_id, source_ids=[presented[0]])
    assert acquired == ["session:lock-session", f"pack:{pack_id}"]

    end_session(root, expected_session_id="lock-session")
    extra_pack = build_context_pack(root, "late task", session_id="lock-session")
    for label, fn in (
        ("emit", lambda: emit_context_pack(root, extra_pack)),
        ("consume", lambda: consume_context_sources(root, pack_id=pack_id, source_ids=[presented[0]], purpose="plan")),
        ("review", lambda: review_context_pack(root, pack_id=pack_id, disposition="skipped", reason="late")),
        ("cite", lambda: cite_context_sources(root, pack_id=pack_id, source_ids=[presented[0]])),
    ):
        try:
            fn()
        except AgentDirStateError as exc:
            assert "ended session" in str(exc), f"{label}: {exc}"
        else:
            raise AssertionError(f"{label} should have failed on ended session")

    archive_sessions(root, sessions=["lock-session"], apply=True)
    live_session_dir = paths_for(root).sessions / "lock-session"
    assert not live_session_dir.exists()

    for label, fn in (
        ("emit", lambda: emit_context_pack(root, extra_pack)),
        ("consume", lambda: consume_context_sources(root, pack_id=pack_id, source_ids=[presented[0]], purpose="plan")),
        ("review", lambda: review_context_pack(root, pack_id=pack_id, disposition="skipped", reason="late")),
        ("cite", lambda: cite_context_sources(root, pack_id=pack_id, source_ids=[presented[0]])),
    ):
        try:
            fn()
        except AgentDirStateError as exc:
            assert "archived session" in str(exc), f"{label}: {exc}"
        else:
            raise AssertionError(f"{label} should have failed on archived session")
        assert not live_session_dir.exists(), f"{label} resurrected archived session directory"


def test_lifecycle_and_operational_noise_filtered_before_jev_shortlist(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    update_index(root)
    configure_context_reranker(root, "jev")

    noise_hits = [
        {
            "source_id": f"message:noise-{idx}",
            "session_id": f"noise-{idx}",
            "source_kind": "message",
            "event_type": "work.started" if idx % 2 == 0 else "claim.recorded",
            "subject": "checkout retry policy",
            "body_text": "checkout retry policy lifecycle noise",
            "memory_score": 0.99 - idx * 0.001,
        }
        for idx in range(25)
    ]
    real_hits = [
        {
            "source_id": "message:decision-1",
            "session_id": "real-1",
            "source_kind": "message",
            "event_type": "decision.recorded",
            "subject": "checkout retry policy",
            "body_text": "Use idempotency keys for checkout retry policy.",
            "memory_score": 0.75,
        },
        {
            "source_id": "message:decision-2",
            "session_id": "real-2",
            "source_kind": "message",
            "event_type": "decision.recorded",
            "subject": "checkout retry backoff",
            "body_text": "Cap exponential backoff at 30 seconds.",
            "memory_score": 0.74,
        },
    ]
    seen_by_jev: list[dict[str, object]] = []

    def fake_jev(_root, _task, hits, **_kwargs):
        seen_by_jev.extend(hits)
        return hits, {"provider": "jev", "status": "applied"}

    with (
        patch.object(context_module, "search_memory", return_value=[*noise_hits, *real_hits]),
        patch.object(context_module, "filter_context_hits", side_effect=fake_jev),
    ):
        pack = build_context_pack(
            root,
            "implement checkout retry policy",
            recent_limit=0,
            evidence_limit=0,
            rebuild=False,
        )

    assert [row["source_id"] for row in seen_by_jev] == ["message:decision-1", "message:decision-2"]
    assert [row["source_id"] for row in pack["memory_hits"]] == [
        "message:decision-1",
        "message:decision-2",
    ]

    seen_targeted: list[dict[str, object]] = []

    def fake_jev_targeted(_root, _task, hits, **_kwargs):
        seen_targeted.extend(hits)
        return hits, {"provider": "jev", "status": "applied"}

    with (
        patch.object(context_module, "search_memory", return_value=[noise_hits[0], *real_hits]),
        patch.object(context_module, "filter_context_hits", side_effect=fake_jev_targeted),
    ):
        build_context_pack(
            root,
            "diagnose work start checkout retry policy",
            recent_limit=0,
            evidence_limit=0,
            rebuild=False,
        )
    assert "message:noise-0" in [row["source_id"] for row in seen_targeted]


def test_build_context_manifest_gives_current_evidence_precedence_on_dedupe() -> None:
    shared_row = {
        "source_id": "message:sessions/cur/Maildir/new/tool1",
        "session_id": "cur",
        "event_type": "tool.result",
        "subject": "tool.result pytest",
        "body_text": "tool=pytest exit=0",
        "memory_score": 0.25,
    }
    manifest = build_context_manifest(
        {
            "task": "database connection pool leak",
            "retrieval_query": "database connection pool leak",
            "retrieval_query_state": "specific_terms",
            "session_id": "cur",
            "retrieval_mode": "hybrid",
            "memory_hits": [shared_row],
            "recent_session_summaries": [],
            "evidence": [shared_row],
        }
    )
    assert len(manifest["sources"]) == 1
    assert manifest["sources"][0]["origin"] == "evidence"
    assert manifest["sources"][0]["match_quality"] == "current"
    assert manifest["briefing"]["source_ids"] == [shared_row["source_id"]]


def test_selection_excludes_historical_tool_from_decision_sessions_and_higher_signal() -> None:
    task = "explainable multi base environment composition"

    manifest_summary = build_context_manifest(
        {
            "task": task,
            "retrieval_query": task,
            "retrieval_query_state": "specific_terms",
            "session_id": "current-session",
            "retrieval_mode": "hybrid",
            "memory_hits": [
                {
                    "source_id": "message:unmatched-tool",
                    "session_id": "prior-session",
                    "event_type": "tool.call",
                    "subject": "tool.call pytest",
                    "body_text": "tool=pytest argv=['tests/test_unrelated.py']",
                    "memory_score": 0.45,
                },
                {
                    "source_id": "session:prior-session:summary",
                    "source_kind": "session_summary",
                    "session_id": "prior-session",
                    "event_type": "summary.compacted",
                    "body_text": f"Session summary: prior-session\n{task} notes",
                    "memory_score": 0.60,
                },
            ],
            "recent_session_summaries": [],
            "evidence": [],
        }
    )
    assert manifest_summary["briefing"]["source_ids"] == ["session:prior-session:summary"]

    manifest_fallback = build_context_manifest(
        {
            "task": task,
            "retrieval_query": task,
            "retrieval_query_state": "specific_terms",
            "session_id": "current-session",
            "retrieval_mode": "hybrid",
            "memory_hits": [
                {
                    "source_id": "message:unmatched-tool",
                    "session_id": "tool-session",
                    "event_type": "tool.call",
                    "subject": "tool.call pytest",
                    "body_text": "tool=pytest argv=['tests/test_unrelated.py']",
                    "memory_score": 0.45,
                },
                {
                    "source_id": "message:final-report",
                    "session_id": "report-session",
                    "event_type": "work.report.final",
                    "subject": "final report",
                    "body_text": f"{task} completed with rollback verification",
                    "memory_score": 0.72,
                },
            ],
            "recent_session_summaries": [],
            "evidence": [],
        }
    )
    assert manifest_fallback["briefing"]["source_ids"] == ["message:final-report"]


def test_context_module_exports_clean_and_unused_imports_removed() -> None:
    assert context_module.brief_context_manifest is brief_context_manifest
    assert context_module.read_context_manifest is read_context_manifest
    assert context_module.CONSUMPTION_PURPOSES is CONSUMPTION_PURPOSES
    assert hasattr(context_module, "_diversify_memory_hits")
    for removed in (
        "parsedate_to_datetime",
        "artifact_path",
        "CONTEXT_BRIEFING_PROTOCOL",
        "EVENT_CONTEXT_SOURCES_EXPANDED",
        "context_pack_body_ids",
        "context_pack_identity_error",
        "_event_body_value",
        "CONTEXT_BRIEFING_LIMIT",
        "CONTEXT_MAX_PER_CLASS_FIRST_PASS",
        "CONTEXT_MAX_PER_SESSION",
        "CONTEXT_QUALITY_ORDER",
        "CONTEXT_QUALITY_POLICY",
        "CONTEXT_QUALITY_THRESHOLDS",
        "CONTEXT_REDUNDANT_WITH_DECISION_SESSION",
        "CONTEXT_SOURCE_PREFERENCE_ORDER",
        "CONTEXT_SOURCE_SELECTION_TIERS",
        "EVIDENCE_EVENTS",
        "LIFECYCLE_EVENTS",
        "MEMORY_STOPWORDS",
        "parse_envelope",
        "connect_index",
        "RETRIEVAL_DOCUMENT",
        "RETRIEVAL_SEMANTIC",
        "RETRIEVAL_SEMANTIC_HYBRID",
    ):
        assert not hasattr(context_module, removed), f"unused import {removed} still present"


def test_decision_validation_rejects_duplicate_headers_and_contradictory_body_fields() -> None:
    pack_id = "ctx-validation"
    presented = ["message:s1", "message:s2"]
    manifest = {
        "pack_id": pack_id,
        "session_id": "sess-1",
        "sources": [{"source_id": "message:s1"}, {"source_id": "message:s2"}],
        "briefing": {"source_ids": presented, "review_required": True},
    }
    decision_id = context_decision_id(
        pack_id=pack_id,
        disposition="used",
        purpose="plan",
        reason="needed for design",
        reviewed_source_ids=presented,
        used_source_ids=["message:s1"],
        dismissed_source_ids=["message:s2"],
    )

    def make_event(
        *,
        pack_ids: list[str] | None = None,
        header_values: dict[str, list[str]] | None = None,
        body_text: str | None = None,
    ) -> dict[str, object]:
        base_hv = {
            HEADER_PACK_ID: [pack_id],
            HEADER_CONTEXT_DISPOSITION: ["used"],
            HEADER_CONTEXT_DECISION_ID: [decision_id],
            HEADER_CONTEXT_DECISION_REVISION: ["1"],
            HEADER_CONSUMPTION_PURPOSE: ["plan"],
        }
        if header_values:
            base_hv.update(header_values)
        return {
            "event_type": EVENT_CONTEXT_PACK_CONSUMED,
            "session_id": "sess-1",
            "pack_ids": pack_ids if pack_ids is not None else list(base_hv[HEADER_PACK_ID]),
            "header_values": base_hv,
            "headers": {k: v[0] for k, v in base_hv.items() if v},
            "reviewed_source_ids": list(presented),
            "used_source_ids": ["message:s1"],
            "dismissed_source_ids": ["message:s2"],
            "body_text": body_text
            if body_text is not None
            else format_context_review_body(
                pack_id=pack_id,
                disposition="used",
                purpose="plan",
                reason="needed for design",
                presented_count=2,
                reviewed_source_ids=presented,
                used_source_ids=["message:s1"],
                dismissed_source_ids=["message:s2"],
            ),
        }

    assert context_decision_validation_errors(make_event(), manifest, presented) == []

    dup_pack = context_decision_validation_errors(
        make_event(pack_ids=[pack_id, "ctx-other"]), manifest, presented
    )
    assert any(HEADER_PACK_ID in err for err in dup_pack)

    for header_name, values in (
        (HEADER_CONTEXT_DISPOSITION, ["used", "skipped"]),
        (HEADER_CONTEXT_DECISION_ID, [decision_id, "ctxd-second"]),
        (HEADER_CONTEXT_DECISION_REVISION, ["1", "2"]),
        (HEADER_CONSUMPTION_PURPOSE, ["plan", "answer"]),
    ):
        errs = context_decision_validation_errors(
            make_event(header_values={header_name: values}), manifest, presented
        )
        assert any(header_name in err for err in errs), f"missing error for {header_name}: {errs}"

    bad_body_pack = context_decision_validation_errors(
        make_event(body_text=f"pack_id=ctx-other\ndisposition=used\nreason=needed for design"),
        manifest,
        presented,
    )
    assert any("body pack_id" in err for err in bad_body_pack)

    bad_body_disp = context_decision_validation_errors(
        make_event(body_text=f"pack_id={pack_id}\ndisposition=skipped\nreason=needed for design"),
        manifest,
        presented,
    )
    assert any("body disposition" in err for err in bad_body_disp)

    dup_reason = context_decision_validation_errors(
        make_event(
            body_text=(
                f"pack_id={pack_id}\ndisposition=used\n"
                "reason=needed for design\nreason=second reason"
            )
        ),
        manifest,
        presented,
    )
    assert any("duplicate reason" in err for err in dup_reason)


def test_used_decision_omitting_prior_consumed_presented_source_flags_conflict(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    pack_id, manifest = _emit_two_source_pack(root, session_id="conflict-session")
    presented = manifest["briefing"]["source_ids"]
    assert len(presented) >= 2
    s1, s2 = presented[0], presented[1]

    consume_context_sources(root, pack_id=pack_id, source_ids=[s1], purpose="plan")

    dismissed = [sid for sid in presented if sid != s2]
    forged_id = context_decision_id(
        pack_id=pack_id,
        disposition="used",
        purpose="plan",
        reason="omitting s1",
        reviewed_source_ids=presented,
        used_source_ids=[s2],
        dismissed_source_ids=dismissed,
    )
    emit_event(
        root,
        session_id="conflict-session",
        event_type=EVENT_CONTEXT_PACK_CONSUMED,
        subject="forged omitting decision",
        body=format_context_review_body(
            pack_id=pack_id,
            disposition="used",
            purpose="plan",
            reason="omitting s1",
            presented_count=len(presented),
            reviewed_source_ids=presented,
            used_source_ids=[s2],
            dismissed_source_ids=dismissed,
        ),
        extra_headers={
            HEADER_PROTOCOL: CONTEXT_PROTOCOL,
            HEADER_PACK_ID: pack_id,
            HEADER_SOURCE_ID: [s2],
            HEADER_REVIEWED_SOURCE_ID: presented,
            HEADER_USED_SOURCE_ID: [s2],
            "X-AgentDir-Dismissed-Source-Id": dismissed,
            HEADER_CONTEXT_DISPOSITION: "used",
            HEADER_CONTEXT_DECISION_ID: forged_id,
            HEADER_CONTEXT_DECISION_REVISION: "1",
            HEADER_CONSUMPTION_PURPOSE: "plan",
        },
    )

    audit = audit_context_pack(root, pack_id)
    assert audit["transition_conflict"] is True
    assert audit["review_status"] == "conflict"
    assert audit["finish_allowed"] is False
    assert audit["lineage_valid"] is False
    assert any("omits previously consumed presented" in err for err in audit["decision_validation_errors"])


def test_consuming_only_omitted_source_keeps_review_pending_and_allows_no_relevant(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    init_root(root)
    for idx in range(6):
        start_session(root, session_id=f"prior-{idx}")
        emit_event(
            root,
            session_id=f"prior-{idx}",
            event_type="decision.recorded",
            subject=f"checkout omitted guard decision {idx}",
            body=f"checkout omitted guard marker variant {idx}",
        )
        end_session(root, expected_session_id=f"prior-{idx}")

    start_session(root, session_id="active-omitted")
    pack = build_context_pack(
        root,
        "checkout omitted guard marker",
        session_id="active-omitted",
        memory_limit=8,
        exclude_session_from_memory=True,
    )
    emitted = emit_context_pack(root, pack)
    manifest = emitted.manifest
    pack_id = manifest["pack_id"]
    presented_set = set(manifest["briefing"]["source_ids"])
    omitted_id = next(
        src["source_id"] for src in manifest["sources"] if src["source_id"] not in presented_set
    )

    consume_context_sources(root, pack_id=pack_id, source_ids=[omitted_id], purpose="plan")
    pending_audit = audit_context_pack(root, pack_id)
    assert pending_audit["review_status"] == "pending"
    assert pending_audit["finish_allowed"] is False
    assert pending_audit["used_count"] == 0
    assert pending_audit["consumed_count"] == 1
    assert pending_audit["additional_consumed_count"] == 1

    review = review_context_pack(
        root,
        pack_id=pack_id,
        disposition="no_relevant",
        reason="none of the presented items constrain this work",
    )
    assert review["recorded"] is True
    assert review["disposition"] == "no_relevant"

    complete_audit = audit_context_pack(root, pack_id)
    assert complete_audit["review_status"] == "complete"
    assert complete_audit["decision"] == "no_relevant"
    assert complete_audit["transition_conflict"] is False
    assert complete_audit["finish_allowed"] is True
    assert complete_audit["lineage_valid"] is True
    assert complete_audit["additional_consumed_count"] == 1
    assert complete_audit["used_count"] == 0

    root_2 = tmp_path / "store-2"
    pack_id_2, manifest_2 = _emit_two_source_pack(root_2, session_id="presented-guard")
    presented_2 = manifest_2["briefing"]["source_ids"]
    consume_context_sources(root_2, pack_id=pack_id_2, source_ids=[presented_2[0]], purpose="plan")
    try:
        review_context_pack(
            root_2,
            pack_id=pack_id_2,
            disposition="no_relevant",
            reason="cannot dismiss consumed presented source",
        )
    except AgentDirStateError as exc:
        assert "already has consumed sources" in str(exc)
    else:
        raise AssertionError("expected no_relevant to be blocked after consuming a presented source")
