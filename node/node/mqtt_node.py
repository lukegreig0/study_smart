"""MQTT wiring: connects the pure registry to the broker.

Threading model (the important bit)
-----------------------------------
paho's loop_start() runs the network in a background thread, and paho calls
on_connect / on_message / on_disconnect *from that thread*. If those callbacks
touched the registry while the main thread was also using it (timeout
checks) we would need locks.

Instead the callbacks do nothing but put an item on a queue.Queue (which is
thread-safe on its own). The main thread is the only one that ever reads or
changes the registry or decides what to publish. One owner, no shared state,
no locks. client.publish() itself is safe to call from the main thread.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable

import paho.mqtt.client as mqtt

from . import messages
from .config import Config
from .registry import Registry

log = logging.getLogger(__name__)

QOS = 1

# Markers the network thread puts on the queue next to (topic, payload)
# items, so the main thread learns about connection changes in order with the
# messages.
_CONNECTED = object()
_DISCONNECTED = object()

DEATH_PUBLISH_TIMEOUT_S = 5.0


class MqttNode:
    def __init__(
        self,
        config: Config,
        client: mqtt.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], str] = messages.now_utc,
    ) -> None:
        self.config = config
        self.registry = Registry(config.device_timeout_s)
        self._clock = clock
        self._utc_now = utc_now
        self._queue: queue.Queue = queue.Queue()
        self._status_topic = messages.status_topic(config.site, config.node_id)
        self._data_topic = messages.data_topic(config.site, config.node_id)

        # Only the main thread reads or writes these.
        self._connected = False

        if client is None:
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2, client_id=config.node_id
            )
        self._client = client
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message

        # The Last Will must be registered before connect(): it is sent to the
        # broker inside the CONNECT packet. If we vanish without a clean
        # disconnect (crash, power cut, network gone) the broker publishes it
        # for us, so the host learns this node is dead.
        client.will_set(
            self._status_topic,
            messages.node_death_payload(config.node_id),
            qos=QOS,
            retain=True,
        )
        # After an unexpected drop, paho's network thread reconnects on its
        # own, waiting 1 s, 2 s, 4 s ... up to 30 s between attempts.
        client.reconnect_delay_set(min_delay=1, max_delay=30)

    # ------------------------------------------------- network-thread callbacks
    # Keep these tiny: queue the event and return. See the module docstring.

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            log.error("broker refused connection: %s", reason_code)
            return
        log.info("connected to %s:%s", self.config.broker_host, self.config.broker_port)
        # Subscribing here (not once after connect()) means the subscription is
        # renewed on every reconnect. With a clean session the broker forgets
        # our subscriptions when we drop.
        client.subscribe(self.config.device_sub, qos=QOS)
        self._queue.put((_CONNECTED, None))

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            log.warning("disconnected unexpectedly (%s); paho will reconnect", reason_code)
        self._queue.put((_DISCONNECTED, None))

    def _on_message(self, client, userdata, msg) -> None:
        self._queue.put((msg.topic, msg.payload))

    # ------------------------------------------------------ main-thread work

    def connect_with_retry(self, stop: threading.Event) -> bool:
        """Do the first connect, retrying while the broker isn't up yet
        (common with docker compose, where containers start together).

        Returns False if `stop` was set before we managed to connect.
        """
        delay = 1.0
        while not stop.is_set():
            try:
                self._client.connect(
                    self.config.broker_host,
                    self.config.broker_port,
                    keepalive=self.config.keepalive_s,
                )
                return True
            except OSError as exc:  # ConnectionRefusedError, DNS failure, ...
                log.warning(
                    "cannot reach broker %s:%s (%s); retrying in %.0fs",
                    self.config.broker_host, self.config.broker_port, exc, delay,
                )
                # Event.wait instead of time.sleep so Ctrl+C / SIGTERM is
                # handled straight away instead of after the delay.
                stop.wait(delay)
                delay = min(delay * 2, 10.0)
        return False

    def run(self, stop: threading.Event) -> None:
        """Connect, process until `stop` is set, then shut down cleanly."""
        if not self.connect_with_retry(stop):
            return
        self._client.loop_start()
        try:
            while not stop.is_set():
                self.run_once()
        finally:
            self.shutdown()

    def run_once(self, wait_s: float = 0.2) -> None:
        """One pass of the main loop.

        The short queue timeout means we come back regularly to check
        timeouts even when no messages arrive, and to notice that `stop` was
        set.

        Data is only published when something changed (see
        _publish_changes), so there is no periodic refresh here.
        """
        try:
            item = self._queue.get(timeout=wait_s) if wait_s > 0 else self._queue.get_nowait()
        except queue.Empty:
            item = None

        now = self._clock()
        if item is not None:
            self._handle_item(*item, now=now)

        went_offline = self.registry.check_timeouts(now)
        if went_offline:
            self._publish_changes(went_offline)

    def _handle_item(self, topic, payload, now: float) -> None:
        if topic is _CONNECTED:
            self._connected = True
            # Fresh session: announce ourselves, then the full picture, so a
            # host listening now gets every device's state after an outage.
            self._publish_birth()
            self._publish_full_snapshot()
            return
        if topic is _DISCONNECTED:
            self._connected = False
            return

        try:
            msg = messages.parse(topic, payload)
        except messages.ParseError as exc:
            log.warning("rejected message on %s: %s", topic, exc)
            return

        events = self.registry.handle(msg, now)
        # Heartbeats themselves are never forwarded; we only publish if one
        # changed something (occupancy flip, device back online).
        if events.rebirth:
            self._publish_birth()
        if events.changed:
            self._publish_changes(events.changed)

    def shutdown(self) -> None:
        """Clean teardown. Order matters:

        1. Publish our death and wait until it's actually sent. A clean
           disconnect does NOT trigger the Last Will, so without this the host
           would keep showing us as alive.
        2. disconnect() sends DISCONNECT, which tells the broker to discard
           the will (we already said goodbye ourselves).
        3. loop_stop() ends the network thread. It has to come last: the
           thread is what actually sends steps 1 and 2.
        """
        log.info("shutting down")
        try:
            info = self._client.publish(
                self._status_topic,
                messages.node_death_payload(self.config.node_id),
                qos=QOS,
                retain=True,
            )
            info.wait_for_publish(timeout=DEATH_PUBLISH_TIMEOUT_S)
        except (RuntimeError, ValueError) as exc:
            # e.g. the broker is down right now. The will covers this case.
            log.warning("could not publish node death: %s", exc)
        self._client.disconnect()
        self._client.loop_stop()

    # -------------------------------------------------------------- publishing

    def _publish(self, topic: str, payload: str) -> None:
        if not self._connected:
            # Don't queue stale state while offline: after reconnecting we
            # publish a fresh birth and data anyway.
            log.debug("not connected; skipping publish to %s", topic)
            return
        # Retained so a host that (re)starts gets the current picture at once.
        info = self._client.publish(topic, payload, qos=QOS, retain=True)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            log.warning("publish to %s failed: %s", topic, mqtt.error_string(info.rc))
        log.debug("published %s %s", topic, payload)

    def _publish_birth(self) -> None:
        payload = messages.node_birth_payload(
            self.config.node_id, self._utc_now(), self.registry.birth_devices()
        )
        self._publish(self._status_topic, payload)

    def _publish_changes(self, changed: list[str]) -> None:
        """Publish a delta: only the devices in `changed`, nobody else."""
        payload = messages.data_payload(
            self.config.node_id, self._utc_now(), changed,
            self.registry.snapshot(changed),
        )
        self._publish(self._data_topic, payload)

    def _publish_full_snapshot(self) -> None:
        """Publish every known device. Only done after (re)connecting.

        "changed": [] marks it as a full snapshot rather than a delta.
        """
        payload = messages.data_payload(
            self.config.node_id, self._utc_now(), [], self.registry.snapshot()
        )
        self._publish(self._data_topic, payload)
