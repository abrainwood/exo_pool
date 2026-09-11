from __future__ import annotations

from tests.conftest import load_exo_pool_module

redact = load_exo_pool_module("redact")


def test_redact_hides_a_known_secret_key_at_the_top_level():
    hidden = redact.redact({"password": "hunter2", "serial_number": "JT00000000"})

    assert hidden["password"] != "hunter2"
    assert hidden["serial_number"] == "JT00000000"


def test_redact_hides_a_secret_nested_inside_an_aws_credentials_dict():
    hidden = redact.redact(
        {
            "credentials": {
                "AccessKeyId": "AKIAIOSFODNN7EXAMPLE",
                "SecretKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                "SessionToken": "FwoGZXIvYXdzEBYaDH7example+session+token",
                "Expiration": "2026-04-15T10:00:00.000Z",
            }
        }
    )

    creds = hidden["credentials"]
    assert creds["AccessKeyId"] != "AKIAIOSFODNN7EXAMPLE"
    assert creds["SecretKey"] != "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    assert creds["SessionToken"] != "FwoGZXIvYXdzEBYaDH7example+session+token"
    assert creds["Expiration"] == "2026-04-15T10:00:00.000Z"
