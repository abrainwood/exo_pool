from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from datetime import datetime, timedelta, timezone
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
    store = api._get_entry_store(hass, entry)
    task = store.pop("mqtt_retry_task", None)
    if isinstance(task, asyncio.Task):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.fixture
def real_mqtt_client_with_rejected_subscribes(monkeypatch):
    mqtt_client_mod = sys.modules["custom_components.exo_pool.mqtt_client"]
    real_exo_mqtt_client = mqtt_client_mod.ExoMqttClient

    mock_connection = MagicMock()
    connect_future = MagicMock()
    connect_future.result.return_value = None
    mock_connection.connect.return_value = connect_future
    sub_future = MagicMock()
    sub_future.result.side_effect = Exception("Forbidden")
    mock_connection.subscribe.return_value = (sub_future, 1)

    def _build_real_client(*, loop, endpoint, region, serial):
        client = real_exo_mqtt_client(
            loop=loop, endpoint=endpoint, region=region, serial=serial
        )
        client._build_connection = MagicMock(return_value=mock_connection)
        return client

    monkeypatch.setattr(mqtt_client_mod, "ExoMqttClient", _build_real_client)
    return mock_connection


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

    def _fake_create_task(coro, name=None, **_kwargs):
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


async def test_schedule_mqtt_retry_never_sleeps_past_the_fifteen_minute_cap(
    hass, entry, monkeypatch, captured_reconnect_coros
):
    sleep_mock = AsyncMock()
    monkeypatch.setattr(api.asyncio, "sleep", sleep_mock)
    # Always the maximum possible jitter for whatever bounds are passed.
    monkeypatch.setattr(api.random, "uniform", lambda a, b: b)
    monkeypatch.setattr(api, "_refresh_authentication", AsyncMock(return_value=None))
    monkeypatch.setattr(api, "_connect_mqtt", MagicMock(return_value=True))

    store = api._get_entry_store(hass, entry)
    store["mqtt_retry_delay"] = api.MQTT_RETRY_MAX_DELAY

    api._schedule_mqtt_retry(hass, entry)
    await captured_reconnect_coros[0]

    sleep_mock.assert_called_once()
    assert sleep_mock.call_args.args[0] <= api.MQTT_RETRY_MAX_DELAY


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


async def test_entry_unloaded_during_connect_does_not_resurrect_the_store(
    hass, entry, monkeypatch
):
    monkeypatch.setattr(api, "_refresh_authentication", AsyncMock(return_value=None))

    def _connect_then_unload(hass_, entry_):
        # Simulate async_unload_entry racing in while _connect_mqtt is
        # running on its executor thread.
        del hass_.data[api.DOMAIN][entry_.entry_id]
        return False

    monkeypatch.setattr(api, "_connect_mqtt", _connect_then_unload)

    await api._async_refresh_and_reconnect(hass, entry)

    assert entry.entry_id not in hass.data.get(api.DOMAIN, {})


async def test_reconnect_skips_credential_refresh_when_credentials_are_still_fresh(
    hass, entry, monkeypatch
):

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

    store = api._get_entry_store(hass, entry)
    store["aws_credentials"] = {
        "Expiration": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    }
    refresh = AsyncMock(return_value=None)
    monkeypatch.setattr(api, "_refresh_authentication", refresh)
    monkeypatch.setattr(api, "_connect_mqtt", MagicMock(return_value=True))

    await api._async_refresh_and_reconnect(hass, entry)

    refresh.assert_called_once()


async def test_a_fully_failed_subscribe_does_not_reset_the_backoff(
    hass, entry, monkeypatch, real_mqtt_client_with_rejected_subscribes
):
    store = api._get_entry_store(hass, entry)
    store["aws_credentials"] = {
        "AccessKeyId": "x",
        "SecretKey": "y",
        "SessionToken": "z",
        "Expiration": "",
    }
    store["coordinator"] = MagicMock()
    store["mqtt_retry_delay"] = 120.0
    store["mqtt_retry_attempts"] = 2
    monkeypatch.setattr(api, "_refresh_authentication", AsyncMock(return_value=None))

    try:
        await api._async_refresh_and_reconnect(hass, entry)

        assert store["mqtt_retry_delay"] > 120.0
        assert store["mqtt_retry_attempts"] == 3
    finally:
        await _cancel_retry_task(hass, entry)


async def test_get_coordinator_setup_subscribe_failure_arms_exactly_one_retry(
    hass, entry, monkeypatch, real_mqtt_client_with_rejected_subscribes
):
    store = api._get_entry_store(hass, entry)
    store["aws_credentials"] = {
        "AccessKeyId": "x",
        "SecretKey": "y",
        "SessionToken": "z",
        "Expiration": (datetime.now(timezone.utc) + timedelta(minutes=60)).isoformat(),
    }
    monkeypatch.setattr(api, "_refresh_authentication", AsyncMock(return_value=None))
    coord = MagicMock()
    coord.data = None
    coord.async_config_entry_first_refresh = AsyncMock()
    monkeypatch.setattr(api, "DataUpdateCoordinator", MagicMock(return_value=coord))

    try:
        await api.get_coordinator(hass, entry)

        coord.async_config_entry_first_refresh.assert_awaited_once()
        retry_task = store.get("mqtt_retry_task")
        assert retry_task is not None
        assert not retry_task.done()
        assert store["mqtt_retry_attempts"] == 1
    finally:
        await _cancel_retry_task(hass, entry)


async def test_credential_refresh_reconnect_arms_exactly_one_retry_on_subscribe_failure(
    hass, entry, monkeypatch, real_mqtt_client_with_rejected_subscribes
):
    store = api._get_entry_store(hass, entry)
    store["aws_credentials"] = {
        "AccessKeyId": "x",
        "SecretKey": "y",
        "SessionToken": "z",
        "Expiration": (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
    }
    store["coordinator"] = MagicMock()
    monkeypatch.setattr(api, "_refresh_authentication", AsyncMock(return_value=None))

    api._schedule_credential_refresh(hass, entry)
    refresh_task = store["credential_refresh_task"]

    try:
        with contextlib.suppress(asyncio.CancelledError):
            await refresh_task

        reconnect_attempt = store["mqtt_retry_task"]
        with contextlib.suppress(asyncio.CancelledError):
            await reconnect_attempt

        retry_task = store["mqtt_retry_task"]
        live_retries = [
            t
            for t in asyncio.all_tasks()
            if t.get_name() == "exo_pool_mqtt_retry" and not t.done()
        ]
        assert live_retries == [retry_task]
        assert store["mqtt_retry_attempts"] == 1
    finally:
        await _cancel_retry_task(hass, entry)


def _exo_warnings(caplog):
    return [
        r
        for r in caplog.records
        if r.name.startswith("custom_components.exo_pool") and r.levelno >= logging.WARNING
    ]


async def test_get_coordinator_setup_failure_logs_exactly_one_warning_no_traceback(
    hass, entry, monkeypatch, real_mqtt_client_with_rejected_subscribes, caplog
):
    store = api._get_entry_store(hass, entry)
    store["aws_credentials"] = {
        "AccessKeyId": "x",
        "SecretKey": "y",
        "SessionToken": "z",
        "Expiration": (datetime.now(timezone.utc) + timedelta(minutes=60)).isoformat(),
    }
    monkeypatch.setattr(api, "_refresh_authentication", AsyncMock(return_value=None))
    coord = MagicMock()
    coord.data = None
    coord.async_config_entry_first_refresh = AsyncMock()
    monkeypatch.setattr(api, "DataUpdateCoordinator", MagicMock(return_value=coord))

    try:
        with caplog.at_level(logging.WARNING):
            await api.get_coordinator(hass, entry)

        warnings = _exo_warnings(caplog)
        assert len(warnings) == 1
        assert warnings[0].exc_info is None
        assert "reconnect attempt" not in warnings[0].getMessage()
    finally:
        await _cancel_retry_task(hass, entry)


async def test_get_coordinator_setup_with_no_credentials_logs_the_reason_at_warning(
    hass, entry, monkeypatch, caplog
):
    coord = MagicMock()
    coord.data = None
    coord.async_config_entry_first_refresh = AsyncMock()
    monkeypatch.setattr(api, "DataUpdateCoordinator", MagicMock(return_value=coord))
    monkeypatch.setattr(api, "_refresh_authentication", AsyncMock(return_value=None))

    try:
        with caplog.at_level(logging.WARNING):
            await api.get_coordinator(hass, entry)

        warnings = _exo_warnings(caplog)
        assert len(warnings) == 1
        assert "no AWS credentials" in warnings[0].getMessage()
    finally:
        await _cancel_retry_task(hass, entry)


async def test_credential_refresh_handoff_failure_leaves_the_armed_retry_in_the_slot(
    hass, entry, monkeypatch
):
    store = api._get_entry_store(hass, entry)
    store["aws_credentials"] = {
        "Expiration": (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    }
    monkeypatch.setattr(
        api, "_refresh_authentication", AsyncMock(side_effect=OSError("DNS timeout"))
    )

    api._schedule_credential_refresh(hass, entry)
    refresh_task = store["credential_refresh_task"]

    try:
        with contextlib.suppress(asyncio.CancelledError):
            await refresh_task
        await asyncio.sleep(0)

        slot = store.get("mqtt_retry_task")
        # An eager handoff task can run far enough to arm its own retry and
        # overwrite the slot with itself (now finished) before the trigger's
        # own assignment lands, orphaning the retry it just armed outside
        # the slot cleanup_entry and _schedule_mqtt_retry both key off.
        assert slot is not None
        assert not slot.done()
    finally:
        await _cancel_retry_task(hass, entry)


async def test_credential_refresh_rearms_itself_when_deferred_to_an_in_flight_retry(
    hass, entry, monkeypatch
):
    store = api._get_entry_store(hass, entry)
    store["aws_credentials"] = {
        "Expiration": (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    }
    monkeypatch.setattr(api.asyncio, "sleep", AsyncMock())
    calls = []

    def _trigger(hass_, entry_, *, name):
        calls.append(name)
        # First call defers to an in-flight retry attempt (as
        # _trigger_mqtt_reconnect does when one is already running); the
        # second succeeds once that attempt has cleared.
        return len(calls) > 1

    monkeypatch.setattr(api, "_trigger_mqtt_reconnect", _trigger)

    api._schedule_credential_refresh(hass, entry)
    task = store["credential_refresh_task"]

    try:
        await task

        # A proactive refresh deferred to an in-flight retry must not
        # vanish - a simpler impl that returns on the first False leaves
        # credentials to expire with nothing armed to refresh them.
        assert calls == ["exo_pool_credential_refresh", "exo_pool_credential_refresh"]
    finally:
        await _cancel_retry_task(hass, entry)
        rescheduled = store.get("credential_refresh_task")
        if rescheduled is not None and not rescheduled.done():
            rescheduled.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await rescheduled


async def test_schedule_credential_refresh_does_not_cancel_the_task_currently_running_it(
    hass, entry, monkeypatch
):

    store = api._get_entry_store(hass, entry)
    store["aws_credentials"] = {
        "Expiration": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    }
    monkeypatch.setattr(api.asyncio, "sleep", AsyncMock(return_value=None))
    already_ran = False

    async def _fake_refresh_and_reconnect(hass_, entry_, **kwargs):
        nonlocal already_ran
        if already_ran:
            return
        already_ran = True

        fut = hass_.loop.create_future()

        def _fail_mid_flight() -> None:
            api._schedule_credential_refresh(hass_, entry_)
            if not fut.done():
                fut.set_result(None)

        hass_.loop.call_soon(_fail_mid_flight)
        await fut

    monkeypatch.setattr(api, "_async_refresh_and_reconnect", _fake_refresh_and_reconnect)

    api._schedule_credential_refresh(hass, entry)
    task = store["credential_refresh_task"]

    try:
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert not task.cancelled()
    finally:
        rescheduled = store.get("credential_refresh_task")
        if rescheduled is not None and rescheduled is not task:
            rescheduled.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await rescheduled


async def test_scheduling_credential_refresh_twice_cancels_the_first_task(
    hass, entry
):
    store = api._get_entry_store(hass, entry)
    store["aws_credentials"] = {
        "Expiration": (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
    }

    api._schedule_credential_refresh(hass, entry)
    first_task = store["credential_refresh_task"]
    await asyncio.sleep(0)

    api._schedule_credential_refresh(hass, entry)
    second_task = store["credential_refresh_task"]

    try:
        with contextlib.suppress(asyncio.CancelledError):
            await first_task

        assert first_task.cancelled()
        assert second_task is not first_task

        live = [
            t
            for t in asyncio.all_tasks()
            if t.get_name() == "exo_pool_credential_refresh" and not t.done()
        ]
        assert live == [second_task]
    finally:
        second_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await second_task


async def test_watchdog_reconnect_arms_exactly_one_retry_when_iot_connect_times_out(
    hass, entry, monkeypatch, captured_reconnect_coros
):
    monkeypatch.setattr(api, "_refresh_authentication", AsyncMock(return_value=None))
    monkeypatch.setattr(
        api, "_connect_mqtt", MagicMock(side_effect=TimeoutError("IoT connect timed out"))
    )

    api._trigger_mqtt_reconnect(hass, entry, name="exo_pool_watchdog_reconnect")

    store = api._get_entry_store(hass, entry)
    real_task = asyncio.ensure_future(captured_reconnect_coros[0])
    store["mqtt_retry_task"] = real_task

    try:
        await real_task

        retry_task = store.get("mqtt_retry_task")
        assert retry_task is not None
        assert not retry_task.done()
        assert store["mqtt_retry_delay"] > api.MQTT_RETRY_BASE_DELAY
        assert store["mqtt_retry_attempts"] == 1
        _failed_attempt, _rearmed_retry = captured_reconnect_coros
    finally:
        await _cancel_retry_task(hass, entry)


async def test_trigger_mqtt_reconnect_forces_credential_refresh_even_when_not_expired(
    hass, entry, monkeypatch, captured_reconnect_coros
):

    store = api._get_entry_store(hass, entry)
    store["aws_credentials"] = {
        "Expiration": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    }
    refresh = AsyncMock(return_value=None)
    monkeypatch.setattr(api, "_refresh_authentication", refresh)
    monkeypatch.setattr(api, "_connect_mqtt", MagicMock(return_value=True))

    # _on_reconnect_failed fires precisely because subscribes were rejected
    # despite a future Expiration - a plain expiry check would never
    # re-authenticate here.
    api._trigger_mqtt_reconnect(hass, entry, name="exo_pool_reconnect_refresh")
    await captured_reconnect_coros[0]

    refresh.assert_called_once()


async def test_trigger_mqtt_reconnect_preempts_a_sleeping_backoff_wait(
    hass, entry, monkeypatch, captured_reconnect_coros
):
    monkeypatch.setattr(api, "_refresh_authentication", AsyncMock(return_value=None))
    monkeypatch.setattr(api, "_connect_mqtt", MagicMock(return_value=True))

    # Three failures have already pushed the backoff to 240s and the current
    # retry task is asleep waiting it out - exactly the multi-hour-outage
    # shape the watchdog exists to interrupt.
    store = api._get_entry_store(hass, entry)
    sleeping_task = MagicMock(done=MagicMock(return_value=False))
    store["mqtt_retry_task"] = sleeping_task
    store["mqtt_retry_sleeping"] = True

    api._trigger_mqtt_reconnect(hass, entry, name="exo_pool_watchdog_reconnect")

    sleeping_task.cancel.assert_called_once()
    assert len(captured_reconnect_coros) == 1


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
    hass, entry, monkeypatch, wired_fake_mqtt_client
):
    fake_mqtt_client, _coordinator = wired_fake_mqtt_client
    reconnect = AsyncMock()
    monkeypatch.setattr(api, "_async_refresh_and_reconnect", reconnect)

    api._connect_mqtt(hass, entry)

    fake_mqtt_client.set_interrupted_watchdog_callback.assert_called_once()
    watchdog_fire = fake_mqtt_client.set_interrupted_watchdog_callback.call_args.args[0]

    watchdog_fire()
    await hass.async_block_till_done()

    reconnect.assert_called_once_with(hass, entry, force_credential_refresh=True)


async def test_connect_mqtt_wires_the_reconnect_failed_callback_to_force_a_reconnect(
    hass, entry, monkeypatch, wired_fake_mqtt_client
):
    fake_mqtt_client, _coordinator = wired_fake_mqtt_client
    trigger = MagicMock()
    monkeypatch.setattr(api, "_trigger_mqtt_reconnect", trigger)

    api._connect_mqtt(hass, entry)

    fake_mqtt_client.set_reconnect_failed_callback.assert_called_once()
    reconnect_failed = fake_mqtt_client.set_reconnect_failed_callback.call_args.args[0]

    reconnect_failed()

    trigger.assert_called_once_with(hass, entry, name="exo_pool_reconnect_refresh")


async def test_connect_mqtt_wires_the_state_changed_callback_to_coordinator_listeners(
    hass, entry, monkeypatch, wired_fake_mqtt_client
):
    fake_mqtt_client, coordinator = wired_fake_mqtt_client

    api._connect_mqtt(hass, entry)

    fake_mqtt_client.set_state_changed_callback.assert_called_once()
    state_changed = fake_mqtt_client.set_state_changed_callback.call_args.args[0]

    state_changed(False)

    coordinator.async_update_listeners.assert_called_once()


async def test_reconnect_after_unload_does_not_recreate_the_entry_store(hass, entry):
    api._get_entry_store(hass, entry)
    del hass.data[api.DOMAIN][entry.entry_id]

    api._wake_held_write_on_reconnect(hass, entry, True)

    assert entry.entry_id not in hass.data[api.DOMAIN]


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


async def test_connect_mqtt_failure_does_not_reschedule_credential_refresh(
    hass, entry, monkeypatch
):
    store = api._get_entry_store(hass, entry)
    store["aws_credentials"] = {"Expiration": ""}
    store["coordinator"] = MagicMock()
    fake_mqtt_client = MagicMock()
    fake_mqtt_client.connect.side_effect = Exception("boom")
    monkeypatch.setattr(
        sys.modules["custom_components.exo_pool.mqtt_client"],
        "ExoMqttClient",
        MagicMock(return_value=fake_mqtt_client),
    )
    original_call_soon_threadsafe = hass.loop.call_soon_threadsafe
    scheduled = []

    def _spy(callback, *args):
        scheduled.append(callback)
        return original_call_soon_threadsafe(callback, *args)

    monkeypatch.setattr(hass.loop, "call_soon_threadsafe", _spy)

    # A failure-path reschedule with the same, still-soon-expiring
    # credentials races the backoff-retry it also arms: each preempts the
    # other's sleep and re-arms again, a tight cascade that only ends
    # non-deterministically at test teardown.
    ok = api._connect_mqtt(hass, entry)

    assert ok is False
    assert scheduled == []


async def test_scheduling_mqtt_retry_twice_cancels_the_first_task(
    hass, entry, monkeypatch
):
    store = api._get_entry_store(hass, entry)
    store["mqtt_retry_delay"] = 0.01
    monkeypatch.setattr(api.random, "uniform", lambda a, b: 0.0)

    api._schedule_mqtt_retry(hass, entry)
    first_task = store["mqtt_retry_task"]

    api._schedule_mqtt_retry(hass, entry)

    try:
        with contextlib.suppress(asyncio.CancelledError):
            await first_task

        assert first_task.cancelled()
        assert store["mqtt_retry_task"] is not first_task
    finally:
        await _cancel_retry_task(hass, entry)
