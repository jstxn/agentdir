from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from email.message import EmailMessage
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
    env_extra: dict[str, str] | None = None,
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


def write_body(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def session_maildir(root: Path, session_id: str) -> Path:
    return root / "sessions" / session_id / "Maildir"


def actor_inbox_maildir(root: Path, actor_id: str) -> Path:
    return root / "actors" / actor_id / "inbox" / "Maildir"


def ensure_maildir(maildir: Path) -> Path:
    for name in ("tmp", "new", "cur"):
        (maildir / name).mkdir(parents=True, exist_ok=True)
    return maildir


def parse_message(path: Path) -> EmailMessage:
    with path.open("rb") as handle:
        return BytesParser(policy=default).parse(handle)


def visible_messages(maildir: Path) -> list[Path]:
    return sorted((maildir / "new").glob("*")) + sorted((maildir / "cur").glob("*"))


def index_path(root: Path) -> Path:
    return root / "indexes" / "agentdir.sqlite3"


def fetch_scalar(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> object:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(sql, params).fetchone()
    assert row is not None
    return row[0]


def write_envelope(
    destination: Path,
    *,
    message_id: str,
    event_type: str,
    subject: str,
    body: str,
    session_id: str | None = None,
    date_header: str = "Fri, 08 May 2026 15:45:00 -0000",
    from_actor: str = "codex@agentdir.local",
    to_actor: str = "session@agentdir.local",
    extra_headers: dict[str, str] | None = None,
) -> Path:
    message = EmailMessage()
    message["Message-ID"] = message_id
    message["Date"] = date_header
    message["From"] = from_actor
    message["To"] = to_actor
    message["Subject"] = subject
    message["X-AgentDir-Version"] = "0.1"
    message["X-AgentDir-Event-Type"] = event_type
    if session_id is not None:
        message["X-AgentDir-Session"] = session_id
    for name, value in (extra_headers or {}).items():
        message[name] = value
    message.set_content(body)
    destination.write_bytes(message.as_bytes())
    return destination


def test_init_creates_v1_root_layout(tmp_path: Path) -> None:
    root = tmp_path / "store"

    run_cli("init", str(root))

    assert (root / "VERSION").is_file()
    config = root / "config.json"
    assert config.is_file()
    json.loads(config.read_text(encoding="utf-8"))
    assert (root / "sessions").is_dir()
    assert (root / "actors").is_dir()
    assert (root / "artifacts" / "blobs" / "sha256").is_dir()
    assert (root / "archives" / "sessions").is_dir()
    assert (root / "indexes").is_dir()
    assert (root / "state").is_dir()
    assert (root / "hooks").is_dir()
    assert (root / "integrations").is_dir()


def test_project_scope_defaults_to_repo_hidden_agentdir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    body = write_body(tmp_path / "body.txt", "project scoped event")

    run_cli("init", cwd=repo)
    result = run_cli("root", cwd=repo)
    run_cli("emit", "--session", "session-1", "--type", "agent.message", "--body", str(body), cwd=repo)
    run_cli("index", "rebuild", cwd=repo)

    project_root = repo / ".agentdir"
    assert result.stdout.strip() == str(project_root)
    assert (project_root / "VERSION").is_file()
    assert visible_messages(session_maildir(project_root, "session-1"))


def test_user_and_machine_scopes_resolve_without_explicit_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    machine = tmp_path / "machine"
    home.mkdir()

    user_result = run_cli("init", "--scope", "user", env_extra={"HOME": str(home)})
    machine_result = run_cli(
        "init",
        "--scope",
        "machine",
        env_extra={"AGENTDIR_MACHINE_ROOT": str(machine)},
    )

    user_root = Path(user_result.stdout.strip())
    assert user_root.name == "AgentDir"
    assert home in user_root.parents
    assert (user_root / "VERSION").is_file()
    assert machine_result.stdout.strip() == str(machine)
    assert (machine / "VERSION").is_file()


def test_emit_publishes_a_parseable_envelope_into_session_new(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "body.txt", "hello from emit")

    run_cli(
        "emit",
        "--root",
        str(root),
        "--session",
        "session-1",
        "--type",
        "agent.message",
        "--body",
        str(body),
        "--subject",
        "hello subject",
    )

    delivered = visible_messages(session_maildir(root, "session-1"))
    assert len(delivered) == 1
    message = parse_message(delivered[0])
    assert message["Subject"] == "hello subject"
    assert message["X-AgentDir-Event-Type"] == "agent.message"
    assert message["X-AgentDir-Session"] == "session-1"
    assert "hello from emit" in message.get_body(preferencelist=("plain",)).get_content()


def test_index_rebuild_ignores_partial_files_left_in_tmp(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "body.txt", "visible event")
    run_cli(
        "emit",
        "--root",
        str(root),
        "--session",
        "session-1",
        "--type",
        "session.started",
        "--body",
        str(body),
    )
    partial = session_maildir(root, "session-1") / "tmp" / "partial-message"
    partial.write_text("Message-ID: <partial@agentdir.local>\n", encoding="utf-8")

    run_cli("index", "rebuild", "--root", str(root))

    db_path = index_path(root)
    assert fetch_scalar(db_path, "select count(*) from messages") == 1
    assert fetch_scalar(
        db_path,
        "select count(*) from messages where file_path like ?",
        ("%/tmp/%",),
    ) == 0


def test_index_rebuild_recreates_database_after_index_deletion(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    first = write_body(tmp_path / "first.txt", "first event")
    second = write_body(tmp_path / "second.txt", "second event")
    run_cli("emit", "--root", str(root), "--session", "session-1", "--type", "user.message", "--body", str(first))
    run_cli("emit", "--root", str(root), "--session", "session-1", "--type", "agent.message", "--body", str(second))

    run_cli("index", "rebuild", "--root", str(root))
    db_path = index_path(root)
    assert fetch_scalar(db_path, "select count(*) from messages") == 2

    db_path.unlink()
    run_cli("index", "rebuild", "--root", str(root))

    assert fetch_scalar(db_path, "select count(*) from messages") == 2


def test_archive_dry_run_and_apply_moves_session_out_of_active_index(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "body.txt", "archive candidate event")
    run_cli(
        "emit",
        "--root",
        str(root),
        "--session",
        "session-1",
        "--type",
        "agent.message",
        "--body",
        str(body),
    )
    run_cli("index", "rebuild", "--root", str(root))

    dry_run = run_cli("archive", "--root", str(root), "--session", "session-1", "--json")
    payload = json.loads(dry_run.stdout)

    assert payload["dry_run"] is True
    assert payload["selected"][0]["session_id"] == "session-1"
    assert (root / "sessions" / "session-1").is_dir()
    assert not (root / "archives" / "sessions" / "session-1").exists()

    applied = run_cli(
        "archive",
        "--root",
        str(root),
        "--session",
        "session-1",
        "--apply",
        "--json",
    )
    applied_payload = json.loads(applied.stdout)

    assert applied_payload["dry_run"] is False
    assert applied_payload["rebuilt_index"] is True
    assert not (root / "sessions" / "session-1").exists()
    assert (root / "archives" / "sessions" / "session-1" / "Maildir").is_dir()
    assert fetch_scalar(index_path(root), "select count(*) from messages") == 0


def test_archive_refuses_to_move_the_current_active_session(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("session", "start", "--root", str(root), "--id", "active-session")

    result = run_cli(
        "archive",
        "--root",
        str(root),
        "--session",
        "active-session",
        "--apply",
        expected_returncode=2,
    )

    assert "refusing to archive active session" in result.stderr
    assert (root / "sessions" / "active-session").is_dir()


def test_prune_deletes_archived_sessions_only_when_applied(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "body.txt", "prune candidate event")
    run_cli(
        "emit",
        "--root",
        str(root),
        "--session",
        "session-1",
        "--type",
        "agent.message",
        "--body",
        str(body),
    )
    run_cli("archive", "--root", str(root), "--session", "session-1", "--apply")

    dry_run = run_cli("prune", "--root", str(root), "--session", "session-1", "--json")
    payload = json.loads(dry_run.stdout)

    assert payload["dry_run"] is True
    assert payload["selected"][0]["store"] == "archives"
    assert (root / "archives" / "sessions" / "session-1").is_dir()

    applied = run_cli(
        "prune",
        "--root",
        str(root),
        "--session",
        "session-1",
        "--apply",
        "--json",
    )
    applied_payload = json.loads(applied.stdout)

    assert applied_payload["dry_run"] is False
    assert applied_payload["changed"] == ["session-1:archives->deleted"]
    assert not (root / "archives" / "sessions" / "session-1").exists()


def test_prune_live_session_requires_explicit_live_store_flag(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "body.txt", "live prune candidate event")
    run_cli(
        "emit",
        "--root",
        str(root),
        "--session",
        "session-1",
        "--type",
        "agent.message",
        "--body",
        str(body),
    )
    run_cli("index", "rebuild", "--root", str(root))

    result = run_cli(
        "prune",
        "--root",
        str(root),
        "--session",
        "session-1",
        expected_returncode=2,
    )

    assert "unknown session id" in result.stderr
    assert (root / "sessions" / "session-1").is_dir()

    applied = run_cli(
        "prune",
        "--root",
        str(root),
        "--session",
        "session-1",
        "--include-live-sessions",
        "--apply",
        "--json",
    )
    applied_payload = json.loads(applied.stdout)

    assert applied_payload["rebuilt_index"] is True
    assert applied_payload["changed"] == ["session-1:sessions->deleted"]
    assert not (root / "sessions" / "session-1").exists()
    assert fetch_scalar(index_path(root), "select count(*) from messages") == 0


def test_query_filters_records_by_session(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    session_a = write_body(tmp_path / "session-a.txt", "only session a contains this marker")
    session_b = write_body(tmp_path / "session-b.txt", "only session b contains this marker")
    run_cli("emit", "--root", str(root), "--session", "session-a", "--type", "agent.message", "--body", str(session_a))
    run_cli("emit", "--root", str(root), "--session", "session-b", "--type", "agent.message", "--body", str(session_b))
    run_cli("index", "rebuild", "--root", str(root))

    result = run_cli("query", "--root", str(root), "--session", "session-a", "--json")
    rows = json.loads(result.stdout)

    assert len(rows) == 1
    assert rows[0]["session_id"] == "session-a"
    assert rows[0]["body_text"].strip() == "only session a contains this marker"


def test_query_filters_records_by_tool_git_head_workspace_and_time_range(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "tool.txt", "pytest output marker")
    run_cli(
        "emit",
        "--root",
        str(root),
        "--session",
        "session-1",
        "--type",
        "tool.result",
        "--tool",
        "pytest",
        "--git-head",
        "abc123",
        "--workspace",
        "demo-workspace",
        "--body",
        str(body),
    )
    run_cli("index", "rebuild", "--root", str(root))

    result = run_cli(
        "query",
        "--root",
        str(root),
        "--tool",
        "pytest",
        "--git-head",
        "abc123",
        "--workspace",
        "demo-workspace",
        "--since",
        "2026-05-08T00:00:00+00:00",
        "--json",
    )
    rows = json.loads(result.stdout)

    assert len(rows) == 1
    assert rows[0]["tool"] == "pytest"
    assert rows[0]["git_head"] == "abc123"
    assert rows[0]["workspace"] == "demo-workspace"


def test_replay_orders_session_events_by_envelope_metadata(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    maildir = ensure_maildir(session_maildir(root, "session-1",)) / "new"
    write_envelope(
        maildir / "msg-1",
        message_id="<first@agentdir.local>",
        event_type="tool.call",
        subject="first",
        body="first replay marker",
        session_id="session-1",
        date_header="Fri, 08 May 2026 15:45:00 -0000",
    )
    write_envelope(
        maildir / "msg-2",
        message_id="<second@agentdir.local>",
        event_type="tool.result",
        subject="second",
        body="second replay marker",
        session_id="session-1",
        date_header="Fri, 08 May 2026 15:46:00 -0000",
    )
    run_cli("index", "rebuild", "--root", str(root))

    result = run_cli("replay", "--root", str(root), "--session", "session-1")

    assert "tool.call first" in result.stdout
    assert "tool.result second" in result.stdout
    assert result.stdout.index("tool.call first") < result.stdout.index("tool.result second")


def test_actor_create_and_send_deliver_to_recipient_inbox(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    run_cli("actor", "create", "--root", str(root), "human")
    run_cli("actor", "create", "--root", str(root), "codex")
    body = write_body(tmp_path / "handoff.txt", "please review this change")

    run_cli(
        "send",
        "--root",
        str(root),
        "--from",
        "codex",
        "--to",
        "human",
        "--type",
        "approval.requested",
        "--body",
        str(body),
    )

    delivered = visible_messages(actor_inbox_maildir(root, "human"))
    assert len(delivered) == 1
    message = parse_message(delivered[0])
    assert message["From"] == "codex@agentdir.local"
    assert message["To"] == "human@agentdir.local"
    assert message["X-AgentDir-Event-Type"] == "approval.requested"
    assert "please review this change" in message.get_body(preferencelist=("plain",)).get_content()


def test_artifact_add_stores_blob_at_the_sha256_address(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("artifact payload", encoding="utf-8")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

    run_cli("artifact", "add", "--root", str(root), str(artifact))

    blob_path = root / "artifacts" / "blobs" / "sha256" / digest[:2] / digest[2:4] / digest
    assert blob_path.is_file()
    assert blob_path.read_text(encoding="utf-8") == "artifact payload"


def test_doctor_reports_a_healthy_root(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))

    run_cli("doctor", "--root", str(root))


def test_emit_redacts_secret_like_message_bodies_by_default(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "secret.txt", "API_KEY=sk_test_1234567890abcdef")
    run_cli("emit", "--root", str(root), "--session", "session-1", "--type", "tool.result", "--body", str(body))

    message = parse_message(visible_messages(session_maildir(root, "session-1"))[0])
    content = message.get_body(preferencelist=("plain",)).get_content()

    assert "<redacted:key-value-secret>" in content
    assert "sk_test_1234567890abcdef" not in content
    assert message["X-AgentDir-Redactions"] == "1"
    run_cli("doctor", "--root", str(root))


def test_doctor_errors_on_legacy_secret_like_message_bodies(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    maildir = ensure_maildir(session_maildir(root, "session-1")) / "new"
    write_envelope(
        maildir / "secret",
        message_id="<secret@agentdir.local>",
        event_type="tool.result",
        subject="legacy secret",
        body="API_KEY=sk_test_1234567890abcdef",
        session_id="session-1",
    )

    result = run_cli("doctor", "--root", str(root), expected_returncode=1)

    assert "secret-like" in result.stdout
    assert "agentdir secrets redact --apply" in result.stdout
    assert "sk_test_1234567890abcdef" not in result.stdout


def test_secrets_redact_rewrites_bodies_and_rebuilds_index(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    maildir = ensure_maildir(session_maildir(root, "session-1")) / "new"
    write_envelope(
        maildir / "secret",
        message_id="<secret@agentdir.local>",
        event_type="tool.result",
        subject="legacy secret",
        body="API_KEY=sk_test_1234567890abcdef",
        session_id="session-1",
    )
    run_cli("index", "rebuild", "--root", str(root))

    scan = run_cli("secrets", "scan", "--root", str(root), "--json", expected_returncode=1)
    scan_payload = json.loads(scan.stdout)
    assert scan_payload[0]["path"] == "sessions/session-1/Maildir/new/secret"
    assert "sk_test_1234567890abcdef" not in scan.stdout

    dry_run = run_cli("secrets", "redact", "--root", str(root))
    assert "Dry run only" in dry_run.stdout

    applied = run_cli("secrets", "redact", "--root", str(root), "--apply", "--json")
    payload = json.loads(applied.stdout)
    assert payload["count"] == 1
    assert payload["index_rebuilt"] is True

    message = parse_message(maildir / "secret")
    content = message.get_body(preferencelist=("plain",)).get_content()
    assert "<redacted:key-value-secret>" in content
    assert "sk_test_1234567890abcdef" not in content
    assert message["X-AgentDir-Secret-Redacted"] == "true"
    assert fetch_scalar(index_path(root), "select count(*) from messages where body_text like '%sk_test%'") == 0
    assert (
        fetch_scalar(index_path(root), "select count(*) from memory_documents where body_text like '%sk_test%'")
        == 0
    )
    assert (
        fetch_scalar(index_path(root), "select count(*) from memory_passages where body_text like '%sk_test%'")
        == 0
    )
    run_cli("doctor", "--root", str(root))


def test_doctor_ignores_passage_only_secret_like_false_positive(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    body = write_body(tmp_path / "body.txt", "harmless source body")
    run_cli("emit", "--root", str(root), "--session", "session-1", "--type", "tool.result", "--body", str(body))
    run_cli("index", "rebuild", "--root", str(root))

    with sqlite3.connect(index_path(root)) as conn:
        conn.execute(
            "update memory_passages set body_text = ? where id = (select min(id) from memory_passages)",
            ("API_KEY=sk_test_1234567890abcdef",),
        )

    run_cli("doctor", "--root", str(root))


def test_doctor_errors_on_conflicting_duplicate_message_ids(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    maildir = ensure_maildir(session_maildir(root, "session-1")) / "new"
    duplicate_id = "<conflict@agentdir.local>"
    write_envelope(
        maildir / "msg-1",
        message_id=duplicate_id,
        event_type="user.message",
        subject="first",
        body="first body",
        session_id="session-1",
    )
    write_envelope(
        maildir / "msg-2",
        message_id=duplicate_id,
        event_type="agent.message",
        subject="second",
        body="different body",
        session_id="session-1",
    )

    result = run_cli("doctor", "--root", str(root), expected_returncode=1)

    assert "conflicting duplicate Message-ID" in result.stdout


def test_index_rebuild_reports_duplicate_message_ids_without_aborting(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    maildir = ensure_maildir(session_maildir(root, "session-1")) / "new"
    duplicate_id = "<duplicate@agentdir.local>"
    write_envelope(
        maildir / "msg-1",
        message_id=duplicate_id,
        event_type="user.message",
        subject="first duplicate",
        body="first duplicate body",
        session_id="session-1",
    )
    write_envelope(
        maildir / "msg-2",
        message_id=duplicate_id,
        event_type="agent.message",
        subject="second duplicate",
        body="second duplicate body",
        session_id="session-1",
    )
    write_envelope(
        maildir / "msg-3",
        message_id="<unique@agentdir.local>",
        event_type="tool.result",
        subject="unique",
        body="unique body",
        session_id="session-1",
    )

    result = run_cli("index", "rebuild", "--root", str(root))
    payload = json.loads(result.stdout)

    assert duplicate_id in payload["duplicates"]
    assert fetch_scalar(index_path(root), "select count(*) from messages") == 3
    assert fetch_scalar(
        index_path(root),
        "select count(*) from messages where message_id = ?",
        (duplicate_id,),
    ) == 2


def test_index_rebuild_skips_malformed_envelopes_and_indexes_valid_ones(tmp_path: Path) -> None:
    root = tmp_path / "store"
    run_cli("init", str(root))
    maildir = ensure_maildir(session_maildir(root, "session-1")) / "new"
    write_envelope(
        maildir / "valid-message",
        message_id="<valid@agentdir.local>",
        event_type="tool.result",
        subject="valid",
        body="valid body",
        session_id="session-1",
    )
    (maildir / "malformed-message").write_text(
        "this is not an RFC 5322 message\nand has no required headers\n",
        encoding="utf-8",
    )

    result = run_cli("index", "rebuild", "--root", str(root))
    payload = json.loads(result.stdout)

    assert payload["malformed"] == 1
    assert fetch_scalar(
        index_path(root),
        "select count(*) from messages where malformed = 1",
    ) == 1
    assert fetch_scalar(
        index_path(root),
        "select count(*) from messages where message_id = ?",
        ("<valid@agentdir.local>",),
    ) == 1
