from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .envelope import ParsedEnvelope, envelope_bytes, header_value, parse_envelope
from .fsutil import atomic_write_bytes
from .index import rebuild_index
from .mailbox import iter_records
from .redaction import is_redactable_header, redact_text, secret_labels
from .store import discover_mailboxes, is_mailbox, require_root


@dataclass(frozen=True)
class SecretFinding:
    path: str
    mailbox: str
    message_id: str | None
    event_type: str | None
    subject: str | None
    labels: tuple[str, ...]


@dataclass(frozen=True)
class SecretRedaction:
    path: str
    labels: tuple[str, ...]
    replacements: int
    applied: bool


def scan_secret_records(
    root: str | Path,
    parsed_by_path: dict[Path, ParsedEnvelope] | None = None,
) -> list[SecretFinding]:
    paths = require_root(root)
    findings: list[SecretFinding] = []
    for mailbox in _all_mailboxes(paths.root):
        try:
            records = iter_records(mailbox)
        except OSError:
            continue
        for record in records:
            try:
                parsed = (
                    parsed_by_path.get(record.path)
                    if parsed_by_path is not None
                    else None
                )
                if parsed is None:
                    parsed = parse_envelope(record.path)
                labels = _envelope_secret_labels(parsed)
            except (OSError, ValueError):
                continue
            if not labels:
                continue
            raw_subject = parsed.header("Subject")
            safe_subject = redact_text(raw_subject).text if raw_subject is not None else None
            findings.append(
                SecretFinding(
                    path=str(record.path.relative_to(paths.root)),
                    mailbox=str(mailbox.relative_to(paths.root)),
                    message_id=parsed.message_id,
                    event_type=parsed.header("X-AgentDir-Event-Type"),
                    subject=safe_subject,
                    labels=labels,
                )
            )
    return findings


def redact_secret_records(root: str | Path, *, apply: bool = False) -> dict[str, Any]:
    paths = require_root(root)
    redactions: list[SecretRedaction] = []
    modified = False
    try:
        for mailbox in _all_mailboxes(paths.root):
            for record in iter_records(mailbox):
                parsed = parse_envelope(record.path)
                replacements, labels, single_body = _inspect_redactions(parsed)
                if replacements == 0:
                    continue
                if apply:
                    _rewrite_body(record.path, parsed.message, single_body, labels, replacements)
                    modified = True
                redactions.append(
                    SecretRedaction(
                        path=str(record.path.relative_to(paths.root)),
                        labels=labels,
                        replacements=replacements,
                        applied=apply,
                    )
                )
    finally:
        if apply and modified:
            rebuild_index(paths.root)
    return {
        "applied": apply,
        "count": len(redactions),
        "redactions": [asdict(item) for item in redactions],
        "index_rebuilt": bool(apply and modified),
    }


def format_secret_findings(findings: list[SecretFinding]) -> str:
    if not findings:
        return "No secret-like envelope bodies found."
    lines = [f"Found {len(findings)} secret-like envelope body/bodies:"]
    for finding in findings:
        labels = ",".join(finding.labels)
        event = finding.event_type or "unknown"
        lines.append(f"- {finding.path} [{event}] labels={labels}")
    lines.append("No body text was printed.")
    return "\n".join(lines)


def format_secret_redaction(result: dict[str, Any]) -> str:
    count = int(result["count"])
    if count == 0:
        return "No secret-like envelope bodies found."
    action = "Redacted" if result["applied"] else "Would redact"
    lines = [f"{action} {count} secret-like envelope body/bodies:"]
    for item in result["redactions"]:
        labels = ",".join(item["labels"])
        lines.append(f"- {item['path']} labels={labels} replacements={item['replacements']}")
    if result["applied"]:
        lines.append("Rebuilt the derived SQLite index after redaction.")
    else:
        lines.append("Dry run only. Re-run with --apply to rewrite bodies and rebuild the index.")
    lines.append("No body text was printed.")
    return "\n".join(lines)


def _all_mailboxes(root: Path) -> list[Path]:
    paths = require_root(root)
    mailboxes = [mailbox for _kind, mailbox in discover_mailboxes(paths.root)]
    archived_sessions = paths.archives / "sessions"
    if archived_sessions.is_dir():
        for archived in sorted(archived_sessions.glob("*/Maildir")):
            if is_mailbox(archived):
                mailboxes.append(archived)
    return sorted(set(mailboxes))


def _envelope_secret_labels(parsed: ParsedEnvelope) -> tuple[str, ...]:
    labels: list[str] = list(secret_labels(parsed.body_text))
    for name, raw_value in parsed.message.items():
        if not is_redactable_header(name):
            continue
        labels.extend(secret_labels(header_value(raw_value)))
    return tuple(dict.fromkeys(labels))


def _part_text(part: Any) -> str | None:
    try:
        content = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True)
        if payload is None:
            return None
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    return content if isinstance(content, str) else str(content)


def _inspect_redactions(parsed: ParsedEnvelope) -> tuple[int, tuple[str, ...], str | None]:
    replacements = 0
    labels: list[str] = []
    single_body: str | None = None

    if parsed.message.is_multipart():
        for part in parsed.message.walk():
            if part.is_multipart() or part.get_content_maintype() != "text":
                continue
            content = _part_text(part)
            if content is None:
                continue
            part_result = redact_text(content)
            if part_result.replacements:
                replacements += part_result.replacements
                labels.extend(part_result.labels)
    else:
        body_result = redact_text(parsed.body_text)
        if body_result.replacements:
            replacements += body_result.replacements
            labels.extend(body_result.labels)
            single_body = body_result.text

    for name, raw_value in parsed.message.items():
        if not is_redactable_header(name):
            continue
        header_result = redact_text(header_value(raw_value))
        if header_result.replacements:
            replacements += header_result.replacements
            labels.extend(header_result.labels)

    return replacements, tuple(dict.fromkeys(labels)), single_body


def _redact_message_headers(message: Any) -> None:
    seen: set[str] = set()
    for name in list(message.keys()):
        lower = name.lower()
        if lower in seen or not is_redactable_header(name):
            continue
        seen.add(lower)
        values = message.get_all(name, [])
        updated_values: list[str] = []
        changed = False
        for raw_value in values:
            redacted = redact_text(header_value(raw_value))
            if redacted.replacements:
                changed = True
                updated_values.append(redacted.text)
            else:
                updated_values.append(str(raw_value))
        if changed:
            while name in message:
                del message[name]
            for value in updated_values:
                message[name] = value


def _rewrite_body(
    path: Path,
    message: Any,
    body: str | None,
    labels: tuple[str, ...],
    replacements: int,
) -> None:
    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart() or part.get_content_maintype() != "text":
                continue
            content = _part_text(part)
            if content is None:
                continue
            part_result = redact_text(content)
            if not part_result.replacements:
                continue
            subtype = part.get_content_subtype() or "plain"
            kwargs: dict[str, Any] = {"subtype": subtype, "charset": "utf-8"}
            disposition = part.get_content_disposition()
            if disposition:
                kwargs["disposition"] = disposition
            filename = part.get_filename()
            if filename:
                kwargs["filename"] = filename
            part.set_content(part_result.text, **kwargs)
    elif body is not None:
        subtype = (
            message.get_content_subtype()
            if message.get_content_maintype() == "text"
            else "plain"
        ) or "plain"
        message.set_content(body, subtype=subtype, charset="utf-8")

    _redact_message_headers(message)
    _replace_header(message, "X-AgentDir-Secret-Redacted", "true")
    _replace_header(message, "X-AgentDir-Secret-Redacted-At", datetime.now(UTC).isoformat())
    _replace_header(message, "X-AgentDir-Secret-Redaction-Labels", ",".join(labels))
    _replace_header(message, "X-AgentDir-Secret-Redaction-Replacements", str(replacements))
    _atomic_replace(path, envelope_bytes(message))


def _replace_header(message: Any, name: str, value: str) -> None:
    while name in message:
        del message[name]
    message[name] = value


def _atomic_replace(path: Path, data: bytes) -> None:
    atomic_write_bytes(path, data)
