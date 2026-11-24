from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import tomllib
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import agentdir.daemon as daemon
import agentdir.federation as federation
import agentdir.index as index_module
import agentdir.memory as memory
from agentdir.envelope import header_value
from agentdir.events import emit_event
from agentdir.index import ROWID_INDEXES, connect_index, rebuild_index, update_index
from agentdir.store import AgentDirError, init_root, paths_for


def _project_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError("Could not locate project root")


def _index_names(db_path: Path) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        return {
            row[0]
            for row in conn.execute("select name from sqlite_master where type = 'index'")
        }


def test_schema_and_update_index_create_all_rowid_indexes(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    emit_event(root, session_id="s1", event_type="agent.message", body="index check body")
    rebuild_index(root)

    expected = {name for name, _ddl in ROWID_INDEXES}
    assert expected == {
        "headers_message_idx",
        "headers_lower_name_value_idx",
        "message_artifacts_message_idx",
        "memory_documents_message_idx",
        "memory_passages_message_idx",
    }

    db_path = paths_for(root).index_path
    assert expected <= _index_names(db_path)

    # Simulate an older schema v3 database missing these indexes.
    with closing(sqlite3.connect(db_path)) as conn:
        for name in expected:
            conn.execute(f"drop index if exists {name}")
        conn.commit()
    assert expected.isdisjoint(_index_names(db_path))

    # Incremental update must recreate them idempotently without a full rebuild.
    with patch.object(
        index_module,
        "_rebuild_index_locked",
        side_effect=AssertionError("expected incremental update, not full rebuild"),
    ):
        update_index(root)
        update_index(root)

    assert expected <= _index_names(db_path)


def test_update_index_skips_summary_recompute_for_already_cleaned_receipts(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    emit_event(root, session_id="receipt-s1", event_type="agent.message", body="primary record")
    emit_event(
        root,
        session_id="receipt-s1",
        event_type="context.sources.expanded",
        body="expansion receipt payload",
        extra_headers={"X-AgentDir-Context-View-Pack-Id": "ctx-pack-1"},
    )
    rebuild_index(root)

    calls: list[set[str] | None] = []
    real_summaries = index_module.index_session_summaries

    def counted_summaries(conn: sqlite3.Connection, only_sessions: set[str] | None = None) -> None:
        calls.append(only_sessions)
        real_summaries(conn, only_sessions=only_sessions)

    with patch.object(index_module, "index_session_summaries", side_effect=counted_summaries):
        # Receipt is already absent from memory_documents: no-op update must not recompute summaries.
        update_index(root)
        assert calls == []

        # Insert a legacy memory_documents row for the receipt; update must clean it once.
        with closing(connect_index(root)) as conn:
            row = conn.execute(
                "select * from messages where event_type = 'context.sources.expanded'"
            ).fetchone()
            assert row is not None
            memory.index_memory_document(
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
            conn.commit()

        update_index(root)
        assert calls == [{"receipt-s1"}]

        # Subsequent no-op update must again avoid recomputing session summaries.
        update_index(root)
        assert calls == [{"receipt-s1"}]

    with closing(connect_index(root)) as conn:
        remaining = conn.execute(
            "select count(*) from memory_documents where event_type = 'context.sources.expanded'"
        ).fetchone()[0]
    assert remaining == 0


def test_update_index_chunks_large_excluded_receipt_deletes(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    emit_event(root, session_id="bulk-s1", event_type="agent.message", body="keep this message")
    rebuild_index(root)

    # Seed >1,000 excluded messages + memory_documents rows to exercise chunked deletes (>500 per batch).
    total_excluded = 1150
    with closing(connect_index(root)) as conn:
        for i in range(total_excluded):
            file_path = f"sessions/bulk-s1/Maildir/new/synth-{i:04d}.eml"
            cursor = conn.execute(
                """
                insert into messages(
                  message_id, mailbox_kind, mailbox_path, file_path, state, event_type,
                  subject, session_id, body_text, indexed_at
                ) values (?, 'session', 'sessions/bulk-s1/Maildir', ?, 'new',
                          'context.sources.expanded', 'receipt', 'bulk-s1', 'receipt body', ?)
                """,
                (f"<synth-{i}@agentdir.local>", file_path, "2026-01-01T00:00:00+00:00"),
            )
            rowid = int(cursor.lastrowid)
            conn.execute(
                """
                insert into memory_documents(
                  source_kind, source_id, message_rowid, message_id, session_id,
                  event_type, subject, file_path, body_text, vector_dim, vector_json,
                  text_sha256, token_count, indexed_at
                ) values ('message', ?, ?, ?, 'bulk-s1', 'context.sources.expanded',
                          'receipt', ?, 'receipt body', 1024, '{}', 'abc', 2, ?)
                """,
                (
                    f"message:{file_path}",
                    rowid,
                    f"<synth-{i}@agentdir.local>",
                    file_path,
                    "2026-01-01T00:00:00+00:00",
                ),
            )
        conn.commit()

    # Run incremental update; must succeed across multiple 500-row chunks without falling back to rebuild.
    with patch.object(
        index_module,
        "_rebuild_index_locked",
        side_effect=AssertionError("chunked delete should not trigger full rebuild"),
    ):
        update_index(root)

    with closing(connect_index(root)) as conn:
        excluded_docs = conn.execute(
            "select count(*) from memory_documents where event_type = 'context.sources.expanded'"
        ).fetchone()[0]
        kept_docs = conn.execute(
            "select count(*) from memory_documents where event_type = 'agent.message'"
        ).fetchone()[0]
    assert excluded_docs == 0
    assert kept_docs == 1


def test_folded_headers_are_normalized_in_messages_columns(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    record = emit_event(root, session_id="fold-s1", event_type="agent.message", body="folded test")

    raw_envelope = (
        "Message-ID:\n"
        " <folded-id-123@agentdir.local>\n"
        "Date: Tue, 24 Nov 2026 12:00:00 +0000\n"
        "From: tester@agentdir.local\n"
        "To: fold-s1@agentdir.local\n"
        "Subject: first line of subject\n"
        "\tcontinued on second line\n"
        "X-AgentDir-Version: 0.1\n"
        "X-AgentDir-Event-Type: agent.message\n"
        "X-AgentDir-Session: fold-s1\n"
        "X-AgentDir-Task:\n"
        " task-folded-42\n"
        "X-AgentDir-Workspace:\n"
        " /workspace/very/long/path/repo\n"
        "Content-Type: text/plain; charset=\"utf-8\"\n"
        "\n"
        "folded header body\n"
    )
    record.path.write_text(raw_envelope, encoding="utf-8")
    rebuild_index(root)

    with closing(connect_index(root)) as conn:
        row = conn.execute("select * from messages where session_id = 'fold-s1'").fetchone()

    assert row is not None
    assert row["message_id"] == "<folded-id-123@agentdir.local>"
    assert row["subject"] == "first line of subject continued on second line"
    assert row["task_id"] == "task-folded-42"
    assert row["workspace"] == "/workspace/very/long/path/repo"
    for field in ("message_id", "subject", "task_id", "workspace"):
        value = row[field]
        assert value == header_value(value)
        assert "\n" not in value and "\r" not in value and "\t" not in value


def test_connect_index_sets_busy_timeout_and_foreign_keys(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    rebuild_index(root)

    with closing(connect_index(root)) as conn:
        busy_timeout = conn.execute("pragma busy_timeout").fetchone()[0]
        foreign_keys = conn.execute("pragma foreign_keys").fetchone()[0]

    assert busy_timeout == index_module.SQLITE_BUSY_TIMEOUT_MS
    assert busy_timeout >= 5000
    assert foreign_keys == 1


def test_update_index_preserves_lock_errors_and_rebuilds_corruption(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    emit_event(root, session_id="lock-s1", event_type="agent.message", body="lock test")
    rebuild_index(root)

    for lock_msg in ("database is locked", "database table is locked", "database is busy"):
        with (
            patch.object(index_module, "_update_into", side_effect=sqlite3.OperationalError(lock_msg)),
            patch.object(
                index_module,
                "_rebuild_index_locked",
                side_effect=AssertionError("must not rebuild on lock contention"),
            ),
        ):
            raised = False
            try:
                update_index(root)
            except sqlite3.OperationalError as exc:
                raised = True
                assert lock_msg in str(exc)
            assert raised

    # Lock error during schema check must also propagate without rebuilding.
    with (
        patch.object(
            index_module,
            "_index_schema_current",
            side_effect=sqlite3.OperationalError("database is locked"),
        ),
        patch.object(
            index_module,
            "_rebuild_index_locked",
            side_effect=AssertionError("must not rebuild on schema-check lock"),
        ),
    ):
        raised = False
        try:
            update_index(root)
        except sqlite3.OperationalError:
            raised = True
        assert raised

    # Corruption / schema errors must still recover via full rebuild.
    rebuild_calls = 0
    real_rebuild = index_module._rebuild_index_locked

    def counted_rebuild(paths):
        nonlocal rebuild_calls
        rebuild_calls += 1
        return real_rebuild(paths)

    with (
        patch.object(
            index_module,
            "_update_into",
            side_effect=sqlite3.DatabaseError("database disk image is malformed"),
        ),
        patch.object(index_module, "_rebuild_index_locked", side_effect=counted_rebuild),
    ):
        result = update_index(root)

    assert rebuild_calls == 1
    assert result.indexed == 1


def test_update_index_checks_schema_inside_rebuild_lock(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    rebuild_index(root)

    lock_depth = 0
    observed_depth_during_schema_check: list[int] = []
    real_lock = index_module.index_rebuild_lock
    real_schema_check = index_module._index_schema_current

    from contextlib import contextmanager

    @contextmanager
    def tracked_lock(indexes: Path):
        nonlocal lock_depth
        with real_lock(indexes):
            lock_depth += 1
            try:
                yield
            finally:
                lock_depth -= 1

    def tracked_schema_check(index_path: Path) -> bool:
        observed_depth_during_schema_check.append(lock_depth)
        return real_schema_check(index_path)

    with (
        patch.object(index_module, "index_rebuild_lock", side_effect=tracked_lock),
        patch.object(index_module, "_index_schema_current", side_effect=tracked_schema_check),
    ):
        update_index(root)

    assert observed_depth_during_schema_check == [1]


def test_semantic_search_batches_cache_lookup_hashes_full_text_and_locks_writes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    init_root(root)
    emit_event(
        root,
        session_id="sem-s1",
        event_type="decision.recorded",
        subject="webhook retry ledger",
        tool="pytest",
        body="Tenant callback backoff keeps a bounded retry ledger.",
    )
    emit_event(
        root,
        session_id="sem-s2",
        event_type="agent.message",
        subject="ui spacing",
        body="Adjusted dashboard button margins.",
    )
    rebuild_index(root)

    # Verify expected text hashes stored in memory_documents.
    with closing(connect_index(root)) as conn:
        stored_hashes = {
            row["source_id"]: row["text_sha256"]
            for row in conn.execute("select source_id, text_sha256 from memory_documents").fetchall()
        }

    with patch.object(memory, "_module_available", side_effect=lambda mod: mod == "fastembed"):
        memory.configure_embeddings(root, "fastembed", model="test-model")

        query = "stabilize webhook retries"
        embedded_batches: list[list[str]] = []
        lock_held_during_embed: list[bool] = []
        lock_file = paths_for(root).indexes / ".agentdir-index.lock"

        def fake_vectors(_root: Path, _model: str, texts: list[str]) -> list[list[float]]:
            embedded_batches.append(list(texts))
            # Probe index lock while computing embeddings: must NOT be held.
            held = False
            if lock_file.exists():
                with lock_file.open("a+", encoding="utf-8") as probe:
                    try:
                        fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError as exc:
                        if exc.errno in (errno.EACCES, errno.EAGAIN):
                            held = True
                        else:
                            raise
                    else:
                        fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            lock_held_during_embed.append(held)
            return [
                [1.0, 0.0]
                if text == query
                else ([0.9, 0.1] if "webhook retry ledger" in text else [0.0, 1.0])
                for text in texts
            ]

        write_locks = 0
        real_lock = index_module.index_rebuild_lock

        from contextlib import contextmanager

        @contextmanager
        def counted_lock(indexes: Path):
            nonlocal write_locks
            write_locks += 1
            with real_lock(indexes):
                yield

        # Trace SQL statements on the second (warm-cache) search to prove no N+1 queries and no vector_json.
        traced_sql: list[str] = []
        real_connect = index_module.connect_index

        def tracing_connect(connect_root: str | Path) -> sqlite3.Connection:
            conn = real_connect(connect_root)
            conn.set_trace_callback(traced_sql.append)
            return conn

        with (
            patch.object(memory, "_fastembed_vectors", side_effect=fake_vectors),
            patch.object(index_module, "index_rebuild_lock", side_effect=counted_lock),
        ):
            first_hits = memory.search_memory(root, query, retrieval_mode="semantic")

            assert first_hits
            assert first_hits[0]["subject"] == "webhook retry ledger"
            assert "vector_json" not in first_hits[0]
            assert "cached_semantic_vector_json" not in first_hits[0]
            assert lock_held_during_embed == [False, False]
            assert write_locks == 1

            # Document texts passed to FastEmbed must match the exact SHA-256 stored in memory_documents.
            doc_texts = embedded_batches[1]
            embedded_hashes = {
                hashlib.sha256(text.strip().encode("utf-8")).hexdigest() for text in doc_texts
            }
            assert embedded_hashes == set(stored_hashes.values())

            # Warm-cache search must do ONE SELECT (LEFT JOIN, no vector_json) and zero document embeds.
            embedded_batches.clear()
            traced_sql.clear()
            with patch.object(index_module, "connect_index", side_effect=tracing_connect):
                second_hits = memory.search_memory(root, query, retrieval_mode="semantic")

            assert second_hits[0]["source_id"] == first_hits[0]["source_id"]
            assert embedded_batches == [[query]]
            select_queries = [sql for sql in traced_sql if sql.lstrip().lower().startswith("select")]
            assert len(select_queries) == 1
            assert "left join semantic_embeddings" in select_queries[0].lower()
            assert "md.vector_json" not in select_queries[0].lower()


def test_memory_config_uses_atomic_write_and_dead_helper_removed(tmp_path: Path) -> None:
    assert not hasattr(memory, "_best_passage_for_document")

    root = tmp_path / "store"
    init_root(root)
    rebuild_index(root)

    written: list[Path] = []
    real_atomic = memory.atomic_write_text

    def spy_atomic(path: str | Path, text: str) -> None:
        written.append(Path(path))
        real_atomic(path, text)

    with patch.object(memory, "atomic_write_text", side_effect=spy_atomic):
        memory.configure_embeddings(root, "fastembed", model="test-atomic-model")

    assert written == [paths_for(root).state / memory.MEMORY_CONFIG_FILE]
    cfg = memory.read_memory_config(root)
    assert cfg["embeddings"]["model"] == "test-atomic-model"


def test_daemon_watch_paths_excludes_state_directory(tmp_path: Path) -> None:
    root = tmp_path / "store"
    paths = init_root(root)

    watched = set(daemon._watch_paths(paths.root))
    assert str(paths.state) not in watched
    assert str(paths.sessions) in watched
    assert str(paths.actors) in watched
    assert str(paths.queues) in watched


def test_daemon_refresh_updates_only_stale_federated_roots_incrementally(tmp_path: Path) -> None:
    controller = tmp_path / "controller"
    fresh_root = tmp_path / "fresh"
    stale_root = tmp_path / "stale"
    for r in (controller, fresh_root, stale_root):
        init_root(r)

    emit_event(fresh_root, session_id="f1", event_type="agent.message", body="fresh body")
    emit_event(stale_root, session_id="s1", event_type="agent.message", body="stale body 1")
    rebuild_index(controller)
    rebuild_index(fresh_root)
    rebuild_index(stale_root)

    federation.register_root(controller, fresh_root, name="fresh")
    federation.register_root(controller, stale_root, name="stale")

    # Emit a new record in stale_root and ensure its mtime is strictly newer than index_path.
    new_record = emit_event(stale_root, session_id="s1", event_type="agent.message", body="stale body 2")
    future = time.time() + 10.0
    os.utime(new_record.path, (future, future))

    updated_roots: list[str] = []
    rebuilt_roots: list[str] = []
    real_update = federation.update_index
    real_rebuild = federation.rebuild_index

    def spy_update(target: str | Path):
        updated_roots.append(str(Path(target).resolve()))
        return real_update(target)

    def spy_rebuild(target: str | Path):
        rebuilt_roots.append(str(Path(target).resolve()))
        return real_rebuild(target)

    with (
        patch.object(federation, "update_index", side_effect=spy_update),
        patch.object(federation, "rebuild_index", side_effect=spy_rebuild),
    ):
        daemon._refresh(controller, group=None)

        assert rebuilt_roots == []
        assert updated_roots == [str(stale_root.resolve())]

        # Explicit full rebuild (stale_only=False) still uses rebuild_index for compatibility.
        updated_roots.clear()
        federation.rebuild_registered_roots(controller, stale_only=False)
        assert set(rebuilt_roots) == {str(fresh_root.resolve()), str(stale_root.resolve())}
        assert updated_roots == []


def test_daemon_pid_reuse_does_not_report_running_or_signal_unrelated_process(tmp_path: Path) -> None:
    root = tmp_path / "store"
    paths = init_root(root)

    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        # Simulate stale daemon metadata + unlocked lock file pointing at a reused live PID.
        daemon._write_state(
            paths.root,
            daemon._daemon_state(paths.root, pid=unrelated.pid, interval=10.0, group=None),
        )
        (paths.state / daemon.DAEMON_LOCK_FILE).write_text(f"{unrelated.pid}\n", encoding="utf-8")

        status = daemon.memory_daemon_status(paths.root)
        assert status["running"] is False

        stopped = daemon.stop_memory_daemon(paths.root, timeout=1.0)
        assert stopped["running"] is False
        time.sleep(0.05)
        assert unrelated.poll() is None, "stop_memory_daemon killed an unrelated process"
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_daemon_force_restart_stops_previous_daemon_and_closes_log_fd(tmp_path: Path) -> None:
    root = tmp_path / "store"
    paths = init_root(root)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_project_root() / "src")

    def run_daemon_cli(*args: str) -> dict:
        proc = subprocess.run(
            [sys.executable, "-m", "agentdir", "memory", "daemon", *args, "--root", str(paths.root), "--json"],
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        return json.loads(proc.stdout)

    try:
        first = run_daemon_cli("start", "--interval", "10")
        assert first["started"] is True
        assert first["running"] is True
        pid1 = int(first["pid"])

        # Starting again without --force keeps the existing daemon.
        already = run_daemon_cli("start", "--interval", "10")
        assert already["started"] is False
        assert already["running"] is True
        assert int(already["pid"]) == pid1

        # Starting with --force stops pid1 before spawning a replacement.
        restarted = run_daemon_cli("start", "--interval", "10", "--force")
        assert restarted["started"] is True
        assert restarted["running"] is True
        pid2 = int(restarted["pid"])
        assert pid2 != pid1

        # pid1 must not still be alive (wait briefly for init to reap if needed).
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and daemon._pid_running(pid1):
            time.sleep(0.05)
        assert not daemon._pid_running(pid1)

        # Verify in-process start_memory_daemon closes log file descriptor in the parent.
        stopped = daemon.stop_memory_daemon(paths.root)
        assert stopped["running"] is False

        log_path = (paths.state / daemon.DAEMON_LOG_FILE).resolve()
        with patch.dict(os.environ, {"PYTHONPATH": str(_project_root() / "src")}):
            inproc = daemon.start_memory_daemon(paths.root, interval=10.0)
        assert inproc["running"] is True
        pid3 = int(inproc["pid"])

        proc_fd_dir = Path("/proc/self/fd")
        if proc_fd_dir.is_dir():
            open_targets = []
            for entry in proc_fd_dir.iterdir():
                try:
                    open_targets.append(Path(os.readlink(entry)).resolve())
                except OSError:
                    continue
            assert log_path not in open_targets

        final = daemon.stop_memory_daemon(paths.root)
        assert final["running"] is False
        try:
            os.waitpid(pid3, 0)
        except ChildProcessError:
            pass
    finally:
        daemon.stop_memory_daemon(paths.root)


def test_remove_registered_root_rejects_ambiguous_name(tmp_path: Path) -> None:
    controller = tmp_path / "controller"
    first = tmp_path / "one" / "app" / ".agentdir"
    second = tmp_path / "two" / "app" / ".agentdir"
    for r in (controller, first, second):
        init_root(r)

    entry_one = federation.register_root(controller, first)
    entry_two = federation.register_root(controller, second)
    assert entry_one["name"] == "app"
    assert entry_two["name"] == "app"

    try:
        federation.remove_registered_root(controller, "app")
    except AgentDirError as exc:
        message = str(exc)
        assert entry_one["root_id"] in message
        assert entry_two["root_id"] in message
        assert "root_id" in message
        assert "root_path" in message
    else:
        raise AssertionError("Expected AgentDirError for ambiguous root name")

    # Neither root should have been removed.
    listed = federation.list_registered_roots(controller)
    assert {item["root_id"] for item in listed} == {entry_one["root_id"], entry_two["root_id"]}

    # Removing by unique root_id removes only that root.
    removed = federation.remove_registered_root(controller, entry_one["root_id"])
    assert removed["root_id"] == entry_one["root_id"]
    remaining = federation.list_registered_roots(controller)
    assert [item["root_id"] for item in remaining] == [entry_two["root_id"]]


def test_latest_record_mtime_skips_maildir_tmp_and_handles_vanishing_files(tmp_path: Path) -> None:
    controller = tmp_path / "controller"
    child = tmp_path / "child"
    init_root(controller)
    init_root(child)
    record = emit_event(child, session_id="m1", event_type="agent.message", body="committed record")
    rebuild_index(child)
    federation.register_root(controller, child, name="child")

    # Write a newer in-flight file inside Maildir/tmp; it must be ignored.
    tmp_file = child / "sessions" / "m1" / "Maildir" / "tmp" / "in-flight.eml"
    tmp_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file.write_text("partial write", encoding="utf-8")
    future = time.time() + 3600.0
    os.utime(tmp_file, (future, future))

    latest = federation._latest_record_mtime(child)
    assert latest is not None
    assert latest < future - 100.0
    diagnostics = federation.doctor_registered_roots(controller)
    assert diagnostics[0]["stale"] is False

    # Simulate a concurrent rename deleting a file right before stat().
    vanishing = child / "sessions" / "m1" / "Maildir" / "new" / "vanishing.eml"
    vanishing.write_text("ephemeral", encoding="utf-8")
    real_stat = Path.stat

    def flaky_stat(self: Path, *args, **kwargs):
        if self.name == "vanishing.eml":
            raise FileNotFoundError(self)
        return real_stat(self, *args, **kwargs)

    with patch.object(Path, "stat", flaky_stat):
        mtime_after_race = federation._latest_record_mtime(child)

    assert mtime_after_race is not None
    assert abs(mtime_after_race - record.path.stat().st_mtime) < 1e-3


def test_unimplemented_backends_always_report_disabled_and_not_implemented(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    emit_event(root, session_id="b1", event_type="agent.message", body="backend status check")
    rebuild_index(root)

    with patch.object(memory, "_module_available", return_value=True):
        memory.configure_vector_backend(root, "sqlite-vec")
        memory.configure_team_backend(root, "qdrant")
        status_qdrant = memory.memory_backend_status(root)
        by_name = {entry["name"]: entry for entry in status_qdrant["backends"]}

        assert by_name["sqlite-vec"]["configured"] is True
        assert by_name["sqlite-vec"]["enabled"] is False
        assert by_name["sqlite-vec"]["status"] == "not-implemented"

        assert by_name["qdrant"]["configured"] is True
        assert by_name["qdrant"]["enabled"] is False
        assert by_name["qdrant"]["status"] == "not-implemented"

        assert by_name["lancedb"]["configured"] is False
        assert by_name["lancedb"]["enabled"] is False
        assert by_name["lancedb"]["status"] == "not-implemented"

        # Configuring lancedb still reports enabled=False and status=not-implemented.
        status_lance = memory.configure_team_backend(root, "lancedb")
        by_name_lance = {entry["name"]: entry for entry in status_lance["backends"]}
        assert by_name_lance["lancedb"]["configured"] is True
        assert by_name_lance["lancedb"]["enabled"] is False
        assert by_name_lance["lancedb"]["status"] == "not-implemented"

        # FastEmbed local-embeddings still works normally when configured and available.
        status_embed = memory.configure_embeddings(root, "fastembed")
        by_name_embed = {entry["name"]: entry for entry in status_embed["backends"]}
        assert by_name_embed["local-embeddings"]["enabled"] is True
        assert by_name_embed["local-embeddings"]["status"] == "ready"
        assert status_embed["active"] == "semantic-hybrid"


def test_pyproject_removes_unimplemented_optional_dependencies() -> None:
    pyproject = _project_root() / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    optional = data["project"]["optional-dependencies"]

    assert optional["semantic"] == ["fastembed>=0.4"]
    assert "team" not in optional
    assert optional["watch"] == ["watchfiles>=0.21"]
