"""Find a chair's tare_offset and scale_per_kg for the config file.

    studyspace-calibrate --config config.toml --seat desk-07-a --known-kg 10
"""

from __future__ import annotations

import argparse
import logging
import statistics
from collections.abc import Callable, Sequence

from .config import ConfigError, load_config
from .sensors import Hx711WeightSensor, SensorReadError

log = logging.getLogger("studyspace_node.calibrate")

MIN_RAW_CHANGE = 100  # less than this means the known weight isn't reaching the load cell


def _median_of(read_raw: Callable[[], int], samples: int) -> float:
    readings = []
    for _ in range(samples):
        try:
            readings.append(read_raw())
        except SensorReadError:
            log.debug("Skipping a failed reading")
    if len(readings) < samples / 2:
        raise SensorReadError(f"only {len(readings)} of {samples} readings succeeded")
    return statistics.median(readings)


def calibrate(
    read_raw: Callable[[], int],
    known_kg: float,
    wait_for_user: Callable[[str], object],
    samples: int = 20,
) -> tuple[float, float]:
    """Returns (tare_offset, scale_per_kg). The scale is negative if the cell is reversed."""
    if known_kg <= 0:
        raise ValueError(f"known weight must be positive, got {known_kg}")
    wait_for_user("Empty the chair, then press Enter: ")
    tare = _median_of(read_raw, samples)
    wait_for_user(f"Put {known_kg} kg on the seat, then press Enter: ")
    loaded = _median_of(read_raw, samples)
    if abs(loaded - tare) < MIN_RAW_CHANGE:
        raise ValueError(
            f"the reading barely changed ({tare:.0f} to {loaded:.0f}); check the load cell wiring"
        )
    return tare, (loaded - tare) / known_kg


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="studyspace-calibrate", description=__doc__)
    parser.add_argument("--config", required=True, help="path to the TOML configuration file")
    parser.add_argument("--seat", required=True, help="space_id of the seat to calibrate")
    parser.add_argument("--known-kg", required=True, type=float, help="the calibration weight in kg")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 2
    seat = next((s for s in config.seats if s.space_id == args.seat), None)
    if seat is None or seat.weight is None:
        log.error("No seat %s with a weight sensor in %s", args.seat, args.config)
        return 2

    try:
        sensor = Hx711WeightSensor(seat.weight.data_pin, seat.weight.clock_pin)
    except ImportError:
        log.error('gpiozero is not installed. Run: pip install ".[pi]"')
        return 1
    except Exception as exc:
        log.error("Could not initialise the HX711: %s", exc)
        return 1
    try:
        tare, scale = calibrate(sensor.read_raw, args.known_kg, input)
    except (ValueError, SensorReadError) as exc:
        log.error("Calibration failed: %s", exc)
        return 1
    finally:
        sensor.close()
    log.info("Put these in [seats.weight] for %s:", args.seat)
    log.info("tare_offset = %.1f", tare)
    log.info("scale_per_kg = %.1f", scale)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
