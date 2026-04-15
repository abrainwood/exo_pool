"""Integration test: MQTT shadow messages → coordinator data contract.

Verifies that shadow payloads from AWS IoT produce data in the exact
shape that the existing entity platforms expect. Uses real device data
captured from Andrew's eXO (JT21007072) as test fixtures.

This is the critical integration seam - if these tests pass, the entities
will render correctly regardless of whether data came from REST or MQTT.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from custom_components.exo_pool.mqtt_client import ExoMqttClient
from tests.conftest import (
    IOT_ENDPOINT,
    IOT_REGION,
    SAMPLE_CREDENTIALS,
    SAMPLE_SERIAL,
)


# ---------------------------------------------------------------------------
# Real device shadow data captured from JT21007072 (2026-04-15)
# ---------------------------------------------------------------------------

REAL_REPORTED_STATE = {
    "aws": {
        "status": "connected",
        "timestamp": 1776201508702,
        "session_id": "3c42fd81-6249-4d80-851d-3b5634fbe5bf",
    },
    "debug": {
        "Version Firmware": "V85W4B0",
        "RSSI": -43,
        "MQTT connection": 1,
        "Last error": 65278,
    },
    "vr": "V85W4",
    "equipment": {
        "swc_0": {
            "orp_sp": 700,
            "ph_sp": 74,
            "swc": 30,
            "swc_low": 20,
            "sns_1": {"sensor_type": "Ph", "state": 1, "value": 73},
            "sns_2": {"sensor_type": "Orp", "state": 0, "value": 0},
            "sns_3": {"sensor_type": "Water temp", "state": 1, "value": 22},
            "temp": 1,
            "vsp": 1,
            "boost": 0,
            "boost_time": "24:00",
            "aux_1": {"state": 0, "type": "none", "color": 0, "mode": 0},
            "aux_2": {"state": 0, "color": 0, "mode": 0, "type": "none"},
            "vr": "V85R70",
            "production": 1,
            "filter_pump": {"state": 1, "type": 1},
            "low": 0,
            "ph_only": 1,
            "dual_link": 0,
            "error_code": 0,
            "error_state": 0,
            "lang": 0,
            "amp": 1,
            "aux230": 0,
            "sn": "ALWA01011338210240",
            "version": "V1",
            "exo_state": 1,
        },
    },
    "schedules": {
        "sch1": {
            "timer": {"start": "00:00", "end": "00:00"},
            "enabled": 0,
            "active": 0,
            "id": "sch_1",
            "name": "Salt Water Chlorinator 1",
            "endpoint": "swc_1",
        },
        "sch3": {
            "enabled": 0,
            "active": 0,
            "id": "sch_3",
            "name": "Filter Pump 1",
            "endpoint": "ssp_1",
            "timer": {"start": "00:00", "end": "00:00"},
        },
        "supported": 5,
        "programmed": 0,
    },
}


def _make_get_accepted(reported: dict) -> bytes:
    """Build a shadow/get/accepted payload."""
    return json.dumps({
        "state": {"reported": reported},
        "metadata": {},
        "version": 150334,
        "timestamp": 1776206206,
    }).encode()


def _make_update_documents(current_reported: dict, previous_reported: dict) -> bytes:
    """Build a shadow/update/documents payload."""
    return json.dumps({
        "current": {"state": {"reported": current_reported, "desired": {}}},
        "previous": {"state": {"reported": previous_reported, "desired": {}}},
        "timestamp": 1776208189,
    }).encode()


@pytest.fixture
def connected_client():
    """An ExoMqttClient connected with mocked MQTT internals."""
    mock_conn = MagicMock()
    connect_future = MagicMock()
    connect_future.result.return_value = None
    mock_conn.connect.return_value = connect_future
    disconnect_future = MagicMock()
    disconnect_future.result.return_value = None
    mock_conn.disconnect.return_value = disconnect_future
    sub_future = MagicMock()
    sub_future.result.return_value = None
    mock_conn.subscribe.return_value = (sub_future, 1)
    pub_future = MagicMock()
    pub_future.result.return_value = None
    mock_conn.publish.return_value = (pub_future, 1)

    loop = MagicMock()
    client = ExoMqttClient(
        loop=loop,
        endpoint=IOT_ENDPOINT,
        region=IOT_REGION,
        serial=SAMPLE_SERIAL,
    )
    client._build_connection = MagicMock(return_value=mock_conn)

    # Collect what gets bridged to the HA loop
    received_data = []

    def capture_call_soon(fn, *args):
        fn(*args)

    loop.call_soon_threadsafe = capture_call_soon

    client.set_shadow_callback(lambda data: received_data.append(data))
    client.connect(SAMPLE_CREDENTIALS)

    return client, mock_conn, received_data


def _get_subscribe_callback(mock_conn, topic_fragment: str):
    """Find the MQTT callback registered for a topic containing the fragment."""
    for c in mock_conn.subscribe.call_args_list:
        topic = c.kwargs.get("topic") or c.args[0]
        if topic_fragment in topic:
            return c.kwargs.get("callback") or c.args[2]
    raise AssertionError(f"No subscription found matching '{topic_fragment}'")


class TestGetAcceptedDataContract:
    """shadow/get/accepted → coordinator data matches entity expectations."""

    def test_full_reported_state_reaches_callback(self, connected_client):
        _, mock_conn, received = connected_client
        cb = _get_subscribe_callback(mock_conn, "get/accepted")

        cb(
            topic=f"$aws/things/{SAMPLE_SERIAL}/shadow/get/accepted",
            payload=_make_get_accepted(REAL_REPORTED_STATE),
            dup=False, qos=1, retain=False,
        )

        assert len(received) == 1
        data = received[0]
        # This IS coordinator.data - every entity reads from this dict
        assert data is not None

    def test_sensor_data_paths_exist(self, connected_client):
        """All sensor.py read paths must be present in coordinator data."""
        _, mock_conn, received = connected_client
        cb = _get_subscribe_callback(mock_conn, "get/accepted")
        cb(
            topic=f"$aws/things/{SAMPLE_SERIAL}/shadow/get/accepted",
            payload=_make_get_accepted(REAL_REPORTED_STATE),
            dup=False, qos=1, retain=False,
        )
        data = received[0]
        swc = data["equipment"]["swc_0"]

        # TempSensor
        assert swc["sns_3"]["value"] == 22

        # ORPSensor
        assert swc["sns_2"]["value"] == 0
        assert swc["orp_sp"] == 700

        # PHSensor (value is ×10 in raw, entity divides)
        assert swc["sns_1"]["value"] == 73
        assert swc["ph_sp"] == 74

        # SWCOutputSensor
        assert swc["swc"] == 30

        # SWCLowOutputSensor
        assert swc["swc_low"] == 20

        # ErrorCodeSensor
        assert swc["error_code"] == 0

        # WifiRssiSensor
        assert data["debug"]["RSSI"] == -43

    def test_binary_sensor_data_paths_exist(self, connected_client):
        """All binary_sensor.py read paths must be present."""
        _, mock_conn, received = connected_client
        cb = _get_subscribe_callback(mock_conn, "get/accepted")
        cb(
            topic=f"$aws/things/{SAMPLE_SERIAL}/shadow/get/accepted",
            payload=_make_get_accepted(REAL_REPORTED_STATE),
            dup=False, qos=1, retain=False,
        )
        data = received[0]
        swc = data["equipment"]["swc_0"]

        # FilterPumpBinarySensor
        assert swc["filter_pump"]["state"] == 1

        # ErrorStateBinarySensor
        assert swc["error_state"] == 0

        # SaltWaterChlorinatorBinarySensor
        assert swc["production"] == 1

        # AwsConnectivityBinarySensor
        assert data["aws"]["status"] == "connected"

        # ScheduleBinarySensor - schedules dict exists with valid entries
        schedules = data["schedules"]
        assert "sch1" in schedules
        assert schedules["sch1"]["active"] == 0

    def test_switch_data_paths_exist(self, connected_client):
        """All switch.py read paths must be present."""
        _, mock_conn, received = connected_client
        cb = _get_subscribe_callback(mock_conn, "get/accepted")
        cb(
            topic=f"$aws/things/{SAMPLE_SERIAL}/shadow/get/accepted",
            payload=_make_get_accepted(REAL_REPORTED_STATE),
            dup=False, qos=1, retain=False,
        )
        swc = received[0]["equipment"]["swc_0"]

        # ORPBoostSwitch
        assert swc["boost"] == 0
        assert swc["boost_time"] == "24:00"

        # PowerSwitch
        assert swc["exo_state"] == 1

        # ChlorinatorSwitch
        assert swc["production"] == 1

        # Aux1Switch
        assert swc["aux_1"]["state"] == 0

        # Aux2Switch
        assert swc["aux_2"]["state"] == 0

        # SWCLowModeSwitch
        assert swc["low"] == 0

    def test_number_data_paths_exist(self, connected_client):
        """All number.py read paths must be present."""
        _, mock_conn, received = connected_client
        cb = _get_subscribe_callback(mock_conn, "get/accepted")
        cb(
            topic=f"$aws/things/{SAMPLE_SERIAL}/shadow/get/accepted",
            payload=_make_get_accepted(REAL_REPORTED_STATE),
            dup=False, qos=1, retain=False,
        )
        swc = received[0]["equipment"]["swc_0"]

        # Capability flags that control which number entities are created
        assert swc["ph_only"] == 1
        assert swc["dual_link"] == 0

        # ExoPoolSwcOutputNumber
        assert isinstance(swc["swc"], int)

        # ExoPoolSwcLowOutputNumber
        assert isinstance(swc["swc_low"], int)

    def test_climate_data_paths_exist(self, connected_client):
        """climate.py reads from equipment.swc_0 and aux_2."""
        _, mock_conn, received = connected_client
        cb = _get_subscribe_callback(mock_conn, "get/accepted")
        cb(
            topic=f"$aws/things/{SAMPLE_SERIAL}/shadow/get/accepted",
            payload=_make_get_accepted(REAL_REPORTED_STATE),
            dup=False, qos=1, retain=False,
        )
        swc = received[0]["equipment"]["swc_0"]

        # Climate reads water temp for current_temperature
        assert swc["sns_3"]["value"] == 22

        # Climate reads aux_2.mode to decide visibility
        assert swc["aux_2"]["mode"] == 0  # not heat mode, so climate won't show


class TestUpdateDocumentsDataContract:
    """shadow/update/documents → coordinator data on state change."""

    def test_chlorinator_percentage_change(self, connected_client):
        """Simulates the exact change we observed: swc 30 → 40."""
        _, mock_conn, received = connected_client
        cb = _get_subscribe_callback(mock_conn, "update/documents")

        current = json.loads(json.dumps(REAL_REPORTED_STATE))
        previous = json.loads(json.dumps(REAL_REPORTED_STATE))
        current["equipment"]["swc_0"]["swc"] = 40  # changed
        previous["equipment"]["swc_0"]["swc"] = 30  # original

        cb(
            topic=f"$aws/things/{SAMPLE_SERIAL}/shadow/update/documents",
            payload=_make_update_documents(current, previous),
            dup=False, qos=1, retain=False,
        )

        assert len(received) == 1
        data = received[0]
        assert data["equipment"]["swc_0"]["swc"] == 40

    def test_aux_switch_toggle(self, connected_client):
        """Simulates aux_1 toggled on."""
        _, mock_conn, received = connected_client
        cb = _get_subscribe_callback(mock_conn, "update/documents")

        current = json.loads(json.dumps(REAL_REPORTED_STATE))
        previous = json.loads(json.dumps(REAL_REPORTED_STATE))
        current["equipment"]["swc_0"]["aux_1"]["state"] = 1

        cb(
            topic=f"$aws/things/{SAMPLE_SERIAL}/shadow/update/documents",
            payload=_make_update_documents(current, previous),
            dup=False, qos=1, retain=False,
        )

        assert received[0]["equipment"]["swc_0"]["aux_1"]["state"] == 1

    def test_ph_sensor_reading_update(self, connected_client):
        """Simulates a device-pushed sensor reading (no user action)."""
        _, mock_conn, received = connected_client
        cb = _get_subscribe_callback(mock_conn, "update/documents")

        current = json.loads(json.dumps(REAL_REPORTED_STATE))
        previous = json.loads(json.dumps(REAL_REPORTED_STATE))
        current["equipment"]["swc_0"]["sns_1"]["value"] = 74  # pH changed

        cb(
            topic=f"$aws/things/{SAMPLE_SERIAL}/shadow/update/documents",
            payload=_make_update_documents(current, previous),
            dup=False, qos=1, retain=False,
        )

        assert received[0]["equipment"]["swc_0"]["sns_1"]["value"] == 74

    def test_schedule_activation(self, connected_client):
        """Simulates a schedule becoming active."""
        _, mock_conn, received = connected_client
        cb = _get_subscribe_callback(mock_conn, "update/documents")

        current = json.loads(json.dumps(REAL_REPORTED_STATE))
        previous = json.loads(json.dumps(REAL_REPORTED_STATE))
        current["schedules"]["sch3"]["active"] = 1
        current["schedules"]["sch3"]["enabled"] = 1

        cb(
            topic=f"$aws/things/{SAMPLE_SERIAL}/shadow/update/documents",
            payload=_make_update_documents(current, previous),
            dup=False, qos=1, retain=False,
        )

        assert received[0]["schedules"]["sch3"]["active"] == 1
        assert received[0]["schedules"]["sch3"]["enabled"] == 1


class TestPublishDesiredContract:
    """Verify write payloads match what the device shadow expects."""

    def test_pool_value_write_shape(self, connected_client):
        """set_pool_value("swc", 40) should produce the right shadow payload."""
        client, mock_conn, _ = connected_client

        client.publish_desired({"equipment": {"swc_0": {"swc": 40}}})

        pub_calls = [
            c for c in mock_conn.publish.call_args_list
            if (c.kwargs.get("topic") or c.args[0]).endswith("/shadow/update")
        ]
        assert len(pub_calls) == 1

        payload = json.loads(pub_calls[0].kwargs.get("payload") or pub_calls[0].args[1])
        assert payload == {
            "state": {
                "desired": {
                    "equipment": {"swc_0": {"swc": 40}},
                },
            },
        }

    def test_heating_write_shape(self, connected_client):
        """set_heating_value("sp", 28) should produce the right payload."""
        client, mock_conn, _ = connected_client

        client.publish_desired({"heating": {"sp": 28}})

        pub_calls = [
            c for c in mock_conn.publish.call_args_list
            if (c.kwargs.get("topic") or c.args[0]).endswith("/shadow/update")
        ]
        assert len(pub_calls) == 1

        payload = json.loads(pub_calls[0].kwargs.get("payload") or pub_calls[0].args[1])
        assert payload == {
            "state": {
                "desired": {
                    "heating": {"sp": 28},
                },
            },
        }

    def test_schedule_write_shape(self, connected_client):
        """update_schedule should produce the right payload."""
        client, mock_conn, _ = connected_client

        client.publish_desired({
            "schedules": {"sch3": {"timer": {"start": "08:00", "end": "18:00"}}},
        })

        pub_calls = [
            c for c in mock_conn.publish.call_args_list
            if (c.kwargs.get("topic") or c.args[0]).endswith("/shadow/update")
        ]
        assert len(pub_calls) == 1

        payload = json.loads(pub_calls[0].kwargs.get("payload") or pub_calls[0].args[1])
        assert payload == {
            "state": {
                "desired": {
                    "schedules": {
                        "sch3": {"timer": {"start": "08:00", "end": "18:00"}},
                    },
                },
            },
        }
