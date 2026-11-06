"""Shared fakes and fixtures. Nothing here touches hardware or the network."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from studyspace_node.config import (
    AppConfig,
    MqttConfig,
    NodeConfig,
    SeatConfig,
    SensorConfig,
    WeightSensorConfig,
)
from studyspace_node.node import Node, Seat
from studyspace_node.occupancy import OccupancyDetector, OccupancySettings
from studyspace_node.publisher import Publisher
from studyspace_node.sensors import FakeDistanceSensor, FakeMotionSensor, FakeWeightSensor

REPO_ROOT = Path(__file__).resolve().parent.parent

FIXED_TIME = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)


class FakeMqttClient:
    """Records publishes. Set ``connected`` to simulate the link state."""

    def __init__(self, connected: bool = True) -> None:
        self.connected = connected
        self.published: list[dict] = []
        self.publish_error: Exception | None = None
        self.result = None

    def is_connected(self) -> bool:
        return self.connected

    def publish(self, topic, payload, qos=0, retain=False):
        if self.publish_error is not None:
            raise self.publish_error
        self.published.append({"topic": topic, "payload": payload, "qos": qos, "retain": retain})
        return self.result

    @property
    def payloads(self) -> list:
        return [m["payload"] for m in self.published]


class FakeClock:
    """Injected time source; tests advance it explicitly instead of sleeping."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def client() -> FakeMqttClient:
    return FakeMqttClient()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings() -> OccupancySettings:
    return OccupancySettings()


@pytest.fixture
def detector(settings: OccupancySettings) -> OccupancyDetector:
    return OccupancyDetector(settings)


def seat_config(space_id: str, data_pin: int, clock_pin: int) -> SeatConfig:
    return SeatConfig(
        space_id=space_id,
        label=space_id[-1].upper(),
        weight=WeightSensorConfig(data_pin, clock_pin, 0.0, 1000.0),
    )


@pytest.fixture
def app_config() -> AppConfig:
    return AppConfig(
        node=NodeConfig(id="node-07", desk_id="desk-07", building="library", room="floor2"),
        mqtt=MqttConfig(host="broker.test", heartbeat_s=60),
        seats=(
            seat_config("desk-07-a", 5, 6),
            seat_config("desk-07-b", 13, 19),
            seat_config("desk-07-c", 20, 21),
        ),
        sensors=SensorConfig(poll_interval_s=1.0),
    )


class NodeRig:
    """A Node with one fully-sensored seat (``rig.motion`` etc) plus any extra seats."""

    def __init__(self, config, client, clock, extra_seats=()) -> None:
        self.config = config
        self.client = client
        self.clock = clock
        self.motion = FakeMotionSensor(default=False)
        self.distance = FakeDistanceSensor(default=2.0)
        self.weight = FakeWeightSensor(default=0.0)
        self.detector = OccupancyDetector(config.occupancy)
        self.seat = Seat(
            space_id="desk-07-a",
            label="A",
            detector=self.detector,
            motion=self.motion,
            distance=self.distance,
            weight=self.weight,
        )
        self.publisher = Publisher(client)
        self.node = Node(
            [self.seat, *extra_seats],
            self.publisher,
            config,
            clock=clock,
            wall_clock=lambda: FIXED_TIME,
        )

    def ticks(self, count: int, step: float = 1.0) -> None:
        for _ in range(count):
            self.clock.advance(step)
            self.node.tick()


@pytest.fixture
def rig(app_config: AppConfig, client: FakeMqttClient, clock: FakeClock) -> NodeRig:
    return NodeRig(app_config, client, clock)
