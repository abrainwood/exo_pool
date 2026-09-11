from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock

from tests.conftest import load_exo_pool_module

config_flow = load_exo_pool_module("config_flow")

LOGIN_RESPONSE = {
    "authentication_token": "auth-tok-abc123",
    "id": 999,
    "userPoolOAuth": {"IdToken": "id-tok-abc123"},
}


class _FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self.headers: dict = {"Set-Cookie": "session=leak-me"}
        self._payload = payload

    async def json(self):
        return self._payload

    async def text(self):
        return json.dumps(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self._response = response

    def post(self, url, json=None, headers=None):  # noqa: A002 - matches aiohttp signature
        return self._response


async def test_config_flow_login_success_does_not_log_any_secret_value(
    hass, monkeypatch, caplog
):
    session = _FakeSession(_FakeResponse(200, LOGIN_RESPONSE))
    monkeypatch.setattr(
        config_flow.aiohttp_client, "async_get_clientsession", lambda hass: session
    )

    flow = config_flow.ExoPoolConfigFlow()
    flow.hass = hass
    flow.async_step_select_system = AsyncMock(return_value={"type": "form"})

    with caplog.at_level(logging.DEBUG):
        await flow.async_step_user({"email": "pool.owner@example.com", "password": "hunter2"})

    log_text = caplog.text
    for secret in ("auth-tok-abc123", "id-tok-abc123", "hunter2"):
        assert secret not in log_text


async def test_config_flow_login_missing_id_token_does_not_log_the_partial_response(
    hass, monkeypatch, caplog
):
    # authentication_token present but userPoolOAuth missing - takes the
    # "invalid" branch while still holding a real secret in `result`.
    session = _FakeSession(
        _FakeResponse(200, {"authentication_token": "auth-tok-abc123", "id": 999})
    )
    monkeypatch.setattr(
        config_flow.aiohttp_client, "async_get_clientsession", lambda hass: session
    )

    flow = config_flow.ExoPoolConfigFlow()
    flow.hass = hass

    with caplog.at_level(logging.DEBUG):
        await flow.async_step_user({"email": "pool.owner@example.com", "password": "hunter2"})

    assert "auth-tok-abc123" not in caplog.text
