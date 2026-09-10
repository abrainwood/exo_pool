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
_REPO_ROOT = pathlib.Path(__file__).parent.parent

for pkg in ("custom_components", "custom_components.exo_pool"):
    if pkg not in sys.modules:
        sys.modules[pkg] = types.ModuleType(pkg)


def load_exo_pool_module(name: str):
    """Load a custom_components.exo_pool submodule directly.

    custom_components.exo_pool is the fake stub package registered above, so
    its __init__ can't be reached through a normal `from .foo import bar`.
    """
    full_name = f"custom_components.exo_pool.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(
        full_name, _REPO_ROOT / "custom_components" / "exo_pool" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


load_exo_pool_module("mqtt_client")


SAMPLE_CREDENTIALS = {
    "AccessKeyId": "AKIAIOSFODNN7EXAMPLE",
    "SecretKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "SessionToken": "FwoGZXIvYXdzEBYaDH7example+session+token",
    "Expiration": "2026-04-15T10:00:00.000Z",
    "IdentityId": "us-east-1:00000000-0000-0000-0000-000000000000",
}

SAMPLE_SERIAL = "JT00000000"
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
    """Mock the HA event loop for thread-safe callback bridging.

    call_soon_threadsafe runs its callback immediately so tests see the
    same effects a real loop would produce on its next tick. A test that
    needs to prove a call goes through call_soon_threadsafe rather than
    hitting the loop directly can override it with a bare MagicMock.
    """
    from unittest.mock import MagicMock

    loop = MagicMock()
    loop.call_soon_threadsafe = MagicMock(side_effect=lambda fn, *args: fn(*args))
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
