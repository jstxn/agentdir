from __future__ import annotations

import ast
import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agentdir import jev, secrets
from agentdir.actors import send_message
from agentdir.capture import run_tool
from agentdir.envelope import envelope_bytes, parse_envelope
from agentdir.events import emit_event
from agentdir.index import rebuild_index
from agentdir.mailbox import atomic_deliver
from agentdir.memory import configure_context_reranker
from agentdir.redaction import looks_secret_bearing, redact_text, secret_labels
from agentdir.store import ensure_mailbox, init_root, session_mailbox


def _write_raw_envelope(
    root: Path,
    session_id: str,
    *,
    subject: str,
    body: str,
    extra_headers: dict[str, str] | None = None,
    archived: bool = False,
) -> Path:
    init_root(root)
    if archived:
        mailbox = ensure_mailbox(root / "archives" / "sessions" / session_id / "Maildir")
    else:
        mailbox = session_mailbox(root, session_id)
    msg = EmailMessage()
    msg["Message-ID"] = f"<{time.time_ns()}@agentdir.local>"
    msg["Date"] = "Fri, 08 May 2026 15:45:00 -0000"
    msg["From"] = "agent@agentdir.local"
    msg["To"] = f"{session_id}@agentdir.local"
    msg["Subject"] = subject
    msg["X-AgentDir-Version"] = "0.1"
    msg["X-AgentDir-Event-Type"] = "tool.result"
    msg["X-AgentDir-Session"] = session_id
    for name, value in (extra_headers or {}).items():
        msg[name] = value
    msg.set_content(body, subtype="plain", charset="utf-8")
    return atomic_deliver(mailbox, envelope_bytes(msg))


def test_pem_complete_truncated_and_multiple_blocks_redacted(tmp_path: Path) -> None:
    del tmp_path
    complete = (
        "before\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAn9SecretBase64LineOne\n"
        "SecretBase64LineTwo==\n"
        "-----END RSA PRIVATE KEY-----\n"
        "after"
    )
    res = redact_text(complete)
    assert res.text == "before\n<redacted:private-key>\nafter"
    assert res.replacements == 1
    assert res.labels == ("private-key",)
    assert "SecretBase64" not in res.text

    truncated = (
        "header\n"
        "-----BEGIN EC PRIVATE KEY-----\n"
        "MHcCAQEEIPartialTruncatedBodyLine"
    )
    res_trunc = redact_text(truncated)
    assert res_trunc.text == "header\n<redacted:private-key>"
    assert res_trunc.replacements == 1
    assert "PartialTruncatedBodyLine" not in res_trunc.text

    multi = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjE=\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
        "keep-this-middle-text\n"
        "-----BEGIN PKCS8 PRIVATE KEY-----\n"
        "MIIBVQIBADANBgkqhkiG9w0BAQ==\n"
        "-----END PKCS8 PRIVATE KEY-----"
    )
    res_multi = redact_text(multi)
    assert res_multi.replacements == 2
    assert "keep-this-middle-text" in res_multi.text
    assert "b3BlbnNzaC1rZXktdjE=" not in res_multi.text
    assert "MIIBVQIBADANBgkqhkiG9w0BAQ==" not in res_multi.text


def test_json_and_dict_quoted_key_value_secrets_redacted(tmp_path: Path) -> None:
    del tmp_path
    json_payload = '{"api_key": "sk_live_1234567890abcdef", "password": "supersecretpassword123"}'
    res = redact_text(json_payload)
    assert res.replacements == 2
    assert res.labels == ("key-value-secret",)
    assert "sk_live_1234567890abcdef" not in res.text
    assert "supersecretpassword123" not in res.text
    assert res.text.count("<redacted:key-value-secret>") == 2

    py_dict = "{'token': 'tok_9876543210fedcba', 'client_secret': 'sec_1234567890abcdef'}"
    res_dict = redact_text(py_dict)
    assert res_dict.replacements == 2
    assert "tok_9876543210fedcba" not in res_dict.text
    assert "sec_1234567890abcdef" not in res_dict.text

    escaped_json = r'{\"api_key\": \"sk_live_escaped_1234567890\"}'
    res_escaped = redact_text(escaped_json)
    assert res_escaped.replacements == 1
    assert "sk_live_escaped_1234567890" not in res_escaped.text

    harmless = '{"token_count": 123456789012345, "secret_findings": "none_at_all_here"}'
    assert not looks_secret_bearing(harmless)
    assert redact_text(harmless).replacements == 0


def test_emit_event_redacts_subject_headers_merges_labels_and_honors_no_redact(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    gh_token = "ghp_1234567890abcdefghij1234"
    aws_key = "AKIAIOSFODNN7EXAMPLE"
    kv_secret = "API_KEY=sk_test_1234567890abcdef"
    digest = "a" * 64

    emitted = emit_event(
        root,
        session_id="s1",
        event_type="context.pack.created",
        subject=f"deploy with {gh_token}",
        body=f"payload with {aws_key}",
        extra_headers={
            "X-AgentDir-Context-Query": f"fix {kv_secret}",
            "X-AgentDir-Summary": f"note {gh_token}",
            "X-AgentDir-Blob-SHA256": digest,
            "X-AgentDir-Pack-Id": "ctx-20260508T154500Z-1234567890ab",
            "X-AgentDir-Redactions": "1",
            "X-AgentDir-Redaction-Labels": "custom-prior",
        },
    )
    parsed = parse_envelope(emitted.path)
    assert gh_token not in (parsed.header("Subject") or "")
    assert "<redacted:github-token>" in (parsed.header("Subject") or "")
    assert aws_key not in parsed.body_text
    assert "<redacted:aws-access-key>" in parsed.body_text
    assert "sk_test_1234567890abcdef" not in (parsed.header("X-AgentDir-Context-Query") or "")
    assert "<redacted:key-value-secret>" in (parsed.header("X-AgentDir-Context-Query") or "")
    assert gh_token not in (parsed.header("X-AgentDir-Summary") or "")
    assert parsed.header("X-AgentDir-Blob-SHA256") == digest
    assert parsed.header("X-AgentDir-Pack-Id") == "ctx-20260508T154500Z-1234567890ab"
    assert parsed.header("X-AgentDir-Redactions") == "5"
    labels = (parsed.header("X-AgentDir-Redaction-Labels") or "").split(",")
    assert labels == ["custom-prior", "aws-access-key", "github-token", "key-value-secret"]

    unredacted = emit_event(
        root,
        session_id="s1",
        event_type="tool.result",
        subject=f"raw {gh_token}",
        body=f"raw {kv_secret}",
        extra_headers={"X-AgentDir-Context-Query": f"raw {aws_key}"},
        redact=False,
    )
    parsed_raw = parse_envelope(unredacted.path)
    assert gh_token in (parsed_raw.header("Subject") or "")
    assert kv_secret in parsed_raw.body_text
    assert aws_key in (parsed_raw.header("X-AgentDir-Context-Query") or "")
    assert parsed_raw.header("X-AgentDir-Redactions") is None


def test_send_message_redacts_subject_and_merges_redaction_headers(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    gh_token = "ghp_1234567890abcdefghij1234"
    aws_key = "AKIAIOSFODNN7EXAMPLE"

    inbox_path, outbox_path = send_message(
        root=root,
        from_actor="worker",
        to_actor="reviewer",
        event_type="handoff.note",
        subject=f"review {gh_token}",
        body=f"key is {aws_key}",
    )
    for path in (inbox_path, outbox_path):
        parsed = parse_envelope(path)
        assert gh_token not in (parsed.header("Subject") or "")
        assert "<redacted:github-token>" in (parsed.header("Subject") or "")
        assert aws_key not in parsed.body_text
        assert parsed.header("X-AgentDir-Redactions") == "2"
        assert parsed.header("X-AgentDir-Redaction-Labels") == "aws-access-key,github-token"


def test_capture_preserves_labels_and_honors_no_redact(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    secret_val = "sk_test_1234567890abcdef"
    aws_key = "AKIAIOSFODNN7EXAMPLE"

    res_redacted = run_tool(
        root,
        argv=[
            sys.executable,
            "-c",
            f"print('{aws_key}')",
            f"API_KEY={secret_val}",
        ],
        session_id="s-redact",
        tool_name="python",
        cwd=tmp_path,
        redact=True,
    )
    assert res_redacted.exit_code == 0
    assert res_redacted.event_path is not None
    parsed = parse_envelope(Path(res_redacted.event_path))
    assert secret_val not in parsed.body_text
    assert aws_key not in parsed.body_text
    labels = set((parsed.header("X-AgentDir-Redaction-Labels") or "").split(","))
    assert labels == {"key-value-secret", "aws-access-key"}
    assert int(parsed.header("X-AgentDir-Redactions") or "0") >= 2

    # Ensure argv line stays a valid literal list with <redacted:key-value-secret> intact.
    argv_line = next(line for line in parsed.body_text.splitlines() if line.startswith("argv="))
    parsed_argv = ast.literal_eval(argv_line.removeprefix("argv="))
    assert parsed_argv[-1] == "<redacted:key-value-secret>"

    res_raw = run_tool(
        root,
        argv=[
            sys.executable,
            "-c",
            f"print('API_KEY={secret_val}')",
        ],
        session_id="s-noredact",
        tool_name="python",
        cwd=tmp_path,
        redact=False,
    )
    assert res_raw.exit_code == 0
    assert res_raw.event_path is not None
    parsed_raw = parse_envelope(Path(res_raw.event_path))
    assert f"API_KEY={secret_val}" in parsed_raw.body_text
    assert parsed_raw.header("X-AgentDir-Redactions") == "0"
    assert parsed_raw.header("X-AgentDir-Redaction-Labels") is None

    # Verify tool.call also honored redact=False.
    maildir_files = sorted((session_mailbox(root, "s-noredact") / "new").iterdir())
    call_files = [
        parse_envelope(path)
        for path in maildir_files
        if parse_envelope(path).header("X-AgentDir-Event-Type") == "tool.call"
    ]
    assert call_files and secret_val in call_files[0].body_text


def test_secrets_scan_parsed_cache_headers_errors_and_no_blob_walk(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    gh_token = "ghp_1234567890abcdefghij1234"
    aws_key = "AKIAIOSFODNN7EXAMPLE"

    subject_path = _write_raw_envelope(
        root,
        "s1",
        subject=f"subject leak {gh_token}",
        body="clean body",
        extra_headers={"X-AgentDir-Context-Query": f"query {aws_key}"},
    )
    archived_path = _write_raw_envelope(
        root,
        "archived-s1",
        subject="archived secret",
        body="API_KEY=sk_test_1234567890abcdef",
        archived=True,
    )
    # Decoy Maildir inside artifacts/blobs/sha256 must never be scanned.
    decoy_maildir = ensure_mailbox(root / "artifacts" / "blobs" / "sha256" / "ab" / "cd" / "Maildir")
    decoy_msg = EmailMessage()
    decoy_msg["Message-ID"] = "<decoy@agentdir.local>"
    decoy_msg["Subject"] = "decoy"
    decoy_msg.set_content("API_KEY=sk_decoy_1234567890abcdef")
    atomic_deliver(decoy_maildir, envelope_bytes(decoy_msg))

    # Add an unreadable/broken record to verify (OSError, ValueError) resilience.
    broken_path = session_mailbox(root, "s1") / "new" / "broken-file"
    broken_path.write_bytes(b"placeholder")

    preparsed = {subject_path: parse_envelope(subject_path)}
    real_parse = secrets.parse_envelope
    parsed_calls: list[Path] = []

    def guarded_parse(path: Path):
        parsed_calls.append(path)
        if path == subject_path:
            raise AssertionError("pre-parsed envelope should not be parsed twice")
        if path == broken_path:
            raise OSError("simulated unreadable record")
        return real_parse(path)

    with patch.object(secrets, "parse_envelope", side_effect=guarded_parse):
        findings = secrets.scan_secret_records(root, parsed_by_path=preparsed)

    found_paths = {item.path for item in findings}
    assert str(subject_path.relative_to(root)) in found_paths
    assert str(archived_path.relative_to(root)) in found_paths
    assert not any("artifacts/blobs" in item.path for item in findings)
    assert broken_path in parsed_calls

    subject_finding = next(item for item in findings if item.path == str(subject_path.relative_to(root)))
    assert set(subject_finding.labels) == {"github-token", "aws-access-key"}
    assert gh_token not in (subject_finding.subject or "")


def test_secrets_redact_multipart_headers_and_finally_rebuilds_index(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    gh_token = "ghp_1234567890abcdefghij1234"
    aws_key = "AKIAIOSFODNN7EXAMPLE"
    kv_secret = "API_KEY=sk_test_1234567890abcdef"
    binary_payload = b"\x89PNG\r\n\x1a\nraw-binary-bytes"

    mailbox = session_mailbox(root, "s-multi")
    msg = EmailMessage()
    msg["Message-ID"] = "<multipart@agentdir.local>"
    msg["Date"] = "Fri, 08 May 2026 15:45:00 -0000"
    msg["From"] = "agent@agentdir.local"
    msg["To"] = "s-multi@agentdir.local"
    msg["Subject"] = f"multipart {aws_key}"
    msg["X-AgentDir-Version"] = "0.1"
    msg["X-AgentDir-Event-Type"] = "tool.result"
    msg["X-AgentDir-Session"] = "s-multi"
    msg["X-AgentDir-Context-Query"] = f"query {gh_token}"
    msg.set_content(f"plain body {kv_secret}", subtype="plain", charset="utf-8")
    msg.add_alternative(f"<p>html body {gh_token}</p>", subtype="html", charset="utf-8")
    msg.add_attachment(binary_payload, maintype="application", subtype="octet-stream", filename="blob.bin")
    multi_path = atomic_deliver(mailbox, envelope_bytes(msg))
    rebuild_index(root)

    result = secrets.redact_secret_records(root, apply=True)
    assert result["count"] == 1
    assert result["index_rebuilt"] is True

    rewritten = parse_envelope(multi_path)
    assert rewritten.message.is_multipart()
    assert aws_key not in (rewritten.header("Subject") or "")
    assert gh_token not in (rewritten.header("X-AgentDir-Context-Query") or "")
    assert "sk_test_1234567890abcdef" not in rewritten.body_text
    assert gh_token not in rewritten.body_text
    attachments = [
        part.get_payload(decode=True)
        for part in rewritten.message.walk()
        if part.get_content_type() == "application/octet-stream"
    ]
    assert attachments == [binary_payload]
    assert secrets.scan_secret_records(root) == []

    # Now verify try...finally rebuilds index if a later file fails after an earlier rewrite.
    root2 = tmp_path / "store-partial"
    init_root(root2)
    first_secret = "sk_first_1234567890abcdef"
    second_secret = "sk_second_1234567890abcdef"
    first_path = _write_raw_envelope(root2, "s1", subject="first", body=f"API_KEY={first_secret}")
    second_path = _write_raw_envelope(root2, "s2", subject="second", body=f"API_KEY={second_secret}")
    rebuild_index(root2)

    real_rewrite = secrets._rewrite_body

    def fail_on_second(path: Path, message, body, labels, replacements):
        if path == second_path:
            raise OSError("disk full on second file")
        return real_rewrite(path, message, body, labels, replacements)

    with patch.object(secrets, "_rewrite_body", side_effect=fail_on_second):
        try:
            secrets.redact_secret_records(root2, apply=True)
            raise AssertionError("expected OSError from second file")
        except OSError as exc:
            assert "disk full" in str(exc)

    assert first_secret not in parse_envelope(first_path).body_text
    with sqlite3.connect(root2 / "indexes" / "agentdir.sqlite3") as conn:
        leaked_count = conn.execute(
            "select count(*) from messages where body_text like ?",
            (f"%{first_secret}%",),
        ).fetchone()[0]
    assert leaked_count == 0


def test_jev_scrubs_both_api_keys_private_key_bodies_and_metadata_headers(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)
    configure_context_reranker(root, "jev")

    ts_key = "typesafe-super-secret-key-111"
    jev_key = "jev-secondary-secret-key-222"
    rows = [
        {
            "source_id": "message:0",
            "source_kind": "message",
            "subject": "dual key check",
            "body_text": f"uses {ts_key} and {jev_key}",
            "passage_body_text": (
                "message: <msg-0@agentdir.local>\n"
                "session: private-session-99\n"
                "event: decision.recorded\n"
                "workspace: secret-workspace-name\n"
                "git: deadbeefcafe0123\n"
                "type: note\n"
                "actor: alice\n"
                "tool: pytest\n"
                "subject: dual key check\n"
                f"actual advice with {ts_key} and {jev_key}"
            ),
            "memory_score": 0.95,
        },
        {
            "source_id": "message:1",
            "source_kind": "message",
            "subject": "legacy partial pem",
            "body_text": "<redacted:private-key>\nMIIEvQIBADANLegacyBase64Body\n-----END PRIVATE KEY-----",
            "passage_body_text": "MIIEvQIBADANLegacyBase64Body",
            "memory_score": 0.90,
        },
        {
            "source_id": "message:2",
            "source_kind": "message",
            "subject": "multi passage pem",
            "body_text": "-----BEGIN RSA PRIVATE KEY-----\nMultiPassageKeyChunkA\nMultiPassageKeyChunkB\n-----END RSA PRIVATE KEY-----",
            "passage_body_text": "MultiPassageKeyChunkB",
            "memory_score": 0.85,
        },
        {
            "source_id": "message:3",
            "source_kind": "message",
            "subject": "end marker only",
            "body_text": "OrphanedPemTail\n-----END EC PRIVATE KEY-----",
            "memory_score": 0.80,
        },
    ]

    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured.update(json.loads(kwargs["input"]))
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "model": jev.MODEL,
                    "answers": {
                        f"p{i}": {"type": "noul", "noul": 0.9}
                        for i in range(len(rows))
                    },
                    "usage": {"input_tokens": 20, "output_tokens": 4},
                }
            )
        )

    fake_subprocess = SimpleNamespace(**{**vars(subprocess), "run": fake_run})
    env_patch = {"TYPESAFE_API_KEY": ts_key, "JEV_API_KEY": jev_key}
    with patch.dict(os.environ, env_patch, clear=False), patch.object(jev, "subprocess", fake_subprocess):
        _selected, record = jev.filter_context_hits(
            root,
            f"task referencing {ts_key} and {jev_key}",
            rows,
        )

    assert record["status"] == "applied"
    exported = json.dumps(captured)
    for forbidden in (
        ts_key,
        jev_key,
        "private-session-99",
        "secret-workspace-name",
        "deadbeefcafe0123",
        "msg-0@agentdir.local",
        "MIIEvQIBADANLegacyBase64Body",
        "MultiPassageKeyChunkB",
        "OrphanedPemTail",
    ):
        assert forbidden not in exported, f"leaked {forbidden!r} in Jev export"

    candidates = captured["state"]["candidates"]  # type: ignore[index]
    assert "actual advice with <redacted:api-key> and <redacted:api-key>" in candidates["p0"]
    assert candidates["p1"] == "<redacted:private-key>"
    assert candidates["p2"] == "<redacted:private-key>"
    assert candidates["p3"] == "<redacted:private-key>"


def _pid_is_dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.is_file():
        try:
            content = stat_path.read_text(encoding="utf-8", errors="replace")
            state = content.rsplit(")", 1)[1].split()[0]
            if state in {"Z", "X"}:
                return True
        except (OSError, IndexError):
            return True
    return False


def test_capture_timeout_kills_process_group_and_bounds_lingering_pipes(tmp_path: Path) -> None:
    if os.name != "posix":
        return
    root = tmp_path / "store"
    init_root(root)
    child_pid_file = tmp_path / "grandchild.pid"

    # Grandchild ignores SIGTERM so this also verifies SIGKILL escalation on the process group.
    grandchild_code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    script = (
        "import subprocess, sys\n"
        f"p = subprocess.Popen([sys.executable, '-c', {grandchild_code!r}])\n"
        f"open({str(child_pid_file)!r}, 'w').write(str(p.pid))\n"
        "p.wait()\n"
    )
    started = time.monotonic()
    result = run_tool(
        root,
        argv=[sys.executable, "-c", script],
        session_id="s-pgkill",
        tool_name="pgtest",
        cwd=tmp_path,
        timeout=0.5,
    )
    elapsed = time.monotonic() - started
    assert result.timed_out is True
    assert result.exit_code == 124
    assert elapsed < 5.0

    grandchild_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
    dead = False
    for _ in range(20):
        if _pid_is_dead(grandchild_pid):
            dead = True
            break
        time.sleep(0.05)
    assert dead, f"grandchild process {grandchild_pid} survived timeout killpg"

    # Now spawn a detached descendant (new session) holding stdout open; join must stay bounded.
    detached_pid_file = tmp_path / "detached.pid"
    detached_script = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'], start_new_session=True)\n"
        f"open({str(detached_pid_file)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(30)\n"
    )
    started_detached = time.monotonic()
    try:
        res_detached = run_tool(
            root,
            argv=[sys.executable, "-c", detached_script],
            session_id="s-detached",
            tool_name="detached",
            cwd=tmp_path,
            timeout=0.4,
        )
        elapsed_detached = time.monotonic() - started_detached
        assert res_detached.timed_out is True
        assert res_detached.exit_code == 124
        assert elapsed_detached < 4.0
    finally:
        if detached_pid_file.is_file():
            try:
                os.kill(int(detached_pid_file.read_text(encoding="utf-8").strip()), signal.SIGKILL)
            except OSError:
                pass


def test_capture_permission_error_is_directory_and_signal_exit_codes(tmp_path: Path) -> None:
    root = tmp_path / "store"
    init_root(root)

    # Non-executable file -> PermissionError -> exit code 126 with paired tool.result.
    non_exec = tmp_path / "not_executable.sh"
    non_exec.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    non_exec.chmod(stat.S_IRUSR | stat.S_IWUSR)

    perm_res = run_tool(
        root,
        argv=[str(non_exec)],
        session_id="s-perm",
        cwd=tmp_path,
    )
    assert perm_res.exit_code == 126
    assert perm_res.event_path is not None
    perm_parsed = parse_envelope(Path(perm_res.event_path))
    assert perm_parsed.header("X-AgentDir-Event-Type") == "tool.result"
    assert perm_parsed.header("X-AgentDir-Tool-Exit-Code") == "126"
    events_in_session = [
        parse_envelope(path).header("X-AgentDir-Event-Type")
        for path in sorted((session_mailbox(root, "s-perm") / "new").iterdir())
    ]
    assert "tool.call" in events_in_session and "tool.result" in events_in_session

    # Explicit IsADirectoryError -> exit code 126 with paired tool.result.
    with patch("agentdir.capture.subprocess.Popen", side_effect=IsADirectoryError(21, "Is a directory", str(tmp_path))):
        dir_res = run_tool(
            root,
            argv=[str(tmp_path)],
            session_id="s-isdir",
            cwd=tmp_path,
        )
    assert dir_res.exit_code == 126
    assert dir_res.event_path is not None
    dir_parsed = parse_envelope(Path(dir_res.event_path))
    assert dir_parsed.header("X-AgentDir-Tool-Exit-Code") == "126"
    assert "exit_code=126" in dir_parsed.body_text

    # Negative POSIX signal return code normalization (-9 -> 137, -15 -> 143).
    if os.name == "posix":
        sigkill_res = run_tool(
            root,
            argv=[sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"],
            session_id="s-sigkill",
            tool_name="sigkill-check",
            cwd=tmp_path,
        )
        assert sigkill_res.exit_code == 128 + signal.SIGKILL
        assert sigkill_res.event_path is not None
        sigkill_parsed = parse_envelope(Path(sigkill_res.event_path))
        assert sigkill_parsed.header("X-AgentDir-Tool-Exit-Code") == str(128 + signal.SIGKILL)
        assert f"exit_code={128 + signal.SIGKILL}" in sigkill_parsed.body_text

        sigterm_res = run_tool(
            root,
            argv=[sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"],
            session_id="s-sigterm",
            tool_name="sigterm-check",
            cwd=tmp_path,
        )
        assert sigterm_res.exit_code == 128 + signal.SIGTERM
