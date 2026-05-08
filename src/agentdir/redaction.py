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


def redact_text(text: str) -> RedactionResult:
    replacements = 0
    redacted = text
    for label, pattern in SECRET_PATTERNS:
        redacted, count = pattern.subn(f"<redacted:{label}>", redacted)
        replacements += count
    return RedactionResult(text=redacted, replacements=replacements)


def looks_secret_bearing(text: str) -> bool:
    return any(pattern.search(text) for _label, pattern in SECRET_PATTERNS)
