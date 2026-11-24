from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .artifacts import add_artifact, artifact_headers
from .envelope import build_envelope, envelope_bytes
from .mailbox import atomic_deliver
from .redaction import parse_redaction_count, redact_envelope_fields
from .store import init_root, session_mailbox


@dataclass(frozen=True)
class EmittedEvent:
    path: Path
    session_id: str
    event_type: str


def emit_event(
    root: str | Path,
    *,
    session_id: str,
    event_type: str,
    body: str,
    subject: str | None = None,
    from_actor: str | None = None,
    to_actor: str | None = None,
    task_id: str | None = None,
    workspace: str | None = None,
    git_head: str | None = None,
    tool: str | None = None,
    tool_exit_code: int | None = None,
    parent_message_id: str | None = None,
    artifact: str | Path | None = None,
    extra_headers: dict[str, str | Iterable[str]] | None = None,
    message_id: str | None = None,
    redact: bool = True,
) -> EmittedEvent:
    init_root(root)
    stored_artifact = add_artifact(root, artifact) if artifact else None
    headers: dict[str, Any] = dict(extra_headers or {})
    if redact:
        body_text, subject_text = redact_envelope_fields(body, subject, headers)
    else:
        body_text, subject_text = body, subject
    message = build_envelope(
        event_type=event_type,
        body=body_text,
        subject=subject_text,
        from_actor=from_actor,
        to_actor=to_actor,
        session_id=session_id,
        task_id=task_id,
        workspace=workspace,
        git_head=git_head,
        tool=tool,
        tool_exit_code=tool_exit_code,
        parent_message_id=parent_message_id,
        artifact_headers=artifact_headers(stored_artifact) if stored_artifact else {},
        extra_headers=headers,
        message_id=message_id,
    )
    delivered = atomic_deliver(session_mailbox(root, session_id), envelope_bytes(message))
    return EmittedEvent(path=delivered, session_id=session_id, event_type=event_type)


def _redaction_count(headers: dict[str, str | Iterable[str]]) -> int:
    return parse_redaction_count(headers.get("X-AgentDir-Redactions"))
