"""Message formats: parsing southbound input and building northbound output.

Nothing here touches the network or reads a clock (apart from `now_utc()`,
which exists so callers have one place to get a correctly formatted
timestamp). Timestamps for payloads are always passed in, which keeps the
builders deterministic and easy to test.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

SUPPORTED_SCHEMA = 1


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------

def format_utc(dt: datetime) -> str:
    """Format an aware datetime as ISO 8601 UTC with a 'Z' suffix, e.g.
    '2026-11-10T14:05:09Z' (same style as the simulator)."""
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    utc = dt.astimezone(timezone.utc)
    return utc.isoformat(timespec="seconds").replace("+00:00", "Z")


def now_utc() -> str:
    """Current wall-clock time for payload timestamps. Never use this for
    timeouts or scheduling (wall clocks can jump); use time.monotonic()."""
    return format_utc(datetime.now(timezone.utc))


# --------------------------------------------------------------------------
# Southbound: messages from devices
# --------------------------------------------------------------------------

class ParseError(ValueError):
    """A southbound message was rejected. The message says why."""


@dataclass(frozen=True)
class Birth:
    id: str
    space_id: str
    device_type: str
    firmware: str
    occupied: bool
    timestamp: str


@dataclass(frozen=True)
class Heartbeat:
    id: str
    timestamp: str
    # None means "not included", i.e. occupancy has not changed. Devices only
    # send this field when the value flips.
    occupied: bool | None = None


@dataclass(frozen=True)
class Death:
    id: str


Message = Birth | Heartbeat | Death


def device_id_from_topic(topic: str) -> str:
    """The device id is the second-to-last topic level:
    studyspace/library/floor2/<space>/<device-id>/status"""
    levels = topic.split("/")
    if len(levels) < 2:
        raise ParseError(f"topic {topic!r} has no device id level")
    device_id = levels[-2]
    if not device_id:
        raise ParseError(f"empty device id in topic {topic!r}")
    return device_id


def _require_str(data: dict, field: str) -> str:
    if field not in data:
        raise ParseError(f"missing required field {field!r}")
    value = data[field]
    if not isinstance(value, str) or not value:
        raise ParseError(f"field {field!r} must be a non-empty string")
    return value


def _require_bool(data: dict, field: str) -> bool:
    if field not in data:
        raise ParseError(f"missing required field {field!r}")
    value = data[field]
    # isinstance(1, bool) is False, but isinstance(True, int) is True, so
    # checking for bool is the strict direction we want.
    if not isinstance(value, bool):
        raise ParseError(f"field {field!r} must be true or false")
    return value


def parse(topic: str, payload: bytes | str) -> Message:
    """Validate one southbound message. Raises ParseError if it is unusable.

    The caller logs the error and carries on: one bad device must never take
    the node down.
    """
    # An empty payload is how a retained message is cleared on the broker.
    if not payload:
        raise ParseError("empty payload (cleared retained message)")

    topic_id = device_id_from_topic(topic)

    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParseError(f"invalid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ParseError("payload must be a JSON object")

    payload_id = _require_str(data, "id")
    if payload_id != topic_id:
        raise ParseError(f"payload id {payload_id!r} does not match topic id {topic_id!r}")

    msg_type = data.get("type")
    if msg_type == "birth":
        schema = data.get("schema")
        if "schema" not in data:
            raise ParseError("missing required field 'schema'")
        if schema != SUPPORTED_SCHEMA or isinstance(schema, bool):
            raise ParseError(f"unsupported schema {schema!r}")
        return Birth(
            id=payload_id,
            space_id=_require_str(data, "space_id"),
            device_type=_require_str(data, "device_type"),
            firmware=_require_str(data, "firmware"),
            occupied=_require_bool(data, "occupied"),
            timestamp=_require_str(data, "timestamp"),
        )
    if msg_type == "heartbeat":
        occupied = _require_bool(data, "occupied") if "occupied" in data else None
        return Heartbeat(
            id=payload_id,
            timestamp=_require_str(data, "timestamp"),
            occupied=occupied,
        )
    if msg_type == "death":
        return Death(id=payload_id)
    raise ParseError(f"unknown message type {msg_type!r}")


# --------------------------------------------------------------------------
# Northbound: what the node publishes for the host
# --------------------------------------------------------------------------

def base_topic(site: str, node_id: str) -> str:
    return f"studyspace/v1/{site}/{node_id}"


def status_topic(site: str, node_id: str) -> str:
    return f"{base_topic(site, node_id)}/status"


def data_topic(site: str, node_id: str) -> str:
    return f"{base_topic(site, node_id)}/data"


def node_birth_payload(node_id: str, timestamp: str, devices: list[dict]) -> str:
    return json.dumps(
        {"type": "birth", "id": node_id, "timestamp": timestamp, "devices": devices}
    )


def node_death_payload(node_id: str) -> str:
    return json.dumps({"type": "death", "id": node_id})


def data_payload(
    node_id: str, timestamp: str, changed: list[str], devices: dict[str, dict]
) -> str:
    return json.dumps(
        {"id": node_id, "timestamp": timestamp, "changed": changed, "devices": devices}
    )
