from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock

import aiohttp
from yarl import URL

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
    def __init__(self, post_response: _FakeResponse | None = None, get_response=None):
        self._post_response = post_response
        self._get_response = get_response

    def post(self, url, json=None, headers=None):  # noqa: A002 - matches aiohttp signature
        return self._post_response

    def get(self, url):
        return self._get_response


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


async def test_config_flow_login_missing_actual_id_token_value_does_not_log_the_response(
    hass, monkeypatch, caplog
):
    # status=200, authentication_token and userPoolOAuth both present, but
    # IdToken itself is falsy - takes the *success*-shaped branch's own
    # "missing IdToken" error, a different leak site than the invalid-shape
    # branch already covered above.
    session = _FakeSession(
        _FakeResponse(
            200,
            {
                "authentication_token": "auth-tok-abc123",
                "id": 999,
                "userPoolOAuth": {"IdToken": None},
            },
        )
    )
    monkeypatch.setattr(
        config_flow.aiohttp_client, "async_get_clientsession", lambda hass: session
    )

    flow = config_flow.ExoPoolConfigFlow()
    flow.hass = hass

    with caplog.at_level(logging.DEBUG):
        await flow.async_step_user({"email": "pool.owner@example.com", "password": "hunter2"})

    assert "auth-tok-abc123" not in caplog.text


class _RaisingResponse:
    def __init__(self, exc: Exception):
        self._exc = exc

    async def json(self):
        raise self._exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


async def test_select_system_content_type_error_does_not_log_the_query_string_secrets(
    hass, monkeypatch, caplog
):
    # A non-JSON error body (e.g. an outage HTML page) makes resp.json()
    # raise aiohttp.ContentTypeError, whose own __str__ embeds the full
    # request URL - including the authentication_token query param.
    leaking_url = URL(
        "https://r-api.iaqualink.net/devices.json"
        "?api_key=EOOEMOW4YR6QNB07&authentication_token=auth-tok-abc123"
    )
    request_info = aiohttp.RequestInfo(
        url=leaking_url, method="GET", headers={}, real_url=leaking_url
    )
    content_type_error = aiohttp.ContentTypeError(
        request_info,
        (),
        message="Attempt to decode JSON with unexpected mimetype: text/html",
        headers={},
    )
    session = _FakeSession(get_response=_RaisingResponse(content_type_error))
    monkeypatch.setattr(
        config_flow.aiohttp_client, "async_get_clientsession", lambda hass: session
    )

    flow = config_flow.ExoPoolConfigFlow()
    flow.hass = hass
    flow.auth_token = "auth-tok-abc123"
    flow.user_id = 999

    with caplog.at_level(logging.DEBUG):
        await flow.async_step_select_system()

    assert "auth-tok-abc123" not in caplog.text


DEVICES_RESPONSE = [
    {
        "serial_number": "JT00000000",
        "name": "Backyard Pool",
        "device_type": "exo",
        "authentication_token": "auth-tok-abc123",
    }
]


async def test_select_system_does_not_log_any_secret_value_from_the_device_list(
    hass, monkeypatch, caplog
):
    session = _FakeSession(get_response=_FakeResponse(200, DEVICES_RESPONSE))
    monkeypatch.setattr(
        config_flow.aiohttp_client, "async_get_clientsession", lambda hass: session
    )

    flow = config_flow.ExoPoolConfigFlow()
    flow.hass = hass
    flow.auth_token = "auth-tok-abc123"
    flow.user_id = 999

    with caplog.at_level(logging.DEBUG):
        await flow.async_step_select_system()

    assert "auth-tok-abc123" not in caplog.text
