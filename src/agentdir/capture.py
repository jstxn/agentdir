from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .events import emit_event
from .git import git_head, workspace_name
from .redaction import redact_text
from .sessions import ensure_session
from .store import AgentDirError

DEFAULT_MAX_CAPTURE_BYTES = 256 * 1024


@dataclass
class CapturedText:
    text: str = ""
    truncated: bool = False


def _append_limited(capture: CapturedText, chunk: str, max_bytes: int) -> None:
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


def run_tool(
    root: str | Path,
    *,
    argv: list[str],
    session_id: str | None = None,
    tool_name: str | None = None,
    cwd: str | Path | None = None,
    max_capture_bytes: int = DEFAULT_MAX_CAPTURE_BYTES,
    redact: bool = True,
) -> int:
    if not argv:
        raise AgentDirError("argv is required")
    if max_capture_bytes < 0:
        raise AgentDirError("max_capture_bytes must be non-negative")

    cwd_path = Path(cwd).expanduser().resolve() if cwd else Path.cwd()
    session = ensure_session(root, session_id, title=f"AgentDir run: {argv[0]}", emit_started=session_id is None)
    tool = tool_name or Path(argv[0]).name
    start = datetime.now(UTC)
    command_text = " ".join(argv)
    head = git_head(cwd_path)
    workspace = workspace_name(cwd_path)

    emit_event(
        root,
        session_id=session.session_id,
        event_type="tool.call",
        subject=f"tool.call {tool}",
        from_actor="agent",
        body="\n".join(
            [
                f"tool={tool}",
                f"argv={argv!r}",
                f"cwd={cwd_path}",
                f"started_at={start.isoformat()}",
            ]
        ),
        workspace=workspace,
        git_head=head,
        tool=tool,
    )

    stdout_capture = CapturedText()
    stderr_capture = CapturedText()
    env = os.environ.copy()
    env["AGENTDIR_SESSION"] = session.session_id
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd_path,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            errors="replace",
        )
    except FileNotFoundError:
        message = f"agentdir: command not found: {argv[0]}\n"
        sys.stderr.write(message)
        sys.stderr.flush()
        emit_event(
            root,
            session_id=session.session_id,
            event_type="tool.result",
            subject=f"tool.result {tool} exit 127",
            from_actor="agent",
            body=f"command={command_text}\nexit_code=127\nstderr:\n{message}",
            workspace=workspace,
            git_head=head,
            tool=tool,
            tool_exit_code=127,
        )
        return 127

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
    exit_code = process.wait()
    stdout_thread.join()
    stderr_thread.join()
    duration_ms = int((time.monotonic() - started) * 1000)

    stored_stdout = redact_text(stdout_capture.text) if redact else None
    stored_stderr = redact_text(stderr_capture.text) if redact else None
    stdout_text = stored_stdout.text if stored_stdout else stdout_capture.text
    stderr_text = stored_stderr.text if stored_stderr else stderr_capture.text
    redactions = (stored_stdout.replacements if stored_stdout else 0) + (
        stored_stderr.replacements if stored_stderr else 0
    )

    body = "\n".join(
        [
            f"command={command_text}",
            f"exit_code={exit_code}",
            f"duration_ms={duration_ms}",
            f"cwd={cwd_path}",
            f"redactions={redactions}",
            f"stdout_truncated={str(stdout_capture.truncated).lower()}",
            f"stderr_truncated={str(stderr_capture.truncated).lower()}",
            "",
            "stdout:",
            stdout_text,
            "",
            "stderr:",
            stderr_text,
        ]
    )
    emit_event(
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
        },
    )
    return int(exit_code)
