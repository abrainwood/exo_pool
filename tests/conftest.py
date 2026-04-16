"""Shared fixtures for exo_pool tests."""
from __future__ import annotations

import importlib
import pathlib
import sys
import types

import pytest

# Stub out the homeassistant package so mqtt_client.py can be imported
# without a full HA installation. mqtt_client.py itself has no HA deps,
# but importing via custom_components.exo_pool triggers __init__.py which does.
# We pre-register the mqtt_client module directly to short-circuit that.
_REPO_ROOT = pathlib.Path(__file__).parent.parent
_mqtt_spec = importlib.util.spec_from_file_location(
    "custom_components.exo_pool.mqtt_client",
    _REPO_ROOT / "custom_components" / "exo_pool" / "mqtt_client.py",
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


@pytest.fixture
def mock_mqtt_connection():
    """Create a mock MQTT connection that behaves like awscrt mqtt."""
    from unittest.mock import MagicMock

    conn = MagicMock()

    connect_future = MagicMock()
    connect_future.result.return_value = None
    conn.connect.return_value = connect_future

    disconnect_future = MagicMock()
    disconnect_future.result.return_value = None
    conn.disconnect.return_value = disconnect_future

    sub_future = MagicMock()
    sub_future.result.return_value = None
    conn.subscribe.return_value = (sub_future, 1)

    pub_future = MagicMock()
    pub_future.result.return_value = None
    conn.publish.return_value = (pub_future, 1)

    return conn


@pytest.fixture
def mock_event_loop():
    """Mock the HA event loop for thread-safe callback bridging."""
    from unittest.mock import MagicMock

    loop = MagicMock()
    loop.call_soon_threadsafe = MagicMock()
    return loop


@pytest.fixture
def build_client(mock_mqtt_connection, mock_event_loop):
    """Factory to build an ExoMqttClient with mocked internals."""
    from unittest.mock import MagicMock
    from custom_components.exo_pool.mqtt_client import ExoMqttClient

    def _build(**kwargs):
        client = ExoMqttClient(
            loop=mock_event_loop,
            endpoint=kwargs.get("endpoint", IOT_ENDPOINT),
            region=kwargs.get("region", IOT_REGION),
            serial=kwargs.get("serial", SAMPLE_SERIAL),
        )
        client._build_connection = MagicMock(return_value=mock_mqtt_connection)
        return client

    return _build
