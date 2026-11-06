"""MQTT topics and JSON payloads: the contract the backend relies on (see README)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .occupancy import OccupancySource, OccupancyState

SCHEMA_VERSION = 1
STATUS_ONLINE = "online"
STATUS_OFFLINE = "offline"
REASON_CHANGE = "change"
REASON_HEARTBEAT = "heartbeat"
_REASONS = frozenset({REASON_CHANGE, REASON_HEARTBEAT})
_PUBLISHABLE_STATES = frozenset({OccupancyState.FREE, OccupancyState.OCCUPIED})
_SOURCES = frozenset(OccupancySource)


class PayloadError(ValueError):
    """A state payload does not conform to the message contract."""


def validate_segment(name: str, value: object) -> str:
    """Return ``value`` if it is safe as one MQTT topic level, else raise ValueError."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")
    if any(c in "/+#\0" or c.isspace() for c in value):
        raise ValueError(
            f"{name} must not contain '/', '+', '#', NUL or whitespace, got {value!r}"
        )
    return value


def _topic(prefix: str, building: str, room: str, subject: str, leaf: str) -> str:
    validate_segment("topic prefix", prefix)
    validate_segment("building", building)
    validate_segment("room", room)
    validate_segment(leaf + " topic id", subject)
    return f"{prefix}/{building}/{room}/{subject}/{leaf}"


def state_topic(prefix: str, building: str, room: str, space_id: str) -> str:
    return _topic(prefix, building, room, space_id, "state")


def status_topic(prefix: str, building: str, room: str, node_id: str) -> str:
    """Per node, not per seat: an MQTT client can only have one Last Will."""
    return _topic(prefix, building, room, node_id, "status")


def _format_timestamp(timestamp: datetime) -> str:
    if timestamp.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return timestamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class StateMessage:
    node_id: str
    desk_id: str
    space_id: str
    state: OccupancyState
    healthy: bool
    reason: str
    source: OccupancySource
    timestamp: datetime
    schema: int = SCHEMA_VERSION


def build_payload(
    node_id: str,
    desk_id: str,
    space_id: str,
    state: OccupancyState,
    healthy: bool,
    reason: str,
    source: OccupancySource,
    timestamp: datetime,
) -> str:
    """Serialise a state message to JSON. Deliberately has no sensor-data fields."""
    if state not in _PUBLISHABLE_STATES:
        raise ValueError(f"state {state!r} must not be published")
    if reason not in _REASONS:
        raise ValueError(f"unknown reason {reason!r}")
    if source not in _SOURCES:
        raise ValueError(f"unknown source {source!r}")
    return json.dumps(
        {
            "schema": SCHEMA_VERSION,
            "node_id": node_id,
            "desk_id": desk_id,
            "space_id": space_id,
            "state": OccupancyState(state).value,
            "healthy": healthy,
            "reason": reason,
            "source": OccupancySource(source).value,
            "timestamp": _format_timestamp(timestamp),
        }
    )


def _require(data: dict[str, Any], key: str, expected_type: type) -> Any:
    if key not in data:
        raise PayloadError(f"missing field {key!r}")
    value = data[key]
    # type() rather than isinstance(): JSON true must not pass as the integer 1.
    if type(value) is not expected_type:
        raise PayloadError(f"field {key!r} must be {expected_type.__name__}, got {value!r}")
    return value


def parse_payload(payload: str | bytes) -> StateMessage:
    """Parse and validate a state payload; raises PayloadError on anything invalid.

    Unknown extra fields are ignored so schema 1 can gain optional fields.
    """
    try:
        data = json.loads(payload)
    except (ValueError, TypeError) as exc:  # includes JSON and Unicode decode errors
        raise PayloadError(f"payload is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PayloadError("payload must be a JSON object")

    schema = _require(data, "schema", int)
    if schema != SCHEMA_VERSION:
        raise PayloadError(f"unsupported schema {schema!r}")

    raw_state = _require(data, "state", str)
    if raw_state not in _PUBLISHABLE_STATES:  # StrEnum members compare equal to their values
        raise PayloadError(f"invalid state {raw_state!r}")
    state = OccupancyState(raw_state)

    reason = _require(data, "reason", str)
    if reason not in _REASONS:
        raise PayloadError(f"invalid reason {reason!r}")

    raw_source = _require(data, "source", str)
    if raw_source not in _SOURCES:
        raise PayloadError(f"invalid source {raw_source!r}")

    node_id = _require(data, "node_id", str)
    desk_id = _require(data, "desk_id", str)
    space_id = _require(data, "space_id", str)
    if not node_id or not desk_id or not space_id:
        raise PayloadError("node_id, desk_id and space_id must be non-empty")

    raw_timestamp = _require(data, "timestamp", str)
    try:
        timestamp = datetime.fromisoformat(raw_timestamp)
    except ValueError as exc:
        raise PayloadError(f"invalid timestamp {raw_timestamp!r}") from exc
    if timestamp.utcoffset() is None:
        raise PayloadError(f"timestamp {raw_timestamp!r} has no UTC offset")

    return StateMessage(
        node_id=node_id,
        desk_id=desk_id,
        space_id=space_id,
        state=state,
        healthy=_require(data, "healthy", bool),
        reason=reason,
        source=OccupancySource(raw_source),
        timestamp=timestamp.astimezone(UTC),
    )
