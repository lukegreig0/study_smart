"""Reliable-ish publishing: bounded offline buffering, plus the paho client factory."""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Protocol

from .messages import STATUS_OFFLINE, STATUS_ONLINE

if TYPE_CHECKING:
    from .config import AppConfig

log = logging.getLogger(__name__)

QOS = 1
RETAIN = True
RECONNECT_MIN_DELAY_S = 1
RECONNECT_MAX_DELAY_S = 120


class MqttClient(Protocol):
    def publish(self, topic: str, payload: str, qos: int, retain: bool) -> Any: ...

    def is_connected(self) -> bool: ...


class Publisher:
    """Publishes retained QoS 1 messages, buffering the latest per topic while offline.

    Thread-safe: ``publish`` runs on the node loop while ``flush`` is also called
    from paho's network thread on connect.
    """

    def __init__(self, client: MqttClient, max_buffer: int = 100) -> None:
        if max_buffer < 1:
            raise ValueError(f"max_buffer must be >= 1, got {max_buffer}")
        self._client = client
        self._max_buffer = max_buffer
        self._pending: OrderedDict[str, str] = OrderedDict()  # topic -> payload, oldest first
        self._lock = threading.Lock()

    def is_connected(self) -> bool:
        return bool(self._client.is_connected())

    @property
    def pending(self) -> list[tuple[str, str]]:
        """Buffered (topic, payload) pairs, oldest first."""
        with self._lock:
            return list(self._pending.items())

    def publish(self, topic: str, payload: str) -> bool:
        """Send, or buffer if that's not possible. Returns True if sent now.

        Going through the buffer even when connected keeps ordering correct and
        stops a superseded message for this topic from being sent first.
        """
        with self._lock:
            self._pending.pop(topic, None)  # a re-buffered topic moves to newest
            self._pending[topic] = payload
            if len(self._pending) > self._max_buffer:
                dropped, _ = self._pending.popitem(last=False)
                log.warning("Publish buffer full; dropped oldest message for %s", dropped)
            self._flush_locked()
            return topic not in self._pending

    def flush(self) -> int:
        """Send buffered messages in order; returns how many were sent."""
        with self._lock:
            return self._flush_locked()

    def _flush_locked(self) -> int:
        sent = 0
        while self._pending and self._client.is_connected():
            topic, payload = next(iter(self._pending.items()))
            if not self._send(topic, payload):
                break  # keep this and later messages, in order, for the next flush
            del self._pending[topic]
            sent += 1
        return sent

    def _send(self, topic: str, payload: str) -> bool:
        try:
            result = self._client.publish(topic, payload, qos=QOS, retain=RETAIN)
        except Exception:
            log.warning("Publish to %s failed; will retry", topic, exc_info=True)
            return False
        # paho reports errors through MQTTMessageInfo.rc rather than raising.
        rc = getattr(result, "rc", 0)
        if rc != 0:
            log.warning("Publish to %s failed with rc=%s; will retry", topic, rc)
            return False
        return True


def create_mqtt_client(config: AppConfig) -> Any:
    """Build a configured but unconnected paho client.

    To have buffered messages flushed on connect, register the publisher as the
    client's userdata before connecting::

        client = create_mqtt_client(config)
        publisher = Publisher(client)
        client.user_data_set(publisher)
        client.connect_async(config.mqtt.host, config.mqtt.port)  # tolerates a broker that's down
        client.loop_start()                                       # reconnects with backoff
    """
    import paho.mqtt.client as mqtt

    status_topic = config.status_topic
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=config.client_id)
    if config.mqtt.username is not None:
        client.username_pw_set(config.mqtt.username, config.mqtt.password)
    if config.mqtt.tls:
        client.tls_set()
    client.will_set(status_topic, STATUS_OFFLINE, qos=QOS, retain=RETAIN)
    client.reconnect_delay_set(RECONNECT_MIN_DELAY_S, RECONNECT_MAX_DELAY_S)

    def on_connect(client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        if reason_code.is_failure:
            log.error("MQTT connection refused: %s", reason_code)
            return
        log.info("Connected to MQTT broker")
        client.publish(status_topic, STATUS_ONLINE, qos=QOS, retain=RETAIN)
        if userdata is not None:
            userdata.flush()

    def on_disconnect(client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        level = logging.WARNING if reason_code.is_failure else logging.INFO
        log.log(level, "Disconnected from MQTT broker: %s", reason_code)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    return client


def publish_offline(client: Any, status_topic: str, timeout_s: float = 2.0) -> None:
    """Best-effort clean-shutdown status. If not delivered, the Last Will covers it."""
    if not client.is_connected():
        log.info("Not connected at shutdown; relying on Last Will for offline status")
        return
    try:
        info = client.publish(status_topic, STATUS_OFFLINE, qos=QOS, retain=RETAIN)
        info.wait_for_publish(timeout=timeout_s)
    except (RuntimeError, ValueError) as exc:  # raised by wait_for_publish on send errors
        log.warning("Could not publish offline status: %s", exc)
