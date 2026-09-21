from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests.conftest import SECRET_ENTRY_DATA, load_exo_pool_module

api = load_exo_pool_module("api")
exo_init = load_exo_pool_module("__init__")


@pytest.fixture
def entry(hass):
    config_entry = MockConfigEntry(domain=api.DOMAIN, data=SECRET_ENTRY_DATA, options={})
    config_entry.add_to_hass(hass)
    return config_entry


async def test_setup_entry_does_not_log_any_secret_value(hass, entry, monkeypatch, caplog):
    monkeypatch.setattr(exo_init, "get_coordinator", AsyncMock(return_value=MagicMock(data=None)))
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())

    with caplog.at_level(logging.DEBUG):
        await exo_init.async_setup_entry(hass, entry)

    log_text = caplog.text
    for secret in (
        "pool.owner@example.com",
        "hunter2",
        "auth-tok-abc123",
        "id-tok-abc123",
        "refresh-tok-abc123",
    ):
        assert secret not in log_text
