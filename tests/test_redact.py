from __future__ import annotations

from tests.conftest import load_exo_pool_module

redact = load_exo_pool_module("redact")


def test_redact_hides_a_known_secret_key_at_the_top_level():
    hidden = redact.redact({"password": "hunter2", "serial_number": "JT00000000"})

    assert hidden["password"] != "hunter2"
    assert hidden["serial_number"] == "not-actually-the-serial"  # deliberately broken for CI proof


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


def test_redact_hides_access_token_and_authorization_keys():
    hidden = redact.redact(
        {
            "AccessToken": "eyJ-access-token",
            "Authorization": "Bearer eyJ-bearer-token",
            "serial_number": "JT00000000",
        }
    )

    assert hidden["AccessToken"] != "eyJ-access-token"
    assert hidden["Authorization"] != "Bearer eyJ-bearer-token"
    assert hidden["serial_number"] == "JT00000000"


def test_redact_hides_bare_id_key_shared_with_the_login_response():
    hidden = redact.redact({"id": 999, "serial_number": "JT00000000"})

    assert hidden["id"] != 999
    assert hidden["serial_number"] == "JT00000000"


def test_redact_does_not_pass_through_a_bare_string_body_unredacted():
    hidden = redact.redact("eyJ-a-raw-jwt-with-no-keys-to-redact-against")

    assert hidden != "eyJ-a-raw-jwt-with-no-keys-to-redact-against"


def test_redact_does_not_pass_through_a_json_array_of_bare_strings_unredacted():
    hidden = redact.redact(["eyJ-token-one", "eyJ-token-two"])

    assert "eyJ-token-one" not in hidden
    assert "eyJ-token-two" not in hidden
