"""Command line entry point: python -m studyspace_node --config path.toml"""

from __future__ import annotations

import argparse
import logging
import math
import signal
import threading
from collections.abc import Sequence
from typing import Any

from .config import AppConfig, ConfigError, SeatConfig, load_config
from .node import Node, Seat
from .occupancy import OccupancyDetector
from .publisher import Publisher, create_mqtt_client, publish_offline
from .sensors import (
    FakeDistanceSensor,
    FakeMotionSensor,
    FakeWeightSensor,
    Hx711WeightSensor,
    PirMotionSensor,
    UltrasonicDistanceSensor,
)
from .ui_server import UiServer

log = logging.getLogger("studyspace_node")

_SIM_FAR_M = 2.0
_SIM_NEAR_M = 0.5
_SIM_EMPTY_KG = 0.5
_SIM_SEATED_KG = 70.0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="studyspace-node", description=__doc__)
    parser.add_argument("--config", required=True, help="path to the TOML configuration file")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="use scripted fake sensors instead of GPIO (MQTT is still real)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        type=str.upper,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="logging level (default: INFO)",
    )
    return parser


def _new_seat(config: AppConfig, seat: SeatConfig, **sensors: Any) -> Seat:
    return Seat(
        space_id=seat.space_id,
        label=seat.label,
        position_deg=seat.position_deg,
        detector=OccupancyDetector(config.occupancy),
        **sensors,
    )


def simulated_seats(config: AppConfig) -> list[Seat]:
    """Repeating free -> occupied -> free scenarios, staggered so seats differ."""
    settings = config.occupancy
    settle = settings.confirm_readings + max(settings.distance_window, settings.weight_window)
    vacating = math.ceil(settings.vacancy_timeout_s / config.sensors.poll_interval_s) + settle
    pattern = [False] * settle + [True] * settle + [False] * vacating

    seats = []
    for index, seat in enumerate(config.seats):
        shift = index * len(pattern) // len(config.seats)
        present = pattern[shift:] + pattern[:shift]
        sensors: dict[str, Any] = {}
        if seat.motion:
            sensors["motion"] = FakeMotionSensor(present, loop=True)
        if seat.distance:
            sensors["distance"] = FakeDistanceSensor(
                [_SIM_NEAR_M if p else _SIM_FAR_M for p in present], loop=True
            )
        if seat.weight:
            sensors["weight"] = FakeWeightSensor(
                [_SIM_SEATED_KG if p else _SIM_EMPTY_KG for p in present], loop=True
            )
        seats.append(_new_seat(config, seat, **sensors))
    return seats


def _real_seat(config: AppConfig, seat_config: SeatConfig) -> Seat:
    seat = _new_seat(config, seat_config)
    try:
        if seat_config.weight:
            w = seat_config.weight
            seat.weight = Hx711WeightSensor(w.data_pin, w.clock_pin, w.tare_offset, w.scale_per_kg)
        if seat_config.motion:
            seat.motion = PirMotionSensor(seat_config.motion.pin)
        if seat_config.distance:
            seat.distance = UltrasonicDistanceSensor(
                seat_config.distance.trigger_pin,
                seat_config.distance.echo_pin,
                max_distance_m=config.occupancy.max_valid_distance_m,
            )
    except Exception:
        seat.close()
        raise
    return seat


def real_seats(config: AppConfig) -> list[Seat]:
    seats: list[Seat] = []
    try:
        for seat_config in config.seats:
            seats.append(_real_seat(config, seat_config))
    except Exception:
        for seat in seats:
            seat.close()
        raise
    return seats


def _shutdown_mqtt(client: Any, config: AppConfig) -> None:
    publish_offline(client, config.status_topic)
    client.disconnect()
    client.loop_stop()


def _start_ui(node: Node, path: str) -> UiServer | None:
    """The screen is optional: failing to serve it must not stop the node reporting."""
    server = UiServer(path, node.submit_command)
    try:
        server.start()
    except OSError as exc:
        log.error("Touchscreen interface unavailable: cannot open %s: %s", path, exc)
        return None
    node.attach_ui(server.publish)
    return server


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 2

    try:
        seats = simulated_seats(config) if args.simulate else real_seats(config)
    except ImportError:
        log.error('gpiozero is not installed. Run: pip install ".[pi]" (or use --simulate)')
        return 1
    except Exception as exc:
        log.error("Could not initialise GPIO sensors: %s", exc)
        return 1

    stop_event = threading.Event()

    def request_stop(signum: int, _frame: Any) -> None:
        log.info("Received %s; shutting down", signal.Signals(signum).name)
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    client = create_mqtt_client(config)
    publisher = Publisher(client)
    client.user_data_set(publisher)
    node = Node(seats, publisher, config)
    ui_server = _start_ui(node, config.ui.socket_path)

    log.info(
        "Starting node %s for %s with %d seat(s) (%s sensors), broker %s:%s",
        config.node.id,
        config.node.desk_id,
        len(seats),
        "simulated" if args.simulate else "GPIO",
        config.mqtt.host,
        config.mqtt.port,
    )
    client.connect_async(config.mqtt.host, config.mqtt.port)
    client.loop_start()
    try:
        node.run(stop_event)
    finally:
        _shutdown_mqtt(client, config)
        if ui_server is not None:
            ui_server.stop()
        for seat in seats:
            seat.close()
    log.info("Stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
