from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from tests.conftest import load_exo_pool_module

api = load_exo_pool_module("api")


@pytest.fixture
def coordinator(hass, entry):
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

    coord = DataUpdateCoordinator(hass, api._LOGGER, name="Test")
    store = api._get_entry_store(hass, entry)
    store["coordinator"] = coord
    return coord


class TestSupersessionByDesired:
    async def test_documents_desired_overriding_ours_clears_pending_and_shows_the_new_value(
        self, hass, entry
    ):
        api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 1)

        reported = {"equipment": {"swc_0": {"production": 0}}}
        desired = {"equipment": {"swc_0": {"production": 0}}}
        overlaid = api._overlay_pending_writes(hass, entry, reported, desired)

        assert overlaid["equipment"]["swc_0"]["production"] == 0
        assert ("equipment", "swc_0", "production") not in api._get_entry_store(
            hass, entry
        )["pending_writes"]

    async def test_desired_absent_leaves_overlay_behaviour_unchanged(self, hass, entry):
        api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 1)

        reported = {"equipment": {"swc_0": {"production": 0}}}
        overlaid = api._overlay_pending_writes(hass, entry, reported)

        assert overlaid["equipment"]["swc_0"]["production"] == 1


class TestSettleRuleAndExpiry:
    async def test_real_ticket_sequence_shows_one_throughout_and_clears_at_one(
        self, hass, entry
    ):
        api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 1)

        for transient_value in (0, 2):
            overlaid = api._overlay_pending_writes(
                hass, entry, {"equipment": {"swc_0": {"production": transient_value}}}
            )
            assert overlaid["equipment"]["swc_0"]["production"] == 1

        final = api._overlay_pending_writes(
            hass, entry, {"equipment": {"swc_0": {"production": 1}}}
        )
        assert final["equipment"]["swc_0"]["production"] == 1
        assert ("equipment", "swc_0", "production") not in api._get_entry_store(
            hass, entry
        )["pending_writes"]

    async def test_expiry_shortened_to_fifteen_seconds(self, hass, entry, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])
        api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 1)

        clock[0] += 16

        overlaid = api._overlay_pending_writes(
            hass, entry, {"equipment": {"swc_0": {"production": 0}}}
        )
        assert overlaid["equipment"]["swc_0"]["production"] == 0

    def test_is_settled_is_exact_match(self):
        assert api._is_settled(1, 1) is True
        assert api._is_settled(2, 1) is False

    def test_has_expired_boundary_is_inclusive_at_the_exact_tick(self):
        assert api._has_expired(expires_at=100.0, now=100.0) is True
        assert api._has_expired(expires_at=100.0, now=99.999) is False


class TestExpiryTelemetry:
    async def test_expiry_logs_a_warning_with_path_desired_and_reported(
        self, hass, entry, monkeypatch, caplog
    ):
        clock = [1000.0]
        monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])
        api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 1)
        clock[0] += api.PENDING_WRITE_EXPIRY_SECONDS + 1

        with caplog.at_level(logging.WARNING):
            api._overlay_pending_writes(
                hass, entry, {"equipment": {"swc_0": {"production": 0}}}
            )

        assert "equipment.swc_0.production" in caplog.text
        assert "desired=1" in caplog.text
        assert "reported=0" in caplog.text


class TestNonDictIntermediateRobustness:
    async def test_non_dict_intermediate_is_skipped_with_a_warning_not_raised(
        self, hass, entry, caplog
    ):
        api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 1)
        reported = {"equipment": {"swc_0": "unexpected_string"}}

        with caplog.at_level(logging.WARNING):
            overlaid = api._overlay_pending_writes(hass, entry, reported)

        assert overlaid["equipment"]["swc_0"] == "unexpected_string"
        assert "equipment.swc_0.production" in caplog.text


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

    async def test_write_held_behind_a_long_cooldown_still_overlays_after_it_sends(
        self, hass, entry, connected_mqtt, monkeypatch
    ):
        clock = [1000.0]
        monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])

        async def _fake_sleep(seconds):
            clock[0] += seconds

        monkeypatch.setattr(api.asyncio, "sleep", _fake_sleep)
        api._set_cooldown(
            hass, entry, api.PENDING_WRITE_EXPIRY_SECONDS + 5, reason="post_write"
        )
        connected_mqtt.connected = False  # forces the REST/cooldown path

        execute_rest = AsyncMock(return_value=None)
        monkeypatch.setattr(api, "_execute_write_rest", execute_rest)

        await api.set_pool_value(hass, entry, "production", 1)

        pending = api._get_entry_store(hass, entry)["pending_writes"]
        assert pending[("equipment", "swc_0", "production")]["expires_at"] > clock[0]

    async def test_failed_rest_write_clears_its_own_pending_entry(
        self, hass, entry, monkeypatch
    ):
        monkeypatch.setattr(api.asyncio, "sleep", AsyncMock())
        monkeypatch.setattr(
            api, "_execute_write_rest", AsyncMock(side_effect=Exception("429"))
        )

        with pytest.raises(Exception):
            await api.set_pool_value(hass, entry, "production", 1)

        pending = api._get_entry_store(hass, entry).get("pending_writes", {})
        assert ("equipment", "swc_0", "production") not in pending

    def test_newer_writes_entry_survives_an_older_writes_failure(self, hass, entry):
        api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 1)
        # A newer write already overwrote the same path before the older one's
        # failure handler runs.
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
        self, hass, entry, connected_mqtt, monkeypatch
    ):
        connected_mqtt.publish_desired.side_effect = ConnectionError("dropped")
        clock = [1000.0]
        monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])
        api._set_cooldown(hass, entry, 12.0, reason="post_write")
        sleep_mock = AsyncMock()
        monkeypatch.setattr(api.asyncio, "sleep", sleep_mock)
        execute_rest = AsyncMock(return_value=None)
        monkeypatch.setattr(api, "_execute_write_rest", execute_rest)

        await api.set_pool_value(hass, entry, "production", 1)

        sleep_mock.assert_any_call(pytest.approx(12.0))
        execute_rest.assert_called_once()
