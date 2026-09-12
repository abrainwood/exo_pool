from __future__ import annotations

import copy

from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests.conftest import load_exo_pool_module

api = load_exo_pool_module("api")


@pytest.fixture
def no_write_gap_sleep(monkeypatch):
    monkeypatch.setattr(api.asyncio, "sleep", AsyncMock())


@pytest.fixture
def coordinator(hass, entry):
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

    coord = DataUpdateCoordinator(hass, api._LOGGER, name="Test")
    store = api._get_entry_store(hass, entry)
    store["coordinator"] = coord
    return coord


SWC_0 = {
    "production": 0,
    "sns_1": {"value": 76},
}


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
    hass, entry, connected_mqtt, coordinator, monkeypatch, no_write_gap_sleep
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
    import sys

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

    api._record_pending_writes(hass, entry, ["equipment", "swc_0", "production"], 1)
    stale_echo = {"equipment": {"swc_0": {"production": 0}}}
    shadow_callback(stale_echo, {})

    coordinator.async_set_updated_data.assert_called_once_with(
        {"equipment": {"swc_0": {"production": 1}}}
    )


class _FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self.headers: dict = {}
        self._payload = payload

    async def json(self):
        return self._payload

    async def text(self):
        return "{}"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self._response = response

    def get(self, url, headers=None):
        return self._response


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

    session = _FakeSession(
        _FakeResponse(
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
