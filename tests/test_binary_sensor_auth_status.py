from __future__ import annotations

from unittest.mock import MagicMock

from tests.conftest import load_exo_pool_module

api = load_exo_pool_module("api")
binary_sensor = load_exo_pool_module("binary_sensor")


class FakeEntry:
    def __init__(self, entry_id: str = "test_entry") -> None:
        self.entry_id = entry_id


def _make_sensor(entry, coordinator):
    return binary_sensor.AuthenticationStatusBinarySensor(entry, coordinator)


def test_is_on_reflects_auth_failure_set_after_module_import(monkeypatch):
    monkeypatch.setattr(api, "_authentication_failed", False)
    entry = FakeEntry()
    coordinator = MagicMock(data={})
    sensor = _make_sensor(entry, coordinator)
    assert sensor.is_on is True

    monkeypatch.setattr(api, "_authentication_failed", True)

    assert sensor.is_on is False


def test_extra_state_attributes_surfaces_redacted_error_set_after_module_import(monkeypatch):
    monkeypatch.setattr(api, "_authentication_failed", False)
    monkeypatch.setattr(api, "_last_auth_error_redacted", None)
    entry = FakeEntry()
    coordinator = MagicMock(data={})
    sensor = _make_sensor(entry, coordinator)
    assert sensor.extra_state_attributes == {}

    monkeypatch.setattr(api, "_authentication_failed", True)
    monkeypatch.setattr(api, "_last_auth_error_redacted", "token expired")

    assert sensor.extra_state_attributes == {"last_error": "token expired"}


def test_extra_state_attributes_never_surfaces_the_raw_unredacted_error(monkeypatch):
    monkeypatch.setattr(api, "_authentication_failed", True)
    monkeypatch.setattr(api, "_last_auth_error", "raw secret token value")
    monkeypatch.setattr(api, "_last_auth_error_redacted", "token expired")
    entry = FakeEntry()
    coordinator = MagicMock(data={})
    sensor = _make_sensor(entry, coordinator)

    assert sensor.extra_state_attributes == {"last_error": "token expired"}
    assert "raw secret token value" not in sensor.extra_state_attributes.values()
