"""The node loop: read sensors, update each seat's detector, publish when required."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .messages import REASON_CHANGE, REASON_HEARTBEAT, build_payload
from .occupancy import OccupancyDetector, OccupancySource, OccupancyState
from .publisher import Publisher
from .sensors import DistanceSensor, MotionSensor, WeightSensor
from .ui_protocol import MARK_OCCUPIED, Command, result_message

if TYPE_CHECKING:
    from .config import AppConfig

log = logging.getLogger(__name__)

COMMAND_POLL_S = 0.1  # how quickly a touchscreen command is acted on between ticks
_MAX_QUEUED_COMMANDS = 32

Reply = Callable[[dict[str, Any]], None]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Seat:
    """One seat's sensors and detector. Any sensor it lacks is left as None."""

    space_id: str
    detector: OccupancyDetector
    label: str = ""
    position_deg: float | None = None
    motion: MotionSensor | None = None
    distance: DistanceSensor | None = None
    weight: WeightSensor | None = None
    _published_state: OccupancyState | None = field(default=None, repr=False)
    _published_healthy: bool | None = field(default=None, repr=False)
    _published_source: OccupancySource | None = field(default=None, repr=False)
    _published_at: float | None = field(default=None, repr=False)
    _logged_healthy: bool = field(default=True, repr=False)
    _failing: set[str] = field(default_factory=set, repr=False)

    def close(self) -> None:
        for sensor in (self.motion, self.distance, self.weight):
            if sensor is not None:
                sensor.close()


class Node:
    """Ties seats, publisher and the optional UI together.

    ``clock`` drives all timing and should be monotonic: a Pi has no RTC, so its
    wall clock can jump when NTP syncs after boot, which must not skew the vacancy
    timeout, holds or heartbeats. ``wall_clock`` is only used for message timestamps.

    Detectors are only touched on the thread calling ``tick``/``run``. UI commands
    arrive on another thread and wait in a queue until that thread picks them up.
    """

    def __init__(
        self,
        seats: Sequence[Seat],
        publisher: Publisher,
        config: AppConfig,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._seats = list(seats)
        self._seats_by_id = {seat.space_id: seat for seat in self._seats}
        self._publisher = publisher
        self._config = config
        self._clock = clock
        self._wall_clock = wall_clock
        self._commands: queue.Queue[tuple[Command, Reply]] = queue.Queue(_MAX_QUEUED_COMMANDS)
        self._ui_publish: Callable[[dict[str, Any]], None] | None = None

    def attach_ui(self, ui_publish: Callable[[dict[str, Any]], None]) -> None:
        self._ui_publish = ui_publish

    def tick(self) -> None:
        """Run one poll cycle across all seats."""
        for seat in self._seats:
            motion = self._read(seat, "motion", seat.motion)
            distance = self._read(seat, "distance", seat.distance)
            weight = self._read(seat, "weight", seat.weight)
            now = self._clock()
            seat.detector.update(motion, distance, now, weight_kg=weight)
            healthy = seat.detector.healthy
            if healthy != seat._logged_healthy:
                level = logging.INFO if healthy else logging.WARNING
                log.log(level, "%s healthy=%s", seat.space_id, healthy)
                seat._logged_healthy = healthy
            self._publish_if_needed(seat, now)
        self._publish_snapshot()

    def run(self, stop_event: threading.Event) -> None:
        """Tick every ``poll_interval_s`` and act on UI commands until ``stop_event`` is set."""
        interval = self._config.sensors.poll_interval_s
        next_tick = self._clock()
        while not stop_event.is_set():
            try:
                self.process_commands()
                if self._clock() >= next_tick:
                    self.tick()
                    next_tick = self._clock() + interval
            except Exception:  # an unattended node must keep going
                log.exception("Unexpected error in node loop")
                next_tick = self._clock() + interval
            stop_event.wait(min(COMMAND_POLL_S, max(0.0, next_tick - self._clock())))

    def submit_command(self, command: Command, reply: Reply) -> None:
        """Queue a UI command. Thread-safe and non-blocking."""
        try:
            self._commands.put_nowait((command, reply))
        except queue.Full:
            reply(result_message(command.id, "Node is busy, please try again"))

    def process_commands(self) -> None:
        """Apply queued UI commands now, publishing any resulting change immediately."""
        handled = False
        while True:
            try:
                command, reply = self._commands.get_nowait()
            except queue.Empty:
                break
            reply(result_message(command.id, self._apply_command(command)))
            handled = True
        if handled:
            self._publish_snapshot()

    def _apply_command(self, command: Command) -> str | None:
        """Returns an error message for the GUI, or None on success."""
        seat = self._seats_by_id.get(command.space_id)
        if seat is None:
            log.warning("UI command for unknown seat %s", command.space_id)
            return f"Unknown seat {command.space_id}"
        now = self._clock()
        if command.command == MARK_OCCUPIED:
            seat.detector.mark_occupied(now)
        elif not seat.detector.mark_vacant():
            log.info("Refused mark vacant for %s: someone detected", seat.space_id)
            return "Someone is sitting here"
        log.info("%s applied to %s", command.command, seat.space_id)
        self._publish_if_needed(seat, now)
        return None

    def _publish_if_needed(self, seat: Seat, now: float) -> None:
        reason = self._publish_reason(seat, now)
        if reason is None:
            return
        detector = seat.detector
        state, healthy, source = detector.state, detector.healthy, detector.source
        if reason == REASON_CHANGE:
            log.info(
                "%s changed: %s -> %s (%s)",
                seat.space_id,
                seat._published_state or "unknown",
                state.value,
                source.value,
            )
        payload = build_payload(
            self._config.node.id,
            self._config.node.desk_id,
            seat.space_id,
            state,
            healthy,
            reason,
            source,
            self._wall_clock(),
        )
        self._publisher.publish(self._config.state_topic(seat.space_id), payload)
        seat._published_state = state
        seat._published_healthy = healthy
        seat._published_source = source
        seat._published_at = now

    def _publish_reason(self, seat: Seat, now: float) -> str | None:
        detector = seat.detector
        if detector.state is OccupancyState.UNKNOWN:
            return None
        if detector.state != seat._published_state:
            return REASON_CHANGE
        if detector.healthy != seat._published_healthy or detector.source != seat._published_source:
            return REASON_HEARTBEAT
        if now - seat._published_at >= self._config.mqtt.heartbeat_s:
            return REASON_HEARTBEAT
        return None

    def _publish_snapshot(self) -> None:
        if self._ui_publish is not None:
            self._ui_publish(self.snapshot())

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        return {
            "type": "snapshot",
            "node_id": self._config.node.id,
            "desk_id": self._config.node.desk_id,
            "mqtt_connected": self._publisher.is_connected(),
            "poll_interval_s": self._config.sensors.poll_interval_s,
            "seats": [
                {
                    "space_id": seat.space_id,
                    "label": seat.label or seat.space_id,
                    "position_deg": seat.position_deg,
                    "state": seat.detector.state.value,
                    "source": seat.detector.source.value,
                    "healthy": seat.detector.healthy,
                    "hold_remaining_s": seat.detector.hold_remaining_s(now),
                }
                for seat in self._seats
            ],
        }

    def _read(self, seat: Seat, name: str, sensor: Any) -> Any:
        """Read a sensor, returning None if it is absent or fails.

        Only transitions log at WARNING, so a dead sensor doesn't flood the journal
        on every poll.
        """
        if sensor is None:
            return None
        try:
            value = sensor.read()
        except Exception as exc:
            if name in seat._failing:
                log.debug("%s %s sensor still failing: %s", seat.space_id, name, exc)
            else:
                seat._failing.add(name)
                log.warning("%s %s sensor read failed: %s", seat.space_id, name, exc, exc_info=True)
            return None
        if name in seat._failing:
            seat._failing.discard(name)
            log.info("%s %s sensor recovered", seat.space_id, name)
        return value
