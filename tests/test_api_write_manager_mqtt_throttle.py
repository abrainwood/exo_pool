from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests.conftest import load_exo_pool_module

api = load_exo_pool_module("api")


@pytest.fixture
def entry(hass):
    config_entry = MockConfigEntry(
        domain=api.DOMAIN,
        data={"serial_number": "JT00000000", "id_token": "tok"},
        options={},
    )
    config_entry.add_to_hass(hass)
    api._get_entry_store(hass, config_entry)
    return config_entry


@pytest.fixture(autouse=True)
def fake_client_session(monkeypatch):
    monkeypatch.setattr(
        api.aiohttp_client, "async_get_clientsession", MagicMock(return_value=MagicMock())
    )


@pytest.fixture
def connected_mqtt(hass, entry):
    store = api._get_entry_store(hass, entry)
    client = MagicMock()
    client.connected = True
    client.publish_desired = MagicMock()
    store["mqtt_client"] = client
    return client


async def test_two_back_to_back_mqtt_writes_publish_with_no_cooldown_or_gap_sleep(
    hass, entry, connected_mqtt, monkeypatch
):
    sleep_mock = AsyncMock()
    monkeypatch.setattr(api.asyncio, "sleep", sleep_mock)

    await api.set_pool_value(hass, entry, "production", 1)
    await api.set_pool_value(hass, entry, "swc", 40)

    assert connected_mqtt.publish_desired.call_count == 2
    sleep_mock.assert_not_called()
    store = api._get_entry_store(hass, entry)
    assert store.get("cooldown_until", 0.0) == 0.0


async def test_rest_fallback_write_still_sets_the_post_write_cooldown(
    hass, entry, monkeypatch
):
    monkeypatch.setattr(api, "_execute_write_rest", AsyncMock(return_value=None))
    monkeypatch.setattr(api.asyncio, "sleep", AsyncMock())
    clock = [1000.0]
    monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])

    await api.set_pool_value(hass, entry, "production", 1)

    store = api._get_entry_store(hass, entry)
    assert store["cooldown_until"] == pytest.approx(
        clock[0] + api.POST_WRITE_COOLDOWN_SECONDS
    )
    assert store["write_quiet_until"] == pytest.approx(
        clock[0] + api.POST_WRITE_COOLDOWN_SECONDS
    )
