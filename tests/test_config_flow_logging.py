from __future__ import annotations

import json
import logging
import urllib.parse
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
    assert "leak-me" not in log_text


async def test_config_flow_login_missing_id_token_does_not_log_the_partial_response(
    hass, monkeypatch, caplog
):
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
    def __init__(self, exc: Exception, status: int = 200):
        self._exc = exc
        self.status = status

    async def json(self):
        raise self._exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _RaisingSession:
    def __init__(self, exc: Exception):
        self._exc = exc

    def post(self, url, json=None, headers=None):  # noqa: A002 - matches aiohttp signature
        raise self._exc


async def test_select_system_content_type_error_does_not_log_the_query_string_secrets(
    hass, monkeypatch, caplog
):
    leaking_url = URL(
        "https://r-api.iaqualink.net/devices.json"
        f"?api_key={config_flow.API_KEY_R}&authentication_token=auth-tok-abc123"
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


async def test_select_system_connection_timeout_does_not_log_the_query_string_secrets(
    hass, monkeypatch, caplog
):
    leaking_url = (
        "https://r-api.iaqualink.net/devices.json"
        f"?api_key={config_flow.API_KEY_R}&authentication_token=auth-tok-abc123"
    )
    timeout_error = aiohttp.ServerTimeoutError(f"Connection timeout to host {leaking_url}")
    session = _FakeSession(get_response=_RaisingResponse(timeout_error))
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
    assert config_flow.API_KEY_R not in caplog.text


async def test_select_system_arbitrary_exception_does_not_log_the_query_string_secrets(
    hass, monkeypatch, caplog
):
    leaking_url = (
        "https://r-api.iaqualink.net/devices.json"
        f"?api_key={config_flow.API_KEY_R}&authentication_token=auth-tok-abc123"
    )
    session = _FakeSession(
        get_response=_RaisingResponse(RuntimeError(f"boom while fetching {leaking_url}"))
    )
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
    assert config_flow.API_KEY_R not in caplog.text


async def test_select_system_exception_does_not_leak_percent_encoded_token_or_user_id(
    hass, monkeypatch, caplog
):
    token = "auth/tok+abc=123"
    leaking_url = (
        "https://r-api.iaqualink.net/devices.json"
        f"?authentication_token={urllib.parse.quote(token, safe='')}&user_id=987654321"
    )
    session = _FakeSession(
        get_response=_RaisingResponse(RuntimeError(f"boom while fetching {leaking_url}"))
    )
    monkeypatch.setattr(
        config_flow.aiohttp_client, "async_get_clientsession", lambda hass: session
    )

    flow = config_flow.ExoPoolConfigFlow()
    flow.hass = hass
    flow.auth_token = token
    flow.user_id = 987654321

    with caplog.at_level(logging.DEBUG):
        await flow.async_step_select_system()

    assert token not in caplog.text
    assert urllib.parse.quote(token, safe="") not in caplog.text
    assert "987654321" not in caplog.text


async def test_select_system_client_response_error_logs_the_status_but_not_the_url(
    hass, monkeypatch, caplog
):
    leaking_url = URL(
        "https://r-api.iaqualink.net/devices.json"
        f"?api_key={config_flow.API_KEY_R}&authentication_token=auth-tok-abc123"
    )
    request_info = aiohttp.RequestInfo(
        url=leaking_url, method="GET", headers={}, real_url=leaking_url
    )
    response_error = aiohttp.ClientResponseError(
        request_info, (), status=429, message="Too Many Requests", headers={}
    )
    session = _FakeSession(get_response=_RaisingResponse(response_error))
    monkeypatch.setattr(
        config_flow.aiohttp_client, "async_get_clientsession", lambda hass: session
    )

    flow = config_flow.ExoPoolConfigFlow()
    flow.hass = hass
    flow.auth_token = "auth-tok-abc123"
    flow.user_id = 999

    with caplog.at_level(logging.DEBUG):
        await flow.async_step_select_system()

    assert "429" in caplog.text
    assert "auth-tok-abc123" not in caplog.text
    assert config_flow.API_KEY_R not in caplog.text


async def test_login_client_response_error_logs_the_status_but_not_the_url(
    hass, monkeypatch, caplog
):
    leaking_url = URL(
        f"{config_flow.LOGIN_URL}?api_key={config_flow.API_KEY_PROD}"
    )
    request_info = aiohttp.RequestInfo(
        url=leaking_url, method="POST", headers={}, real_url=leaking_url
    )
    response_error = aiohttp.ClientResponseError(
        request_info, (), status=429, message="Too Many Requests", headers={}
    )
    session = _RaisingSession(response_error)
    monkeypatch.setattr(
        config_flow.aiohttp_client, "async_get_clientsession", lambda hass: session
    )

    flow = config_flow.ExoPoolConfigFlow()
    flow.hass = hass

    with caplog.at_level(logging.DEBUG):
        await flow.async_step_user({"email": "pool.owner@example.com", "password": "hunter2"})

    assert "429" in caplog.text
    assert config_flow.API_KEY_PROD not in caplog.text


async def test_login_arbitrary_exception_does_not_log_the_query_string_secrets(
    hass, monkeypatch, caplog
):
    session = _RaisingSession(
        RuntimeError(
            f"boom while posting to {config_flow.LOGIN_URL}"
            f"?api_key={config_flow.API_KEY_PROD}"
        )
    )
    monkeypatch.setattr(
        config_flow.aiohttp_client, "async_get_clientsession", lambda hass: session
    )

    flow = config_flow.ExoPoolConfigFlow()
    flow.hass = hass

    with caplog.at_level(logging.DEBUG):
        await flow.async_step_user({"email": "pool.owner@example.com", "password": "hunter2"})

    assert config_flow.API_KEY_PROD not in caplog.text


async def test_login_json_parse_failure_does_not_log_the_raw_response_body(
    hass, monkeypatch, caplog
):
    raw_body = '{"password": "hunter2", "email": "pool.owner@example.com"'
    json_error = ValueError(f"Expecting ',' delimiter: {raw_body}")
    session = _FakeSession(post_response=_RaisingResponse(json_error))
    monkeypatch.setattr(
        config_flow.aiohttp_client, "async_get_clientsession", lambda hass: session
    )

    flow = config_flow.ExoPoolConfigFlow()
    flow.hass = hass

    with caplog.at_level(logging.DEBUG):
        await flow.async_step_user({"email": "pool.owner@example.com", "password": "hunter2"})

    assert "hunter2" not in caplog.text
