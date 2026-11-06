"""Sensor interfaces, gpiozero-backed implementations, and scripted fakes."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Generic, Protocol, TypeVar

T = TypeVar("T")


class SensorReadError(RuntimeError):
    """The sensor could not produce a trustworthy reading."""


class MotionSensor(Protocol):
    def read(self) -> bool: ...


class DistanceSensor(Protocol):
    def read(self) -> float:
        """Return the distance in metres."""
        ...


class WeightSensor(Protocol):
    def read(self) -> float:
        """Return the weight on the seat in kilograms."""
        ...


class PirMotionSensor:
    """PIR sensor on a GPIO pin (BCM numbering)."""

    def __init__(self, pin: int) -> None:
        from gpiozero import MotionSensor as GpioMotionSensor

        self._device = GpioMotionSensor(pin)

    def read(self) -> bool:
        return bool(self._device.motion_detected)

    def close(self) -> None:
        self._device.close()


class UltrasonicDistanceSensor:
    """HC-SR04 on two GPIO pins (BCM numbering).

    gpiozero samples in a background thread and needs some care:

    * ``partial=True``: the default blocks reads until the sample queue fills,
      which is forever if the sensor is disconnected at startup. With no samples
      ``distance`` is 0.0, which the detector rejects as out of range.
    * No-echo samples are discarded, so a sensor that dies mid-run would keep
      reporting its last value. ``_read`` is wrapped to record the last real
      echo and ``read()`` raises once that is older than ``stale_after_s``.
    * ``max_distance`` defaults to 1 m and clamps readings to it, so pass the
      real maximum. "Nothing in range" then reads as exactly ``max_distance_m``.
    """

    def __init__(
        self,
        trigger_pin: int,
        echo_pin: int,
        max_distance_m: float = 4.0,
        stale_after_s: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        from gpiozero import DistanceSensor as GpioDistanceSensor

        class _TrackedDistanceSensor(GpioDistanceSensor):
            # Must exist on the class: gpiozero rejects new instance attributes.
            last_echo_at: float | None = None

            def _read(self):  # runs on gpiozero's sampling thread
                value = super()._read()
                if value is not None:
                    self.last_echo_at = clock()
                return value

        self._clock = clock
        self._stale_after_s = stale_after_s
        self._started_at = clock()
        self._device = _TrackedDistanceSensor(
            echo=echo_pin, trigger=trigger_pin, max_distance=max_distance_m, partial=True
        )

    def read(self) -> float:
        last_echo = self._device.last_echo_at
        since = self._started_at if last_echo is None else last_echo
        age = self._clock() - since
        if age > self._stale_after_s:
            raise SensorReadError(f"no ultrasonic echo for {age:.1f}s")
        return float(self._device.distance)

    def close(self) -> None:
        self._device.close()


class Hx711WeightSensor:
    """Load cell through an HX711 amplifier, bit-banged on two GPIO pins (BCM numbering).

    * The data line uses a pull-up, so an unplugged HX711 floats high and reads as
      "never ready" (a SensorReadError) instead of as a plausible weight.
    * ``pin.state`` is the raw level: gpiozero's ``value`` is inverted by the pull-up.
    * Python can't guarantee the HX711's clock timing. If the OS preempts us with the
      clock high for over 60 us, that one reading is corrupt; the detector's median
      filter is what rejects it.
    * Readings are at most one conversion old (10 samples/s), so a read waits up to
      about 100 ms, which is why it happens on the poll loop and only once per tick.
    """

    _BITS = 24
    _GAIN_128_PULSES = 1  # extra clock pulses selecting channel A, gain 128
    _RAIL_HIGH = 0x7FFFFF
    _RAIL_LOW = -0x800000

    def __init__(
        self,
        data_pin: int,
        clock_pin: int,
        tare_offset: float = 0.0,
        scale_per_kg: float = 1.0,
        ready_timeout_s: float = 0.25,
        clock: Callable[[], float] = time.monotonic,
        wait: Callable[[float], None] = time.sleep,
    ) -> None:
        from gpiozero import DigitalInputDevice, DigitalOutputDevice

        self._tare_offset = tare_offset
        self._scale_per_kg = scale_per_kg
        self._ready_timeout_s = ready_timeout_s
        self._clock = clock
        self._wait = wait
        self._data = DigitalInputDevice(data_pin, pull_up=True)
        try:
            self._sck = DigitalOutputDevice(clock_pin, initial_value=False)
        except Exception:
            self._data.close()
            raise

    def read_raw(self) -> int:
        """Return one signed 24-bit conversion."""
        deadline = self._clock() + self._ready_timeout_s
        while self._data.pin.state:
            if self._clock() >= deadline:
                raise SensorReadError("HX711 not ready (disconnected?)")
            self._wait(0.005)

        raw = 0
        for _ in range(self._BITS):
            self._sck.on()
            self._sck.off()
            raw = (raw << 1) | int(self._data.pin.state)
        for _ in range(self._GAIN_128_PULSES):
            self._sck.on()
            self._sck.off()

        if raw & 0x800000:
            raw -= 0x1000000
        if raw in (self._RAIL_HIGH, self._RAIL_LOW):
            raise SensorReadError(f"HX711 reading at its limit ({raw}); check wiring")
        return raw

    def read(self) -> float:
        return (self.read_raw() - self._tare_offset) / self._scale_per_kg

    def close(self) -> None:
        self._sck.close()
        self._data.close()


class _ScriptedSensor(Generic[T]):
    """Returns scripted values in order; exceptions in the script are raised.

    When the script runs out it wraps around if ``loop`` is set, otherwise it
    repeats the last entry (or ``default`` if the script was empty).
    """

    def __init__(self, script: Iterable[T | BaseException | type[BaseException]], loop: bool, default: T):
        self._script = list(script)
        self._loop = loop
        self._default = default
        self._index = 0

    def extend(self, script: Iterable[T | BaseException | type[BaseException]]) -> None:
        self._script.extend(script)

    def read(self) -> T:
        if not self._script:
            return self._default
        if self._index >= len(self._script):
            self._index = 0 if self._loop else len(self._script) - 1
        item = self._script[self._index]
        self._index += 1
        if isinstance(item, BaseException) or (
            isinstance(item, type) and issubclass(item, BaseException)
        ):
            raise item
        return item  # type: ignore[return-value]

    def close(self) -> None:
        pass


class FakeMotionSensor(_ScriptedSensor[bool]):
    def __init__(self, script: Iterable = (), loop: bool = False, default: bool = False) -> None:
        super().__init__(script, loop, default)


class FakeDistanceSensor(_ScriptedSensor[float]):
    def __init__(self, script: Iterable = (), loop: bool = False, default: float = 2.0) -> None:
        super().__init__(script, loop, default)


class FakeWeightSensor(_ScriptedSensor[float]):
    def __init__(self, script: Iterable = (), loop: bool = False, default: float = 0.0) -> None:
        super().__init__(script, loop, default)
