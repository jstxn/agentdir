from __future__ import annotations

import re
from dataclasses import dataclass


SECRET_PATTERNS = [
    ("github-token", re.compile(r"gh[opsu]_[A-Za-z0-9_]{20,}")),
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    (
        "key-value-secret",
        re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{12,}"),
    ),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]


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
