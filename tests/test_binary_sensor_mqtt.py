"""Tests for the MQTT transport health binary sensor (issue #2, item 4).

The whole point of this sensor is that it reflects the MQTT transport
specifically - during the real outage, binary_sensor.connected and
binary_sensor.pool_exo_disconnected both stayed healthy because they track
cloud/auth state, not the transport. These tests would pass on a sensor
wired to auth state, so the adversarial one pins auth state to "healthy"
while the transport is down and asserts the sensor still reports off.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
from unittest.mock import MagicMock

_ROOT = pathlib.Path(__file__).parent.parent / "custom_components" / "exo_pool"


def _load(name: str, filename: str):
    full_name = f"custom_components.exo_pool.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, _ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


api = _load("api", "api.py")
binary_sensor = _load("binary_sensor", "binary_sensor.py")


class FakeEntry:
    def __init__(self, entry_id: str = "test_entry") -> None:
        self.entry_id = entry_id


def _make_sensor(hass, entry, coordinator):
    return binary_sensor.MqttConnectedBinarySensor(hass, entry, coordinator)


class FakeHass:
    def __init__(self) -> None:
        self.data: dict = {}


def test_is_on_true_when_mqtt_transport_is_connected():
    hass = FakeHass()
    entry = FakeEntry()
    coordinator = MagicMock(data={"aws": {"status": "connected"}})
    store = api._get_entry_store(hass, entry)
    store["mqtt_client"] = MagicMock(connected=True)

    sensor = _make_sensor(hass, entry, coordinator)

    assert sensor.is_on is True


def test_is_on_false_when_mqtt_transport_down_even_though_auth_and_aws_are_healthy():
    hass = FakeHass()
    entry = FakeEntry()
    coordinator = MagicMock(data={"aws": {"status": "connected"}})
    store = api._get_entry_store(hass, entry)
    store["mqtt_client"] = MagicMock(connected=False)
    api._authentication_failed = False

    sensor = _make_sensor(hass, entry, coordinator)

    assert sensor.is_on is False
