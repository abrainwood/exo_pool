from __future__ import annotations

from unittest.mock import MagicMock

from tests.conftest import load_exo_pool_module

api = load_exo_pool_module("api")
binary_sensor = load_exo_pool_module("binary_sensor")


class FakeEntry:
    def __init__(self, entry_id: str = "test_entry") -> None:
        self.entry_id = entry_id


class FakeHass:
    def __init__(self) -> None:
        self.data: dict = {}


def _make_sensor(hass, entry, coordinator):
    return binary_sensor.MqttConnectedBinarySensor(hass, entry, coordinator)


def test_is_on_true_when_mqtt_transport_is_connected():
    hass = FakeHass()
    entry = FakeEntry()
    coordinator = MagicMock(data={"aws": {"status": "connected"}})
    store = api._get_entry_store(hass, entry)
    store["mqtt_client"] = MagicMock(connected=True)

    sensor = _make_sensor(hass, entry, coordinator)

    assert sensor.is_on is True


def test_is_on_false_when_mqtt_transport_down_even_though_aws_status_is_connected():
    hass = FakeHass()
    entry = FakeEntry()
    coordinator = MagicMock(data={"aws": {"status": "connected"}})
    store = api._get_entry_store(hass, entry)
    store["mqtt_client"] = MagicMock(connected=False)

    sensor = _make_sensor(hass, entry, coordinator)

    assert sensor.is_on is False
