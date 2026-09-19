from __future__ import annotations

import logging

from tests.conftest import SAMPLE_CREDENTIALS

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
