"""Occupancy decision logic for one seat: pure, no I/O, time is always injected."""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass
from enum import StrEnum


class OccupancyState(StrEnum):
    UNKNOWN = "unknown"
    FREE = "free"
    OCCUPIED = "occupied"


class OccupancySource(StrEnum):
    """What the current state is based on."""

    SENSOR = "sensor"
    MANUAL = "manual"


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class OccupancySettings:
    presence_distance_m: float = 0.8
    confirm_readings: int = 3
    vacancy_timeout_s: float = 300
    distance_window: int = 5
    min_valid_distance_m: float = 0.02
    max_valid_distance_m: float = 4.0
    max_consecutive_faults: int = 5
    presence_weight_kg: float = 20.0
    weight_window: int = 5
    min_valid_weight_kg: float = -10.0
    max_valid_weight_kg: float = 300.0
    manual_hold_s: float = 1800

    def __post_init__(self) -> None:
        for name in (
            "presence_distance_m",
            "vacancy_timeout_s",
            "min_valid_distance_m",
            "max_valid_distance_m",
            "presence_weight_kg",
            "max_valid_weight_kg",
            "manual_hold_s",
        ):
            value = getattr(self, name)
            if not _is_number(value) or value <= 0:
                raise ValueError(f"{name} must be a positive number, got {value!r}")
        if not _is_number(self.min_valid_weight_kg):
            raise ValueError(f"min_valid_weight_kg must be a number, got {self.min_valid_weight_kg!r}")
        for name in ("confirm_readings", "distance_window", "weight_window", "max_consecutive_faults"):
            value = getattr(self, name)
            if not _is_int(value) or value < 1:
                raise ValueError(f"{name} must be an integer >= 1, got {value!r}")
        if self.min_valid_distance_m >= self.max_valid_distance_m:
            raise ValueError(
                "min_valid_distance_m must be less than max_valid_distance_m, got "
                f"{self.min_valid_distance_m!r} and {self.max_valid_distance_m!r}"
            )
        if not self.min_valid_distance_m < self.presence_distance_m <= self.max_valid_distance_m:
            raise ValueError(
                "presence_distance_m must be above min_valid_distance_m and at most "
                f"max_valid_distance_m, got {self.presence_distance_m!r}"
            )
        if not self.min_valid_weight_kg < self.presence_weight_kg <= self.max_valid_weight_kg:
            raise ValueError(
                "presence_weight_kg must be above min_valid_weight_kg and at most "
                f"max_valid_weight_kg, got {self.presence_weight_kg!r}"
            )


class _FilteredChannel:
    """Median filter over the last N valid readings of one analogue sensor.

    A brief dropout keeps the window, so one glitch doesn't break a debounce run.
    After ``window`` consecutive invalid readings it is cleared; otherwise a dead
    sensor whose last readings meant "present" would hold the seat forever.
    """

    def __init__(self, window: int, min_valid: float, max_valid: float) -> None:
        self._size = window
        self._min = min_valid
        self._max = max_valid
        self._values: deque[float] = deque(maxlen=window)
        self._invalid_run = 0

    def record(self, value: float | None) -> bool:
        """Add a reading; returns True if it was valid."""
        # The range comparison is also False for NaN.
        if value is not None and self._min <= value <= self._max:
            self._values.append(value)
            self._invalid_run = 0
            return True
        self._invalid_run += 1
        if self._invalid_run >= self._size:
            self._values.clear()
        return False

    def median(self) -> float | None:
        return statistics.median(self._values) if self._values else None

    def clear(self) -> None:
        self._values.clear()


class OccupancyDetector:
    """Fuses any combination of sensors, plus manual marks, into one seat's state.

    ``present = motion OR median distance < threshold OR median weight >= threshold``,
    so a seat works with whichever sensors it has; absent ones simply pass None.

    Manual marks:

    * ``mark_occupied`` holds the seat OCCUPIED for ``manual_hold_s`` even if it is
      empty, for someone stepping away. The hold ends early once someone is confirmed
      sitting down after the seat has been seen empty. Pressing it while seated does
      not end it immediately: the person is expected to leave and come back.
    * ``mark_vacant`` frees the seat at once, skipping the vacancy timeout and ending
      any hold. It is refused while the sensors currently detect someone, so a passer-by
      can't mark a seated person's seat as free.

    The vacancy timeout measures time since presence was last observed, including any
    period where all sensors were down. State is held during the outage, but the first
    usable absent reading after a long outage can free the seat.
    """

    def __init__(self, settings: OccupancySettings | None = None) -> None:
        self.settings = s = settings or OccupancySettings()
        self._distance = _FilteredChannel(
            s.distance_window, s.min_valid_distance_m, s.max_valid_distance_m
        )
        self._weight = _FilteredChannel(s.weight_window, s.min_valid_weight_kg, s.max_valid_weight_kg)
        self._sensor_state = OccupancyState.UNKNOWN
        self._present_run = 0
        self._absent_run = 0
        self._last_presence: float | None = None
        self._present_now = False
        self._faults = 0
        self._hold_until: float | None = None
        self._hold_seen_empty = False
        self._hold_present_run = 0

    @property
    def state(self) -> OccupancyState:
        return OccupancyState.OCCUPIED if self._hold_until is not None else self._sensor_state

    @property
    def source(self) -> OccupancySource:
        return OccupancySource.MANUAL if self._hold_until is not None else OccupancySource.SENSOR

    @property
    def consecutive_faults(self) -> int:
        return self._faults

    @property
    def healthy(self) -> bool:
        return self._faults < self.settings.max_consecutive_faults

    def hold_remaining_s(self, now: float) -> float:
        return 0.0 if self._hold_until is None else max(0.0, self._hold_until - now)

    def update(
        self,
        motion: bool | None,
        distance_m: float | None,
        now: float,
        weight_kg: float | None = None,
    ) -> OccupancyState:
        """Process one round of readings; ``None`` means no reading from that sensor."""
        if self._hold_until is not None and now >= self._hold_until:
            self._end_hold()

        distance_usable = self._distance.record(distance_m)
        weight_usable = self._weight.record(weight_kg)
        if motion is None and not distance_usable and not weight_usable:
            self._faults += 1
            self._present_now = False
            return self.state

        self._faults = 0
        present = bool(motion) or self._distance_is_near() or self._weight_is_heavy()
        self._present_now = present
        self._update_hold(present)
        if present:
            self._on_present(now)
        else:
            self._on_absent(now)
        return self.state

    def mark_occupied(self, now: float) -> None:
        self._hold_until = now + self.settings.manual_hold_s
        self._hold_seen_empty = not self._present_now
        self._hold_present_run = 0

    def mark_vacant(self) -> bool:
        """Free the seat now. Returns False, changing nothing, if someone is detected."""
        if self._present_now:
            return False
        self._end_hold()
        self._sensor_state = OccupancyState.FREE
        self._present_run = 0
        self._absent_run = 0
        self._last_presence = None
        self._distance.clear()
        self._weight.clear()
        return True

    def _distance_is_near(self) -> bool:
        median = self._distance.median()
        return median is not None and median < self.settings.presence_distance_m

    def _weight_is_heavy(self) -> bool:
        median = self._weight.median()
        return median is not None and median >= self.settings.presence_weight_kg

    def _end_hold(self) -> None:
        self._hold_until = None
        self._hold_present_run = 0

    def _update_hold(self, present: bool) -> None:
        if self._hold_until is None:
            return
        if not present:
            self._hold_seen_empty = True
            self._hold_present_run = 0
        elif self._hold_seen_empty:
            self._hold_present_run += 1
            if self._hold_present_run >= self.settings.confirm_readings:
                self._end_hold()

    def _on_present(self, now: float) -> None:
        self._last_presence = now
        self._absent_run = 0
        if self._sensor_state is OccupancyState.OCCUPIED:
            return
        self._present_run += 1
        if self._present_run >= self.settings.confirm_readings:
            self._sensor_state = OccupancyState.OCCUPIED
            self._present_run = 0

    def _on_absent(self, now: float) -> None:
        self._present_run = 0
        if self._sensor_state is OccupancyState.UNKNOWN:
            self._absent_run += 1
            if self._absent_run >= self.settings.confirm_readings:
                self._sensor_state = OccupancyState.FREE
                self._absent_run = 0
        elif self._sensor_state is OccupancyState.OCCUPIED:
            assert self._last_presence is not None  # OCCUPIED is only entered on presence
            if now - self._last_presence >= self.settings.vacancy_timeout_s:
                self._sensor_state = OccupancyState.FREE
