"""Shared test helpers. No broker, no sleeping: time is a value we pass in."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make `import host` work when pytest is run from host/ (or anywhere).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.ingest import Ingest  # noqa: E402
from host.messages import parse_strict  # noqa: E402
from host.store import Store  # noqa: E402

SITE = "library"
NODE = "node-floor2"
T0 = "2026-11-10T12:00:00Z"
STATUS = f"studyspace/v1/{SITE}/{NODE}/status"
DATA = f"studyspace/v1/{SITE}/{NODE}/data"


def birth(node_id=NODE, devices=("placemat-0007", "placemat-0009"), timestamp=T0) -> dict:
    return {
        "type": "birth", "id": node_id, "timestamp": timestamp,
        "devices": [
            {"id": d, "space_id": "desk-" + d[-2:], "device_type": "placemat", "firmware": "0.1.0"}
            for d in devices
        ],
    }


def death(node_id=NODE) -> dict:
    return {"type": "death", "id": node_id}


def data(devices: dict, node_id=NODE, timestamp=T0, changed=None) -> dict:
    return {"id": node_id, "timestamp": timestamp,
            "changed": list(devices) if changed is None else changed, "devices": devices}


def on(occupied: bool, since: str = T0) -> dict:
    return {"online": True, "occupied": occupied, "since": since}


OFF = {"online": False}


def encode(msg: dict) -> bytes:
    return json.dumps(msg).encode()


def msg(payload: dict, node_id: str = NODE):
    """Parse a payload dict as if it arrived on the right topic."""
    kind = "status" if "type" in payload else "data"
    return parse_strict(f"studyspace/v1/{SITE}/{node_id}/{kind}", encode(payload))


@pytest.fixture
def store():
    s = Store.connect(":memory:")
    yield s
    s.close()


@pytest.fixture
def ingest(store) -> Ingest:
    return Ingest(store, node_timeout_s=90)
