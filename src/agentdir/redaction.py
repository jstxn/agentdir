from __future__ import annotations

import re
from dataclasses import dataclass


# Private keys are masked as a whole block, not just the BEGIN line. When the
# END line is missing (truncated output), the base64 lines that follow the
# BEGIN line are masked instead. Line breaks may be real or JSON-escaped.
_PEM_BEGIN = r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
_PEM_END = r"-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
_PEM_BREAK = r"(?:\s|\\[nr])"
_PEM_LINES = rf"(?:{_PEM_BREAK}+[A-Za-z0-9+/=]{{16,}})"
_PRIVATE_KEY = (
    rf"{_PEM_BEGIN}(?:[A-Za-z0-9+/=\s\\:,-]*?{_PEM_END}|{_PEM_LINES}+)?"
    # Key bodies left in place by older versions, which masked only the
    # BEGIN line.
    rf"|<redacted:private-key>{_PEM_BREAK}+[A-Za-z0-9+/=]{{40,}}"
    rf"{_PEM_LINES}*(?:{_PEM_BREAK}*{_PEM_END})?"
)

SECRET_PATTERNS = [
    ("github-token", re.compile(r"gh[opsu]_[A-Za-z0-9_]{20,}")),
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    (
        "key-value-secret",
        # Covers JSON-quoted keys ("password": "...") and names ending in
        # SECRET_KEY or SECRET_ACCESS_KEY, but not words that merely start
        # with token, secret or password (tokenizer: ...).
        re.compile(
            r"(?i)(api[_-]?key|secret(?:[_-]?access)?[_-]?key|secret|token|password)"
            r"['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{12,}"
        ),
    ),
    # The length floor keeps prose such as "the bearer of bad news" intact.
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_.~+/-]{20,}=*")),
    # Standalone keys such as sk-proj-... or sk-ant-...; requiring a digit
    # keeps kebab-case words like sk-learn-tutorial-for-beginners intact.
    ("api-key", re.compile(r"\bsk-(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{20,}")),
    ("private-key", re.compile(_PRIVATE_KEY)),
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
