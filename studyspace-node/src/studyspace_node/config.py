"""Load and validate the TOML configuration into typed dataclasses."""

from __future__ import annotations

import math
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from . import messages
from .occupancy import OccupancySettings

PASSWORD_ENV_VAR = "STUDYSPACE_MQTT_PASSWORD"
DEFAULT_UI_SOCKET = "/run/studyspace-node/ui.sock"
_REQUIRED = object()


class ConfigError(ValueError):
    """The configuration file is missing, malformed, or has invalid values."""


@dataclass(frozen=True)
class NodeConfig:
    id: str
    desk_id: str
    building: str
    room: str


@dataclass(frozen=True)
class WeightSensorConfig:
    data_pin: int
    clock_pin: int
    tare_offset: float
    scale_per_kg: float


@dataclass(frozen=True)
class MotionSensorConfig:
    pin: int


@dataclass(frozen=True)
class DistanceSensorConfig:
    trigger_pin: int
    echo_pin: int


@dataclass(frozen=True)
class SeatConfig:
    """One seat with at least one sensor, in any combination."""

    space_id: str
    label: str
    position_deg: float | None = None  # clockwise from the top of the on-screen diagram
    weight: WeightSensorConfig | None = None
    motion: MotionSensorConfig | None = None
    distance: DistanceSensorConfig | None = None


@dataclass(frozen=True)
class SensorConfig:
    poll_interval_s: float = 2.0


@dataclass(frozen=True)
class UiConfig:
    socket_path: str = DEFAULT_UI_SOCKET


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int = 1883
    topic_prefix: str = "studyspace"
    username: str | None = None
    password: str | None = field(default=None, repr=False)  # keep out of logs
    tls: bool = False
    heartbeat_s: float = 60
    client_id: str | None = None


@dataclass(frozen=True)
class AppConfig:
    node: NodeConfig
    mqtt: MqttConfig
    seats: tuple[SeatConfig, ...]
    sensors: SensorConfig = field(default_factory=SensorConfig)
    occupancy: OccupancySettings = field(default_factory=OccupancySettings)
    ui: UiConfig = field(default_factory=UiConfig)

    def state_topic(self, space_id: str) -> str:
        return messages.state_topic(
            self.mqtt.topic_prefix, self.node.building, self.node.room, space_id
        )

    @property
    def status_topic(self) -> str:
        return messages.status_topic(
            self.mqtt.topic_prefix, self.node.building, self.node.room, self.node.id
        )

    @property
    def client_id(self) -> str:
        return self.mqtt.client_id or self.node.id


class _Section:
    """Typed access to one TOML table, tracking keys so typos can be reported."""

    def __init__(self, table: Any, name: str) -> None:
        if not isinstance(table, dict):
            raise ConfigError(f"[{name}] must be a table")
        self.name = name
        self.table = table
        self._seen: set[str] = set()

    @classmethod
    def of(cls, raw: Mapping[str, Any], name: str, required: bool = False) -> _Section:
        if name not in raw and required:
            raise ConfigError(f"missing required section [{name}]")
        return cls(raw.get(name, {}), name)

    def _get(self, key: str, default: Any, is_valid: Any, expected: str) -> Any:
        self._seen.add(key)
        if key not in self.table:
            if default is _REQUIRED:
                raise ConfigError(f"[{self.name}] missing required field '{key}'")
            return default
        value = self.table[key]
        if not is_valid(value):
            raise ConfigError(f"[{self.name}] {key}: expected {expected}, got {value!r}")
        return value

    def string(self, key: str, default: Any = _REQUIRED) -> Any:
        return self._get(key, default, lambda v: isinstance(v, str) and v != "", "a non-empty string")

    def integer(self, key: str, default: Any = _REQUIRED) -> Any:
        return self._get(
            key, default, lambda v: isinstance(v, int) and not isinstance(v, bool), "an integer"
        )

    def number(self, key: str, default: Any = _REQUIRED) -> Any:
        return self._get(
            key,
            default,
            lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v),
            "a number",
        )

    def boolean(self, key: str, default: Any = _REQUIRED) -> Any:
        return self._get(key, default, lambda v: isinstance(v, bool), "true or false")

    def pin(self, key: str) -> int:
        value = self.integer(key)
        if not 0 <= value <= 27:
            raise ConfigError(f"[{self.name}] {key}: expected a BCM GPIO number 0-27, got {value}")
        return value

    def mark_seen(self, *keys: str) -> None:
        self._seen.update(keys)

    def check_no_unknown_keys(self) -> None:
        unknown = sorted(set(self.table) - self._seen)
        if unknown:
            raise ConfigError(f"[{self.name}] unknown field(s): {', '.join(unknown)}")


def _segment(section: _Section, key: str, value: str) -> str:
    try:
        return messages.validate_segment(key, value)
    except ValueError as exc:
        raise ConfigError(f"[{section.name}] {exc}") from exc


def _parse_node(raw: Mapping[str, Any]) -> NodeConfig:
    s = _Section.of(raw, "node", required=True)
    node = NodeConfig(
        id=_segment(s, "id", s.string("id")),
        desk_id=_segment(s, "desk_id", s.string("desk_id")),
        building=_segment(s, "building", s.string("building")),
        room=_segment(s, "room", s.string("room")),
    )
    s.check_no_unknown_keys()
    return node


def _parse_weight(table: Any, name: str) -> WeightSensorConfig:
    s = _Section(table, name)
    weight = WeightSensorConfig(
        data_pin=s.pin("data_pin"),
        clock_pin=s.pin("clock_pin"),
        tare_offset=s.number("tare_offset"),
        scale_per_kg=s.number("scale_per_kg"),
    )
    s.check_no_unknown_keys()
    if weight.scale_per_kg == 0:
        raise ConfigError(f"[{name}] scale_per_kg: must not be zero")
    return weight


def _parse_motion(table: Any, name: str) -> MotionSensorConfig:
    s = _Section(table, name)
    motion = MotionSensorConfig(pin=s.pin("pin"))
    s.check_no_unknown_keys()
    return motion


def _parse_distance(table: Any, name: str) -> DistanceSensorConfig:
    s = _Section(table, name)
    distance = DistanceSensorConfig(trigger_pin=s.pin("trigger_pin"), echo_pin=s.pin("echo_pin"))
    s.check_no_unknown_keys()
    return distance


_SEAT_SENSORS = {"weight": _parse_weight, "motion": _parse_motion, "distance": _parse_distance}


def _parse_seat(table: Any, index: int) -> SeatConfig:
    name = f"seats[{index}]"
    s = _Section(table, name)
    space_id = _segment(s, "space_id", s.string("space_id"))
    sensors = {
        key: parser(s.table[key], f"{name}.{key}")
        for key, parser in _SEAT_SENSORS.items()
        if key in s.table
    }
    s.mark_seen(*sensors)
    if not sensors:
        raise ConfigError(f"[{name}] needs at least one of: {', '.join(_SEAT_SENSORS)}")
    seat = SeatConfig(
        space_id=space_id,
        label=s.string("label", space_id),
        position_deg=s.number("position_deg", None),
        **sensors,
    )
    s.check_no_unknown_keys()
    return seat


def _seat_pins(seat: SeatConfig) -> list[int]:
    pins: list[int] = []
    if seat.weight:
        pins += [seat.weight.data_pin, seat.weight.clock_pin]
    if seat.motion:
        pins.append(seat.motion.pin)
    if seat.distance:
        pins += [seat.distance.trigger_pin, seat.distance.echo_pin]
    return pins


def _parse_seats(raw: Mapping[str, Any]) -> tuple[SeatConfig, ...]:
    tables = raw.get("seats")
    if tables is None:
        raise ConfigError("missing required section [[seats]]: configure at least one seat")
    if not isinstance(tables, list) or not tables:
        raise ConfigError("[[seats]] must be a non-empty array of tables")
    seats = tuple(_parse_seat(table, i) for i, table in enumerate(tables))

    ids = [seat.space_id for seat in seats]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ConfigError(f"[[seats]] duplicate space_id: {', '.join(duplicates)}")
    pins = [pin for seat in seats for pin in _seat_pins(seat)]
    shared = sorted({p for p in pins if pins.count(p) > 1})
    if shared:
        raise ConfigError(f"[[seats]] GPIO pins used more than once: {shared}")
    return seats


def _parse_sensors(raw: Mapping[str, Any]) -> SensorConfig:
    s = _Section.of(raw, "sensors")
    sensors = SensorConfig(poll_interval_s=s.number("poll_interval_s", SensorConfig.poll_interval_s))
    s.check_no_unknown_keys()
    if sensors.poll_interval_s <= 0:
        raise ConfigError(f"[sensors] poll_interval_s: must be > 0, got {sensors.poll_interval_s}")
    return sensors


def _parse_occupancy(raw: Mapping[str, Any]) -> OccupancySettings:
    s = _Section.of(raw, "occupancy")
    names = [f.name for f in fields(OccupancySettings)]
    s.mark_seen(*names)
    s.check_no_unknown_keys()
    try:
        return OccupancySettings(**{k: s.table[k] for k in names if k in s.table})
    except ValueError as exc:
        raise ConfigError(f"[occupancy] {exc}") from exc


def _parse_ui(raw: Mapping[str, Any]) -> UiConfig:
    s = _Section.of(raw, "ui")
    ui = UiConfig(socket_path=s.string("socket_path", DEFAULT_UI_SOCKET))
    s.check_no_unknown_keys()
    return ui


def _parse_mqtt(raw: Mapping[str, Any], env: Mapping[str, str]) -> MqttConfig:
    s = _Section.of(raw, "mqtt", required=True)
    tls = s.boolean("tls", False)
    file_password = s.string("password", None)
    mqtt = MqttConfig(
        host=s.string("host"),
        port=s.integer("port", 8883 if tls else 1883),
        topic_prefix=_segment(s, "topic_prefix", s.string("topic_prefix", "studyspace")),
        username=s.string("username", None),
        # An empty variable (e.g. a blank line in a systemd env file) means "not set".
        password=env.get(PASSWORD_ENV_VAR) or file_password,
        tls=tls,
        heartbeat_s=s.number("heartbeat_s", 60),
        client_id=s.string("client_id", None),
    )
    s.check_no_unknown_keys()
    if not 1 <= mqtt.port <= 65535:
        raise ConfigError(f"[mqtt] port: expected 1-65535, got {mqtt.port}")
    if mqtt.heartbeat_s <= 0:
        raise ConfigError(f"[mqtt] heartbeat_s: must be > 0, got {mqtt.heartbeat_s}")
    if mqtt.password is not None and mqtt.username is None:
        raise ConfigError(f"[mqtt] a password (or {PASSWORD_ENV_VAR}) requires a username")
    return mqtt


def parse_config(raw: Mapping[str, Any], env: Mapping[str, str] | None = None) -> AppConfig:
    """Build an AppConfig from parsed TOML; ``env`` defaults to ``os.environ``."""
    env = os.environ if env is None else env
    known = {"node", "seats", "sensors", "occupancy", "ui", "mqtt"}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(f"unknown section(s): {', '.join(unknown)}")
    return AppConfig(
        node=_parse_node(raw),
        mqtt=_parse_mqtt(raw, env),
        seats=_parse_seats(raw),
        sensors=_parse_sensors(raw),
        occupancy=_parse_occupancy(raw),
        ui=_parse_ui(raw),
    )


def load_config(path: str | Path, env: Mapping[str, str] | None = None) -> AppConfig:
    """Load a TOML config file; raises ConfigError with a readable message."""
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc.strerror or exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    return parse_config(raw, env)
