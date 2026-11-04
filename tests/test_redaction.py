from __future__ import annotations

import pytest

from agentdir.redaction import looks_secret_bearing, redact_text

# Secret-looking values are assembled at runtime so this file contains no
# realistic secret strings of its own.
AWS_SECRET = "wJalrXUtnFEMI" + "/K7MDENG/bPxRfiCY" + "EXAMPLEKEY"
JWT = "eyJhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "SflKxwRJSMeKKF2QT4fwpM"
SK_KEY = "sk-" + "proj-" + "Ab3dEf6hIj9kLm2nOp5q" + "Rs8tUv1wXy4z"
PLAIN_VALUE = "Zx9" + "q" * 14 + "42"
KEY_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" + "BKcwggSjAgEAAoIBAQC7" * 3
PEM_BEGIN = "-----BEGIN " + "PRIVATE KEY-----"
PEM_END = "-----END " + "PRIVATE KEY-----"

SECRETS = {
    "aws secret key env var": (
        f"export AWS_SECRET_ACCESS_KEY={AWS_SECRET}", AWS_SECRET, "key-value-secret"
    ),
    "aws credentials file": (
        f"aws_secret_access_key = {AWS_SECRET}", AWS_SECRET, "key-value-secret"
    ),
    "aws sts json": (
        f'{{"SecretAccessKey": "{AWS_SECRET}"}}', AWS_SECRET, "key-value-secret"
    ),
    "token env var": (f"GITHUB_AUTH_TOKEN={PLAIN_VALUE}", PLAIN_VALUE, "key-value-secret"),
    "json password": (f'{{"password": "{PLAIN_VALUE}"}}', PLAIN_VALUE, "key-value-secret"),
    "json api key": (f'{{"api_key": "{PLAIN_VALUE}"}}', PLAIN_VALUE, "key-value-secret"),
    "authorization header": (f"Authorization: Bearer {JWT}", JWT, "bearer-token"),
    "standalone sk key": (f"openai --key {SK_KEY} chat", SK_KEY, "api-key"),
    "private key block": (
        f"{PEM_BEGIN}\n{KEY_BODY}\n{KEY_BODY[:40]}\n{PEM_END}\n", KEY_BODY, "private-key"
    ),
    "json-escaped private key": (
        f'{{"key": "{PEM_BEGIN}\\n{KEY_BODY}\\n{PEM_END}\\n"}}', KEY_BODY, "private-key"
    ),
    "truncated private key": (
        f"{PEM_BEGIN}\n{KEY_BODY}\n{KEY_BODY[:40]}", KEY_BODY, "private-key"
    ),
    "key body left by older versions": (
        f"<redacted:private-key>\n{KEY_BODY}\n{PEM_END}", KEY_BODY, "private-key"
    ),
}


@pytest.mark.parametrize(("text", "secret", "label"), SECRETS.values(), ids=list(SECRETS))
def test_redact_text_masks_secret(text: str, secret: str, label: str) -> None:
    assert looks_secret_bearing(text)

    result = redact_text(text)

    assert secret not in result.text
    assert label in result.labels
    # Redacted output must not look secret-bearing, or doctor would flag it.
    assert not looks_secret_bearing(result.text)
    assert redact_text(result.text).replacements == 0


def test_private_key_block_is_masked_through_its_end_line() -> None:
    text = f"before\n{PEM_BEGIN}\n{KEY_BODY}\n{PEM_END}\nafter"

    assert redact_text(text).text == "before\n<redacted:private-key>\nafter"


ORDINARY_TEXT = [
    "tokenizer: context_selection_v2",
    "passwords: must_be_rotated_quarterly",
    "secret_name=prod-db-credentials",
    "max_tokens=4096 temperature=0.2",
    "the bearer of bad news",
    "bearer tokens should be short-lived",
    "curl -H 'Authorization: Bearer $GITHUB_TOKEN' https://api.github.com",
    "risk-assessment-framework-for-q3-review",
    "sk-learn-tutorial-for-beginners-and-experts",
    "<redacted:private-key>\nDone.",
    "GITHUB_AUTH_<redacted:key-value-secret>",
]


@pytest.mark.parametrize("text", ORDINARY_TEXT)
def test_redact_text_leaves_ordinary_text_alone(text: str) -> None:
    assert not looks_secret_bearing(text)

    result = redact_text(text)

    assert result.text == text
    assert result.replacements == 0
