"""AWS IoT MQTT client for eXO device shadow communication.

Wraps the awsiotsdk library to provide a clean interface for connecting
to AWS IoT, subscribing to device shadow topics, and publishing desired
state changes. Handles the CRT thread to asyncio event loop bridge.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

from awscrt import auth, io, mqtt
from awsiot import mqtt_connection_builder

_LOGGER = logging.getLogger(__name__)

# Shadow MQTT topic patterns
_SHADOW_GET = "$aws/things/{serial}/shadow/get"
_SHADOW_GET_ACCEPTED = "$aws/things/{serial}/shadow/get/accepted"
_SHADOW_UPDATE = "$aws/things/{serial}/shadow/update"
_SHADOW_UPDATE_DOCUMENTS = "$aws/things/{serial}/shadow/update/documents"
_SHADOW_UPDATE_ACCEPTED = "$aws/things/{serial}/shadow/update/accepted"
_SHADOW_UPDATE_DELTA = "$aws/things/{serial}/shadow/update/delta"

_SUBSCRIBE_TOPICS = (
    _SHADOW_GET_ACCEPTED,
    _SHADOW_UPDATE_DOCUMENTS,
    _SHADOW_UPDATE_ACCEPTED,
    _SHADOW_UPDATE_DELTA,
)

_SUBSCRIBE_TIMEOUT = 5
_CONNECT_TIMEOUT = 10
_DISCONNECT_TIMEOUT = 5
_SUBSCRIBE_DELAY = 0.3


class ExoMqttClient:
    """AWS IoT MQTT client for eXO device shadow.

    Manages a persistent MQTT connection to AWS IoT for real-time
    shadow updates. All shadow callbacks are bridged onto the provided
    event loop (typically the HA event loop) via call_soon_threadsafe.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        endpoint: str,
        region: str,
        serial: str,
    ) -> None:
        self._loop = loop
        self._endpoint = endpoint
        self._region = region
        self._serial = serial
        self._connection: mqtt.Connection | None = None
        self._connected = False
        self._shadow_callback: Callable[[dict], None] | None = None

        # CRT resources - created once, reused across reconnections
        self._event_loop_group = io.EventLoopGroup(1)
        self._host_resolver = io.DefaultHostResolver(self._event_loop_group)
        self._client_bootstrap = io.ClientBootstrap(
            self._event_loop_group, self._host_resolver
        )

    @property
    def connected(self) -> bool:
        return self._connected

    def set_shadow_callback(self, callback: Callable[[dict], None]) -> None:
        """Register a callback for shadow state updates.

        The callback receives the reported state dict and is always
        invoked on the event loop passed to the constructor.
        """
        self._shadow_callback = callback

    def connect(self, credentials: dict) -> None:
        """Connect to AWS IoT and subscribe to shadow topics.

        If already connected, disconnects first (for credential refresh).
        This is a blocking call (waits for connection + subscriptions).
        """
        if self._connection is not None:
            self.disconnect()

        self._connection = self._build_connection(credentials)
        self._connection.connect().result(timeout=_CONNECT_TIMEOUT)
        self._connected = True
        _LOGGER.info("MQTT connected to %s for device %s", self._endpoint, self._serial)

        self._subscribe_shadow_topics()
        self._request_shadow()

    def disconnect(self) -> None:
        """Disconnect from AWS IoT."""
        if self._connection is not None:
            try:
                self._connection.disconnect().result(timeout=_DISCONNECT_TIMEOUT)
            except Exception:
                _LOGGER.debug("MQTT disconnect error (ignored)", exc_info=True)
            self._connection = None
        self._connected = False

    def publish_desired(self, desired: dict) -> None:
        """Publish a desired state change to the device shadow.

        Raises ConnectionError if not connected.
        """
        if not self._connected or self._connection is None:
            raise ConnectionError("MQTT not connected")

        topic = _SHADOW_UPDATE.format(serial=self._serial)
        payload = json.dumps({"state": {"desired": desired}})
        self._connection.publish(
            topic=topic,
            payload=payload,
            qos=mqtt.QoS.AT_LEAST_ONCE,
        )
        _LOGGER.debug("Published desired state to %s", topic)

    def _build_connection(self, credentials: dict) -> mqtt.Connection:
        """Build an MQTT connection with SigV4 WebSocket auth."""
        credentials_provider = auth.AwsCredentialsProvider.new_static(
            access_key_id=credentials["AccessKeyId"],
            secret_access_key=credentials["SecretKey"],
            session_token=credentials["SessionToken"],
        )

        return mqtt_connection_builder.websockets_with_default_aws_signing(
            endpoint=self._endpoint,
            region=self._region,
            credentials_provider=credentials_provider,
            client_bootstrap=self._client_bootstrap,
            client_id=f"exo_pool_{self._serial}_{int(time.time())}",
            clean_session=True,
            keep_alive_secs=30,
            on_connection_interrupted=self._on_connection_interrupted,
            on_connection_resumed=self._on_connection_resumed,
        )

    def _subscribe_shadow_topics(self) -> None:
        """Subscribe to all shadow topics with the appropriate callbacks."""
        for topic_template in _SUBSCRIBE_TOPICS:
            topic = topic_template.format(serial=self._serial)
            try:
                future, _ = self._connection.subscribe(
                    topic=topic,
                    qos=mqtt.QoS.AT_LEAST_ONCE,
                    callback=self._on_shadow_message,
                )
                future.result(timeout=_SUBSCRIBE_TIMEOUT)
                _LOGGER.debug("Subscribed to %s", topic)
            except Exception:
                _LOGGER.warning("Failed to subscribe to %s", topic, exc_info=True)
            time.sleep(_SUBSCRIBE_DELAY)

    def _request_shadow(self) -> None:
        """Publish to shadow/get to request the current shadow state."""
        topic = _SHADOW_GET.format(serial=self._serial)
        self._connection.publish(
            topic=topic,
            payload="{}",
            qos=mqtt.QoS.AT_LEAST_ONCE,
        )
        _LOGGER.debug("Requested shadow state via %s", topic)

    def _on_shadow_message(self, topic, payload, dup, qos, retain, **kwargs):
        """Handle incoming shadow messages (runs on CRT thread)."""
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            _LOGGER.warning("Malformed shadow payload on %s", topic)
            return

        reported = self._extract_reported(topic, data)
        if reported is None:
            return

        if self._shadow_callback is not None:
            self._loop.call_soon_threadsafe(self._shadow_callback, reported)

    def _extract_reported(self, topic: str, data: dict) -> dict | None:
        """Extract the reported state dict from a shadow message."""
        if "update/documents" in topic:
            return data.get("current", {}).get("state", {}).get("reported")
        if "get/accepted" in topic:
            return data.get("state", {}).get("reported")
        # update/accepted and update/delta don't carry the full reported state
        return None

    def _on_connection_interrupted(self, connection, error, **kwargs):
        """Called by CRT when the connection drops."""
        self._connected = False
        _LOGGER.warning("MQTT connection interrupted: %s", error)

    def _on_connection_resumed(self, connection, return_code, session_present, **kwargs):
        """Called by CRT when the connection is re-established."""
        _LOGGER.info(
            "MQTT connection resumed (rc=%s, session_present=%s)",
            return_code,
            session_present,
        )
        self._connected = True
        # Re-subscribe since we use clean_session=True
        self._subscribe_shadow_topics()
        self._request_shadow()
