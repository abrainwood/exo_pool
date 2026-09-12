"""Shared fixtures for exo_pool tests."""
from __future__ import annotations

import asyncio
import importlib
import json
import pathlib
import sys
import types

import pytest

_UNPATCHED_ASYNCIO_SLEEP = asyncio.sleep

# Stub out the homeassistant package so mqtt_client.py can be imported
# without a full HA installation. mqtt_client.py itself has no HA deps,
# but importing via custom_components.exo_pool triggers __init__.py which does.
_REPO_ROOT = pathlib.Path(__file__).parent.parent

for pkg in ("custom_components", "custom_components.exo_pool"):
    if pkg not in sys.modules:
        sys.modules[pkg] = types.ModuleType(pkg)

# Give the exo_pool stub a real __path__ so that a module loaded standalone
# below (e.g. api.py) can still resolve its own relative imports of sibling
# modules (e.g. `from .redact import redact`) via normal package lookup.
sys.modules["custom_components.exo_pool"].__path__ = [
    str(_REPO_ROOT / "custom_components" / "exo_pool")
]


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


@pytest.fixture(autouse=True)
def _fast_subscribe(monkeypatch):
    """Skip the real per-topic subscribe pacing delay in tests."""
    monkeypatch.setattr(load_exo_pool_module("mqtt_client"), "_SUBSCRIBE_DELAY", 0)


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
    """Mock HA event loop - call_soon_threadsafe executes its callback inline."""
    from unittest.mock import MagicMock

    loop = MagicMock()
    loop.call_soon_threadsafe = MagicMock(side_effect=lambda fn, *args: fn(*args))
    return loop


@pytest.fixture
def mock_event_loop_deferred():
    """Mock HA event loop - call_soon_threadsafe only records the call."""
    from unittest.mock import MagicMock

    loop = MagicMock()
    loop.call_soon_threadsafe = MagicMock()
    return loop


@pytest.fixture
def entry(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    api = load_exo_pool_module("api")
    config_entry = MockConfigEntry(
        domain=api.DOMAIN,
        data={"serial_number": SAMPLE_SERIAL, "id_token": "tok"},
        options={},
    )
    config_entry.add_to_hass(hass)
    api._get_entry_store(hass, config_entry)
    return config_entry


@pytest.fixture(autouse=True)
def no_network_client_session(monkeypatch):
    from unittest.mock import MagicMock

    api = load_exo_pool_module("api")
    monkeypatch.setattr(
        api.aiohttp_client, "async_get_clientsession", MagicMock(return_value=MagicMock())
    )


@pytest.fixture
def connected_mqtt(hass, entry):
    from unittest.mock import MagicMock

    api = load_exo_pool_module("api")
    store = api._get_entry_store(hass, entry)
    client = MagicMock()
    client.connected = True
    client.publish_desired = MagicMock()
    store["mqtt_client"] = client
    return client


@pytest.fixture
def disconnected_mqtt(hass, entry):
    from unittest.mock import MagicMock

    api = load_exo_pool_module("api")
    store = api._get_entry_store(hass, entry)
    client = MagicMock()
    client.connected = False
    client.publish_desired = MagicMock()
    store["mqtt_client"] = client
    return client


@pytest.fixture
def coordinator(hass, entry):
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

    api = load_exo_pool_module("api")
    coord = DataUpdateCoordinator(hass, api._LOGGER, name="Test")
    store = api._get_entry_store(hass, entry)
    store["coordinator"] = coord
    return coord


@pytest.fixture
def build_client(mock_mqtt_connection, mock_event_loop):
    """Factory to build an ExoMqttClient with mocked internals."""
    from unittest.mock import MagicMock
    from custom_components.exo_pool.mqtt_client import ExoMqttClient

    def _build(**kwargs):
        client = ExoMqttClient(
            loop=kwargs.get("loop", mock_event_loop),
            endpoint=kwargs.get("endpoint", IOT_ENDPOINT),
            region=kwargs.get("region", IOT_REGION),
            serial=kwargs.get("serial", SAMPLE_SERIAL),
        )
        client._build_connection = MagicMock(return_value=mock_mqtt_connection)
        return client

    return _build


class FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self.headers: dict = {}
        self._payload = payload

    async def json(self):
        return self._payload

    async def text(self):
        return json.dumps(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class FakeSession:
    def __init__(self, response: FakeResponse):
        self._response = response

    def get(self, url, headers=None):
        return self._response

    def post(self, url, json=None, headers=None):  # noqa: A002 - matches aiohttp signature
        return self._response


def get_subscribe_callback(mock_conn, topic_fragment: str):
    """Find the MQTT callback registered for a topic containing the fragment."""
    for c in mock_conn.subscribe.call_args_list:
        topic = c.kwargs.get("topic") or c.args[0]
        if topic_fragment in topic:
            return c.kwargs.get("callback") or c.args[2]
    raise AssertionError(f"No subscription found matching '{topic_fragment}'")


@pytest.fixture
def fake_clock(monkeypatch):
    """Monkeypatch api.time.monotonic to an advanceable fake clock starting at 1000.0."""
    api = load_exo_pool_module("api")
    clock = [1000.0]
    monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])
    return clock


@pytest.fixture
def post_write_cooldown_seconds():
    api = load_exo_pool_module("api")
    return api.POST_WRITE_COOLDOWN_SECONDS + api.DELAY_REFRESH_EXTRA_DELAY_SECONDS


@pytest.fixture
def fake_sleep_that_wakes_at_full_cooldown(fake_clock, post_write_cooldown_seconds):
    """Build a fake asyncio.sleep that only reacts to the full-cooldown sleep call.

    Any shorter sleep (e.g. WRITE_GAP_SECONDS) passes through unpatched; the
    full-cooldown call advances the fake clock by `elapsed`, invokes `wake`,
    then hangs so only the reconnect event can resolve the race.
    """

    def _build(wake, elapsed=3):
        async def fake_sleep(seconds):
            if seconds != pytest.approx(post_write_cooldown_seconds):
                await _UNPATCHED_ASYNCIO_SLEEP(0)
                return
            fake_clock[0] += elapsed
            wake()
            await asyncio.Future()

        return fake_sleep

    return _build
