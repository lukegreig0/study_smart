import logging
import sys
from types import ModuleType

import pytest

from conftest import REPO_ROOT
from studyspace_node import calibrate as cal
from studyspace_node.sensors import SensorReadError

EXAMPLE = str(REPO_ROOT / "config.example.toml")


class Rig:
    """Raw readings depend on which prompt the user has answered."""

    def __init__(self, empty=1000, loaded=21000):
        self.values = [None, empty, loaded]
        self.step = 0
        self.prompts = []

    def wait(self, prompt):
        self.prompts.append(prompt)
        self.step += 1

    def read(self):
        return self.values[self.step]


def test_computes_tare_and_scale():
    rig = Rig()
    assert cal.calibrate(rig.read, 10, rig.wait) == (1000, 2000.0)
    assert "Empty" in rig.prompts[0]
    assert "10 kg" in rig.prompts[1]


def test_reversed_cell_gives_a_negative_scale():
    rig = Rig(empty=1000, loaded=-19000)
    assert cal.calibrate(rig.read, 10, rig.wait) == (1000, -2000.0)


def test_median_rejects_spikes():
    rig = Rig()
    readings = iter([1000, 9_000_000, 1000, 21000, 21000, -9_000_000])
    tare, scale = cal.calibrate(lambda: next(readings), 10, rig.wait, samples=3)
    assert (tare, scale) == (1000, 2000.0)


def test_failed_readings_are_skipped():
    rig = Rig()
    readings = iter([SensorReadError("x"), 1000, 1000, 21000, 21000, 21000])

    def read():
        value = next(readings)
        if isinstance(value, Exception):
            raise value
        return value

    assert cal.calibrate(read, 10, rig.wait, samples=3)[0] == 1000


def test_too_many_failures_raises():
    def read():
        raise SensorReadError("dead")

    with pytest.raises(SensorReadError, match="0 of 3"):
        cal.calibrate(read, 10, Rig().wait, samples=3)


def test_no_change_means_a_wiring_problem():
    rig = Rig(empty=1000, loaded=1050)
    with pytest.raises(ValueError, match="wiring"):
        cal.calibrate(rig.read, 10, rig.wait)


@pytest.mark.parametrize("known", [0, -5])
def test_known_weight_must_be_positive(known):
    with pytest.raises(ValueError, match="positive"):
        cal.calibrate(lambda: 0, known, lambda _: None)


class TestMain:
    @pytest.fixture
    def fake_sensor(self, monkeypatch):
        rig = Rig()

        class Sensor:
            built = []
            closed = []

            def __init__(self, data_pin, clock_pin):
                Sensor.built.append((data_pin, clock_pin))

            read_raw = staticmethod(rig.read)

            def close(self):
                Sensor.closed.append(True)

        monkeypatch.setattr(cal, "Hx711WeightSensor", Sensor)
        monkeypatch.setattr("builtins.input", rig.wait)
        return Sensor

    def args(self, seat="desk-07-b"):
        return ["--config", EXAMPLE, "--seat", seat, "--known-kg", "10"]

    def test_outputs_config_lines_for_the_right_seat(self, fake_sensor, caplog):
        with caplog.at_level(logging.INFO):
            assert cal.main(self.args()) == 0
        assert fake_sensor.built == [(13, 19)]
        assert fake_sensor.closed == [True]
        assert "tare_offset = 1000.0" in caplog.text
        assert "scale_per_kg = 2000.0" in caplog.text

    def test_unknown_seat(self, fake_sensor, caplog):
        with caplog.at_level(logging.ERROR):
            assert cal.main(self.args("nope")) == 2
        assert "No seat nope" in caplog.text

    def test_bad_config(self, caplog):
        with caplog.at_level(logging.ERROR):
            assert cal.main(["--config", "/nope.toml", "--seat", "a", "--known-kg", "1"]) == 2
        assert "Configuration error" in caplog.text

    def test_calibration_failure_still_releases_pins(self, fake_sensor, monkeypatch, caplog):
        def fail(*args):
            raise ValueError("barely changed")

        monkeypatch.setattr(cal, "calibrate", fail)
        with caplog.at_level(logging.ERROR):
            assert cal.main(self.args()) == 1
        assert fake_sensor.closed == [True]
        assert "barely changed" in caplog.text

    def test_missing_gpiozero(self, monkeypatch, caplog):
        def missing(*args):
            raise ImportError

        monkeypatch.setattr(cal, "Hx711WeightSensor", missing)
        with caplog.at_level(logging.ERROR):
            assert cal.main(self.args()) == 1
        assert "pip install" in caplog.text

    def test_gpio_failure(self, monkeypatch, caplog):
        def broken(*args):
            raise RuntimeError("pin busy")

        monkeypatch.setattr(cal, "Hx711WeightSensor", broken)
        with caplog.at_level(logging.ERROR):
            assert cal.main(self.args()) == 1
        assert "pin busy" in caplog.text
