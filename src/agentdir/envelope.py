from __future__ import annotations

import hashlib
import re
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

_CRLF_RUN_RE = re.compile(r"[\r\n]+")
_SINGLE_ADDRESS_LOCAL_RE = re.compile(r"^[A-Za-z0-9_+-]+(?:\.[A-Za-z0-9_+-]+)*\Z")
_SINGLE_ADDRESS_DOMAIN_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\Z"
)


def header_value(value: object) -> str:
    """Normalise a parsed header value.

    A header longer than the fold width is written across continuation lines.
    Python 3.12 and newer strip the continuation's leading whitespace when the
    value is read back; 3.11 does not. `X-AgentDir-Blob-SHA256` is 89 characters
    with its name, so it always folds, and on 3.11 the artifact lookup was made
    with a leading space and never matched.
    """
    return " ".join(str(value).split())


@dataclass(frozen=True)
class ParsedEnvelope:
    message: Message
    raw: bytes
    path: Path

    @property
    def message_id(self) -> str | None:
        return self.header("Message-ID")

    def header(self, name: str) -> str | None:
        raw = self.message.get(name)
        return None if raw is None else header_value(raw)

    def headers(self, name: str) -> list[str]:
        return [header_value(raw) for raw in self.message.get_all(name, [])]

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


def _sanitize_header_value(value: str) -> str:
    return _CRLF_RUN_RE.sub(" ", str(value)).strip()


def _validate_header_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or not name.isascii()
        or any(ch in name for ch in "\r\n:")
        or any(ord(ch) < 33 or ord(ch) > 126 for ch in name)
    ):
        raise AgentDirError(f"Invalid header name: {name!r}")
    return name


def _safe_local_part(value: str) -> str:
    safe = "".join(
        ch if ch.isascii() and (ch.isalnum() or ch in "._+-") else "-"
        for ch in value
    )
    safe = re.sub(r"\.{2,}", ".", safe).strip(".")
    return safe or "agentdir"


def actor_address(actor: str | None) -> str:
    local = _sanitize_header_value(actor or "agentdir") or "agentdir"
    if "@" in local:
        parts = local.split("@")
        if (
            len(parts) == 2
            and _SINGLE_ADDRESS_LOCAL_RE.fullmatch(parts[0])
            and _SINGLE_ADDRESS_DOMAIN_RE.fullmatch(parts[1])
        ):
            return local
    return f"{_safe_local_part(local)}@agentdir.local"


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
    artifact_headers: dict[str, str | Iterable[str]] | None = None,
    extra_headers: dict[str, str | Iterable[str]] | None = None,
    message_id: str | None = None,
) -> EmailMessage:
    clean_event_type = _sanitize_header_value(event_type) if event_type else ""
    if not clean_event_type:
        raise AgentDirError("event_type is required")

    clean_message_id = _sanitize_header_value(message_id) if message_id else ""
    clean_subject = _sanitize_header_value(subject) if subject is not None else ""

    msg = EmailMessage(policy=policy.default)
    msg["Message-ID"] = clean_message_id or make_msgid(domain="agentdir.local")
    msg["Date"] = format_datetime(datetime.now(UTC))
    msg["From"] = actor_address(from_actor)
    msg["To"] = actor_address(to_actor or session_id or "agentdir")
    msg["Subject"] = clean_subject or clean_event_type
    msg["X-AgentDir-Version"] = AGENTDIR_VERSION
    msg["X-AgentDir-Event-Type"] = clean_event_type
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
            cleaned = _sanitize_header_value(value)
            if cleaned:
                msg[name] = cleaned

    clean_parent = _sanitize_header_value(parent_message_id) if parent_message_id else ""
    if clean_parent:
        msg["In-Reply-To"] = clean_parent
    refs = [
        cleaned
        for ref in (references or [])
        if (cleaned := _sanitize_header_value(ref))
    ]
    if clean_parent and clean_parent not in refs:
        refs.append(clean_parent)
    if refs:
        msg["References"] = " ".join(refs)

    for source in (artifact_headers or {}, extra_headers or {}):
        for name, value in source.items():
            if value is None:
                continue
            header_name = _validate_header_name(name)
            if isinstance(value, str):
                msg[header_name] = _sanitize_header_value(value)
                continue
            for item in value:
                if item is not None:
                    msg[header_name] = _sanitize_header_value(str(item))

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
