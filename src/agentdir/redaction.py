from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


SECRET_PATTERNS = [
    ("github-token", re.compile(r"gh[opsu]_[A-Za-z0-9_]{20,}")),
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    (
        "key-value-secret",
        re.compile(
            r"(?i)(api[_-]?key|secret|token|password)(?:\\?['\"])?\s*[:=]\s*(?:\\?['\"])?[A-Za-z0-9_./+=-]{12,}"
        ),
    ),
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)"
        ),
    ),
]

NON_REDACTABLE_HEADERS = frozenset(
    {
        "message-id",
        "date",
        "from",
        "to",
        "in-reply-to",
        "references",
        "mime-version",
        "content-type",
        "content-transfer-encoding",
        "x-agentdir-version",
        "x-agentdir-event-type",
        "x-agentdir-created-ns",
        "x-agentdir-session",
        "x-agentdir-task",
        "x-agentdir-workspace",
        "x-agentdir-git-head",
        "x-agentdir-tool",
        "x-agentdir-tool-exit-code",
        "x-agentdir-blob-sha256",
        "x-agentdir-blob-bytes",
        "x-agentdir-blob-mime",
        "x-agentdir-protocol",
        "x-agentdir-pack-id",
        "x-agentdir-context-scope",
        "x-agentdir-source-id",
        "x-agentdir-reviewed-source-id",
        "x-agentdir-used-source-id",
        "x-agentdir-dismissed-source-id",
        "x-agentdir-context-disposition",
        "x-agentdir-context-decision-id",
        "x-agentdir-context-decision-revision",
        "x-agentdir-consumption-purpose",
        "x-agentdir-enforcement-mode",
        "x-agentdir-claim-family",
        "x-agentdir-claim-outcome",
        "x-agentdir-duration-ms",
        "x-agentdir-truncated",
        "x-agentdir-timed-out",
        "x-agentdir-redactions",
        "x-agentdir-redaction-labels",
        "x-agentdir-secret-redacted",
        "x-agentdir-secret-redacted-at",
        "x-agentdir-secret-redaction-labels",
        "x-agentdir-secret-redaction-replacements",
    }
)


@dataclass(frozen=True)
class RedactionResult:
    text: str
    replacements: int
    labels: tuple[str, ...] = ()


def redact_text(text: str) -> RedactionResult:
    replacements = 0
    redacted = text
    labels: list[str] = []
    for label, pattern in SECRET_PATTERNS:
        redacted, count = pattern.subn(f"<redacted:{label}>", redacted)
        replacements += count
        if count:
            labels.append(label)
    return RedactionResult(text=redacted, replacements=replacements, labels=tuple(labels))


def looks_secret_bearing(text: str) -> bool:
    return any(pattern.search(text) for _label, pattern in SECRET_PATTERNS)


def secret_labels(text: str) -> tuple[str, ...]:
    return tuple(label for label, pattern in SECRET_PATTERNS if pattern.search(text))


def is_redactable_header(name: str) -> bool:
    lower = name.strip().lower()
    if (
        lower in NON_REDACTABLE_HEADERS
        or lower.startswith("x-agentdir-context-view-")
        or lower.endswith("-sha256")
    ):
        return False
    return True


def parse_redaction_count(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, (str, int)):
        try:
            return int(str(value).strip() or "0")
        except ValueError:
            return 0
    if isinstance(value, Iterable):
        total = 0
        for item in value:
            total += parse_redaction_count(item)
        return total
    return 0


def parse_redaction_labels(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, Iterable):
        labels: list[str] = []
        for item in value:
            labels.extend(parse_redaction_labels(item))
        return labels
    return []


def merge_redaction_headers(
    headers: dict[str, Any],
    replacements: int,
    labels: Iterable[str],
) -> None:
    if replacements <= 0:
        return
    existing_count = 0
    existing_labels: list[str] = []
    for key in list(headers):
        lower = key.lower()
        if lower == "x-agentdir-redactions":
            existing_count += parse_redaction_count(headers.pop(key))
        elif lower == "x-agentdir-redaction-labels":
            existing_labels.extend(parse_redaction_labels(headers.pop(key)))
    merged_labels = list(dict.fromkeys([*existing_labels, *(label for label in labels if label)]))
    headers["X-AgentDir-Redactions"] = str(existing_count + replacements)
    if merged_labels:
        headers["X-AgentDir-Redaction-Labels"] = ",".join(merged_labels)


def redact_envelope_fields(
    body: str,
    subject: str | None,
    headers: dict[str, Any],
) -> tuple[str, str | None]:
    total_replacements = 0
    labels: list[str] = []

    redacted_body = redact_text(body)
    total_replacements += redacted_body.replacements
    labels.extend(redacted_body.labels)

    subject_text = subject
    if subject is not None:
        redacted_subject = redact_text(subject)
        subject_text = redacted_subject.text
        total_replacements += redacted_subject.replacements
        labels.extend(redacted_subject.labels)

    for name, value in list(headers.items()):
        if value is None or not is_redactable_header(name):
            continue
        if isinstance(value, str):
            redacted_value = redact_text(value)
            if redacted_value.replacements:
                headers[name] = redacted_value.text
                total_replacements += redacted_value.replacements
                labels.extend(redacted_value.labels)
            continue
        updated_items: list[str | None] = []
        for item in value:
            if item is None:
                updated_items.append(None)
                continue
            redacted_item = redact_text(str(item))
            total_replacements += redacted_item.replacements
            labels.extend(redacted_item.labels)
            updated_items.append(redacted_item.text)
        headers[name] = updated_items

    merge_redaction_headers(headers, total_replacements, labels)
    return redacted_body.text, subject_text
