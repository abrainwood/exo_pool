"""Tests for the MQTT reconnect retry chain (bounded exponential backoff).

Regression coverage for issue #2: a transient failure inside
_refresh_authentication used to be terminal because only _connect_mqtt's
own success/failure paths re-armed anything. These tests drive the retry
chain from the failure path itself, the way the real DNS-outage did.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import pathlib
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

# Load api.py directly: custom_components.exo_pool is a fake stub package
# in sys.modules (see conftest.py), so its __init__ can't be reached normally.
_API_PATH = pathlib.Path(__file__).parent.parent / "custom_components" / "exo_pool" / "api.py"
_spec = importlib.util.spec_from_file_location("custom_components.exo_pool.api", _API_PATH)
api = importlib.util.module_from_spec(_spec)
sys.modules["custom_components.exo_pool.api"] = api
_spec.loader.exec_module(api)


@pytest.fixture
def entry(hass):
    config_entry = MockConfigEntry(
        domain=api.DOMAIN, data={"serial_number": "JT00000000"}, options={}
    )
    config_entry.add_to_hass(hass)
    return config_entry


@pytest.fixture(autouse=True)
def fake_client_session(monkeypatch):
    """Stub the HA aiohttp helper - these tests don't hit the network."""
    monkeypatch.setattr(
        api.aiohttp_client, "async_get_clientsession", MagicMock(return_value=MagicMock())
    )


async def _cancel_retry_task(hass, entry) -> None:
    """Cancel and await any pending mqtt_retry_task so teardown sees no lingering task."""
    store = api._get_entry_store(hass, entry)
    if task := store.pop("mqtt_retry_task", None):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_dns_failure_inside_refresh_authentication_leaves_a_retry_task_armed(
    hass, entry, monkeypatch
):
    monkeypatch.setattr(
        api, "_refresh_authentication", AsyncMock(side_effect=OSError("DNS timeout"))
    )

    try:
        await api._async_refresh_and_reconnect(hass, entry)

        store = api._get_entry_store(hass, entry)
        retry_task = store.get("mqtt_retry_task")
        assert retry_task is not None
        assert not retry_task.done()
    finally:
        await _cancel_retry_task(hass, entry)


async def test_repeated_failures_grow_backoff_and_clamp_at_the_fifteen_minute_cap(
    hass, entry
):
    observed_delays = []
    try:
        for _ in range(6):
            api._schedule_mqtt_retry(hass, entry)
            store = api._get_entry_store(hass, entry)
            observed_delays.append(store["mqtt_retry_delay"])

        assert observed_delays == [60.0, 120.0, 240.0, 480.0, 900.0, 900.0]
    finally:
        await _cancel_retry_task(hass, entry)


async def test_a_retry_that_succeeds_resets_the_backoff_to_base(
    hass, entry, monkeypatch
):
    monkeypatch.setattr(api, "MQTT_RETRY_BASE_DELAY", 0.01)
    monkeypatch.setattr(
        api, "_refresh_authentication", AsyncMock(side_effect=[OSError("DNS timeout"), None])
    )
    monkeypatch.setattr(api, "_connect_mqtt", MagicMock(return_value=True))

    await api._async_refresh_and_reconnect(hass, entry)
    store = api._get_entry_store(hass, entry)
    first_retry_task = store["mqtt_retry_task"]

    await first_retry_task

    assert store["mqtt_retry_delay"] == 0.01
    assert store.get("mqtt_retry_task") is None


async def test_connect_mqtt_wires_the_watchdog_callback_to_force_a_reconnect(
    hass, entry, monkeypatch
):
    store = api._get_entry_store(hass, entry)
    store["aws_credentials"] = {"Expiration": ""}
    store["coordinator"] = MagicMock()

    fake_mqtt_client = MagicMock()
    fake_mqtt_client.connect.return_value = None
    fake_client_cls = MagicMock(return_value=fake_mqtt_client)
    monkeypatch.setattr(
        sys.modules["custom_components.exo_pool.mqtt_client"],
        "ExoMqttClient",
        fake_client_cls,
    )
    reconnect = AsyncMock()
    monkeypatch.setattr(api, "_async_refresh_and_reconnect", reconnect)

    api._connect_mqtt(hass, entry)

    fake_mqtt_client.set_interrupted_watchdog_callback.assert_called_once()
    watchdog_fire = fake_mqtt_client.set_interrupted_watchdog_callback.call_args.args[0]

    watchdog_fire()
    await hass.async_block_till_done()

    reconnect.assert_called_once_with(hass, entry)


async def test_connect_mqtt_wires_the_state_changed_callback_to_coordinator_listeners(
    hass, entry, monkeypatch
):
    store = api._get_entry_store(hass, entry)
    store["aws_credentials"] = {"Expiration": ""}
    coordinator = MagicMock()
    store["coordinator"] = coordinator

    fake_mqtt_client = MagicMock()
    fake_mqtt_client.connect.return_value = None
    monkeypatch.setattr(
        sys.modules["custom_components.exo_pool.mqtt_client"],
        "ExoMqttClient",
        MagicMock(return_value=fake_mqtt_client),
    )

    api._connect_mqtt(hass, entry)

    fake_mqtt_client.set_state_changed_callback.assert_called_once()
    state_changed = fake_mqtt_client.set_state_changed_callback.call_args.args[0]

    state_changed(False)

    coordinator.async_update_listeners.assert_called_once()


async def test_cleanup_entry_cancels_the_pending_mqtt_retry_task(hass, entry):
    api._schedule_mqtt_retry(hass, entry)
    store = api._get_entry_store(hass, entry)
    retry_task = store["mqtt_retry_task"]

    api.cleanup_entry(hass, entry)
    with contextlib.suppress(asyncio.CancelledError):
        await retry_task

    assert retry_task.done()


async def test_connect_mqtt_failing_without_raising_also_leaves_a_retry_task_armed(
    hass, entry, monkeypatch
):
    monkeypatch.setattr(api, "_refresh_authentication", AsyncMock(return_value=None))
    monkeypatch.setattr(api, "_connect_mqtt", MagicMock(return_value=False))

    try:
        await api._async_refresh_and_reconnect(hass, entry)

        store = api._get_entry_store(hass, entry)
        retry_task = store.get("mqtt_retry_task")
        assert retry_task is not None
        assert not retry_task.done()
    finally:
        await _cancel_retry_task(hass, entry)
