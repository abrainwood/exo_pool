"""Shared credential redaction for logs and diagnostics.

Zodiac's cloud API and our own config entries name the same secrets
differently at different points in the pipeline (entry.data uses
snake_case like id_token; the raw login/refresh response uses
camelCase like IdToken, nested under userPoolOAuth). One key set
covers both so a log line anywhere in the integration redacts
consistently.
"""
from __future__ import annotations

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
}


def redact(data: dict | list) -> dict | list:
    """Return a copy of data with known-secret keys redacted, any depth."""
    return async_redact_data(data, SENSITIVE_KEYS)
