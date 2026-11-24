from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .events import emit_event
from .git import git_head, workspace_name
from .redaction import redact_text
from .sessions import ensure_session
from .store import AgentDirError

DEFAULT_MAX_CAPTURE_BYTES = 256 * 1024
TIMEOUT_EXIT_CODE = 124
CANNOT_EXECUTE_EXIT_CODE = 126
COMMAND_NOT_FOUND_EXIT_CODE = 127
_TERMINATE_GRACE_SECONDS = 0.2
_TIMEOUT_JOIN_SECONDS = 1.0


@dataclass
class CapturedText:
    text: str = ""
    truncated: bool = False
    had_output: bool = False
    ended_with_newline: bool = True


@dataclass(frozen=True)
class RunToolResult:
    exit_code: int
    session_id: str
    tool: str
    duration_ms: int
    truncated_streams: tuple[str, ...] = ()
    timed_out: bool = False
    event_path: str | None = None
    stderr_needs_newline: bool = False


def _append_limited(capture: CapturedText, chunk: str, max_bytes: int) -> None:
    if chunk:
        capture.had_output = True
        capture.ended_with_newline = chunk.endswith("\n")
    if capture.truncated:
        return
    current = len(capture.text.encode("utf-8", errors="replace"))
    remaining = max_bytes - current
    encoded = chunk.encode("utf-8", errors="replace")
    if len(encoded) <= remaining:
        capture.text += chunk
        return
    capture.text += encoded[: max(0, remaining)].decode("utf-8", errors="replace")
    capture.truncated = True


def _tee_stream(stream, target, capture: CapturedText, max_bytes: int) -> None:
    try:
        for chunk in iter(lambda: stream.readline(), ""):
            if chunk == "":
                break
            target.write(chunk)
            target.flush()
            _append_limited(capture, chunk, max_bytes)
    finally:
        stream.close()


def _redact_argv_and_command(
    argv: list[str],
    *,
    redact: bool,
) -> tuple[list[str], str, int, tuple[str, ...]]:
    if not redact:
        return list(argv), " ".join(argv), 0, ()
    stored_argv: list[str] = []
    replacements = 0
    labels: list[str] = []
    for arg in argv:
        res = redact_text(arg)
        stored_argv.append(res.text)
        replacements += res.replacements
        labels.extend(res.labels)
    command_res = redact_text(" ".join(stored_argv))
    replacements += command_res.replacements
    labels.extend(command_res.labels)
    return stored_argv, command_res.text, replacements, tuple(dict.fromkeys(labels))


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
        try:
            process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
        if process.poll() is None:
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
        return

    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass


def run_tool(
    root: str | Path,
    *,
    argv: list[str],
    session_id: str | None = None,
    tool_name: str | None = None,
    cwd: str | Path | None = None,
    max_capture_bytes: int = DEFAULT_MAX_CAPTURE_BYTES,
    redact: bool = True,
    timeout: float | None = None,
) -> RunToolResult:
    if not argv:
        raise AgentDirError("argv is required")
    if max_capture_bytes < 0:
        raise AgentDirError("max_capture_bytes must be non-negative")
    if timeout is not None and timeout <= 0:
        raise AgentDirError("timeout must be positive")

    cwd_path = Path(cwd).expanduser().resolve() if cwd else Path.cwd()
    session = ensure_session(root, session_id, title=f"AgentDir run: {argv[0]}", emit_started=session_id is None)
    tool = tool_name or Path(argv[0]).name
    start = datetime.now(UTC)
    stored_argv, stored_command, argv_redactions, argv_labels = _redact_argv_and_command(
        argv,
        redact=redact,
    )
    head = git_head(cwd_path)
    workspace = workspace_name(cwd_path)

    call_headers: dict[str, str] | None = None
    if redact and argv_redactions:
        call_headers = {
            "X-AgentDir-Redactions": str(argv_redactions),
            **({"X-AgentDir-Redaction-Labels": ",".join(argv_labels)} if argv_labels else {}),
        }

    emit_event(
        root,
        session_id=session.session_id,
        event_type="tool.call",
        subject=f"tool.call {tool}",
        from_actor="agent",
        body="\n".join(
            [
                f"tool={tool}",
                f"argv={stored_argv!r}",
                f"cwd={cwd_path}",
                f"started_at={start.isoformat()}",
            ]
        ),
        workspace=workspace,
        git_head=head,
        tool=tool,
        extra_headers=call_headers,
        redact=redact,
    )

    stdout_capture = CapturedText()
    stderr_capture = CapturedText()
    env = os.environ.copy()
    env["AGENTDIR_SESSION"] = session.session_id
    popen_kwargs: dict[str, Any] = {
        "cwd": cwd_path,
        "env": env,
        "text": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "errors": "replace",
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    started = time.monotonic()
    try:
        process = subprocess.Popen(argv, **popen_kwargs)
    except (FileNotFoundError, PermissionError, IsADirectoryError) as exc:
        if isinstance(exc, FileNotFoundError):
            spawn_exit = COMMAND_NOT_FOUND_EXIT_CODE
            message = f"agentdir: command not found: {argv[0]}\n"
        elif isinstance(exc, IsADirectoryError):
            spawn_exit = CANNOT_EXECUTE_EXIT_CODE
            message = f"agentdir: cannot execute directory: {argv[0]}: {exc}\n"
        else:
            spawn_exit = CANNOT_EXECUTE_EXIT_CODE
            message = f"agentdir: permission denied: {argv[0]}: {exc}\n"
        sys.stderr.write(message)
        sys.stderr.flush()
        delivered = emit_event(
            root,
            session_id=session.session_id,
            event_type="tool.result",
            subject=f"tool.result {tool} exit {spawn_exit}",
            from_actor="agent",
            body=f"argv={stored_argv!r}\ncommand={stored_command}\nexit_code={spawn_exit}\nstderr:\n{message}",
            workspace=workspace,
            git_head=head,
            tool=tool,
            tool_exit_code=spawn_exit,
            extra_headers=call_headers,
            redact=redact,
        )
        return RunToolResult(
            exit_code=spawn_exit,
            session_id=session.session_id,
            tool=tool,
            duration_ms=0,
            event_path=str(delivered.path),
        )

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_thread = threading.Thread(
        target=_tee_stream,
        args=(process.stdout, sys.stdout, stdout_capture, max_capture_bytes),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_tee_stream,
        args=(process.stderr, sys.stderr, stderr_capture, max_capture_bytes),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    timed_out = False
    try:
        exit_code = process.wait(timeout=timeout)
        if exit_code < 0:
            exit_code = 128 + (-exit_code)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_tree(process)
        exit_code = TIMEOUT_EXIT_CODE
    join_timeout = _TIMEOUT_JOIN_SECONDS if timed_out else None
    stdout_thread.join(timeout=join_timeout)
    stderr_thread.join(timeout=join_timeout)
    duration_ms = int((time.monotonic() - started) * 1000)

    if timed_out:
        sys.stderr.write(
            f"agentdir: warning: command timed out after {timeout}s and was killed; "
            f"recording exit code {TIMEOUT_EXIT_CODE}\n"
        )
        sys.stderr.flush()

    truncated_streams = [
        name
        for name, capture in (("stdout", stdout_capture), ("stderr", stderr_capture))
        if capture.truncated
    ]
    if truncated_streams:
        sys.stderr.write(
            f"agentdir: warning: captured {'+'.join(truncated_streams)} truncated at "
            f"{max_capture_bytes} bytes; recorded evidence is partial "
            "(use --max-capture-bytes to raise the limit)\n"
        )
        sys.stderr.flush()

    stored_stdout = redact_text(stdout_capture.text) if redact else None
    stored_stderr = redact_text(stderr_capture.text) if redact else None
    stdout_text = stored_stdout.text if stored_stdout else stdout_capture.text
    stderr_text = stored_stderr.text if stored_stderr else stderr_capture.text
    redactions = (
        argv_redactions
        + (stored_stdout.replacements if stored_stdout else 0)
        + (stored_stderr.replacements if stored_stderr else 0)
    )
    redaction_labels = tuple(
        dict.fromkeys(
            [
                *argv_labels,
                *(stored_stdout.labels if stored_stdout else ()),
                *(stored_stderr.labels if stored_stderr else ()),
            ]
        )
    )

    body = "\n".join(
        [
            f"argv={stored_argv!r}",
            f"command={stored_command}",
            f"exit_code={exit_code}",
            f"duration_ms={duration_ms}",
            f"cwd={cwd_path}",
            f"redactions={redactions}",
            f"stdout_truncated={str(stdout_capture.truncated).lower()}",
            f"stderr_truncated={str(stderr_capture.truncated).lower()}",
            f"timed_out={str(timed_out).lower()}",
            "",
            "stdout:",
            stdout_text,
            "",
            "stderr:",
            stderr_text,
        ]
    )
    delivered = emit_event(
        root,
        session_id=session.session_id,
        event_type="tool.result",
        subject=f"tool.result {tool} exit {exit_code}",
        from_actor="agent",
        body=body,
        workspace=workspace,
        git_head=head,
        tool=tool,
        tool_exit_code=exit_code,
        extra_headers={
            "X-AgentDir-Duration-Ms": str(duration_ms),
            "X-AgentDir-Redactions": str(redactions),
            **(
                {"X-AgentDir-Redaction-Labels": ",".join(redaction_labels)}
                if redaction_labels
                else {}
            ),
            **({"X-AgentDir-Truncated": ",".join(truncated_streams)} if truncated_streams else {}),
            **({"X-AgentDir-Timed-Out": "true"} if timed_out else {}),
        },
        redact=redact,
    )
    return RunToolResult(
        exit_code=int(exit_code),
        session_id=session.session_id,
        tool=tool,
        duration_ms=duration_ms,
        truncated_streams=tuple(truncated_streams),
        timed_out=timed_out,
        event_path=str(delivered.path),
        stderr_needs_newline=(
            stderr_capture.had_output and not stderr_capture.ended_with_newline
        ),
    )
