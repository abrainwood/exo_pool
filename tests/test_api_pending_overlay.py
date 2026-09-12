from __future__ import annotations

import copy
import logging
import sys
from unittest.mock import MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests.conftest import FakeResponse, FakeSession, load_exo_pool_module

api = load_exo_pool_module("api")


SWC_0 = {
    "production": 0,
    "sns_1": {"value": 76},
}


def _install_shadow_callback(hass, entry, monkeypatch):
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
    shadow_callback = fake_mqtt_client.set_shadow_callback.call_args.args[0]
    return coordinator, shadow_callback


async def test_write_then_stale_echo_with_reported_still_zero_keeps_the_written_value(
    hass, entry, connected_mqtt, coordinator
):
    coordinator.async_set_updated_data({"equipment": {"swc_0": copy.deepcopy(SWC_0)}})

    await api.set_pool_value(hass, entry, "production", 1)

    stale_echo = {"equipment": {"swc_0": {**SWC_0, "production": 0}}}
    overlaid = api._overlay_pending_writes(hass, entry, stale_echo)

    assert overlaid["equipment"]["swc_0"]["production"] == 1


async def test_unrelated_sensor_change_while_pending_updates_but_production_stays_pending(
    hass, entry, connected_mqtt, coordinator
):
    coordinator.async_set_updated_data({"equipment": {"swc_0": copy.deepcopy(SWC_0)}})

    await api.set_pool_value(hass, entry, "production", 1)

    report_with_sensor_tick = {
        "equipment": {"swc_0": {**SWC_0, "production": 0, "sns_1": {"value": 75}}}
    }
    overlaid = api._overlay_pending_writes(hass, entry, report_with_sensor_tick)

    assert overlaid["equipment"]["swc_0"]["production"] == 1
    assert overlaid["equipment"]["swc_0"]["sns_1"]["value"] == 75


async def test_matching_report_clears_pending_so_a_later_off_report_is_honored(
    hass, entry, connected_mqtt, coordinator
):
    coordinator.async_set_updated_data({"equipment": {"swc_0": copy.deepcopy(SWC_0)}})

    await api.set_pool_value(hass, entry, "production", 1)

    matching_report = {"equipment": {"swc_0": {**SWC_0, "production": 1}}}
    api._overlay_pending_writes(hass, entry, matching_report)

    later_off_report = {"equipment": {"swc_0": {**SWC_0, "production": 0}}}
    overlaid = api._overlay_pending_writes(hass, entry, later_off_report)

    assert overlaid["equipment"]["swc_0"]["production"] == 0


async def test_write_one_then_zero_quickly_leaves_pending_at_the_latest_value(
    hass, entry, connected_mqtt, coordinator, monkeypatch
):
    coordinator.async_set_updated_data({"equipment": {"swc_0": copy.deepcopy(SWC_0)}})
    clock = [1000.0]
    monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])

    await api.set_pool_value(hass, entry, "production", 1)
    await api.set_pool_value(hass, entry, "production", 0)

    echo_with_reported_one = {"equipment": {"swc_0": {**SWC_0, "production": 1}}}
    overlaid = api._overlay_pending_writes(hass, entry, echo_with_reported_one)

    assert overlaid["equipment"]["swc_0"]["production"] == 0


async def test_set_heating_value_records_a_pending_write(hass, entry, connected_mqtt):
    await api.set_heating_value(hass, entry, "sp", 28)

    pending = api._get_entry_store(hass, entry)["pending_writes"]

    assert pending[("heating", "sp")]["value"] == 28


async def test_update_schedule_records_pending_writes_for_each_leaf(
    hass, entry, connected_mqtt, monkeypatch
):
    monkeypatch.setattr(api, "_schedule_debounced_refresh", MagicMock())

    await api.update_schedule(hass, entry, "sch3", start="08:00", end="18:00")

    pending = api._get_entry_store(hass, entry)["pending_writes"]

    assert pending[("schedules", "sch3", "timer", "start")]["value"] == "08:00"
    assert pending[("schedules", "sch3", "timer", "end")]["value"] == "18:00"


async def test_connect_mqtt_shadow_callback_overlays_pending_writes(hass, entry, monkeypatch):
    coordinator, shadow_callback = _install_shadow_callback(hass, entry, monkeypatch)

    api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 1)
    stale_echo = {"equipment": {"swc_0": {"production": 0}}}
    shadow_callback(stale_echo, {})

    coordinator.async_set_updated_data.assert_called_once_with(
        {"equipment": {"swc_0": {"production": 1}}}
    )


async def test_async_update_data_rest_fetch_overlays_pending_writes(
    hass, monkeypatch
):
    import time as time_module

    fresh_entry = MockConfigEntry(
        domain=api.DOMAIN,
        data={
            "serial_number": "JT00000000",
            "id_token": "tok",
            "expires_at": time_module.time() + 3600,
        },
        options={},
    )
    fresh_entry.add_to_hass(hass)
    api._get_entry_store(hass, fresh_entry)
    entry = fresh_entry
    api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 1)

    session = FakeSession(
        FakeResponse(
            200,
            {"state": {"reported": {"equipment": {"swc_0": {"production": 0}}}}},
        )
    )
    monkeypatch.setattr(api.aiohttp_client, "async_get_clientsession", lambda hass: session)

    reported = await api.async_update_data(hass, entry)

    assert reported["equipment"]["swc_0"]["production"] == 1


async def test_no_matching_report_before_expiry_lets_stale_reported_value_win(
    hass, entry, connected_mqtt, coordinator, monkeypatch
):
    coordinator.async_set_updated_data({"equipment": {"swc_0": copy.deepcopy(SWC_0)}})
    clock = [1000.0]
    monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])

    await api.set_pool_value(hass, entry, "production", 1)

    clock[0] += api.PENDING_WRITE_EXPIRY_SECONDS + 1

    still_zero_report = {"equipment": {"swc_0": {**SWC_0, "production": 0}}}
    overlaid = api._overlay_pending_writes(hass, entry, still_zero_report)

    assert overlaid["equipment"]["swc_0"]["production"] == 0


async def test_real_ticket_sequence_shows_one_throughout_and_clears_at_one(hass, entry):
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


class TestExpiryBoundsDerivedFromConstant:
    async def test_just_before_expiry_still_overlaid(self, hass, entry, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])
        api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 1)

        clock[0] += api.PENDING_WRITE_EXPIRY_SECONDS - 0.001
        overlaid = api._overlay_pending_writes(
            hass, entry, {"equipment": {"swc_0": {"production": 0}}}
        )
        assert overlaid["equipment"]["swc_0"]["production"] == 1

    async def test_exactly_at_expiry_is_expired(self, hass, entry, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])
        api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 1)

        clock[0] += api.PENDING_WRITE_EXPIRY_SECONDS
        overlaid = api._overlay_pending_writes(
            hass, entry, {"equipment": {"swc_0": {"production": 0}}}
        )
        assert overlaid["equipment"]["swc_0"]["production"] == 0


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

    async def test_expiry_warning_renders_missing_reported_as_absent(
        self, hass, entry, monkeypatch, caplog
    ):
        clock = [1000.0]
        monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])
        api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 1)
        clock[0] += api.PENDING_WRITE_EXPIRY_SECONDS + 1

        with caplog.at_level(logging.WARNING):
            api._overlay_pending_writes(hass, entry, {})

        assert "reported=absent" in caplog.text
        assert "object at 0x" not in caplog.text


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

    async def test_non_dict_intermediate_warning_includes_node_type(
        self, hass, entry, caplog
    ):
        api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 1)

        with caplog.at_level(logging.WARNING):
            api._overlay_pending_writes(
                hass, entry, {"equipment": {"swc_0": "unexpected_string"}}
            )

        assert "unexpected shape: str" in caplog.text


class TestSupersessionOnlyOnChange:
    async def test_own_echo_through_connect_mqtt_still_shows_our_value(
        self, hass, entry, monkeypatch
    ):
        mqtt_client_module = load_exo_pool_module("mqtt_client")
        real_exo_mqtt_client = mqtt_client_module.ExoMqttClient
        coordinator, shadow_callback = _install_shadow_callback(hass, entry, monkeypatch)

        api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 1)

        client = real_exo_mqtt_client.__new__(real_exo_mqtt_client)
        message = {
            "previous": {"state": {"reported": {}, "desired": {}}},
            "current": {
                "state": {
                    "reported": {"equipment": {"swc_0": {"production": 0}}},
                    "desired": {"equipment": {"swc_0": {"production": 1}}},
                }
            },
        }
        reported, changed_desired = client._extract_state(
            "$aws/things/x/shadow/update/documents", message
        )
        shadow_callback(reported, changed_desired)

        coordinator.async_set_updated_data.assert_called_once_with(
            {"equipment": {"swc_0": {"production": 1}}}
        )

    async def test_echo_of_our_own_first_write_after_a_second_write_still_shows_the_latest(
        self, hass, entry
    ):
        api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 1)
        api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 0)

        changed_desired = {"equipment": {"swc_0": {"production": 1}}}
        overlaid = api._overlay_pending_writes(
            hass, entry, {"equipment": {"swc_0": {"production": 1}}}, changed_desired
        )

        assert overlaid["equipment"]["swc_0"]["production"] == 0

    async def test_desired_unchanged_from_previous_but_different_from_ours_keeps_pending(
        self, hass, entry
    ):
        api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 1)

        overlaid = api._overlay_pending_writes(
            hass, entry, {"equipment": {"swc_0": {"production": 0}}}, {}
        )

        assert overlaid["equipment"]["swc_0"]["production"] == 1

    async def test_another_apps_write_not_in_our_published_values_drops_pending(
        self, hass, entry
    ):
        api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 1)

        changed_desired = {"equipment": {"swc_0": {"production": 0}}}
        overlaid = api._overlay_pending_writes(
            hass, entry, {"equipment": {"swc_0": {"production": 0}}}, changed_desired
        )

        assert overlaid["equipment"]["swc_0"]["production"] == 0
        assert ("equipment", "swc_0", "production") not in api._get_entry_store(
            hass, entry
        )["pending_writes"]

    async def test_desired_missing_our_path_keeps_pending(self, hass, entry):
        api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 1)

        changed_desired = {"equipment": {"swc_0": {"sns_1": {"value": 2}}}}
        overlaid = api._overlay_pending_writes(
            hass, entry, {"equipment": {"swc_0": {"production": 0}}}, changed_desired
        )

        assert overlaid["equipment"]["swc_0"]["production"] == 1

    async def test_on_shadow_update_passes_changed_desired_through(
        self, hass, entry, monkeypatch
    ):
        coordinator, shadow_callback = _install_shadow_callback(hass, entry, monkeypatch)

        api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 1)
        changed_desired = {"equipment": {"swc_0": {"production": 0}}}
        shadow_callback({"equipment": {"swc_0": {"production": 0}}}, changed_desired)

        coordinator.async_set_updated_data.assert_called_once_with(
            {"equipment": {"swc_0": {"production": 0}}}
        )


class TestExpiredEntryDoesNotCarryForwardPublishedValues:
    def test_recording_after_expiry_starts_published_values_fresh(self, hass, entry, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])

        api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 1)
        clock[0] += api.PENDING_WRITE_EXPIRY_SECONDS + 1
        api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 0)

        pending = api._get_entry_store(hass, entry)["pending_writes"]
        published = pending[("equipment", "swc_0", "production")]["published_values"]
        assert published == [0]
