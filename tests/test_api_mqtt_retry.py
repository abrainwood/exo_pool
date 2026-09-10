from __future__ import annotations

import asyncio
import contextlib
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests.conftest import load_exo_pool_module

api = load_exo_pool_module("api")


@pytest.fixture
def entry(hass):
    config_entry = MockConfigEntry(
        domain=api.DOMAIN, data={"serial_number": "JT00000000"}, options={}
    )
    config_entry.add_to_hass(hass)
    # Mirror async_setup_entry: create the entry's store so it counts as
    # "loaded" for _entry_is_loaded, the way it always is by the time
    # any of these reconnect paths can actually run in production.
    api._get_entry_store(hass, config_entry)
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
    task = store.pop("mqtt_retry_task", None)
    if isinstance(task, asyncio.Task):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.fixture
def captured_reconnect_coros(hass, monkeypatch):
    """Intercept hass.async_create_background_task on the retry chain.

    A mocked instant asyncio.sleep makes a real background task race ahead
    of the test coroutine - each hop schedules the next before the test's
    own await resumes, so awaiting "the" retry task doesn't bound the chain
    to one hop. Capturing the coroutines lets a test await exactly one hop
    at a time, deterministically.
    """
    captured: list = []

    def _fake_create_task(coro, name=None):
        captured.append(coro)
        return MagicMock(name=name, done=MagicMock(return_value=False))

    monkeypatch.setattr(hass, "async_create_background_task", _fake_create_task)
    yield captured
    for coro in captured:
        coro.close()


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
    base = api.MQTT_RETRY_BASE_DELAY
    cap = api.MQTT_RETRY_MAX_DELAY
    expected = []
    delay = base
    for _ in range(6):
        delay = min(delay * 2, cap)
        expected.append(delay)

    observed_delays = []
    try:
        for _ in range(6):
            api._schedule_mqtt_retry(hass, entry)
            store = api._get_entry_store(hass, entry)
            observed_delays.append(store["mqtt_retry_delay"])

        assert observed_delays == expected
    finally:
        await _cancel_retry_task(hass, entry)


async def test_schedule_mqtt_retry_adds_jitter_on_top_of_the_backoff_delay(
    hass, entry, monkeypatch, captured_reconnect_coros
):
    sleep_mock = AsyncMock()
    monkeypatch.setattr(api.asyncio, "sleep", sleep_mock)
    monkeypatch.setattr(api.random, "uniform", lambda a, b: 5.0)
    monkeypatch.setattr(api, "_refresh_authentication", AsyncMock(return_value=None))
    monkeypatch.setattr(api, "_connect_mqtt", MagicMock(return_value=True))

    api._schedule_mqtt_retry(hass, entry)
    await captured_reconnect_coros[0]

    sleep_mock.assert_called_once_with(api.MQTT_RETRY_BASE_DELAY + 5.0)


async def test_a_retry_that_succeeds_resets_the_backoff_to_base(
    hass, entry, monkeypatch, captured_reconnect_coros
):
    monkeypatch.setattr(api.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        api, "_refresh_authentication", AsyncMock(side_effect=[OSError("DNS timeout"), None])
    )
    monkeypatch.setattr(api, "_connect_mqtt", MagicMock(return_value=True))

    await api._async_refresh_and_reconnect(hass, entry)
    store = api._get_entry_store(hass, entry)

    # The success runs from inside this coroutine itself, so this also
    # exercises the self-cancel guard in _reset_mqtt_retry_backoff.
    await captured_reconnect_coros[0]

    assert store["mqtt_retry_delay"] == api.MQTT_RETRY_BASE_DELAY
    assert store.get("mqtt_retry_task") is None


async def test_three_consecutive_failures_through_the_real_chain_each_rearm_the_next(
    hass, entry, monkeypatch, captured_reconnect_coros
):
    monkeypatch.setattr(api.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        api, "_refresh_authentication", AsyncMock(side_effect=OSError("still down"))
    )

    await api._async_refresh_and_reconnect(hass, entry)
    store = api._get_entry_store(hass, entry)

    seen_attempts = []
    try:
        for i in range(3):
            await captured_reconnect_coros[i]
            seen_attempts.append(store["mqtt_retry_attempts"])

        # An impl that arms a retry once and stops re-arming would stall at
        # [2, 2, 2]: no second or third coroutine would ever get captured
        # to await, since nothing would call async_create_background_task
        # again after the first attempt.
        assert seen_attempts == [2, 3, 4]
    finally:
        await _cancel_retry_task(hass, entry)


async def test_retries_keep_rearming_with_no_maximum_attempt_count(
    hass, entry, monkeypatch, captured_reconnect_coros
):
    monkeypatch.setattr(api.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        api, "_refresh_authentication", AsyncMock(side_effect=OSError("still down"))
    )

    await api._async_refresh_and_reconnect(hass, entry)
    store = api._get_entry_store(hass, entry)

    try:
        for i in range(20):
            await captured_reconnect_coros[i]

        assert store["mqtt_retry_attempts"] == 21
        # A 21st attempt was scheduled - the chain never hit a cap.
        assert len(captured_reconnect_coros) == 21
    finally:
        await _cancel_retry_task(hass, entry)


async def test_reconnect_bails_out_when_the_entry_is_no_longer_loaded(
    hass, entry, monkeypatch
):
    refresh = AsyncMock(side_effect=OSError("still down"))
    monkeypatch.setattr(api, "_refresh_authentication", refresh)
    del hass.data[api.DOMAIN][entry.entry_id]

    await api._async_refresh_and_reconnect(hass, entry)

    refresh.assert_not_called()
    assert entry.entry_id not in hass.data.get(api.DOMAIN, {})


async def test_reconnect_skips_credential_refresh_when_credentials_are_still_fresh(
    hass, entry, monkeypatch
):
    from datetime import datetime, timedelta, timezone

    store = api._get_entry_store(hass, entry)
    store["aws_credentials"] = {
        "Expiration": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    }
    refresh = AsyncMock(return_value=None)
    monkeypatch.setattr(api, "_refresh_authentication", refresh)
    monkeypatch.setattr(api, "_connect_mqtt", MagicMock(return_value=True))

    await api._async_refresh_and_reconnect(hass, entry)

    refresh.assert_not_called()


async def test_reconnect_refreshes_credentials_when_they_are_expired(
    hass, entry, monkeypatch
):
    from datetime import datetime, timedelta, timezone

    store = api._get_entry_store(hass, entry)
    store["aws_credentials"] = {
        "Expiration": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    }
    refresh = AsyncMock(return_value=None)
    monkeypatch.setattr(api, "_refresh_authentication", refresh)
    monkeypatch.setattr(api, "_connect_mqtt", MagicMock(return_value=True))

    await api._async_refresh_and_reconnect(hass, entry)

    refresh.assert_called_once()


async def test_trigger_mqtt_reconnect_does_not_double_schedule_while_one_is_in_flight(
    hass, entry, monkeypatch, captured_reconnect_coros
):
    monkeypatch.setattr(
        api, "_refresh_authentication", AsyncMock(side_effect=OSError("still down"))
    )

    # Simulate the watchdog and the re-subscribe-failure path both firing
    # for the same underlying outage before the first attempt has finished.
    api._trigger_mqtt_reconnect(hass, entry, name="watchdog")
    api._trigger_mqtt_reconnect(hass, entry, name="reconnect_failed")

    assert len(captured_reconnect_coros) == 1


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
