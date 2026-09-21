from __future__ import annotations

import logging
from unittest.mock import MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests.conftest import SAMPLE_CREDENTIALS, load_exo_pool_module

api = load_exo_pool_module("api")

# Fixture build_client is defined in conftest.py.


def test_connect_does_not_log_any_credential_value(build_client, caplog):
    client = build_client()

    with caplog.at_level(logging.DEBUG):
        client.connect(SAMPLE_CREDENTIALS)

    log_text = caplog.text
    for secret in (
        SAMPLE_CREDENTIALS["AccessKeyId"],
        SAMPLE_CREDENTIALS["SecretKey"],
        SAMPLE_CREDENTIALS["SessionToken"],
    ):
        assert secret not in log_text


def test_connect_mqtt_exc_info_traceback_does_not_log_any_credential_value(
    hass, caplog
):
    entry = MockConfigEntry(domain=api.DOMAIN, data={"serial_number": "JT00000000"})
    entry.add_to_hass(hass)
    store = api._get_entry_store(hass, entry)
    store["aws_credentials"] = SAMPLE_CREDENTIALS
    store["coordinator"] = MagicMock()

    mqtt_client = MagicMock()
    mqtt_client.connect.side_effect = RuntimeError("boom")
    store["mqtt_client"] = mqtt_client

    with caplog.at_level(logging.DEBUG):
        result = api._connect_mqtt(hass, entry)

    assert result is False
    log_text = caplog.text
    for secret in (
        SAMPLE_CREDENTIALS["AccessKeyId"],
        SAMPLE_CREDENTIALS["SecretKey"],
        SAMPLE_CREDENTIALS["SessionToken"],
    ):
        assert secret not in log_text
