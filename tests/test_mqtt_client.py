"""Tests for ExoMqttClient - AWS IoT MQTT client for eXO device shadow."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch, call

import pytest

from tests.conftest import (
    IOT_ENDPOINT,
    IOT_REGION,
    SAMPLE_CREDENTIALS,
    SAMPLE_SERIAL,
)


@pytest.fixture
def mock_mqtt_connection():
    """Create a mock MQTT connection that behaves like awscrt mqtt."""
    conn = MagicMock()

    # connect() returns a future that resolves immediately
    connect_future = MagicMock()
    connect_future.result.return_value = None
    conn.connect.return_value = connect_future

    # disconnect() returns a future
    disconnect_future = MagicMock()
    disconnect_future.result.return_value = None
    conn.disconnect.return_value = disconnect_future

    # subscribe() returns (future, packet_id)
    sub_future = MagicMock()
    sub_future.result.return_value = None
    conn.subscribe.return_value = (sub_future, 1)

    # publish() returns (future, packet_id)
    pub_future = MagicMock()
    pub_future.result.return_value = None
    conn.publish.return_value = (pub_future, 1)

    return conn


@pytest.fixture
def mock_event_loop():
    """Mock the HA event loop for thread-safe callback bridging."""
    loop = MagicMock()
    loop.call_soon_threadsafe = MagicMock()
    return loop


@pytest.fixture
def build_client(mock_mqtt_connection, mock_event_loop):
    """Factory to build an ExoMqttClient with mocked internals."""
    from custom_components.exo_pool.mqtt_client import ExoMqttClient

    def _build(**kwargs):
        client = ExoMqttClient(
            loop=mock_event_loop,
            endpoint=kwargs.get("endpoint", IOT_ENDPOINT),
            region=kwargs.get("region", IOT_REGION),
            serial=kwargs.get("serial", SAMPLE_SERIAL),
        )
        # Inject the mock connection so we don't hit real AWS
        client._build_connection = MagicMock(return_value=mock_mqtt_connection)
        return client

    return _build


class TestConnect:
    """Connection lifecycle tests."""

    def test_connect_creates_connection_and_subscribes(
        self, build_client, mock_mqtt_connection
    ):
        client = build_client()
        client.connect(SAMPLE_CREDENTIALS)

        # Should have built a connection with the credentials
        client._build_connection.assert_called_once_with(SAMPLE_CREDENTIALS)

        # Should have connected
        mock_mqtt_connection.connect.assert_called_once()

        # Should have subscribed to shadow topics
        subscribed_topics = [
            c.kwargs["topic"] if "topic" in c.kwargs else c.args[0]
            for c in mock_mqtt_connection.subscribe.call_args_list
        ]
        assert f"$aws/things/{SAMPLE_SERIAL}/shadow/get/accepted" in subscribed_topics
        assert (
            f"$aws/things/{SAMPLE_SERIAL}/shadow/update/documents"
            in subscribed_topics
        )
        assert (
            f"$aws/things/{SAMPLE_SERIAL}/shadow/update/accepted"
            in subscribed_topics
        )
        assert (
            f"$aws/things/{SAMPLE_SERIAL}/shadow/update/delta" in subscribed_topics
        )

    def test_connect_requests_initial_shadow(
        self, build_client, mock_mqtt_connection
    ):
        client = build_client()
        client.connect(SAMPLE_CREDENTIALS)

        # Should publish to shadow/get to request current state
        publish_calls = mock_mqtt_connection.publish.call_args_list
        get_calls = [
            c
            for c in publish_calls
            if f"$aws/things/{SAMPLE_SERIAL}/shadow/get"
            == (c.kwargs.get("topic") or c.args[0])
        ]
        assert len(get_calls) == 1

    def test_connected_property_reflects_state(
        self, build_client, mock_mqtt_connection
    ):
        client = build_client()
        assert client.connected is False

        client.connect(SAMPLE_CREDENTIALS)
        assert client.connected is True

    def test_connect_when_already_connected_disconnects_first(
        self, build_client, mock_mqtt_connection
    ):
        client = build_client()
        client.connect(SAMPLE_CREDENTIALS)
        assert mock_mqtt_connection.disconnect.call_count == 0

        # Connect again (e.g. credential refresh)
        client.connect(SAMPLE_CREDENTIALS)
        # Should have disconnected the old connection first
        assert mock_mqtt_connection.disconnect.call_count >= 1


class TestDisconnect:
    """Disconnect tests."""

    def test_disconnect_when_connected(self, build_client, mock_mqtt_connection):
        client = build_client()
        client.connect(SAMPLE_CREDENTIALS)
        client.disconnect()

        mock_mqtt_connection.disconnect.assert_called()
        assert client.connected is False

    def test_disconnect_when_not_connected(self, build_client):
        client = build_client()
        # Should not raise
        client.disconnect()
        assert client.connected is False


class TestShadowCallback:
    """Shadow update callback tests."""

    def test_shadow_callback_invoked_on_update_documents(
        self, build_client, mock_mqtt_connection, mock_event_loop
    ):
        callback = MagicMock()
        client = build_client()
        client.set_shadow_callback(callback)
        client.connect(SAMPLE_CREDENTIALS)

        # Find the callback registered for update/documents
        doc_sub_call = None
        for c in mock_mqtt_connection.subscribe.call_args_list:
            topic = c.kwargs.get("topic") or c.args[0]
            if "update/documents" in topic:
                doc_sub_call = c
                break
        assert doc_sub_call is not None, "No subscription for update/documents"

        # Extract the callback the client registered with subscribe()
        mqtt_callback = doc_sub_call.kwargs.get("callback") or doc_sub_call.args[2]

        # Simulate a shadow update message
        reported_state = {
            "equipment": {"swc_0": {"swc": 40, "production": 1}},
            "schedules": {},
        }
        shadow_doc = {
            "current": {
                "state": {"reported": reported_state, "desired": {}},
            },
            "previous": {
                "state": {"reported": {"equipment": {"swc_0": {"swc": 30}}}}
            },
            "timestamp": 1776208189,
        }
        mqtt_callback(
            topic=f"$aws/things/{SAMPLE_SERIAL}/shadow/update/documents",
            payload=json.dumps(shadow_doc).encode(),
            dup=False,
            qos=1,
            retain=False,
        )

        # Should bridge to HA event loop, not call directly
        mock_event_loop.call_soon_threadsafe.assert_called()
        # Extract the callback and args passed to call_soon_threadsafe
        bridged_call = mock_event_loop.call_soon_threadsafe.call_args
        bridged_fn = bridged_call.args[0]
        bridged_args = bridged_call.args[1:]

        # Execute the bridged callback
        bridged_fn(*bridged_args)

        # Now our shadow callback should have received the reported state
        callback.assert_called_once_with(reported_state)

    def test_shadow_callback_invoked_on_get_accepted(
        self, build_client, mock_mqtt_connection, mock_event_loop
    ):
        callback = MagicMock()
        client = build_client()
        client.set_shadow_callback(callback)
        client.connect(SAMPLE_CREDENTIALS)

        # Find the get/accepted subscription callback
        get_sub_call = None
        for c in mock_mqtt_connection.subscribe.call_args_list:
            topic = c.kwargs.get("topic") or c.args[0]
            if "get/accepted" in topic:
                get_sub_call = c
                break
        assert get_sub_call is not None

        mqtt_callback = get_sub_call.kwargs.get("callback") or get_sub_call.args[2]

        reported_state = {"equipment": {"swc_0": {"swc": 30}}}
        shadow_get = {
            "state": {"reported": reported_state},
            "metadata": {},
            "version": 150334,
            "timestamp": 1776206206,
        }
        mqtt_callback(
            topic=f"$aws/things/{SAMPLE_SERIAL}/shadow/get/accepted",
            payload=json.dumps(shadow_get).encode(),
            dup=False,
            qos=1,
            retain=False,
        )

        mock_event_loop.call_soon_threadsafe.assert_called()
        bridged_fn = mock_event_loop.call_soon_threadsafe.call_args.args[0]
        bridged_args = mock_event_loop.call_soon_threadsafe.call_args.args[1:]
        bridged_fn(*bridged_args)

        callback.assert_called_once_with(reported_state)

    def test_no_callback_set_does_not_raise(
        self, build_client, mock_mqtt_connection
    ):
        client = build_client()
        # No callback set
        client.connect(SAMPLE_CREDENTIALS)

        # Find any subscription callback and fire it - should not raise
        for c in mock_mqtt_connection.subscribe.call_args_list:
            topic = c.kwargs.get("topic") or c.args[0]
            if "update/documents" in topic:
                mqtt_callback = c.kwargs.get("callback") or c.args[2]
                shadow_doc = {
                    "current": {"state": {"reported": {}}},
                    "previous": {"state": {"reported": {}}},
                    "timestamp": 0,
                }
                mqtt_callback(
                    topic=topic,
                    payload=json.dumps(shadow_doc).encode(),
                    dup=False,
                    qos=1,
                    retain=False,
                )
                break

    def test_malformed_payload_does_not_raise(
        self, build_client, mock_mqtt_connection
    ):
        callback = MagicMock()
        client = build_client()
        client.set_shadow_callback(callback)
        client.connect(SAMPLE_CREDENTIALS)

        for c in mock_mqtt_connection.subscribe.call_args_list:
            topic = c.kwargs.get("topic") or c.args[0]
            if "update/documents" in topic:
                mqtt_callback = c.kwargs.get("callback") or c.args[2]
                # Send garbage
                mqtt_callback(
                    topic=topic,
                    payload=b"not json{{{",
                    dup=False,
                    qos=1,
                    retain=False,
                )
                break

        # Callback should NOT have been called with bad data
        callback.assert_not_called()


class TestPublishDesired:
    """Write (desired state) tests."""

    def test_publish_desired_sends_to_shadow_update(
        self, build_client, mock_mqtt_connection
    ):
        client = build_client()
        client.connect(SAMPLE_CREDENTIALS)

        desired = {"equipment": {"swc_0": {"swc": 40}}}
        client.publish_desired(desired)

        # Find the publish call for shadow/update
        update_calls = [
            c
            for c in mock_mqtt_connection.publish.call_args_list
            if f"$aws/things/{SAMPLE_SERIAL}/shadow/update"
            == (c.kwargs.get("topic") or c.args[0])
        ]
        assert len(update_calls) == 1

        payload = json.loads(
            update_calls[0].kwargs.get("payload") or update_calls[0].args[1]
        )
        assert payload == {"state": {"desired": desired}}

    def test_publish_desired_raises_when_not_connected(self, build_client):
        client = build_client()

        with pytest.raises(ConnectionError):
            client.publish_desired({"equipment": {"swc_0": {"swc": 40}}})


class TestReconnection:
    """Reconnection and connection event tests."""

    def test_on_connection_interrupted_sets_connected_false(
        self, build_client, mock_mqtt_connection
    ):
        client = build_client()
        client.connect(SAMPLE_CREDENTIALS)
        assert client.connected is True

        # Simulate connection interrupted
        client._on_connection_interrupted(
            connection=mock_mqtt_connection, error=Exception("network down")
        )
        assert client.connected is False

    def test_on_connection_resumed_resubscribes_and_gets_shadow(
        self, build_client, mock_mqtt_connection
    ):
        client = build_client()
        client.connect(SAMPLE_CREDENTIALS)
        initial_sub_count = mock_mqtt_connection.subscribe.call_count
        initial_pub_count = mock_mqtt_connection.publish.call_count

        # Simulate reconnection
        client._on_connection_resumed(
            connection=mock_mqtt_connection,
            return_code=0,
            session_present=False,
        )

        # Should re-subscribe to topics
        assert mock_mqtt_connection.subscribe.call_count > initial_sub_count

        # Should re-request shadow
        new_pub_calls = mock_mqtt_connection.publish.call_args_list[
            initial_pub_count:
        ]
        get_calls = [
            c
            for c in new_pub_calls
            if (c.kwargs.get("topic") or c.args[0]).endswith("/shadow/get")
        ]
        assert len(get_calls) >= 1

    def test_on_connection_resumed_restores_connected_state(
        self, build_client, mock_mqtt_connection
    ):
        client = build_client()
        client.connect(SAMPLE_CREDENTIALS)
        client._on_connection_interrupted(
            connection=mock_mqtt_connection, error=Exception("blip")
        )
        assert client.connected is False

        client._on_connection_resumed(
            connection=mock_mqtt_connection,
            return_code=0,
            session_present=False,
        )
        assert client.connected is True


class TestBuildConnection:
    """Test that _build_connection creates a properly configured MQTT connection."""

    def test_build_connection_uses_sigv4_websockets(self):
        from custom_components.exo_pool.mqtt_client import ExoMqttClient
        import custom_components.exo_pool.mqtt_client as mqtt_mod

        mock_builder = MagicMock()
        mock_conn = MagicMock()
        mock_builder.websockets_with_default_aws_signing.return_value = mock_conn

        loop = MagicMock()
        client = ExoMqttClient(
            loop=loop,
            endpoint=IOT_ENDPOINT,
            region=IOT_REGION,
            serial=SAMPLE_SERIAL,
        )

        original_builder = mqtt_mod.mqtt_connection_builder
        mqtt_mod.mqtt_connection_builder = mock_builder
        try:
            result = client._build_connection(SAMPLE_CREDENTIALS)
        finally:
            mqtt_mod.mqtt_connection_builder = original_builder

        mock_builder.websockets_with_default_aws_signing.assert_called_once()
        call_kwargs = mock_builder.websockets_with_default_aws_signing.call_args.kwargs

        assert call_kwargs["endpoint"] == IOT_ENDPOINT
        assert call_kwargs["region"] == IOT_REGION
        assert "credentials_provider" in call_kwargs
        assert call_kwargs["on_connection_interrupted"] == client._on_connection_interrupted
        assert call_kwargs["on_connection_resumed"] == client._on_connection_resumed

        assert result is mock_conn
