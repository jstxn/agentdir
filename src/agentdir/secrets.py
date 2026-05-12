from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .envelope import envelope_bytes, parse_envelope
from .index import rebuild_index
from .mailbox import fsync_directory, iter_records
from .redaction import redact_text, secret_labels
from .store import is_mailbox, require_root


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


def scan_secret_records(root: str | Path) -> list[SecretFinding]:
    paths = require_root(root)
    findings: list[SecretFinding] = []
    for mailbox in _all_mailboxes(paths.root):
        for record in iter_records(mailbox):
            parsed = parse_envelope(record.path)
            labels = secret_labels(parsed.body_text)
            if not labels:
                continue
            findings.append(
                SecretFinding(
                    path=str(record.path.relative_to(paths.root)),
                    mailbox=str(mailbox.relative_to(paths.root)),
                    message_id=parsed.message_id,
                    event_type=parsed.message.get("X-AgentDir-Event-Type"),
                    subject=parsed.message.get("Subject"),
                    labels=labels,
                )
            )
    return findings


def redact_secret_records(root: str | Path, *, apply: bool = False) -> dict[str, Any]:
    paths = require_root(root)
    redactions: list[SecretRedaction] = []
    for mailbox in _all_mailboxes(paths.root):
        for record in iter_records(mailbox):
            parsed = parse_envelope(record.path)
            result = redact_text(parsed.body_text)
            if result.replacements == 0:
                continue
            if apply:
                _rewrite_body(record.path, parsed.message, result.text, result.labels, result.replacements)
            redactions.append(
                SecretRedaction(
                    path=str(record.path.relative_to(paths.root)),
                    labels=result.labels,
                    replacements=result.replacements,
                    applied=apply,
                )
            )
    if apply and redactions:
        rebuild_index(paths.root)
    return {
        "applied": apply,
        "count": len(redactions),
        "redactions": [asdict(item) for item in redactions],
        "index_rebuilt": bool(apply and redactions),
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
    return sorted(path for path in root.glob("**/Maildir") if is_mailbox(path))


def _rewrite_body(
    path: Path,
    message: Any,
    body: str,
    labels: tuple[str, ...],
    replacements: int,
) -> None:
    message.set_content(body, subtype="plain", charset="utf-8")
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
    temp = path.with_name(f".{path.name}.agentdir-redact-tmp")
    if temp.exists():
        temp.unlink()
    mode = path.stat().st_mode
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
        fsync_directory(path.parent)
    except Exception:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise
