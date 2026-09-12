from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from tests.conftest import load_exo_pool_module

api = load_exo_pool_module("api")
_real_sleep = asyncio.sleep  # captured before any test monkeypatches api.asyncio.sleep


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
    hass, entry, monkeypatch, fake_clock
):
    monkeypatch.setattr(api, "_execute_write_rest", AsyncMock(return_value=None))
    monkeypatch.setattr(api.asyncio, "sleep", AsyncMock())

    await api.set_pool_value(hass, entry, "production", 1)

    store = api._get_entry_store(hass, entry)
    assert store["cooldown_until"] == pytest.approx(
        fake_clock[0] + api.POST_WRITE_COOLDOWN_SECONDS
    )
    assert store["write_quiet_until"] == pytest.approx(
        fake_clock[0] + api.POST_WRITE_COOLDOWN_SECONDS
    )


async def test_rest_fallback_write_still_sleeps_the_write_gap(hass, entry, monkeypatch):
    monkeypatch.setattr(api, "_execute_write_rest", AsyncMock(return_value=None))
    sleep_mock = AsyncMock()
    monkeypatch.setattr(api.asyncio, "sleep", sleep_mock)

    await api.set_pool_value(hass, entry, "production", 1)

    sleep_mock.assert_any_call(api.WRITE_GAP_SECONDS)


async def test_rest_fallback_cooldown_includes_extra_delay_for_delay_refresh_writes(
    hass, entry, monkeypatch, fake_clock
):
    monkeypatch.setattr(api, "_execute_write_rest", AsyncMock(return_value=None))
    monkeypatch.setattr(api.asyncio, "sleep", AsyncMock())

    await api.set_pool_value(hass, entry, "production", 1, delay_refresh=True)

    store = api._get_entry_store(hass, entry)
    assert store["cooldown_until"] == pytest.approx(
        fake_clock[0] + api.POST_WRITE_COOLDOWN_SECONDS + api.DELAY_REFRESH_EXTRA_DELAY_SECONDS
    )


class TestRecordAtSendTime:
    async def test_stale_echo_arriving_during_dispatch_is_already_overlaid(
        self, hass, entry, connected_mqtt, coordinator, monkeypatch
    ):
        monkeypatch.setattr(api.asyncio, "sleep", AsyncMock())
        coordinator.async_set_updated_data({"equipment": {"swc_0": {"production": 0}}})

        def _inject_echo_during_publish(desired):
            data = api._overlay_pending_writes(
                hass, entry, {"equipment": {"swc_0": {"production": 0}}}
            )
            coordinator.async_set_updated_data(data)

        connected_mqtt.publish_desired.side_effect = _inject_echo_during_publish

        await api.set_pool_value(hass, entry, "production", 1)

        assert coordinator.data["equipment"]["swc_0"]["production"] == 1

    async def test_failed_rest_write_raises_and_clears_its_own_pending_entry(
        self, hass, entry, monkeypatch
    ):
        monkeypatch.setattr(api.asyncio, "sleep", AsyncMock())
        monkeypatch.setattr(
            api, "_execute_write_rest", AsyncMock(side_effect=Exception("429"))
        )

        with pytest.raises(Exception, match="429"):
            await api.set_pool_value(hass, entry, "production", 1)

        pending = api._get_entry_store(hass, entry).get("pending_writes", {})
        assert ("equipment", "swc_0", "production") not in pending

    def test_newer_writes_entry_survives_an_older_writes_failure(self, hass, entry):
        api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 1)
        api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 0)

        api._clear_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 1)

        pending = api._get_entry_store(hass, entry)["pending_writes"]
        assert pending[("equipment", "swc_0", "production")]["value"] == 0


class TestMqttSkipsAnyCooldown:
    async def test_existing_cooldown_plus_mqtt_connected_publishes_with_no_sleep(
        self, hass, entry, connected_mqtt, monkeypatch
    ):
        sleep_mock = AsyncMock()
        monkeypatch.setattr(api.asyncio, "sleep", sleep_mock)
        api._set_cooldown(hass, entry, 30.0, reason="post_write")

        await api.set_pool_value(hass, entry, "production", 1)

        connected_mqtt.publish_desired.assert_called_once()
        sleep_mock.assert_not_called()

    async def test_mqtt_publish_raises_then_rest_fallback_sleeps_the_remaining_cooldown(
        self, hass, entry, connected_mqtt, monkeypatch, fake_clock
    ):
        connected_mqtt.publish_desired.side_effect = ConnectionError("dropped")
        api._set_cooldown(hass, entry, 12.0, reason="post_write")
        sleep_mock = AsyncMock()
        monkeypatch.setattr(api.asyncio, "sleep", sleep_mock)
        execute_rest = AsyncMock(return_value=None)
        monkeypatch.setattr(api, "_execute_write_rest", execute_rest)

        await api.set_pool_value(hass, entry, "production", 1)

        sleep_mock.assert_any_call(pytest.approx(12.0))
        execute_rest.assert_called_once()


class TestMqttReconnectDuringRestCooldownWait:
    async def test_reconnect_during_wait_publishes_via_mqtt_not_rest(
        self, hass, entry, disconnected_mqtt, monkeypatch
    ):
        api._set_cooldown(hass, entry, 10.0, reason="post_write")
        execute_rest = AsyncMock(return_value=None)
        monkeypatch.setattr(api, "_execute_write_rest", execute_rest)

        async def _fake_sleep(seconds):
            disconnected_mqtt.connected = True

        monkeypatch.setattr(api.asyncio, "sleep", _fake_sleep)

        await api.set_pool_value(hass, entry, "production", 1)

        disconnected_mqtt.publish_desired.assert_called_once()
        execute_rest.assert_not_called()

    async def test_get_accepted_during_wait_keeps_optimistic_value(
        self, hass, entry, disconnected_mqtt, coordinator, monkeypatch
    ):
        coordinator.async_set_updated_data(
            {"equipment": {"swc_0": {"production": 0}}}
        )
        api._set_cooldown(hass, entry, 10.0, reason="post_write")
        monkeypatch.setattr(api, "_execute_write_rest", AsyncMock(return_value=None))
        mid_wait_result = {}

        async def _fake_sleep(seconds):
            if "production" in mid_wait_result:
                return
            overlaid = api._overlay_pending_writes(
                hass, entry, {"equipment": {"swc_0": {"production": 0}}}, {}
            )
            mid_wait_result["production"] = overlaid["equipment"]["swc_0"]["production"]

        monkeypatch.setattr(api.asyncio, "sleep", _fake_sleep)

        await api.set_pool_value(hass, entry, "production", 1)

        assert mid_wait_result["production"] == 1

    async def test_no_false_expiry_warning_during_a_wait_longer_than_expiry(
        self, hass, entry, disconnected_mqtt, coordinator, monkeypatch, fake_clock, caplog
    ):
        cooldown = api.PENDING_WRITE_EXPIRY_SECONDS + 5
        api._set_cooldown(hass, entry, cooldown, reason="post_write")
        monkeypatch.setattr(api, "_execute_write_rest", AsyncMock(return_value=None))
        coordinator.async_set_updated_data({"equipment": {"swc_0": {"production": 0}}})
        mid_wait_result = {}

        async def _fake_sleep(seconds):
            if "production" in mid_wait_result:
                fake_clock[0] += seconds
                return
            fake_clock[0] += api.PENDING_WRITE_EXPIRY_SECONDS + 1
            overlaid = api._overlay_pending_writes(
                hass, entry, {"equipment": {"swc_0": {"production": 0}}}
            )
            mid_wait_result["production"] = overlaid["equipment"]["swc_0"]["production"]
            fake_clock[0] += seconds - (api.PENDING_WRITE_EXPIRY_SECONDS + 1)

        monkeypatch.setattr(api.asyncio, "sleep", _fake_sleep)

        with caplog.at_level(logging.WARNING):
            await api.set_pool_value(hass, entry, "production", 1)

        assert mid_wait_result["production"] == 1
        assert "expired unsettled" not in caplog.text


def _pool_write_item(value):
    return api._WriteItem(
        kind="pool",
        key="pool:production",
        target="production",
        payload={"production": value},
    )


class TestCancelledDuringDispatchClearsPending:
    async def test_cancelled_during_cooldown_wait_clears_pending(
        self, hass, entry, disconnected_mqtt, monkeypatch
    ):
        api._set_cooldown(hass, entry, 5.0, reason="post_write")
        recorded_during_wait = {}
        fired = {"once": False}

        async def _capture_then_cancel(seconds):
            if fired["once"]:
                return
            fired["once"] = True
            recorded_during_wait.update(
                api._get_entry_store(hass, entry).get("pending_writes", {})
            )
            raise asyncio.CancelledError()

        monkeypatch.setattr(api.asyncio, "sleep", _capture_then_cancel)

        with pytest.raises(asyncio.CancelledError):
            await api._execute_write(hass, entry, _pool_write_item(1))

        assert recorded_during_wait[("equipment", "swc_0", "production")]["value"] == 1
        pending = api._get_entry_store(hass, entry).get("pending_writes", {})
        assert ("equipment", "swc_0", "production") not in pending

    async def test_cancelled_during_rest_send_clears_pending(self, hass, entry, monkeypatch):
        monkeypatch.setattr(
            api, "_execute_write_rest", AsyncMock(side_effect=asyncio.CancelledError())
        )

        with pytest.raises(asyncio.CancelledError):
            await api._execute_write(hass, entry, _pool_write_item(1))

        pending = api._get_entry_store(hass, entry).get("pending_writes", {})
        assert ("equipment", "swc_0", "production") not in pending


class TestMqttPublishFailurePreservesPublishedValues:
    async def test_late_echo_of_earlier_success_does_not_supersede_rest_fallback(
        self, hass, entry, connected_mqtt, coordinator, monkeypatch
    ):
        monkeypatch.setattr(api.asyncio, "sleep", AsyncMock())
        monkeypatch.setattr(api, "_execute_write_rest", AsyncMock(return_value=None))
        coordinator.async_set_updated_data({"equipment": {"swc_0": {"production": 0}}})

        connected_mqtt.publish_desired.side_effect = [None, ConnectionError("dropped")]
        await api.set_pool_value(hass, entry, "production", 1)
        await api.set_pool_value(hass, entry, "production", 0)

        overlaid = api._overlay_pending_writes(
            hass,
            entry,
            {"equipment": {"swc_0": {"production": 1}}},
            {"equipment": {"swc_0": {"production": 1}}},
        )

        assert overlaid["equipment"]["swc_0"]["production"] == 0


class TestRecordBeforeRestSend:
    async def test_stale_echo_arriving_during_rest_send_is_already_overlaid(
        self, hass, entry, disconnected_mqtt, coordinator, monkeypatch
    ):
        monkeypatch.setattr(api.asyncio, "sleep", AsyncMock())
        coordinator.async_set_updated_data({"equipment": {"swc_0": {"production": 0}}})

        async def _inject_echo_during_send(hass, entry, item, desired):
            data = api._overlay_pending_writes(
                hass, entry, {"equipment": {"swc_0": {"production": 0}}}
            )
            coordinator.async_set_updated_data(data)

        monkeypatch.setattr(api, "_execute_write_rest", _inject_echo_during_send)

        await api.set_pool_value(hass, entry, "production", 1)


class TestWakeHeldWriteOnMqttReconnect:
    async def test_mqtt_reconnect_wakes_the_wait_before_the_full_cooldown_elapses(
        self, hass, entry, disconnected_mqtt, monkeypatch, fake_clock
    ):
        api._set_cooldown(hass, entry, 55.0, reason="post_write")
        execute_rest = AsyncMock(return_value=None)
        monkeypatch.setattr(api, "_execute_write_rest", execute_rest)

        async def fake_sleep(seconds):
            if seconds != pytest.approx(55.0):
                await _real_sleep(0)
                return
            fake_clock[0] += 3
            disconnected_mqtt.connected = True
            api._notify_mqtt_state_changed(hass, entry, True)
            await asyncio.Future()

        monkeypatch.setattr(api.asyncio, "sleep", fake_sleep)

        await api.set_pool_value(hass, entry, "production", 1)

        disconnected_mqtt.publish_desired.assert_called_once()
        execute_rest.assert_not_called()
        assert fake_clock[0] == pytest.approx(1003.0)

    async def test_no_reconnect_waits_the_full_cooldown_then_falls_back_to_rest(
        self, hass, entry, disconnected_mqtt, monkeypatch, fake_clock
    ):
        api._set_cooldown(hass, entry, 55.0, reason="post_write")
        execute_rest = AsyncMock(return_value=None)
        monkeypatch.setattr(api, "_execute_write_rest", execute_rest)

        async def fake_sleep(seconds):
            fake_clock[0] += seconds

        monkeypatch.setattr(api.asyncio, "sleep", fake_sleep)

        await api.set_pool_value(hass, entry, "production", 1)

        disconnected_mqtt.publish_desired.assert_not_called()
        execute_rest.assert_called_once()
        assert fake_clock[0] == pytest.approx(1055.0 + api.WRITE_GAP_SECONDS)

    async def test_spurious_reconnect_signal_while_still_disconnected_falls_back_to_rest(
        self, hass, entry, disconnected_mqtt, monkeypatch, fake_clock
    ):
        api._set_cooldown(hass, entry, 55.0, reason="post_write")
        execute_rest = AsyncMock(return_value=None)
        monkeypatch.setattr(api, "_execute_write_rest", execute_rest)

        async def fake_sleep(seconds):
            if seconds != pytest.approx(55.0):
                await _real_sleep(0)
                return
            fake_clock[0] += 3
            api._notify_mqtt_state_changed(hass, entry, True)
            await asyncio.Future()

        monkeypatch.setattr(api.asyncio, "sleep", fake_sleep)

        await api.set_pool_value(hass, entry, "production", 1)

        disconnected_mqtt.publish_desired.assert_not_called()
        execute_rest.assert_called_once()

    async def test_hold_and_early_wake_are_logged_at_info(
        self, hass, entry, disconnected_mqtt, monkeypatch, fake_clock, caplog
    ):
        api._set_cooldown(hass, entry, 55.0, reason="post_write")
        monkeypatch.setattr(api, "_execute_write_rest", AsyncMock(return_value=None))

        async def fake_sleep(seconds):
            if seconds != pytest.approx(55.0):
                await _real_sleep(0)
                return
            fake_clock[0] += 3
            disconnected_mqtt.connected = True
            api._notify_mqtt_state_changed(hass, entry, True)
            await asyncio.Future()

        monkeypatch.setattr(api.asyncio, "sleep", fake_sleep)

        with caplog.at_level(logging.INFO):
            await api.set_pool_value(hass, entry, "production", 1)

        assert "held behind cooldown" in caplog.text
        assert "pool:production" in caplog.text
        assert "55.0" in caplog.text
        assert "woken early" in caplog.text

    async def test_stale_reconnect_signal_does_not_wake_a_later_held_write(
        self, hass, entry, disconnected_mqtt, monkeypatch, fake_clock, caplog
    ):
        execute_rest = AsyncMock(return_value=None)
        monkeypatch.setattr(api, "_execute_write_rest", execute_rest)
        api._set_cooldown(hass, entry, 10.0, reason="post_write")

        async def fake_sleep_first(seconds):
            fake_clock[0] += 3
            disconnected_mqtt.connected = True
            api._notify_mqtt_state_changed(hass, entry, True)
            await asyncio.Future()

        monkeypatch.setattr(api.asyncio, "sleep", fake_sleep_first)
        with caplog.at_level(logging.INFO):
            await api.set_pool_value(hass, entry, "production", 1)
        disconnected_mqtt.publish_desired.assert_called_once()

        api._notify_mqtt_state_changed(hass, entry, True)

        disconnected_mqtt.connected = False
        disconnected_mqtt.publish_desired.reset_mock()
        api._set_cooldown(hass, entry, 10.0, reason="post_write")
        caplog.clear()

        async def fake_sleep_second(seconds):
            fake_clock[0] += seconds

        monkeypatch.setattr(api.asyncio, "sleep", fake_sleep_second)
        with caplog.at_level(logging.INFO):
            await api.set_pool_value(hass, entry, "swc", 40)

        disconnected_mqtt.publish_desired.assert_not_called()
        execute_rest.assert_called_once()
        assert "woken early" not in caplog.text
