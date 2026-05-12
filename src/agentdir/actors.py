from __future__ import annotations

from pathlib import Path

from .envelope import build_envelope, envelope_bytes
from .mailbox import atomic_deliver
from .redaction import redact_text
from .store import actor_mailbox


def create_actor(root: str | Path, actor_id: str) -> tuple[Path, Path]:
    inbox = actor_mailbox(root, actor_id, "inbox", create=True)
    outbox = actor_mailbox(root, actor_id, "outbox", create=True)
    return inbox, outbox


def send_message(
    *,
    root: str | Path,
    from_actor: str,
    to_actor: str,
    event_type: str,
    body: str,
    subject: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    message_id: str | None = None,
) -> tuple[Path, Path]:
    create_actor(root, from_actor)
    create_actor(root, to_actor)
    redacted = redact_text(body)
    extra_headers = {}
    if redacted.replacements:
        extra_headers["X-AgentDir-Redactions"] = str(redacted.replacements)
        extra_headers["X-AgentDir-Redaction-Labels"] = ",".join(redacted.labels)
    msg = build_envelope(
        event_type=event_type,
        body=redacted.text,
        subject=subject,
        from_actor=from_actor,
        to_actor=to_actor,
        session_id=session_id,
        task_id=task_id,
        extra_headers=extra_headers,
        message_id=message_id,
    )
    data = envelope_bytes(msg)
    inbox_path = atomic_deliver(actor_mailbox(root, to_actor, "inbox"), data)
    outbox_path = atomic_deliver(actor_mailbox(root, from_actor, "outbox"), data)
    return inbox_path, outbox_path
