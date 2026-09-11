from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests.conftest import load_exo_pool_module

api = load_exo_pool_module("api")

SECRET_VALUES = (
    "auth-tok-abc123",
    "id-tok-abc123",
    "refresh-tok-abc123",
    "AKIA-FAKE-ACCESS-KEY",
    "FAKE-SECRET-ACCESS-KEY",
    "FAKE-SESSION-TOKEN",
)

LOGIN_RESPONSE = {
    "authentication_token": "auth-tok-abc123",
    "id": 999,
    "userPoolOAuth": {
        "IdToken": "id-tok-abc123",
        "RefreshToken": "refresh-tok-abc123",
        "ExpiresIn": 3600,
    },
    "credentials": {
        "AccessKeyId": "AKIA-FAKE-ACCESS-KEY",
        "SecretKey": "FAKE-SECRET-ACCESS-KEY",
        "SessionToken": "FAKE-SESSION-TOKEN",
        "Expiration": "2026-04-15T10:00:00.000Z",
    },
}


class _FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self.headers: dict = {}
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

    def get(self, url, headers=None):
        return self._response


@pytest.fixture
def entry(hass):
    config_entry = MockConfigEntry(
        domain=api.DOMAIN,
        data={
            "serial_number": "JT00000000",
            "email": "pool.owner@example.com",
            "password": "hunter2",
            "refresh_token": "old-refresh-tok",
            "id_token": "id-tok-abc123",
        },
        options={},
    )
    config_entry.add_to_hass(hass)
    api._get_entry_store(hass, config_entry)
    return config_entry


async def test_full_login_does_not_log_any_secret_value(hass, entry, caplog):
    session = _FakeSession(_FakeResponse(200, LOGIN_RESPONSE))

    with caplog.at_level(logging.DEBUG):
        await api._full_login(hass, entry, session)

    log_text = caplog.text
    for secret in SECRET_VALUES:
        assert secret not in log_text


async def test_async_update_data_does_not_log_even_a_truncated_prefix_of_the_refreshed_id_token(
    hass, entry, monkeypatch, caplog
):
    new_id_token = "id-tok-abc123"

    async def fake_refresh_token(hass, entry, session):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "id_token": new_id_token}
        )
        return True

    monkeypatch.setattr(api, "_refresh_token", fake_refresh_token)
    monkeypatch.setattr(
        api.aiohttp_client,
        "async_get_clientsession",
        lambda hass: _FakeSession(
            _FakeResponse(200, {"state": {"reported": {"equipment": {}}}})
        ),
    )

    with caplog.at_level(logging.DEBUG):
        await api.async_update_data(hass, entry)

    assert new_id_token[:10] not in caplog.text


async def test_write_rate_limited_response_does_not_log_a_secret_shaped_body_value(
    hass, entry, monkeypatch, caplog
):
    rate_limited_body = json.dumps({"message": "Too Many Requests", "id_token": "id-tok-abc123"})
    session = _FakeSession(_FakeResponse(429, {}))
    monkeypatch.setattr(
        api.aiohttp_client, "async_get_clientsession", lambda hass: session
    )
    monkeypatch.setattr(
        api,
        "_post_write",
        AsyncMock(return_value=(429, rate_limited_body)),
    )

    item = api._WriteItem(kind="pool", key="pool:filter_pump", target="filter_pump", payload={})

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Exception):
            await api._execute_write_rest(hass, entry, item, {"filter_pump": {}})

    assert "id-tok-abc123" not in caplog.text


async def test_write_rate_limited_non_json_body_does_not_log_the_raw_body(
    hass, entry, monkeypatch, caplog
):
    non_json_body = "<html>upstream outage, token=id-tok-abc123</html>"
    session = _FakeSession(_FakeResponse(429, {}))
    monkeypatch.setattr(
        api.aiohttp_client, "async_get_clientsession", lambda hass: session
    )
    monkeypatch.setattr(
        api,
        "_post_write",
        AsyncMock(return_value=(429, non_json_body)),
    )

    item = api._WriteItem(kind="pool", key="pool:filter_pump", target="filter_pump", payload={})

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Exception):
            await api._execute_write_rest(hass, entry, item, {"filter_pump": {}})

    assert "id-tok-abc123" not in caplog.text


async def test_refresh_token_does_not_log_any_secret_value(hass, entry, caplog):
    session = _FakeSession(_FakeResponse(200, LOGIN_RESPONSE))

    with caplog.at_level(logging.DEBUG):
        await api._refresh_token(hass, entry, session)

    log_text = caplog.text
    for secret in SECRET_VALUES:
        assert secret not in log_text
