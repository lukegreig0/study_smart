"""MQTT wiring: connects the broker to ingest + store.

Threading model (the important bit)
-----------------------------------
paho's loop_start() runs the network in a background thread, and paho calls
on_connect / on_message / on_disconnect *from that thread*.

A sqlite3 connection must not be shared between threads: Python refuses by
default (check_same_thread), and even when allowed, two threads issuing
statements on one connection can interleave inside each other's transactions.
So on_message does nothing but put (topic, payload) on a queue.Queue, which
is thread-safe on its own. The main thread is the only one that parses,
applies the rules and touches the database. One owner, no locks.
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
from .ingest import Ingest
from .store import Store

log = logging.getLogger(__name__)

QOS = 1
# Status first: on (re)subscribe the broker replays retained messages per
# subscription, so a node's birth/death is normally processed before its data.
SUBSCRIPTIONS = ("studyspace/v1/+/+/status", "studyspace/v1/+/+/data")


class MqttHost:
    def __init__(
        self,
        config: Config,
        store: Store,
        client: mqtt.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], str] = messages.now_utc,
    ) -> None:
        self.config = config
        self.store = store
        self.ingest = Ingest(store, config.node_timeout_s)
        self._clock = clock
        self._utc_now = utc_now
        self._queue: queue.Queue[tuple[str, bytes]] = queue.Queue()

        if client is None:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=config.client_id)
        self._client = client
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        # After an unexpected drop, paho's network thread reconnects on its
        # own, waiting 1 s, 2 s, 4 s ... up to 30 s between attempts.
        client.reconnect_delay_set(min_delay=1, max_delay=30)

    # ------------------------------------------------- network-thread callbacks
    # Keep these tiny. They run on paho's thread: no database access here.

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            log.error("broker refused connection: %s", reason_code)
            return
        log.info("connected to %s:%s", self.config.broker_host, self.config.broker_port)
        # Subscribing here (not once after connect()) renews the subscriptions
        # on every reconnect: with a clean session the broker forgets them
        # when we drop. Each subscribe also replays the retained messages,
        # which ingest handles idempotently.
        for topic in SUBSCRIPTIONS:
            client.subscribe(topic, qos=QOS)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            log.warning("disconnected unexpectedly (%s); paho will reconnect", reason_code)

    def _on_message(self, client, userdata, msg) -> None:
        self._queue.put((msg.topic, msg.payload))

    # ------------------------------------------------------ main-thread work

    def connect_with_retry(self, stop: threading.Event) -> bool:
        """First connect, retrying while the broker isn't up yet (common with
        docker compose, where containers start together). Backoff 1 s, 2 s,
        4 s ... capped at 10 s. Returns False if `stop` was set first."""
        delay = 1.0
        while not stop.is_set():
            try:
                self._client.connect(
                    self.config.broker_host, self.config.broker_port,
                    keepalive=self.config.keepalive_s,
                )
                return True
            except OSError as exc:  # ConnectionRefusedError, DNS failure, ...
                log.warning("cannot reach broker %s:%s (%s); retrying in %.0fs",
                            self.config.broker_host, self.config.broker_port, exc, delay)
                # Event.wait rather than time.sleep: a signal ends it at once.
                stop.wait(delay)
                delay = min(delay * 2, 10.0)
        return False

    def run(self, stop: threading.Event) -> None:
        """Connect, process until `stop` is set, then shut down cleanly."""
        try:
            if not self.connect_with_retry(stop):
                return
            self._client.loop_start()
            while not stop.is_set():
                self.run_once()
        finally:
            self.shutdown()

    def run_once(self, wait_s: float = 0.5) -> None:
        """One pass of the main loop: handle at most one message, then check
        node timeouts. The short timeout brings us back regularly even when
        nothing arrives, to run the timeout check and notice `stop`."""
        try:
            item = self._queue.get(timeout=wait_s) if wait_s > 0 else self._queue.get_nowait()
        except queue.Empty:
            item = None

        if item is not None:
            self.handle(*item)
        self.ingest.check_node_timeouts(self._utc_now(), self._clock())

    def handle(self, topic: str, payload: bytes) -> None:
        msg = messages.parse(topic, payload)
        if msg is None:
            return  # already logged by parse()
        try:
            self.ingest.apply(msg, self._utc_now(), self._clock())
        except Exception:
            # A bug or database error on one message mustn't kill the host.
            # The transaction was rolled back, so the db is still consistent.
            log.exception("failed to apply message on %s", topic)

    def shutdown(self) -> None:
        """Clean teardown, in this order:
        1. disconnect(): sends DISCONNECT (queued for the network thread).
        2. loop_stop(): the network thread sends it and exits. It must come
           after disconnect(), or nothing would be left to send it.
        3. close the database, once nothing can produce more work for it.
        """
        log.info("shutting down")
        self._client.disconnect()
        self._client.loop_stop()
        self.store.close()
