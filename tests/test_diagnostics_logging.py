from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests.conftest import load_exo_pool_module

api = load_exo_pool_module("api")
diagnostics = load_exo_pool_module("diagnostics")

SECRET_ENTRY_DATA = {
    "serial_number": "JT00000000",
    "email": "pool.owner@example.com",
    "password": "hunter2",
    "auth_token": "auth-tok-abc123",
    "id_token": "id-tok-abc123",
    "refresh_token": "refresh-tok-abc123",
    "user_id": 42,
}


@pytest.fixture
def entry(hass):
    config_entry = MockConfigEntry(domain=api.DOMAIN, data=SECRET_ENTRY_DATA, options={})
    config_entry.add_to_hass(hass)
    return config_entry


async def test_config_entry_diagnostics_does_not_expose_any_secret_value(hass, entry):
    hass.data.setdefault(api.DOMAIN, {})[entry.entry_id] = {
        "coordinator": MagicMock(last_update_success=True, last_exception=None, data={})
    }

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

    diag_text = repr(diag)
    for secret in (
        "hunter2",
        "auth-tok-abc123",
        "id-tok-abc123",
        "refresh-tok-abc123",
        "pool.owner@example.com",
    ):
        assert secret not in diag_text
