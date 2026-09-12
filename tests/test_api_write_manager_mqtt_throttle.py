from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tests.conftest import load_exo_pool_module

api = load_exo_pool_module("api")


async def test_two_back_to_back_mqtt_writes_are_both_delivered(
    hass, entry, connected_mqtt, monkeypatch
):
    monkeypatch.setattr(api.asyncio, "sleep", AsyncMock())

    await api.set_pool_value(hass, entry, "production", 1)
    await api.set_pool_value(hass, entry, "swc", 40)

    assert connected_mqtt.publish_desired.call_count == 2


async def test_two_back_to_back_mqtt_writes_sleep_neither_cooldown_nor_gap(
    hass, entry, connected_mqtt, monkeypatch
):
    sleep_mock = AsyncMock()
    monkeypatch.setattr(api.asyncio, "sleep", sleep_mock)

    await api.set_pool_value(hass, entry, "production", 1)
    await api.set_pool_value(hass, entry, "swc", 40)

    sleep_mock.assert_not_called()
    store = api._get_entry_store(hass, entry)
    assert store.get("cooldown_until", 0.0) == 0.0


async def test_mqtt_write_leaves_write_quiet_until_unset(
    hass, entry, connected_mqtt, monkeypatch
):
    monkeypatch.setattr(api.asyncio, "sleep", AsyncMock())

    await api.set_pool_value(hass, entry, "production", 1)

    store = api._get_entry_store(hass, entry)
    assert "write_quiet_until" not in store


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


async def test_rest_fallback_write_still_sleeps_the_write_gap(hass, entry, monkeypatch):
    monkeypatch.setattr(api, "_execute_write_rest", AsyncMock(return_value=None))
    sleep_mock = AsyncMock()
    monkeypatch.setattr(api.asyncio, "sleep", sleep_mock)

    await api.set_pool_value(hass, entry, "production", 1)

    sleep_mock.assert_any_call(api.WRITE_GAP_SECONDS)


async def test_rest_fallback_cooldown_includes_extra_delay_for_delay_refresh_writes(
    hass, entry, monkeypatch
):
    monkeypatch.setattr(api, "_execute_write_rest", AsyncMock(return_value=None))
    monkeypatch.setattr(api.asyncio, "sleep", AsyncMock())
    clock = [1000.0]
    monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])

    await api.set_pool_value(hass, entry, "production", 1, delay_refresh=True)

    store = api._get_entry_store(hass, entry)
    assert store["cooldown_until"] == pytest.approx(
        clock[0] + api.POST_WRITE_COOLDOWN_SECONDS + 10.0
    )


async def test_mqtt_publish_raises_falls_back_to_rest(hass, entry, connected_mqtt, monkeypatch):
    connected_mqtt.publish_desired.side_effect = ConnectionError("dropped")
    execute_rest = AsyncMock(return_value=None)
    monkeypatch.setattr(api, "_execute_write_rest", execute_rest)
    monkeypatch.setattr(api.asyncio, "sleep", AsyncMock())

    await api.set_pool_value(hass, entry, "production", 1)

    execute_rest.assert_called_once()
