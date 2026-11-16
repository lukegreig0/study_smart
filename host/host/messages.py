"""Parsing the node's northbound messages.

No paho, no sqlite, no clock reads: just "bytes on a topic" in, a typed,
validated message (or None) out. That keeps it trivially testable.

Topic: studyspace/v1/{site}/{node_id}/{status|data}

  status  {"type":"birth","id","timestamp","devices":[{id,space_id,device_type,firmware}]}
          {"type":"death","id"}
  data    {"id","timestamp","changed":[...],"devices":{dev_id: {"online":true,"occupied":bool,"since"}
                                                       | {"online":false}}}

A data message is a *delta* (only the devices that changed) except right after
the node (re)connects, when it sends every device with "changed": [].
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Ids end up as MQTT topic levels and database keys, so keep them boring.
_ID = re.compile(r"^[a-z0-9-]+$")

STATES = ("free", "occupied", "unknown")


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------

def format_utc(dt: datetime) -> str:
    """ISO 8601 UTC with a 'Z' suffix and whole seconds, e.g.
    '2026-11-10T14:05:09Z' (the same style as the node and simulator).

    Every timestamp we store goes through this, so all stored timestamps have
    the same fixed-width format and can be compared as plain strings
    (including in SQL), which is what ingest.py relies on.
    """
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def now_utc() -> str:
    """Current wall-clock time for stored timestamps. Only called from the
    MQTT wiring; parsing and ingest get time passed in."""
    return format_utc(datetime.now(timezone.utc))


def normalize_timestamp(value: str) -> str:
    """Validate an incoming UTC timestamp and return it in our canonical form.

    Raises ValueError for anything that isn't an ISO 8601 time in UTC.
    """
    if not value.endswith("Z"):
        raise ValueError("must be UTC and end in 'Z'")
    dt = datetime.fromisoformat(value)  # Python 3.11+ understands the 'Z'
    if dt.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError("must be UTC")
    return format_utc(dt)


# --------------------------------------------------------------------------
# Parsed message types
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class DeviceInfo:
    """One entry of a node birth's device list."""
    id: str
    space_id: str
    device_type: str | None
    firmware: str | None


@dataclass(frozen=True)
class DeviceReport:
    """One entry of a data message's "devices" object."""
    online: bool
    occupied: bool | None = None  # only meaningful when online
    since: str | None = None      # last occupancy change, when online

    @property
    def state(self) -> str:
        if not self.online:
            # Offline: the node doesn't know whether the desk is in use.
            return "unknown"
        return "occupied" if self.occupied else "free"


@dataclass(frozen=True)
class NodeBirth:
    site: str
    node_id: str
    timestamp: str
    devices: tuple[DeviceInfo, ...]


@dataclass(frozen=True)
class NodeDeath:
    site: str
    node_id: str


@dataclass(frozen=True)
class NodeData:
    site: str
    node_id: str
    timestamp: str
    changed: tuple[str, ...]
    devices: dict[str, DeviceReport]

    @property
    def is_full_snapshot(self) -> bool:
        # The node marks "this is every device I know" with an empty list.
        return not self.changed


Message = NodeBirth | NodeDeath | NodeData


class ParseError(ValueError):
    """A message was rejected. The text says why."""


# --------------------------------------------------------------------------
# Field helpers
# --------------------------------------------------------------------------

def _require(data: dict, field: str, where: str = ""):
    if field not in data:
        raise ParseError(f"missing required field {where}{field!r}")
    return data[field]


def _require_str(data: dict, field: str, where: str = "") -> str:
    value = _require(data, field, where)
    if not isinstance(value, str) or not value:
        raise ParseError(f"field {where}{field!r} must be a non-empty string")
    return value


def _optional_str(data: dict, field: str, where: str = "") -> str | None:
    value = data.get(field)
    if value is not None and not isinstance(value, str):
        raise ParseError(f"field {where}{field!r} must be a string")
    return value


def _require_bool(data: dict, field: str, where: str = "") -> bool:
    value = _require(data, field, where)
    # isinstance(True, int) is True but isinstance(1, bool) is False, so
    # checking for bool is the strict direction: 0/1 are rejected.
    if not isinstance(value, bool):
        raise ParseError(f"field {where}{field!r} must be true or false")
    return value


def _require_id(value: str, what: str) -> str:
    if not _ID.match(value):
        raise ParseError(f"{what} {value!r} must match {_ID.pattern}")
    return value


def _timestamp(value: str, field: str) -> str:
    try:
        return normalize_timestamp(value)
    except ValueError as exc:
        raise ParseError(f"field {field!r} is not a valid UTC timestamp ({exc})") from None


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def parse_strict(topic: str, payload: bytes | str) -> Message:
    """Validate one message. Raises ParseError if it is unusable."""
    levels = topic.split("/")
    if len(levels) != 5 or levels[:2] != ["studyspace", "v1"]:
        raise ParseError("topic is not studyspace/v1/{site}/{node_id}/{status|data}")
    _, _, site, topic_node, kind = levels
    _require_id(site, "site")
    _require_id(topic_node, "topic node id")
    if kind not in ("status", "data"):
        raise ParseError(f"unknown topic kind {kind!r}")

    # An empty payload is how a retained message is cleared on the broker.
    if not payload:
        raise ParseError("empty payload (cleared retained message)")
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParseError(f"invalid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ParseError("payload must be a JSON object")

    node_id = _require_str(data, "id")
    if node_id != topic_node:
        raise ParseError(f"payload id {node_id!r} does not match topic node id {topic_node!r}")

    if kind == "status":
        return _parse_status(site, node_id, data)
    return _parse_data(site, node_id, data)


def _parse_status(site: str, node_id: str, data: dict) -> Message:
    msg_type = data.get("type")
    if msg_type == "death":
        return NodeDeath(site=site, node_id=node_id)
    if msg_type != "birth":
        raise ParseError(f"unknown status type {msg_type!r}")

    timestamp = _timestamp(_require_str(data, "timestamp"), "timestamp")
    raw_devices = _require(data, "devices")
    if not isinstance(raw_devices, list):
        raise ParseError("field 'devices' must be a list")
    devices = []
    for i, entry in enumerate(raw_devices):
        where = f"devices[{i}]."
        if not isinstance(entry, dict):
            raise ParseError(f"devices[{i}] must be an object")
        devices.append(DeviceInfo(
            id=_require_id(_require_str(entry, "id", where), "device id"),
            space_id=_require_id(_require_str(entry, "space_id", where), "space id"),
            device_type=_optional_str(entry, "device_type", where),
            firmware=_optional_str(entry, "firmware", where),
        ))
    return NodeBirth(site=site, node_id=node_id, timestamp=timestamp, devices=tuple(devices))


def _parse_data(site: str, node_id: str, data: dict) -> NodeData:
    timestamp = _timestamp(_require_str(data, "timestamp"), "timestamp")

    # "changed" is informational only (we diff against the database), but if
    # it's there it must have the right shape.
    changed = data.get("changed", [])
    if not isinstance(changed, list) or not all(isinstance(c, str) for c in changed):
        raise ParseError("field 'changed' must be a list of strings")

    raw_devices = _require(data, "devices")
    if not isinstance(raw_devices, dict):
        raise ParseError("field 'devices' must be an object")
    devices: dict[str, DeviceReport] = {}
    for dev_id, entry in raw_devices.items():
        _require_id(dev_id, "device id")
        where = f"devices.{dev_id}."
        if not isinstance(entry, dict):
            raise ParseError(f"devices.{dev_id} must be an object")
        if not _require_bool(entry, "online", where):
            devices[dev_id] = DeviceReport(online=False)
            continue
        since = entry.get("since")
        if since is not None:
            if not isinstance(since, str):
                raise ParseError(f"field {where}'since' must be a string")
            since = _timestamp(since, f"{where}since")
        devices[dev_id] = DeviceReport(
            online=True, occupied=_require_bool(entry, "occupied", where), since=since
        )
    return NodeData(
        site=site, node_id=node_id, timestamp=timestamp,
        changed=tuple(changed), devices=devices,
    )


def parse(topic: str, payload: bytes | str) -> Message | None:
    """Like parse_strict(), but logs and returns None instead of raising.

    One bad message must never take the host down.
    """
    try:
        return parse_strict(topic, payload)
    except ParseError as exc:
        log.warning("rejected message on %s: %s", topic, exc)
        return None
