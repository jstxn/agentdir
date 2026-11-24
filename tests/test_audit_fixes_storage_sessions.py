from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from agentdir import doctor, envelope, replay, retention, sessions, store
from agentdir.artifacts import add_artifact, artifact_path
from agentdir.events import emit_event
from agentdir.index import rebuild_index
from agentdir.query import query_messages
from agentdir.store import AgentDirError, AgentDirStateError, init_root, validate_id


def _init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git_env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    subprocess.run(["git", "init"], cwd=path, env=git_env, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "dev@example.test"], cwd=path, env=git_env, check=True)
    subprocess.run(["git", "config", "user.name", "Dev User"], cwd=path, env=git_env, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=path, env=git_env, check=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, env=git_env, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, env=git_env, check=True, capture_output=True)
    return path


def _add_worktree(repo: Path, dest: Path, branch: str) -> Path:
    subprocess.run(
        ["git", "worktree", "add", str(dest), "-b", branch],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return dest


def _write_raw_envelope(
    destination: Path,
    *,
    message_id: str,
    event_type: str = "tool.result",
    subject: str = "event",
    body: str = "body",
    session_id: str = "sess-1",
    date_header: str = "Fri, 08 May 2026 15:45:00 -0000",
    from_actor: str = "agent@agentdir.local",
    to_actor: str = "sess-1@agentdir.local",
    extra_headers: dict[str, str] | None = None,
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    msg = EmailMessage()
    msg["Message-ID"] = message_id
    msg["Date"] = date_header
    msg["From"] = from_actor
    msg["To"] = to_actor
    msg["Subject"] = subject
    msg["X-AgentDir-Version"] = "0.1"
    msg["X-AgentDir-Event-Type"] = event_type
    msg["X-AgentDir-Session"] = session_id
    for name, value in (extra_headers or {}).items():
        msg[name] = value
    msg.set_content(body)
    destination.write_bytes(msg.as_bytes())
    return destination


def _assert_raises(exc_type: type[BaseException], fn, *args, **kwargs) -> BaseException:
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        return exc
    raise AssertionError(f"Expected {exc_type.__name__} to be raised")


# --- Fix 1: Artifacts & Doctor blob safety ----------------------------------


def test_artifact_path_rejects_invalid_and_traversal_digests(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    valid = "a" * 64

    resolved = artifact_path(root, valid)
    assert resolved == root.resolve() / "artifacts" / "blobs" / "sha256" / "aa" / "aa" / valid

    for bad in (
        "../etc/passwd",
        "../../" + "a" * 58,
        "A" * 64,
        "a" * 63,
        "a" * 65,
        valid + "\n",
        "g" * 64,
        "",
    ):
        _assert_raises(AgentDirError, artifact_path, root, bad)


def test_add_artifact_single_pass_atomic_and_cleans_temp_on_failure(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    source = tmp_path / "payload.bin"
    payload = b"streamed-content-" * 1024
    source.write_bytes(payload)
    expected_sha = hashlib.sha256(payload).hexdigest()

    open_count = 0
    real_open = Path.open

    def counting_open(self: Path, *args, **kwargs):
        nonlocal open_count
        if self == source.resolve():
            open_count += 1
        return real_open(self, *args, **kwargs)

    with patch.object(Path, "open", counting_open):
        first = add_artifact(root, source)

    assert open_count == 1
    assert first.sha256 == expected_sha
    assert first.bytes == len(payload)
    assert first.path.read_bytes() == payload

    # Deduplicating add leaves no temp files behind.
    second = add_artifact(root, source)
    assert second.sha256 == expected_sha
    blobs_root = root / "artifacts" / "blobs" / "sha256"
    assert not list(blobs_root.glob(".artifact-*.tmp"))

    # Failed replacement cleans up the temp file.
    other = tmp_path / "other.bin"
    other.write_bytes(b"other-payload")
    with patch("agentdir.artifacts.os.replace", side_effect=OSError("simulated disk error")):
        _assert_raises(OSError, add_artifact, root, other)
    assert not list(blobs_root.glob(".artifact-*.tmp"))
    other_sha = hashlib.sha256(b"other-payload").hexdigest()
    assert not artifact_path(root, other_sha).exists()


def test_doctor_rejects_invalid_sha_symlink_nonregular_and_escaping_blobs(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    maildir_new = root / "sessions" / "sess-1" / "Maildir" / "new"
    (root / "sessions" / "sess-1" / "Maildir" / "tmp").mkdir(parents=True, exist_ok=True)
    (root / "sessions" / "sess-1" / "Maildir" / "cur").mkdir(parents=True, exist_ok=True)

    # 1. Invalid sha256 header must report an error, not crash.
    _write_raw_envelope(
        maildir_new / "01-bad-sha",
        message_id="<bad-sha@agentdir.local>",
        extra_headers={"X-AgentDir-Blob-SHA256": "../../etc/passwd"},
    )

    # 2. Symlink blob must be rejected.
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside data", encoding="utf-8")
    symlink_sha = hashlib.sha256(outside.read_bytes()).hexdigest()
    symlink_blob = artifact_path(root, symlink_sha)
    symlink_blob.parent.mkdir(parents=True, exist_ok=True)
    symlink_blob.symlink_to(outside)
    _write_raw_envelope(
        maildir_new / "02-symlink",
        message_id="<symlink@agentdir.local>",
        extra_headers={"X-AgentDir-Blob-SHA256": symlink_sha},
    )

    # 3. Non-regular file (directory at blob path) must be rejected.
    dir_sha = "1" * 64
    dir_blob = artifact_path(root, dir_sha)
    dir_blob.mkdir(parents=True, exist_ok=True)
    _write_raw_envelope(
        maildir_new / "03-dir",
        message_id="<dir@agentdir.local>",
        extra_headers={"X-AgentDir-Blob-SHA256": dir_sha},
    )

    # 4. Parent shard symlink escaping blobs/sha256 must be rejected.
    escape_dir = tmp_path / "escape-shard" / "22"
    escape_dir.mkdir(parents=True, exist_ok=True)
    escape_sha = "22" * 32
    (escape_dir / escape_sha).write_bytes(b"escaped")
    shard_link = root / "artifacts" / "blobs" / "sha256" / "22"
    shard_link.symlink_to(tmp_path / "escape-shard")
    _write_raw_envelope(
        maildir_new / "04-escape",
        message_id="<escape@agentdir.local>",
        extra_headers={"X-AgentDir-Blob-SHA256": escape_sha},
    )

    # 5. Valid large (>1 MiB) blob must stream-hash without Path.read_bytes on the blob.
    large_file = tmp_path / "large.bin"
    large_file.write_bytes(b"Z" * (2 * 1024 * 1024 + 17))
    stored = add_artifact(root, large_file)
    _write_raw_envelope(
        maildir_new / "05-large",
        message_id="<large@agentdir.local>",
        extra_headers={"X-AgentDir-Blob-SHA256": stored.sha256},
    )

    real_read_bytes = Path.read_bytes

    def guarded_read_bytes(self: Path) -> bytes:
        if self.resolve() == stored.path.resolve():
            raise AssertionError("doctor called Path.read_bytes() on artifact blob")
        return real_read_bytes(self)

    with patch.object(Path, "read_bytes", guarded_read_bytes):
        report = doctor.run_doctor(root)

    assert not report.ok
    joined = "\n".join(report.errors)
    assert "invalid artifact blob ../../etc/passwd" in joined
    assert f"artifact blob is a symlink {symlink_sha}" in joined
    assert f"artifact blob is not a regular file {dir_sha}" in joined
    assert f"artifact blob escapes artifact store {escape_sha}" in joined
    assert stored.sha256 not in joined


# --- Fix 2: Retention multi-worktree protection & locking -------------------


def test_retention_protects_all_active_worktree_sessions_from_outside_checkout(
    tmp_path: Path,
) -> None:
    repo = _init_git_repo(tmp_path / "main-repo")
    store_root = repo / ".agentdir"
    init_root(store_root)
    worktree = _add_worktree(repo, tmp_path / "feature-wt", "feature")
    outside = tmp_path / "outside"
    outside.mkdir()

    main_sess = sessions.start_session(store_root, session_id="main-active", cwd=repo)
    wt_sess = sessions.start_session(store_root, session_id="wt-active", cwd=worktree)
    emit_event(store_root, session_id="inactive-old", event_type="note.test", body="inactive")
    rebuild_index(store_root)

    with patch("pathlib.Path.cwd", return_value=outside):
        # Explicit targeting of either active worktree session is refused.
        _assert_raises(
            AgentDirError,
            retention.archive_sessions,
            store_root,
            sessions=[main_sess.session_id],
            apply=True,
        )
        _assert_raises(
            AgentDirError,
            retention.archive_sessions,
            store_root,
            sessions=[wt_sess.session_id],
            apply=True,
        )

        # Age-based archive protects both active worktree sessions and moves the inactive one.
        archive_res = retention.archive_sessions(store_root, older_than_days=0, apply=True)
        assert {c.session_id for c in archive_res.protected} == {"main-active", "wt-active"}
        assert archive_res.changed == ["inactive-old:sessions->archives"]
        assert (store_root / "sessions" / "main-active").is_dir()
        assert (store_root / "sessions" / "wt-active").is_dir()
        assert (store_root / "archives" / "sessions" / "inactive-old").is_dir()

        # Live prune also protects both active sessions across worktrees.
        emit_event(store_root, session_id="inactive-live", event_type="note.test", body="live")
        prune_res = retention.prune_sessions(
            store_root,
            older_than_days=0,
            include_live_sessions=True,
            apply=True,
        )
        assert {c.session_id for c in prune_res.protected} == {"main-active", "wt-active"}
        assert "inactive-live:sessions->deleted" in prune_res.changed
        assert "inactive-old:archives->deleted" in prune_res.changed
        assert (store_root / "sessions" / "main-active").is_dir()
        assert (store_root / "sessions" / "wt-active").is_dir()


def test_retention_fails_closed_on_corrupt_pointer_files(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    emit_event(root, session_id="sess-1", event_type="note.test", body="hello")

    # Corrupt legacy pointer fails closed.
    legacy = root / "state" / "current-session.json"
    legacy.write_text("{not-json", encoding="utf-8")
    _assert_raises(AgentDirStateError, retention.archive_sessions, root, older_than_days=0, apply=True)
    assert (root / "sessions" / "sess-1").is_dir()
    legacy.unlink()

    # Corrupt workspace-scoped pointer fails closed.
    ws_pointer = root / "state" / "workspaces" / "checkout-wt" / "current-session.json"
    ws_pointer.parent.mkdir(parents=True, exist_ok=True)
    ws_pointer.write_text("[]", encoding="utf-8")
    _assert_raises(AgentDirStateError, retention.prune_sessions, root, older_than_days=0, include_live_sessions=True, apply=True)
    assert (root / "sessions" / "sess-1").is_dir()

    # Active pointer missing session_id fails closed.
    ws_pointer.write_text(json.dumps({"status": "active"}), encoding="utf-8")
    _assert_raises(AgentDirStateError, retention.archive_sessions, root, older_than_days=0, apply=True)


def test_retention_rechecks_active_under_lock_and_rebuilds_index_on_partial_failure(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    init_root(root)
    emit_event(root, session_id="sess-a", event_type="note.test", body="alpha")
    emit_event(root, session_id="sess-b", event_type="note.test", body="beta")
    rebuild_index(root)

    # 1. Re-checking active status inside the lock skips a newly activated session.
    calls = {"count": 0}

    def fake_active_ids(_root: Path) -> set[str]:
        calls["count"] += 1
        return set() if calls["count"] == 1 else {"sess-a"}

    with patch("agentdir.retention._active_session_ids", side_effect=fake_active_ids):
        res = retention.archive_sessions(root, older_than_days=0, apply=True)
    assert (root / "sessions" / "sess-a").is_dir()
    assert (root / "archives" / "sessions" / "sess-b").is_dir()
    assert {c.session_id for c in res.protected} == {"sess-a"}
    assert res.changed == ["sess-b:sessions->archives"]

    # 2. Mid-batch archive failure still rebuilds the index for sessions already moved.
    emit_event(root, session_id="sess-c", event_type="note.test", body="gamma")
    rebuild_index(root)
    real_rename = Path.rename

    def fail_on_c(self: Path, target: Path):
        if self.name == "sess-c":
            raise OSError("rename failed")
        return real_rename(self, target)

    with patch.object(Path, "rename", fail_on_c):
        _assert_raises(
            OSError,
            retention.archive_sessions,
            root,
            sessions=["sess-a", "sess-c"],
            apply=True,
        )
    assert not (root / "sessions" / "sess-a").exists()
    assert (root / "sessions" / "sess-c").is_dir()
    assert query_messages(root, session_id="sess-a") == []
    assert len(query_messages(root, session_id="sess-c")) == 1

    # 3. Mid-batch live prune failure still rebuilds the index for sessions already deleted.
    emit_event(root, session_id="sess-d", event_type="note.test", body="delta")
    rebuild_index(root)
    real_rmtree = retention.shutil.rmtree

    def fail_rmtree_on_d(path, *args, **kwargs):
        if Path(path).name == "sess-d":
            raise OSError("rmtree failed")
        return real_rmtree(path, *args, **kwargs)

    with patch("agentdir.retention.shutil.rmtree", side_effect=fail_rmtree_on_d):
        _assert_raises(
            OSError,
            retention.prune_sessions,
            root,
            sessions=["sess-c", "sess-d"],
            include_live_sessions=True,
            apply=True,
        )
    assert not (root / "sessions" / "sess-c").exists()
    assert (root / "sessions" / "sess-d").is_dir()
    assert query_messages(root, session_id="sess-c") == []
    assert len(query_messages(root, session_id="sess-d")) == 1


# --- Fix 3: ID/slug sanitization, rollback & envelope CR/LF safety ----------


def test_validate_id_rejects_trailing_newline_and_queue_mailbox_removed(tmp_path: Path) -> None:
    assert validate_id("session-1") == "session-1"
    _assert_raises(AgentDirError, validate_id, "session-1\n")
    _assert_raises(AgentDirError, validate_id, "session-1\r\n")
    _assert_raises(AgentDirError, validate_id, "\nsession-1")
    assert not hasattr(store, "queue_mailbox")


def test_safe_session_id_ascii_and_leading_punctuation(tmp_path: Path) -> None:
    assert sessions.safe_session_id("café") == "caf"
    assert sessions.safe_session_id("日本語") == "agent-session"
    assert sessions.safe_session_id("_private") == "private"
    assert sessions.safe_session_id("@org/repo") == "org-repo"
    assert sessions.safe_session_id("+plus") == "plus"
    assert sessions.safe_session_id("...___@@@+++---") == "agent-session"
    assert sessions.safe_session_id("line1\nline2") == "line1-line2"
    long_id = sessions.safe_session_id("_" + "x" * 160)
    assert len(long_id) == 128
    assert validate_id(long_id) == long_id


def test_start_session_rolls_back_directory_and_pointer_when_emit_fails(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "repo")
    store_root = repo / ".agentdir"
    init_root(store_root)

    # No prior active session: failed emit leaves no session dir and no active pointer.
    with patch("agentdir.sessions.emit_event", side_effect=RuntimeError("boom")):
        _assert_raises(
            RuntimeError,
            sessions.start_session,
            store_root,
            session_id="failed-start",
            title="broken start",
            cwd=repo,
        )
    assert not (store_root / "sessions" / "failed-start").exists()
    assert sessions.read_current_session(store_root) is None
    assert not (store_root / "state" / "current-session.json").exists()

    # Existing active session: failed start of a second session preserves the first pointer.
    first = sessions.start_session(store_root, session_id="first-ok", title="first", cwd=repo)
    with patch("agentdir.sessions.emit_event", side_effect=RuntimeError("boom-2")):
        _assert_raises(
            RuntimeError,
            sessions.start_session,
            store_root,
            session_id="second-fail",
            title="second",
            cwd=repo,
        )
    assert not (store_root / "sessions" / "second-fail").exists()
    with patch("pathlib.Path.cwd", return_value=repo):
        current = sessions.read_current_session(store_root)
    assert current is not None
    assert current.session_id == first.session_id


def test_envelope_sanitizes_crlf_and_normalizes_malformed_actor_addresses(tmp_path: Path) -> None:
    assert envelope.actor_address("codex@agentdir.local") == "codex@agentdir.local"
    assert envelope.actor_address("alice@example.com") == "alice@example.com"
    assert envelope.actor_address("codex") == "codex@agentdir.local"

    for malformed in (
        "user@",
        "@example.com",
        "a@b, c@d",
        "alice@example.com\r\nBcc: eve@evil.com",
        "bad actor\nwith newline",
    ):
        addr = envelope.actor_address(malformed)
        assert "\r" not in addr and "\n" not in addr
        assert "," not in addr
        assert addr.count("@") == 1
        assert addr.endswith("@agentdir.local")

    msg = envelope.build_envelope(
        event_type="session.started",
        body="hello body",
        subject="Line one\r\nLine two\nLine three",
        from_actor="alice@example.com\r\nBcc: evil@example.net",
        to_actor="a@b, c@d",
        session_id="sess-1",
        workspace="ws\nname",
        tool="pytest\r\nrun",
        extra_headers={
            "X-Note": "first\r\nsecond",
            "X-Multi": ["one\ntwo", "three\rfour"],
        },
    )
    raw = envelope.envelope_bytes(msg)
    out = tmp_path / "env.eml"
    out.write_bytes(raw)
    parsed = envelope.parse_envelope(out)

    assert parsed.header("Subject") == "Line one Line two Line three"
    assert parsed.header("X-Note") == "first second"
    assert parsed.headers("X-Multi") == ["one two", "three four"]
    assert parsed.header("Bcc") is None
    assert "," not in (parsed.header("To") or "")
    assert envelope.validate_required(parsed) == []

    _assert_raises(
        AgentDirError,
        envelope.build_envelope,
        event_type="note.test",
        body="x",
        extra_headers={"Bad\nName": "val"},
    )
    _assert_raises(
        AgentDirError,
        envelope.build_envelope,
        event_type="note.test",
        body="x",
        extra_headers={"Bad:Name": "val"},
    )


# --- Fix 4: Doctor single-pass, tmp scan, index header secrets & split text -


def test_doctor_reuses_parsed_envelopes_and_skips_blob_tree_for_tmp_scan(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    emitted = emit_event(root, session_id="sess-1", event_type="note.test", body="clean body")

    # Place real tmp files in live and archived sessions plus a decoy inside artifacts/blobs.
    live_tmp = root / "sessions" / "sess-1" / "Maildir" / "tmp" / "partial-live"
    live_tmp.write_text("partial", encoding="utf-8")
    arch_tmp = root / "archives" / "sessions" / "sess-old" / "Maildir" / "tmp" / "partial-arch"
    arch_tmp.parent.mkdir(parents=True, exist_ok=True)
    arch_tmp.write_text("partial", encoding="utf-8")
    decoy_tmp = root / "artifacts" / "blobs" / "sha256" / "aa" / "bb" / "Maildir" / "tmp" / "decoy"
    decoy_tmp.parent.mkdir(parents=True, exist_ok=True)
    decoy_tmp.write_text("decoy", encoding="utf-8")

    captured_cache: dict[Path, envelope.ParsedEnvelope] = {}

    def fake_scan(scan_root: Path, parsed_by_path: dict[Path, envelope.ParsedEnvelope] | None = None):
        assert parsed_by_path is not None
        captured_cache.update(parsed_by_path)
        return []

    with patch("agentdir.doctor.scan_secret_records", side_effect=fake_scan):
        report = doctor.run_doctor(root)

    assert emitted.path.resolve() in captured_cache
    joined_warnings = "\n".join(report.warnings)
    assert "sessions/sess-1/Maildir/tmp/partial-live" in joined_warnings
    assert "archives/sessions/sess-old/Maildir/tmp/partial-arch" in joined_warnings
    assert "decoy" not in joined_warnings


def test_doctor_detects_secrets_in_indexed_subject_and_headers(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    emit_event(root, session_id="sess-1", event_type="note.test", subject="clean", body="harmless")
    rebuild_index(root)
    index_db = root / "indexes" / "agentdir.sqlite3"

    # Stale secret in messages.subject only (envelope on disk is clean).
    conn = sqlite3.connect(index_db)
    conn.execute(
        "update messages set subject = ?",
        ("Investigate API_KEY=sk_test_1234567890abcdef",),
    )
    conn.commit()
    conn.close()
    report_subj = doctor.run_doctor(root)
    assert not report_subj.ok
    assert any("messages.id=" in err for err in report_subj.errors)

    # Restore clean subject and inject stale secret in headers.value only.
    rebuild_index(root)
    conn = sqlite3.connect(index_db)
    rowid = conn.execute("select id from messages limit 1").fetchone()[0]
    conn.execute(
        "insert into headers(message_rowid, name, value) values (?, ?, ?)",
        (rowid, "X-AgentDir-Context-Query", "token=ghp_1234567890abcdefghij1234"),
    )
    conn.commit()
    conn.close()
    report_hdr = doctor.run_doctor(root)
    assert not report_hdr.ok
    assert any("headers.message_rowid=" in err for err in report_hdr.errors)


def test_doctor_split_worktree_warning_suggests_roots_register(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "main-repo")
    init_root(repo / ".agentdir")
    worktree = _add_worktree(repo, tmp_path / "feature-wt", "feature")
    wt_store = worktree / ".agentdir"
    init_root(wt_store)

    report = doctor.run_doctor(wt_store)
    split_warnings = [w for w in report.warnings if "separate from the main working tree store" in w]
    assert len(split_warnings) == 1
    assert "agentdir roots register " in split_warnings[0]
    assert "agentdir roots add " not in split_warnings[0]


# --- Fix 5: Query & Replay reliability --------------------------------------


def test_query_messages_auto_builds_missing_or_empty_index(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    emit_event(root, session_id="sess-1", event_type="note.test", body="fresh store event")
    index_db = root / "indexes" / "agentdir.sqlite3"
    assert not index_db.exists()

    rows = query_messages(root, session_id="sess-1")
    assert len(rows) == 1
    assert "fresh store event" in rows[0]["body_text"]

    # 0-byte index file triggers automatic rebuild.
    index_db.write_bytes(b"")
    rows_empty = query_messages(root, session_id="sess-1")
    assert len(rows_empty) == 1

    # Valid SQLite DB missing the messages table triggers automatic rebuild.
    index_db.unlink()
    conn = sqlite3.connect(index_db)
    conn.execute("create table dummy (id integer primary key)")
    conn.commit()
    conn.close()
    rows_missing_table = query_messages(root, session_id="sess-1")
    assert len(rows_missing_table) == 1


def test_query_messages_normalizes_since_until_timestamps(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    maildir_new = root / "sessions" / "sess-1" / "Maildir" / "new"
    (root / "sessions" / "sess-1" / "Maildir" / "tmp").mkdir(parents=True, exist_ok=True)
    (root / "sessions" / "sess-1" / "Maildir" / "cur").mkdir(parents=True, exist_ok=True)

    _write_raw_envelope(
        maildir_new / "01-may07",
        message_id="<may07@agentdir.local>",
        subject="may07",
        date_header="Thu, 07 May 2026 23:00:00 -0000",
    )
    _write_raw_envelope(
        maildir_new / "02-may08",
        message_id="<may08@agentdir.local>",
        subject="may08",
        date_header="Fri, 08 May 2026 15:45:00 -0000",
    )
    _write_raw_envelope(
        maildir_new / "03-may09",
        message_id="<may09@agentdir.local>",
        subject="may09",
        date_header="Sat, 09 May 2026 01:00:00 -0000",
    )
    rebuild_index(root)

    # Trailing Z normalizes to +00:00 so exact boundary matches.
    z_rows = query_messages(root, since="2026-05-08T15:45:00Z", until="2026-05-08T15:45:00Z")
    assert [r["subject"] for r in z_rows] == ["may08"]

    # Date-only bounds span the entire UTC day.
    day_rows = query_messages(root, since="2026-05-08", until="2026-05-08")
    assert [r["subject"] for r in day_rows] == ["may08"]

    # Non-UTC offset normalizes to UTC.
    offset_rows = query_messages(
        root,
        since="2026-05-08T17:45:00+02:00",
        until="2026-05-08T17:45:00+02:00",
    )
    assert [r["subject"] for r in offset_rows] == ["may08"]


def test_query_messages_escapes_like_wildcards_and_scopes_actor_matching(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    emit_event(root, session_id="s1", event_type="note.test", from_actor="agent", body="100% done")
    emit_event(root, session_id="s1", event_type="note.test", from_actor="agent-2", body="1000 done")
    emit_event(root, session_id="s1", event_type="note.test", from_actor="agent_1", body="foo_bar")
    emit_event(root, session_id="s1", event_type="note.test", from_actor="agentX1", body="fooxbar")
    emit_event(root, session_id="s1", event_type="note.test", from_actor="other", body=r"path\to\file")
    emit_event(root, session_id="s1", event_type="note.test", from_actor="other", body="pathtofile")
    rebuild_index(root)

    # Text LIKE escaping for %, _, and \.
    pct = query_messages(root, text="100%")
    assert [r["body_text"].strip() for r in pct] == ["100% done"]

    under = query_messages(root, text="foo_bar")
    assert [r["body_text"].strip() for r in under] == ["foo_bar"]

    slash = query_messages(root, text=r"path\to")
    assert [r["body_text"].strip() for r in slash] == [r"path\to\file"]

    # Actor matching does not prefix-match distinct actor names or treat _ as wildcard.
    agent_rows = query_messages(root, actor="agent")
    assert [r["from_actor"] for r in agent_rows] == ["agent@agentdir.local"]

    agent_underscore = query_messages(root, actor="agent_1")
    assert [r["from_actor"] for r in agent_underscore] == ["agent_1@agentdir.local"]

    exact_addr = query_messages(root, actor="agent@agentdir.local")
    assert [r["from_actor"] for r in exact_addr] == ["agent@agentdir.local"]


def test_query_messages_limit_none_and_replay_unbounded(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    maildir_new = root / "sessions" / "big-sess" / "Maildir" / "new"
    (root / "sessions" / "big-sess" / "Maildir" / "tmp").mkdir(parents=True, exist_ok=True)
    (root / "sessions" / "big-sess" / "Maildir" / "cur").mkdir(parents=True, exist_ok=True)
    for idx in range(105):
        _write_raw_envelope(
            maildir_new / f"msg-{idx:03d}",
            message_id=f"<msg-{idx}@agentdir.local>",
            subject=f"ev-{idx}",
            body=f"body-{idx}",
            session_id="big-sess",
        )
    rebuild_index(root)

    default_rows = query_messages(root, session_id="big-sess")
    assert len(default_rows) == 100

    all_rows = query_messages(root, session_id="big-sess", limit=None)
    assert len(all_rows) == 105

    replayed = replay.replay_session(root, "big-sess")
    assert len(replayed) == 105

    captured_limits: list[int | None] = []
    real_query = query_messages

    def spy_query(*args, **kwargs):
        captured_limits.append(kwargs.get("limit", "MISSING"))
        return real_query(*args, **kwargs)

    with patch("agentdir.replay.query_messages", side_effect=spy_query):
        replay.replay_session(root, "big-sess")
    assert captured_limits == [None]
