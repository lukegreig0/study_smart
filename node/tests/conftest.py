"""Shared test helpers. No broker and no sleeping: time is a number we set."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make `import node` work when pytest is run from node/ (or anywhere).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from node.config import Config  # noqa: E402

TS = "2026-11-10T12:00:00Z"
TOPIC_7 = "studyspace/library/floor2/desk-07/placemat-0007/status"
TOPIC_9 = "studyspace/library/floor2/desk-09/placemat-0009/status"


def birth(device_id="placemat-0007", occupied=False, timestamp=TS, **overrides) -> dict:
    msg = {
        "type": "birth",
        "schema": 1,
        "id": device_id,
        "space_id": "desk-07",
        "device_type": "placemat",
        "firmware": "0.1.0",
        "occupied": occupied,
        "timestamp": timestamp,
    }
    msg.update(overrides)
    return msg


def heartbeat(device_id="placemat-0007", timestamp=TS, **extra) -> dict:
    return {"type": "heartbeat", "id": device_id, "timestamp": timestamp, **extra}


def death(device_id="placemat-0007") -> dict:
    return {"type": "death", "id": device_id}


def encode(msg: dict) -> bytes:
    return json.dumps(msg).encode()


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeInfo:
    def __init__(self, rc: int = 0) -> None:
        self.rc = rc
        self.waited = False

    def wait_for_publish(self, timeout=None) -> None:
        self.waited = True


class FakeReasonCode:
    def __init__(self, failure: bool = False) -> None:
        self.is_failure = failure

    def __str__(self) -> str:
        return "Failure" if self.is_failure else "Success"


class FakeMessage:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class FakeClient:
    """Stands in for paho's Client. Records every call in order."""

    def __init__(self, connect_failures: int = 0) -> None:
        self.calls: list[str] = []
        self.published: list[dict] = []
        self.will: dict | None = None
        self.subscriptions: list[tuple[str, int]] = []
        self.connect_failures = connect_failures
        self.on_connect = self.on_disconnect = self.on_message = None

    def will_set(self, topic, payload, qos=0, retain=False):
        self.calls.append("will_set")
        self.will = {"topic": topic, "payload": json.loads(payload), "qos": qos, "retain": retain}

    def reconnect_delay_set(self, min_delay=1, max_delay=120):
        self.calls.append("reconnect_delay_set")

    def connect(self, host, port, keepalive=60):
        self.calls.append("connect")
        if self.connect_failures > 0:
            self.connect_failures -= 1
            raise ConnectionRefusedError("refused")

    def subscribe(self, topic, qos=0):
        self.calls.append("subscribe")
        self.subscriptions.append((topic, qos))

    def publish(self, topic, payload, qos=0, retain=False):
        self.calls.append("publish")
        info = FakeInfo()
        self.published.append(
            {"topic": topic, "payload": json.loads(payload), "qos": qos,
             "retain": retain, "info": info}
        )
        return info

    def disconnect(self):
        self.calls.append("disconnect")

    def loop_start(self):
        self.calls.append("loop_start")

    def loop_stop(self):
        self.calls.append("loop_stop")

    # Helpers to play the broker/network thread.
    def fire_connect(self, failure: bool = False):
        self.on_connect(self, None, {}, FakeReasonCode(failure), None)

    def fire_disconnect(self):
        self.on_disconnect(self, None, {}, FakeReasonCode(True), None)

    def deliver(self, topic: str, payload: bytes):
        self.on_message(self, None, FakeMessage(topic, payload))


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
