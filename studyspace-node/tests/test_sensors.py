import builtins
import sys
from types import ModuleType

import pytest

from conftest import FakeClock
from studyspace_node.sensors import (
    FakeDistanceSensor,
    FakeMotionSensor,
    FakeWeightSensor,
    Hx711WeightSensor,
    PirMotionSensor,
    SensorReadError,
    UltrasonicDistanceSensor,
)


class TestFakes:
    def test_returns_script_in_order_then_holds_last(self):
        sensor = FakeDistanceSensor([1.0, 2.0])
        assert [sensor.read() for _ in range(4)] == [1.0, 2.0, 2.0, 2.0]

    def test_loop_wraps_around(self):
        sensor = FakeMotionSensor([True, False], loop=True)
        assert [sensor.read() for _ in range(5)] == [True, False, True, False, True]

    def test_empty_script_returns_default(self):
        assert FakeMotionSensor().read() is False
        assert FakeDistanceSensor(default=1.5).read() == 1.5
        assert FakeWeightSensor().read() == 0.0

    def test_raises_scripted_exception_instance_and_class(self):
        sensor = FakeMotionSensor([OSError("boom"), ValueError, True])
        with pytest.raises(OSError, match="boom"):
            sensor.read()
        with pytest.raises(ValueError):
            sensor.read()
        assert sensor.read() is True

    def test_extend_appends_to_script(self):
        sensor = FakeMotionSensor([True])
        sensor.read()
        sensor.extend([False])
        assert sensor.read() is False

    def test_close_is_a_no_op(self):
        FakeMotionSensor().close()


class StubGpioMotionSensor:
    def __init__(self, pin):
        self.pin = pin
        self.motion_detected = True
        self.closed = False

    def close(self):
        self.closed = True


class StubGpioDistanceSensor:
    def __init__(self, echo, trigger, max_distance, partial):
        self.echo = echo
        self.trigger = trigger
        self.max_distance = max_distance
        self.partial = partial
        self.distance = 0.75
        self.raw = 0.5
        self.closed = False

    def _read(self):
        return self.raw

    def close(self):
        self.closed = True


@pytest.fixture
def stub_gpiozero(monkeypatch):
    module = ModuleType("gpiozero")
    module.MotionSensor = StubGpioMotionSensor
    module.DistanceSensor = StubGpioDistanceSensor
    monkeypatch.setitem(sys.modules, "gpiozero", module)
    return module


class TestPir:
    def test_wraps_gpiozero(self, stub_gpiozero):
        sensor = PirMotionSensor(17)
        assert sensor._device.pin == 17
        assert sensor.read() is True
        sensor._device.motion_detected = False
        assert sensor.read() is False
        sensor.close()
        assert sensor._device.closed


class TestUltrasonic:
    def test_constructor_avoids_blocking_and_sets_range(self, stub_gpiozero):
        device = UltrasonicDistanceSensor(23, 24, max_distance_m=3.5)._device
        assert (device.trigger, device.echo) == (23, 24)
        assert device.max_distance == 3.5
        assert device.partial is True

    def test_reads_distance_in_metres(self, stub_gpiozero):
        clock = FakeClock()
        sensor = UltrasonicDistanceSensor(23, 24, clock=clock)
        sensor._device._read()
        assert sensor.read() == 0.75

    def test_within_grace_period_before_first_echo(self, stub_gpiozero):
        clock = FakeClock()
        sensor = UltrasonicDistanceSensor(23, 24, stale_after_s=2.0, clock=clock)
        clock.advance(2.0)
        assert sensor.read() == 0.75

    def test_raises_if_sensor_never_echoes(self, stub_gpiozero):
        clock = FakeClock()
        sensor = UltrasonicDistanceSensor(23, 24, stale_after_s=2.0, clock=clock)
        clock.advance(2.1)
        with pytest.raises(SensorReadError, match="no ultrasonic echo"):
            sensor.read()

    def test_stale_value_after_sensor_dies_raises(self, stub_gpiozero):
        clock = FakeClock()
        sensor = UltrasonicDistanceSensor(23, 24, stale_after_s=2.0, clock=clock)
        sensor._device._read()
        clock.advance(1.0)
        assert sensor.read() == 0.75
        sensor._device.raw = None  # gpiozero's sampler gets no echo from here on
        sensor._device._read()
        clock.advance(1.5)
        with pytest.raises(SensorReadError):
            sensor.read()

    def test_recovers_when_echoes_return(self, stub_gpiozero):
        clock = FakeClock()
        sensor = UltrasonicDistanceSensor(23, 24, stale_after_s=2.0, clock=clock)
        clock.advance(5.0)
        with pytest.raises(SensorReadError):
            sensor.read()
        sensor._device._read()
        assert sensor.read() == 0.75

    def test_close(self, stub_gpiozero):
        sensor = UltrasonicDistanceSensor(23, 24)
        sensor.close()
        assert sensor._device.closed


class StubHx711:
    """Simulates an HX711 shifting out queued conversions, MSB first, on clock pulses."""

    def __init__(self):
        self.conversions = []
        self.never_ready = False
        self.bits = []
        self.pulses = 0
        self.pulses_per_read = []
        self.closed = []

    @property
    def level(self):
        if self.bits:
            return self.bits[0]
        if self.never_ready:
            return 1
        if self.conversions:
            if self.pulses:
                self.pulses_per_read.append(self.pulses)
                self.pulses = 0
            value = self.conversions.pop(0) & 0xFFFFFF
            self.bits = [0] + [(value >> (23 - i)) & 1 for i in range(24)]
            return 0
        return 1

    def clock_rising(self):
        self.pulses += 1
        if self.bits:
            self.bits.pop(0)


@pytest.fixture
def hx711(monkeypatch):
    chip = StubHx711()

    class Data:
        def __init__(self, pin, pull_up):
            self.number, self.pull_up = pin, pull_up
            self.pin = self

        @property
        def state(self):
            return chip.level

        def close(self):
            chip.closed.append("data")

    class Clock:
        def __init__(self, pin, initial_value):
            self.number, self.initial_value = pin, initial_value

        def on(self):
            chip.clock_rising()

        def off(self):
            pass

        def close(self):
            chip.closed.append("clock")

    module = ModuleType("gpiozero")
    module.DigitalInputDevice = Data
    module.DigitalOutputDevice = Clock
    monkeypatch.setitem(sys.modules, "gpiozero", module)
    chip.module = module
    return chip


class TestHx711:
    def make(self, **kwargs):
        clock = FakeClock()
        return Hx711WeightSensor(5, 6, clock=clock, wait=clock.advance, **kwargs)

    def test_pull_up_makes_a_disconnected_sensor_read_as_not_ready(self, hx711):
        sensor = self.make()
        assert sensor._data.pull_up is True
        assert sensor._sck.initial_value is False  # a high clock would power the chip down
        assert (sensor._data.number, sensor._sck.number) == (5, 6)

    @pytest.mark.parametrize("value", [0, 1, 123456, 0x7FFFFE, -1, -123456, -0x7FFFFF])
    def test_decodes_signed_24_bit(self, hx711, value):
        hx711.conversions = [value]
        assert self.make().read_raw() == value

    def test_sends_25_pulses_to_select_gain_128(self, hx711):
        hx711.conversions = [5, 6]
        sensor = self.make()
        sensor.read_raw()
        sensor.read_raw()
        assert hx711.pulses_per_read == [25]

    def test_applies_tare_and_scale(self, hx711):
        hx711.conversions = [11000, 1000, -9000]
        sensor = self.make(tare_offset=1000, scale_per_kg=2000)
        assert sensor.read() == 5.0
        assert sensor.read() == 0.0
        assert sensor.read() == -5.0

    def test_not_ready_raises_after_timeout(self, hx711):
        hx711.never_ready = True
        with pytest.raises(SensorReadError, match="not ready"):
            self.make().read()

    def test_waits_for_a_conversion_that_arrives_late(self, hx711):
        clock = FakeClock()
        waits = []

        def wait(seconds):
            waits.append(seconds)
            clock.advance(seconds)
            hx711.conversions = [42]

        sensor = Hx711WeightSensor(5, 6, clock=clock, wait=wait)
        assert sensor.read_raw() == 42
        assert waits

    @pytest.mark.parametrize("rail", [0x7FFFFF, -0x800000])
    def test_rail_values_raise(self, hx711, rail):
        hx711.conversions = [rail]
        with pytest.raises(SensorReadError, match="limit"):
            self.make().read_raw()

    def test_close(self, hx711):
        self.make().close()
        assert sorted(hx711.closed) == ["clock", "data"]

    def test_data_pin_released_if_clock_pin_fails(self, hx711):
        def broken(pin, initial_value):
            raise RuntimeError("pin busy")

        hx711.module.DigitalOutputDevice = broken
        with pytest.raises(RuntimeError):
            self.make()
        assert hx711.closed == ["data"]


class TestRealGpiozeroIfInstalled:
    """Runs against gpiozero's mock pin factory when the 'pi' extra is installed."""

    @pytest.fixture
    def mock_pins(self):
        gpiozero = pytest.importorskip("gpiozero")
        from gpiozero.pins.mock import MockFactory

        original = gpiozero.Device.pin_factory
        gpiozero.Device.pin_factory = MockFactory()
        yield gpiozero.Device.pin_factory
        gpiozero.Device.pin_factory.reset()
        gpiozero.Device.pin_factory = original

    def test_pir(self, mock_pins):
        sensor = PirMotionSensor(17)
        assert sensor.read() is False
        sensor.close()

    @pytest.mark.filterwarnings("ignore")
    def test_ultrasonic_with_no_echo_does_not_block_and_goes_stale(self, mock_pins):
        clock = FakeClock()
        sensor = UltrasonicDistanceSensor(23, 24, stale_after_s=2.0, clock=clock)
        try:
            assert sensor.read() == 0.0  # no samples yet: rejected as out of range by the detector
            clock.advance(3.0)
            with pytest.raises(SensorReadError):
                sensor.read()
        finally:
            sensor.close()


    def test_hx711_pins_and_not_ready(self, mock_pins):
        clock = FakeClock()
        sensor = Hx711WeightSensor(5, 6, clock=clock, wait=clock.advance)
        try:
            data = mock_pins.pin(5)
            data.drive_high()
            with pytest.raises(SensorReadError, match="not ready"):
                sensor.read_raw()
            data.drive_low()  # all-zero conversion: raw level is read despite the pull-up
            assert sensor.read_raw() == 0
        finally:
            sensor.close()


def test_package_imports_without_gpiozero(monkeypatch):
    """Importing the package and using fakes must not touch gpiozero."""
    real_import = builtins.__import__

    def blocking_import(name, *args, **kwargs):
        if name.startswith("gpiozero"):
            raise ImportError("no gpio here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocking_import)
    monkeypatch.delitem(sys.modules, "gpiozero", raising=False)
    assert FakeMotionSensor([True]).read() is True
    with pytest.raises(ImportError):
        PirMotionSensor(17)
    with pytest.raises(ImportError):
        UltrasonicDistanceSensor(23, 24)
    with pytest.raises(ImportError):
        Hx711WeightSensor(5, 6)
