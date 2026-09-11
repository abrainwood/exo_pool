"""Shared credential redaction for logs and diagnostics."""
from __future__ import annotations

from typing import Iterable

from homeassistant.components.diagnostics import async_redact_data

SENSITIVE_KEYS: set[str] = {
    "email",
    "password",
    "auth_token",
    "id_token",
    "refresh_token",
    "access_token",
    "api_key",
    "user_id",
    "IdToken",
    "RefreshToken",
    "authentication_token",
    "AccessKeyId",
    "SecretKey",
    "SessionToken",
    "AccessToken",
    "Authorization",
}

_API_RESPONSE_KEYS: set[str] = SENSITIVE_KEYS | {"id"}


def scrub_text(text: str, secrets: Iterable[str | None]) -> str:
    """Replace any occurrence of a known secret value in free text."""
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "REDACTED")
    return text


def redact(data: object) -> object:
    """Return a copy of data with known-secret keys redacted, any depth."""
    if isinstance(data, dict):
        return async_redact_data(data, _API_RESPONSE_KEYS)
    if isinstance(data, list) and any(isinstance(item, (dict, list)) for item in data):
        return async_redact_data(data, _API_RESPONSE_KEYS)
    return f"<redacted {type(data).__name__}, no keys to redact against>"
