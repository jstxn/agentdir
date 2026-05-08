from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import format_datetime, make_msgid
from pathlib import Path
from typing import Iterable

from .store import AgentDirError

AGENTDIR_VERSION = "0.1"


@dataclass(frozen=True)
class ParsedEnvelope:
    message: Message
    raw: bytes
    path: Path

    @property
    def message_id(self) -> str | None:
        return self.message.get("Message-ID")

    @property
    def body_text(self) -> str:
        if self.message.is_multipart():
            parts: list[str] = []
            for part in self.message.walk():
                if part.get_content_maintype() == "text":
                    try:
                        parts.append(part.get_content())
                    except Exception:
                        continue
            return "\n".join(parts)
        try:
            content = self.message.get_content()
        except Exception:
            payload = self.message.get_payload(decode=True)
            if payload is None:
                return ""
            return payload.decode("utf-8", errors="replace")
        return content if isinstance(content, str) else str(content)

    @property
    def body_sha256(self) -> str:
        return hashlib.sha256(self.body_text.encode("utf-8", errors="replace")).hexdigest()


def actor_address(actor: str | None) -> str:
    local = (actor or "agentdir").strip() or "agentdir"
    if "@" in local:
        return local
    safe = "".join(ch if ch.isalnum() or ch in "._+-" else "-" for ch in local)
    return f"{safe}@agentdir.local"


def build_envelope(
    *,
    event_type: str,
    body: str,
    subject: str | None = None,
    from_actor: str | None = None,
    to_actor: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    workspace: str | None = None,
    git_head: str | None = None,
    tool: str | None = None,
    tool_exit_code: int | None = None,
    parent_message_id: str | None = None,
    references: Iterable[str] | None = None,
    artifact_headers: dict[str, str] | None = None,
    extra_headers: dict[str, str] | None = None,
    message_id: str | None = None,
) -> EmailMessage:
    if not event_type:
        raise AgentDirError("event_type is required")

    msg = EmailMessage(policy=policy.default)
    msg["Message-ID"] = message_id or make_msgid(domain="agentdir.local")
    msg["Date"] = format_datetime(datetime.now(UTC))
    msg["From"] = actor_address(from_actor)
    msg["To"] = actor_address(to_actor or session_id or "agentdir")
    msg["Subject"] = subject or event_type
    msg["X-AgentDir-Version"] = AGENTDIR_VERSION
    msg["X-AgentDir-Event-Type"] = event_type
    msg["X-AgentDir-Created-Ns"] = str(time.time_ns())

    optional = {
        "X-AgentDir-Session": session_id,
        "X-AgentDir-Task": task_id,
        "X-AgentDir-Workspace": workspace,
        "X-AgentDir-Git-Head": git_head,
        "X-AgentDir-Tool": tool,
        "X-AgentDir-Tool-Exit-Code": str(tool_exit_code) if tool_exit_code is not None else None,
    }
    for name, value in optional.items():
        if value:
            msg[name] = value

    if parent_message_id:
        msg["In-Reply-To"] = parent_message_id
    refs = list(references or [])
    if parent_message_id and parent_message_id not in refs:
        refs.append(parent_message_id)
    if refs:
        msg["References"] = " ".join(refs)

    for source in (artifact_headers or {}, extra_headers or {}):
        for name, value in source.items():
            if value is not None:
                msg[name] = str(value)

    msg.set_content(body, subtype="plain", charset="utf-8")
    return msg


def envelope_bytes(message: EmailMessage) -> bytes:
    return message.as_bytes(policy=policy.default.clone(linesep="\n"))


def parse_envelope(path: Path) -> ParsedEnvelope:
    raw = path.read_bytes()
    return ParsedEnvelope(
        message=BytesParser(policy=policy.default).parsebytes(raw),
        raw=raw,
        path=path,
    )


def validate_required(parsed: ParsedEnvelope) -> list[str]:
    return [
        name
        for name in ("Message-ID", "Date", "From", "To", "X-AgentDir-Version", "X-AgentDir-Event-Type")
        if not parsed.message.get(name)
    ]
