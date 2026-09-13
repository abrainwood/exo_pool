"""Tests for ExoMqttClient - AWS IoT MQTT client for eXO device shadow."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from custom_components.exo_pool.mqtt_client import _diff_desired
from tests.conftest import (
    IOT_ENDPOINT,
    IOT_REGION,
    SAMPLE_CREDENTIALS,
    SAMPLE_SERIAL,
    get_subscribe_callback,
)

# Fixtures mock_mqtt_connection, mock_event_loop, and build_client
# are defined in conftest.py and shared with test_mqtt_integration.py.


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
        assert mock_mqtt_connection.disconnect.call_count == 1

    def test_connect_with_all_subscribes_failing_reports_not_connected(
        self, build_client, mock_mqtt_connection
    ):
        sub_future = MagicMock()
        sub_future.result.side_effect = Exception("Forbidden")
        mock_mqtt_connection.subscribe.return_value = (sub_future, 1)
        client = build_client()

        with pytest.raises(ConnectionError):
            client.connect(SAMPLE_CREDENTIALS)

        assert client.connected is False

    def test_connect_with_all_subscribes_failing_does_not_also_call_reconnect_failed_callback(
        self, build_client, mock_mqtt_connection, mock_event_loop
    ):
        reconnect_cb = MagicMock()
        sub_future = MagicMock()
        sub_future.result.side_effect = Exception("Forbidden")
        mock_mqtt_connection.subscribe.return_value = (sub_future, 1)
        client = build_client()
        client.set_reconnect_failed_callback(reconnect_cb)

        with pytest.raises(ConnectionError):
            client.connect(SAMPLE_CREDENTIALS)

        for call in mock_event_loop.call_soon_threadsafe.call_args_list:
            assert reconnect_cb not in call.args

    def test_connect_with_all_subscribes_failing_raises(
        self, build_client, mock_mqtt_connection
    ):
        sub_future = MagicMock()
        sub_future.result.side_effect = Exception("Forbidden")
        mock_mqtt_connection.subscribe.return_value = (sub_future, 1)
        client = build_client()

        with pytest.raises(ConnectionError):
            client.connect(SAMPLE_CREDENTIALS)


class TestDisconnect:
    """Disconnect tests."""

    def test_disconnect_when_connected(self, build_client, mock_mqtt_connection):
        client = build_client()
        client.connect(SAMPLE_CREDENTIALS)
        client.disconnect()

        mock_mqtt_connection.disconnect.assert_called_once()
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

        # Bridged via call_soon_threadsafe (not called directly), which the
        # fixture executes immediately so the callback has already run.
        mock_event_loop.call_soon_threadsafe.assert_called_with(callback, reported_state, {})
        callback.assert_called_once_with(reported_state, {})

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

        mock_event_loop.call_soon_threadsafe.assert_called_with(callback, reported_state, {})
        callback.assert_called_once_with(reported_state, {})

    def test_previous_state_present_without_desired_diffs_against_empty(
        self, build_client, mock_mqtt_connection, mock_event_loop
    ):
        callback = MagicMock()
        client = build_client()
        client.set_shadow_callback(callback)
        client.connect(SAMPLE_CREDENTIALS)

        mqtt_callback = get_subscribe_callback(mock_mqtt_connection, "update/documents")

        reported_state = {"equipment": {"swc_0": {"production": 0}}}
        desired_state = {"equipment": {"swc_0": {"production": 0}}}
        shadow_doc = {
            "current": {
                "state": {"reported": reported_state, "desired": desired_state},
            },
            "previous": {"state": {"reported": {}}},
            "timestamp": 1776208189,
        }
        mqtt_callback(
            topic=f"$aws/things/{SAMPLE_SERIAL}/shadow/update/documents",
            payload=json.dumps(shadow_doc).encode(),
            dup=False,
            qos=1,
            retain=False,
        )

        callback.assert_called_once_with(reported_state, desired_state)

    def test_missing_previous_entirely_never_supersedes_a_pending_write(
        self, build_client, mock_mqtt_connection, mock_event_loop
    ):
        callback = MagicMock()
        client = build_client()
        client.set_shadow_callback(callback)
        client.connect(SAMPLE_CREDENTIALS)

        mqtt_callback = get_subscribe_callback(mock_mqtt_connection, "update/documents")

        shadow_doc = {
            "current": {
                "state": {
                    "reported": {"equipment": {"swc_0": {"production": 0}}},
                    "desired": {"equipment": {"swc_0": {"production": 1}}},
                },
            },
        }
        mqtt_callback(
            topic=f"$aws/things/{SAMPLE_SERIAL}/shadow/update/documents",
            payload=json.dumps(shadow_doc).encode(),
            dup=False,
            qos=1,
            retain=False,
        )

        callback.assert_called_once_with(
            {"equipment": {"swc_0": {"production": 0}}}, {}
        )

    def test_identical_desired_in_previous_and_current_yields_no_change(
        self, build_client, mock_mqtt_connection, mock_event_loop
    ):
        callback = MagicMock()
        client = build_client()
        client.set_shadow_callback(callback)
        client.connect(SAMPLE_CREDENTIALS)

        mqtt_callback = get_subscribe_callback(mock_mqtt_connection, "update/documents")

        reported_state = {"equipment": {"swc_0": {"production": 0}}}
        desired_state = {"equipment": {"swc_0": {"production": 0}}}
        shadow_doc = {
            "current": {
                "state": {"reported": reported_state, "desired": desired_state},
            },
            "previous": {"state": {"reported": {}, "desired": desired_state}},
        }
        mqtt_callback(
            topic=f"$aws/things/{SAMPLE_SERIAL}/shadow/update/documents",
            payload=json.dumps(shadow_doc).encode(),
            dup=False,
            qos=1,
            retain=False,
        )

        callback.assert_called_once_with(reported_state, {})

    def test_shadow_callback_never_surfaces_desired_from_get_accepted(
        self, build_client, mock_mqtt_connection, mock_event_loop
    ):
        callback = MagicMock()
        client = build_client()
        client.set_shadow_callback(callback)
        client.connect(SAMPLE_CREDENTIALS)

        get_sub_call = None
        for c in mock_mqtt_connection.subscribe.call_args_list:
            topic = c.kwargs.get("topic") or c.args[0]
            if "get/accepted" in topic:
                get_sub_call = c
                break
        mqtt_callback = get_sub_call.kwargs.get("callback") or get_sub_call.args[2]

        reported_state = {"equipment": {"swc_0": {"swc": 30}}}
        shadow_get = {
            "state": {
                "reported": reported_state,
                "desired": {"equipment": {"swc_0": {"production": 0}}},
            },
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

        callback.assert_called_once_with(reported_state, {})

    def test_no_callback_set_does_not_bridge(
        self, build_client, mock_mqtt_connection, mock_event_loop
    ):
        client = build_client()
        # No callback set
        client.connect(SAMPLE_CREDENTIALS)
        mock_event_loop.call_soon_threadsafe.reset_mock()

        # Fire a shadow update - should not attempt to bridge to event loop
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

        mock_event_loop.call_soon_threadsafe.assert_not_called()

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

    def test_update_accepted_does_not_invoke_callback_because_its_state_is_partial(
        self, build_client, mock_mqtt_connection, mock_event_loop
    ):
        callback = MagicMock()
        client = build_client()
        client.set_shadow_callback(callback)
        client.connect(SAMPLE_CREDENTIALS)
        mock_event_loop.call_soon_threadsafe.reset_mock()

        for c in mock_mqtt_connection.subscribe.call_args_list:
            topic = c.kwargs.get("topic") or c.args[0]
            if "update/accepted" in topic:
                mqtt_callback = c.kwargs.get("callback") or c.args[2]
                mqtt_callback(
                    topic=topic,
                    payload=json.dumps({
                        "state": {"desired": {"equipment": {"swc_0": {"swc": 40}}}},
                        "metadata": {},
                        "version": 150335,
                        "timestamp": 1776208189,
                    }).encode(),
                    dup=False, qos=1, retain=False,
                )
                break

        mock_event_loop.call_soon_threadsafe.assert_not_called()

    def test_update_delta_does_not_invoke_callback_because_it_carries_only_changed_fields(
        self, build_client, mock_mqtt_connection, mock_event_loop
    ):
        callback = MagicMock()
        client = build_client()
        client.set_shadow_callback(callback)
        client.connect(SAMPLE_CREDENTIALS)
        mock_event_loop.call_soon_threadsafe.reset_mock()

        for c in mock_mqtt_connection.subscribe.call_args_list:
            topic = c.kwargs.get("topic") or c.args[0]
            if "update/delta" in topic:
                mqtt_callback = c.kwargs.get("callback") or c.args[2]
                mqtt_callback(
                    topic=topic,
                    payload=json.dumps({
                        "state": {"equipment": {"swc_0": {"swc": 40}}},
                        "version": 150335,
                        "timestamp": 1776208189,
                    }).encode(),
                    dup=False, qos=1, retain=False,
                )
                break

        mock_event_loop.call_soon_threadsafe.assert_not_called()


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
        assert len(get_calls) == 1

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

    def test_on_connection_resumed_calls_reconnect_failed_on_subscribe_error(
        self, build_client, mock_mqtt_connection, mock_event_loop
    ):
        reconnect_cb = MagicMock()
        client = build_client()
        client.set_reconnect_failed_callback(reconnect_cb)
        client.connect(SAMPLE_CREDENTIALS)

        # Make subscribe fail on reconnect (simulating expired credentials)
        sub_future = MagicMock()
        sub_future.result.side_effect = Exception("Forbidden")
        mock_mqtt_connection.subscribe.return_value = (sub_future, 1)

        client._on_connection_interrupted(
            connection=mock_mqtt_connection, error=Exception("blip")
        )
        client._on_connection_resumed(
            connection=mock_mqtt_connection,
            return_code=0,
            session_present=False,
        )

        assert client.connected is False
        mock_event_loop.call_soon_threadsafe.assert_called_with(reconnect_cb)


class TestInterruptWatchdog:
    def test_interrupt_arms_a_watchdog_timer(
        self, build_client, mock_mqtt_connection, mock_event_loop
    ):
        client = build_client()
        client.connect(SAMPLE_CREDENTIALS)
        initial_call_later_count = mock_event_loop.call_later.call_count

        client._on_connection_interrupted(
            connection=mock_mqtt_connection, error=Exception("network down")
        )

        assert mock_event_loop.call_later.call_count > initial_call_later_count

    def test_watchdog_firing_invokes_the_registered_callback(
        self, build_client, mock_mqtt_connection, mock_event_loop
    ):
        watchdog_cb = MagicMock()
        client = build_client()
        client.set_interrupted_watchdog_callback(watchdog_cb)
        client.connect(SAMPLE_CREDENTIALS)

        client._on_connection_interrupted(
            connection=mock_mqtt_connection, error=Exception("network down")
        )

        # Find the watchdog's call_later registration and fire it, simulating
        # the timeout elapsing with no _on_connection_resumed in between.
        from custom_components.exo_pool.mqtt_client import (
            _INTERRUPT_WATCHDOG_TIMEOUT,
        )

        watchdog_call = next(
            c
            for c in mock_event_loop.call_later.call_args_list
            if c.args[0] == _INTERRUPT_WATCHDOG_TIMEOUT
        )
        fire = watchdog_call.args[1]
        fire()

        watchdog_cb.assert_called_once()

    def test_resume_before_timeout_disarms_the_watchdog(
        self, build_client, mock_mqtt_connection, mock_event_loop
    ):
        watchdog_cb = MagicMock()
        mock_handle = MagicMock()
        mock_event_loop.call_later.return_value = mock_handle
        client = build_client()
        client.set_interrupted_watchdog_callback(watchdog_cb)
        client.connect(SAMPLE_CREDENTIALS)

        client._on_connection_interrupted(
            connection=mock_mqtt_connection, error=Exception("blip")
        )
        client._on_connection_resumed(
            connection=mock_mqtt_connection, return_code=0, session_present=False
        )

        mock_handle.cancel.assert_called()

    def test_watchdog_firing_after_a_resume_race_does_not_invoke_the_callback(
        self, build_client, mock_mqtt_connection, mock_event_loop
    ):
        watchdog_cb = MagicMock()
        client = build_client()
        client.set_interrupted_watchdog_callback(watchdog_cb)
        client.connect(SAMPLE_CREDENTIALS)

        client._on_connection_interrupted(
            connection=mock_mqtt_connection, error=Exception("blip")
        )
        client._on_connection_resumed(
            connection=mock_mqtt_connection, return_code=0, session_present=False
        )
        # Simulate the disarm losing the race: the timer still fires once
        # after resume already restored a healthy connection.
        client._watchdog_fire()

        watchdog_cb.assert_not_called()

    def test_disconnect_after_interrupt_disarms_the_watchdog(
        self, build_client, mock_mqtt_connection, mock_event_loop
    ):
        heartbeat_handle = MagicMock()
        watchdog_handle = MagicMock()
        mock_event_loop.call_later.side_effect = [heartbeat_handle, watchdog_handle]
        client = build_client()
        client.connect(SAMPLE_CREDENTIALS)

        client._on_connection_interrupted(
            connection=mock_mqtt_connection, error=Exception("blip")
        )
        client.disconnect()

        watchdog_handle.cancel.assert_called()

    def test_arming_never_touches_call_later_directly_from_the_crt_thread(
        self, build_client, mock_mqtt_connection, mock_event_loop_deferred
    ):
        client = build_client()
        client.connect(SAMPLE_CREDENTIALS)
        # Swap to the non-executing loop double for the interrupt itself, so
        # the timer mutation's call_soon_threadsafe gating is observable.
        client._loop = mock_event_loop_deferred

        client._on_connection_interrupted(
            connection=mock_mqtt_connection, error=Exception("blip")
        )

        mock_event_loop_deferred.call_later.assert_not_called()
        mock_event_loop_deferred.call_soon_threadsafe.assert_called_with(
            client._arm_watchdog_on_loop
        )


class TestConnectionStateChanged:
    def test_connect_success_notifies_the_state_changed_callback(
        self, build_client, mock_event_loop
    ):
        state_cb = MagicMock()
        client = build_client()
        client.set_state_changed_callback(state_cb)

        client.connect(SAMPLE_CREDENTIALS)

        mock_event_loop.call_soon_threadsafe.assert_any_call(state_cb, True)

    def test_interrupt_notifies_the_state_changed_callback_with_false(
        self, build_client, mock_mqtt_connection, mock_event_loop
    ):
        state_cb = MagicMock()
        client = build_client()
        client.set_state_changed_callback(state_cb)
        client.connect(SAMPLE_CREDENTIALS)
        mock_event_loop.call_soon_threadsafe.reset_mock()

        client._on_connection_interrupted(
            connection=mock_mqtt_connection, error=Exception("blip")
        )

        mock_event_loop.call_soon_threadsafe.assert_any_call(state_cb, False)

    def test_disconnect_notifies_the_state_changed_callback_with_false(
        self, build_client, mock_event_loop
    ):
        state_cb = MagicMock()
        client = build_client()
        client.set_state_changed_callback(state_cb)
        client.connect(SAMPLE_CREDENTIALS)
        mock_event_loop.call_soon_threadsafe.reset_mock()

        client.disconnect()

        mock_event_loop.call_soon_threadsafe.assert_any_call(state_cb, False)


class TestHeartbeat:
    """Heartbeat (periodic shadow/get) tests."""

    def test_connect_starts_heartbeat(self, build_client, mock_event_loop):
        client = build_client()
        client.connect(SAMPLE_CREDENTIALS)

        # Should have scheduled a call_later on the event loop
        mock_event_loop.call_later.assert_called()
        args = mock_event_loop.call_later.call_args
        interval = args.args[0] if args.args else args[0][0]
        from custom_components.exo_pool.mqtt_client import _HEARTBEAT_INTERVAL

        assert interval == _HEARTBEAT_INTERVAL

    def test_starting_never_touches_call_later_directly_off_the_loop_thread(
        self, build_client, mock_event_loop_deferred
    ):
        # connect() runs the blocking handshake on an executor thread in
        # production - prove the heartbeat timer is armed via
        # call_soon_threadsafe rather than call_later called inline there.
        client = build_client(loop=mock_event_loop_deferred)

        client.connect(SAMPLE_CREDENTIALS)

        mock_event_loop_deferred.call_later.assert_not_called()
        mock_event_loop_deferred.call_soon_threadsafe.assert_any_call(
            client._start_heartbeat_on_loop
        )

    def test_heartbeat_tick_requests_shadow(
        self, build_client, mock_mqtt_connection
    ):
        client = build_client()
        client.connect(SAMPLE_CREDENTIALS)
        initial_pub_count = mock_mqtt_connection.publish.call_count

        # Simulate the heartbeat firing
        client._heartbeat_tick()

        new_pub_calls = mock_mqtt_connection.publish.call_args_list[
            initial_pub_count:
        ]
        get_calls = [
            c
            for c in new_pub_calls
            if (c.kwargs.get("topic") or c.args[0]).endswith("/shadow/get")
        ]
        assert len(get_calls) == 1

    def test_heartbeat_reschedules_after_tick(
        self, build_client, mock_event_loop
    ):
        client = build_client()
        client.connect(SAMPLE_CREDENTIALS)
        initial_call_count = mock_event_loop.call_later.call_count

        client._heartbeat_tick()

        assert mock_event_loop.call_later.call_count > initial_call_count

    def test_heartbeat_skips_when_disconnected(
        self, build_client, mock_mqtt_connection
    ):
        client = build_client()
        client.connect(SAMPLE_CREDENTIALS)
        initial_pub_count = mock_mqtt_connection.publish.call_count

        client._on_connection_interrupted(
            connection=mock_mqtt_connection, error=Exception("down")
        )

        client._heartbeat_tick()

        # Should not publish when disconnected
        new_pub_calls = mock_mqtt_connection.publish.call_args_list[
            initial_pub_count:
        ]
        get_calls = [
            c
            for c in new_pub_calls
            if (c.kwargs.get("topic") or c.args[0]).endswith("/shadow/get")
        ]
        assert len(get_calls) == 0

    def test_disconnect_cancels_heartbeat(self, build_client, mock_event_loop):
        # Set up call_later to return a mock handle with cancel()
        mock_handle = MagicMock()
        mock_event_loop.call_later.return_value = mock_handle

        client = build_client()
        client.connect(SAMPLE_CREDENTIALS)
        client.disconnect()

        mock_handle.cancel.assert_called()


class TestBuildConnection:
    """Test that _build_connection creates a properly configured MQTT connection."""

    def test_build_connection_signs_the_websocket_connection_with_aws_credentials(self):
        from unittest.mock import patch
        from custom_components.exo_pool.mqtt_client import ExoMqttClient
        import custom_components.exo_pool.mqtt_client as mqtt_mod

        mock_conn = MagicMock()

        with patch.object(
            mqtt_mod,
            "mqtt_connection_builder",
        ) as mock_builder:
            mock_builder.websockets_with_default_aws_signing.return_value = mock_conn

            loop = MagicMock()
            client = ExoMqttClient(
                loop=loop,
                endpoint=IOT_ENDPOINT,
                region=IOT_REGION,
                serial=SAMPLE_SERIAL,
            )
            result = client._build_connection(SAMPLE_CREDENTIALS)

        mock_builder.websockets_with_default_aws_signing.assert_called_once()
        call_kwargs = mock_builder.websockets_with_default_aws_signing.call_args.kwargs

        assert call_kwargs["endpoint"] == IOT_ENDPOINT
        assert call_kwargs["region"] == IOT_REGION
        assert "credentials_provider" in call_kwargs
        assert call_kwargs["on_connection_interrupted"] == client._on_connection_interrupted
        assert call_kwargs["on_connection_resumed"] == client._on_connection_resumed

        assert result is mock_conn


class TestDiffDesired:
    def test_identical_desired_in_previous_and_current_yields_no_diff(self):
        previous = {"equipment": {"swc_0": {"production": 0}}}
        current = {"equipment": {"swc_0": {"production": 0}}}

        assert _diff_desired(previous, current) == {}

    def test_change_on_a_sibling_leaf_does_not_touch_the_unchanged_leaf(self):
        previous = {"schedules": {"main": {"timer": {"start": "08:00", "end": "18:00"}}}}
        current = {"schedules": {"main": {"timer": {"start": "09:00", "end": "18:00"}}}}

        diff = _diff_desired(previous, current)

        assert diff == {"schedules": {"main": {"timer": {"start": "09:00"}}}}

    def test_key_deleted_between_previous_and_current_is_not_a_change(self):
        previous = {"equipment": {"swc_0": {"production": 0}}}
        current = {"equipment": {}}

        assert _diff_desired(previous, current) == {}
