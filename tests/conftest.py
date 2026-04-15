"""Shared fixtures for exo_pool tests."""
from __future__ import annotations

import importlib
import sys
import types

import pytest

# Stub out the homeassistant package so mqtt_client.py can be imported
# without a full HA installation. mqtt_client.py itself has no HA deps,
# but importing via custom_components.exo_pool triggers __init__.py which does.
# We pre-register the mqtt_client module directly to short-circuit that.
_mqtt_spec = importlib.util.spec_from_file_location(
    "custom_components.exo_pool.mqtt_client",
    "custom_components/exo_pool/mqtt_client.py",
)
_mqtt_mod = importlib.util.module_from_spec(_mqtt_spec)

# Ensure parent packages exist in sys.modules
for pkg in ("custom_components", "custom_components.exo_pool"):
    if pkg not in sys.modules:
        sys.modules[pkg] = types.ModuleType(pkg)

sys.modules["custom_components.exo_pool.mqtt_client"] = _mqtt_mod
_mqtt_spec.loader.exec_module(_mqtt_mod)


SAMPLE_CREDENTIALS = {
    "AccessKeyId": "AKIAIOSFODNN7EXAMPLE",
    "SecretKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "SessionToken": "FwoGZXIvYXdzEBYaDH7example+session+token",
    "Expiration": "2026-04-15T10:00:00.000Z",
    "IdentityId": "us-east-1:00000000-0000-0000-0000-000000000000",
}

SAMPLE_SERIAL = "JT21007072"
IOT_ENDPOINT = "a1zi08qpbrtjyq-ats.iot.us-east-1.amazonaws.com"
IOT_REGION = "us-east-1"
