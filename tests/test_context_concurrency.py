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


def test_work_start_blocks_a_second_pack_until_the_first_is_decided(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect pending briefing marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)

    first = json.loads(
        run_cli("work", "start", "checkout redirect pending briefing marker", "--json", cwd=repo).stdout
    )
    blocked = run_cli(
        "work",
        "start",
        "second task cannot hide the first pack",
        "--json",
        cwd=repo,
        expected_returncode=3,
    )

    assert first["context_pack"]["pack_id"] in blocked.stderr
    assert f"work context --root {repo / '.agentdir'} --pack" in blocked.stderr
    assert f"work context --root {repo / '.agentdir'} --show --pack" in blocked.stderr


def test_concurrent_work_starts_create_only_one_pending_pack(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect concurrent start marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    run_cli("session", "start", "--id", "active-context", cwd=repo)

    def start() -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC_ROOT)
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "agentdir",
                "work",
                "start",
                "checkout redirect concurrent start marker",
                "--json",
            ],
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: start(), range(2)))

    assert sorted(result.returncode for result in results) == [0, 3]
    successful = next(result for result in results if result.returncode == 0)
    pack_id = json.loads(successful.stdout)["context_pack"]["pack_id"]
    run_cli("status", "--json", cwd=repo)
    packs = [
        row
        for row in query_rows(repo / ".agentdir", "context.pack.created")
        if row["session_id"] == "active-context"
    ]
    assert len(packs) == 1
    assert pack_id in next(result.stderr for result in results if result.returncode == 3)


def test_context_pack_emission_linearizes_before_finish_audit(tmp_path: Path, monkeypatch) -> None:
    import fcntl
    import hashlib
    from contextlib import contextmanager
    from threading import current_thread

    import agentdir.context as context
    import agentdir.control as control
    from agentdir.store import AgentDirStateError

    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect finish race marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli("work", "start", "finish race baseline", "--no-context", "--json", cwd=repo).stdout
    )
    store = repo / ".agentdir"
    pack = context.build_context_pack(
        store,
        "checkout redirect finish race marker",
        session_id=started["session"]["session_id"],
        exclude_session_from_memory=True,
    )
    assert context.brief_context_manifest(context.build_context_manifest(pack))["review_required"]

    emitter_inside = Event()
    release_emitter = Event()
    finish_lock_probe = Event()
    finish_scan = Event()
    finisher_ident: dict[str, int | None] = {"value": None}
    probe_blocked: dict[str, bool | None] = {"value": None}
    real_context_emit = context.emit_event
    real_build_report = control.build_final_report
    real_control_lock = control.lifecycle_lock

    def paused_context_emit(*args, **kwargs):
        if kwargs.get("event_type") == "context.pack.created":
            emitter_inside.set()
            if not release_emitter.wait(5):
                raise AssertionError("timed out waiting to release context emission")
        return real_context_emit(*args, **kwargs)

    def observed_build_report(*args, **kwargs):
        finish_scan.set()
        return real_build_report(*args, **kwargs)

    @contextmanager
    def observed_control_lock(root, key):
        expected_key = f"session:{started['session']['session_id']}"
        if current_thread().ident == finisher_ident["value"] and key == expected_key:
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
            lock_path = store / "state" / f".lifecycle-{digest}.lock"
            with lock_path.open("a+", encoding="utf-8") as probe:
                try:
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    probe_blocked["value"] = True
                else:
                    probe_blocked["value"] = False
                    fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            finish_lock_probe.set()
        with real_control_lock(root, key):
            yield

    def finish() -> dict[str, object]:
        finisher_ident["value"] = current_thread().ident
        return control.finish_work(store, run_health_check=False)

    monkeypatch.setattr(context, "emit_event", paused_context_emit)
    monkeypatch.setattr(control, "build_final_report", observed_build_report)
    monkeypatch.setattr(control, "lifecycle_lock", observed_control_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        emitted_future = pool.submit(context.emit_context_pack, store, pack)
        assert emitter_inside.wait(5)
        finish_future = pool.submit(finish)
        assert finish_lock_probe.wait(5)
        try:
            assert probe_blocked["value"] is True
        finally:
            release_emitter.set()
        emitted = emitted_future.result(timeout=5)
        try:
            finish_future.result(timeout=5)
        except AgentDirStateError as exc:
            assert "Context review is pending" in str(exc)
        else:
            raise AssertionError("finish unexpectedly ignored the newly emitted pending pack")

    audit = json.loads(
        run_cli("audit", "context", "--pack", emitted.manifest["pack_id"], "--json", cwd=repo).stdout
    )
    current = json.loads(run_cli("session", "current", "--json", cwd=repo).stdout)
    assert finish_scan.is_set()
    assert audit["review_status"] == "pending"
    assert current["session_id"] == started["session"]["session_id"]


def test_session_start_reservation_orders_context_creation(tmp_path: Path, monkeypatch) -> None:
    import fcntl
    import hashlib
    from contextlib import contextmanager
    from threading import current_thread

    import agentdir.context as context
    import agentdir.sessions as sessions

    repo = init_repo(tmp_path / "repo")
    store = repo / ".agentdir"
    run_cli("init", str(store))
    session_id = "ordered-session"
    pack = context.build_context_pack(store, "ordered context creation", session_id=session_id)
    start_paused = Event()
    release_start = Event()
    emitter_probe_complete = Event()
    emitter_ident: dict[str, int | None] = {"value": None}
    probe_blocked: dict[str, bool | None] = {"value": None}
    real_write_session_state = sessions.write_session_state
    real_context_lock = context.lifecycle_lock

    def paused_write_session_state(*args, **kwargs):
        start_paused.set()
        if not release_start.wait(5):
            raise AssertionError("timed out waiting to release session start")
        return real_write_session_state(*args, **kwargs)

    @contextmanager
    def observed_context_lock(root, key):
        if current_thread().ident == emitter_ident["value"] and key == f"session:{session_id}":
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
            lock_path = store / "state" / f".lifecycle-{digest}.lock"
            with lock_path.open("a+", encoding="utf-8") as probe:
                try:
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    probe_blocked["value"] = True
                else:
                    probe_blocked["value"] = False
                    fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            emitter_probe_complete.set()
        with real_context_lock(root, key):
            yield

    def start():
        return sessions.start_session(store, session_id=session_id, cwd=repo)

    def emit():
        emitter_ident["value"] = current_thread().ident
        return context.emit_context_pack(store, pack)

    monkeypatch.setattr(sessions, "write_session_state", paused_write_session_state)
    monkeypatch.setattr(context, "lifecycle_lock", observed_context_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        start_future = pool.submit(start)
        assert start_paused.wait(5)
        emit_future = pool.submit(emit)
        assert emitter_probe_complete.wait(5)
        try:
            assert probe_blocked["value"] is True
        finally:
            release_start.set()
        started = start_future.result(timeout=5)
        emitted = emit_future.result(timeout=5)

    run_cli("index", "update", "--root", str(store))
    lifecycle_events = [
        row["event_type"]
        for row in query_rows(store)
        if row["session_id"] == session_id
        and row["event_type"] in {"session.started", "context.pack.created"}
    ]
    assert started.session_id == session_id
    assert emitted.manifest["session_id"] == session_id
    assert lifecycle_events == ["session.started", "context.pack.created"]


def test_context_pack_emission_is_rejected_after_finish_wins(tmp_path: Path, monkeypatch) -> None:
    import fcntl
    import hashlib
    from contextlib import contextmanager
    from threading import current_thread

    import agentdir.context as context
    import agentdir.control as control
    from agentdir.store import AgentDirStateError

    repo = init_repo(tmp_path / "repo")
    started = json.loads(
        run_cli("work", "start", "finish wins baseline", "--no-context", "--json", cwd=repo).stdout
    )
    store = repo / ".agentdir"
    pack = context.build_context_pack(
        store,
        "late context pack",
        session_id=started["session"]["session_id"],
    )
    finish_scanned = Event()
    release_finish = Event()
    emitter_lock_probe = Event()
    creation_reached = Event()
    emitter_ident: dict[str, int | None] = {"value": None}
    probe_blocked: dict[str, bool | None] = {"value": None}
    real_build_report = control.build_final_report
    real_context_emit = context.emit_event
    real_context_lock = context.lifecycle_lock

    def paused_build_report(*args, **kwargs):
        report = real_build_report(*args, **kwargs)
        finish_scanned.set()
        if not release_finish.wait(5):
            raise AssertionError("timed out waiting to release finish")
        return report

    def observed_context_emit(*args, **kwargs):
        if kwargs.get("event_type") == "context.pack.created":
            creation_reached.set()
        return real_context_emit(*args, **kwargs)

    @contextmanager
    def observed_context_lock(root, key):
        expected_key = f"session:{started['session']['session_id']}"
        if current_thread().ident == emitter_ident["value"] and key == expected_key:
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
            lock_path = store / "state" / f".lifecycle-{digest}.lock"
            with lock_path.open("a+", encoding="utf-8") as probe:
                try:
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    probe_blocked["value"] = True
                else:
                    probe_blocked["value"] = False
                    fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            emitter_lock_probe.set()
        with real_context_lock(root, key):
            yield

    def emit_late_pack():
        emitter_ident["value"] = current_thread().ident
        return context.emit_context_pack(store, pack)

    monkeypatch.setattr(control, "build_final_report", paused_build_report)
    monkeypatch.setattr(context, "emit_event", observed_context_emit)
    monkeypatch.setattr(context, "lifecycle_lock", observed_context_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        finish_future = pool.submit(control.finish_work, store, run_health_check=False)
        assert finish_scanned.wait(5)
        emitted_future = pool.submit(emit_late_pack)
        assert emitter_lock_probe.wait(5)
        try:
            assert probe_blocked["value"] is True
        finally:
            release_finish.set()
        finished = finish_future.result(timeout=5)
        try:
            emitted_future.result(timeout=5)
        except AgentDirStateError as exc:
            assert "ended session" in str(exc)
        else:
            raise AssertionError("a context pack was emitted after the session ended")

    run_cli("index", "update", cwd=repo)
    created = [
        row
        for row in query_rows(store, "context.pack.created")
        if row["session_id"] == started["session"]["session_id"]
    ]
    assert finished["ended_session"]["session_id"] == started["session"]["session_id"]
    assert len(created) == 1
    assert not creation_reached.is_set()


def test_finish_cannot_end_a_concurrently_started_replacement_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import fcntl
    import hashlib
    from contextlib import contextmanager
    from threading import current_thread

    import agentdir.control as control
    import agentdir.sessions as sessions

    repo = init_repo(tmp_path / "repo")
    first = json.loads(
        run_cli("work", "start", "session A", "--no-context", "--json", cwd=repo).stdout
    )
    first_session_id = first["session"]["session_id"]
    store = repo / ".agentdir"
    finish_paused = Event()
    release_finish = Event()
    pointer_probe_complete = Event()
    starter_ident: dict[str, int | None] = {"value": None}
    probe_blocked: dict[str, bool | None] = {"value": None}
    real_control_emit = control.emit_event
    real_pointer_lock = sessions.session_pointer_lock

    def paused_finish_emit(*args, **kwargs):
        if kwargs.get("event_type") == "work.report.final":
            finish_paused.set()
            if not release_finish.wait(5):
                raise AssertionError("timed out waiting to release finish")
        return real_control_emit(*args, **kwargs)

    @contextmanager
    def observed_pointer_lock(root):
        if current_thread().ident == starter_ident["value"]:
            digest = hashlib.sha256(b"active-session-pointer").hexdigest()[:24]
            lock_path = store / "state" / f".lifecycle-{digest}.lock"
            with lock_path.open("a+", encoding="utf-8") as probe:
                try:
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    probe_blocked["value"] = True
                else:
                    probe_blocked["value"] = False
                    fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            pointer_probe_complete.set()
        with real_pointer_lock(root):
            yield

    def start_replacement():
        starter_ident["value"] = current_thread().ident
        return sessions.start_session(
            store,
            session_id="session-B",
            title="session B",
            cwd=repo,
        )

    monkeypatch.setattr(control, "emit_event", paused_finish_emit)
    monkeypatch.setattr(sessions, "session_pointer_lock", observed_pointer_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        finish_future = pool.submit(control.finish_work, store, run_health_check=False)
        assert finish_paused.wait(5)
        start_future = pool.submit(start_replacement)
        assert pointer_probe_complete.wait(5)
        try:
            assert probe_blocked["value"] is True
        finally:
            release_finish.set()
        finished = finish_future.result(timeout=5)
        replacement = start_future.result(timeout=5)

    run_cli("index", "update", cwd=repo)
    ended = query_rows(store, "session.ended")
    current = json.loads(run_cli("session", "current", "--json", cwd=repo).stdout)
    assert finished["ended_session"]["session_id"] == first_session_id
    assert replacement.session_id == "session-B"
    assert current["session_id"] == "session-B"
    assert [row["session_id"] for row in ended].count(first_session_id) == 1
    assert [row["session_id"] for row in ended].count("session-B") == 0


def test_work_start_cannot_partially_mutate_a_session_being_finished(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import fcntl
    import hashlib
    from contextlib import contextmanager
    from threading import current_thread

    import agentdir.control as control
    import agentdir.sessions as sessions

    repo = init_repo(tmp_path / "repo")
    first = json.loads(
        run_cli("work", "start", "first task", "--no-context", "--json", cwd=repo).stdout
    )
    first_session_id = first["session"]["session_id"]
    store = repo / ".agentdir"
    finish_scanned = Event()
    release_finish = Event()
    start_probe_complete = Event()
    starter_ident: dict[str, int | None] = {"value": None}
    probe_blocked: dict[str, bool | None] = {"value": None}
    real_build_report = control.build_final_report
    real_lifecycle_lock = control.lifecycle_lock

    def paused_build_report(*args, **kwargs):
        report = real_build_report(*args, **kwargs)
        finish_scanned.set()
        if not release_finish.wait(5):
            raise AssertionError("timed out waiting to release finish")
        return report

    @contextmanager
    def observed_lifecycle_lock(root, key):
        if current_thread().ident == starter_ident["value"] and key == "work-start":
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
            lock_path = store / "state" / f".lifecycle-{digest}.lock"
            with lock_path.open("a+", encoding="utf-8") as probe:
                try:
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    probe_blocked["value"] = True
                else:
                    probe_blocked["value"] = False
                    fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            start_probe_complete.set()
        with real_lifecycle_lock(root, key):
            yield

    def start_replacement_work():
        starter_ident["value"] = current_thread().ident
        return control.start_work(store, "replacement task", emit_context=False)

    monkeypatch.setattr(control, "build_final_report", paused_build_report)
    monkeypatch.setattr(control, "lifecycle_lock", observed_lifecycle_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        finish_future = pool.submit(control.finish_work, store, run_health_check=False)
        assert finish_scanned.wait(5)
        start_future = pool.submit(start_replacement_work)
        assert start_probe_complete.wait(5)
        try:
            assert probe_blocked["value"] is True
        finally:
            release_finish.set()
        finished = finish_future.result(timeout=5)
        replacement = start_future.result(timeout=5)

    run_cli("index", "update", cwd=repo)
    work_starts = query_rows(store, "work.started")
    current = sessions.read_current_session(store)
    replacement_session_id = replacement["session"]["session_id"]
    assert finished["ended_session"]["session_id"] == first_session_id
    assert replacement_session_id != first_session_id
    assert current is not None and current.session_id == replacement_session_id
    assert [row["session_id"] for row in work_starts].count(first_session_id) == 1
    assert [row["session_id"] for row in work_starts].count(replacement_session_id) == 1


def test_work_start_serializes_direct_active_session_replacement(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import fcntl
    import hashlib
    from contextlib import contextmanager
    from threading import current_thread

    import agentdir.control as control
    import agentdir.sessions as sessions

    repo = init_repo(tmp_path / "repo")
    store = repo / ".agentdir"
    start_paused = Event()
    release_start = Event()
    pointer_probe_complete = Event()
    replacement_ident: dict[str, int | None] = {"value": None}
    probe_blocked: dict[str, bool | None] = {"value": None}
    real_emit_pack = control.emit_context_pack
    real_pointer_lock = sessions.session_pointer_lock

    def paused_emit_pack(*args, **kwargs):
        start_paused.set()
        if not release_start.wait(5):
            raise AssertionError("timed out waiting to release work start")
        return real_emit_pack(*args, **kwargs)

    @contextmanager
    def observed_pointer_lock(root):
        if current_thread().ident == replacement_ident["value"]:
            digest = hashlib.sha256(b"active-session-pointer").hexdigest()[:24]
            lock_path = store / "state" / f".lifecycle-{digest}.lock"
            with lock_path.open("a+", encoding="utf-8") as probe:
                try:
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    probe_blocked["value"] = True
                else:
                    probe_blocked["value"] = False
                    fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            pointer_probe_complete.set()
        with real_pointer_lock(root):
            yield

    def replace_session():
        replacement_ident["value"] = current_thread().ident
        return sessions.start_session(
            store,
            session_id="replacement-session",
            cwd=repo,
        )

    monkeypatch.setattr(control, "emit_context_pack", paused_emit_pack)
    monkeypatch.setattr(sessions, "session_pointer_lock", observed_pointer_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        work_future = pool.submit(control.start_work, store, "serialized start", emit_context=False)
        assert start_paused.wait(5)
        replacement_future = pool.submit(replace_session)
        assert pointer_probe_complete.wait(5)
        try:
            assert probe_blocked["value"] is True
        finally:
            release_start.set()
        started = work_future.result(timeout=5)
        replacement = replacement_future.result(timeout=5)

    run_cli("index", "update", cwd=repo)
    created = query_rows(store, "context.pack.created")
    work_starts = query_rows(store, "work.started")
    current = json.loads(run_cli("session", "current", "--json", cwd=repo).stdout)
    assert len(created) == 1
    assert [row["session_id"] for row in work_starts] == [started["session"]["session_id"]]
    assert replacement.session_id == "replacement-session"
    assert current["session_id"] == "replacement-session"
