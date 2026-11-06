"""Local protocol between the node and its touchscreen: newline-delimited JSON.

The GUI only displays what the node reports and asks the node to do things. All
decisions stay in the node, which can refuse a command.

Node to GUI::

    {"type": "snapshot", "node_id": ..., "desk_id": ..., "mqtt_connected": bool,
     "poll_interval_s": float,
     "seats": [{"space_id", "label", "position_deg", "state", "source", "healthy",
                "hold_remaining_s"}]}
    {"type": "result", "id": int, "ok": bool, "error": str | null}

GUI to node::

    {"type": "command", "id": int, "command": "mark_occupied" | "mark_vacant",
     "space_id": str}
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MARK_OCCUPIED = "mark_occupied"
MARK_VACANT = "mark_vacant"
_COMMANDS = frozenset({MARK_OCCUPIED, MARK_VACANT})

MAX_LINE_BYTES = 64 * 1024


class ProtocolError(ValueError):
    """A line does not conform to the UI protocol."""


@dataclass(frozen=True)
class Command:
    id: int
    command: str
    space_id: str

    def to_message(self) -> dict[str, Any]:
        return {"type": "command", "id": self.id, "command": self.command, "space_id": self.space_id}


def encode(message: dict[str, Any]) -> bytes:
    return json.dumps(message).encode() + b"\n"


def decode(line: bytes) -> dict[str, Any]:
    try:
        message = json.loads(line)
    except ValueError as exc:
        raise ProtocolError(f"not valid JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("message must be a JSON object")
    return message


def parse_command(message: dict[str, Any]) -> Command:
    if message.get("type") != "command":
        raise ProtocolError(f"expected a command, got type {message.get('type')!r}")
    command_id = message.get("id")
    if type(command_id) is not int:
        raise ProtocolError(f"command id must be an integer, got {command_id!r}")
    command = message.get("command")
    if command not in _COMMANDS:
        raise ProtocolError(f"unknown command {command!r}")
    space_id = message.get("space_id")
    if not isinstance(space_id, str) or not space_id:
        raise ProtocolError(f"space_id must be a non-empty string, got {space_id!r}")
    return Command(command_id, command, space_id)


def result_message(command_id: int | None, error: str | None = None) -> dict[str, Any]:
    return {"type": "result", "id": command_id, "ok": error is None, "error": error}
