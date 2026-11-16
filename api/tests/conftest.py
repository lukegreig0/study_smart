"""Shared test fixtures: a small, known database in a temp file.

The API opens the database read-only, so the tests build it with a separate,
writable connection first. A temp *file* (not ":memory:") because a read-only
connection has to open an existing file.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Make `import api` work when pytest is run from api/ (or anywhere).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.app import create_app  # noqa: E402
from api.config import Config  # noqa: E402

# A copy of the host's schema (host/host/store.py owns the real one). The API
# must not import the host package: they're separate services.
SCHEMA = """
CREATE TABLE nodes (node_id TEXT PRIMARY KEY, site TEXT NOT NULL,
                    online INTEGER NOT NULL, last_seen TEXT);
CREATE TABLE devices (device_id TEXT PRIMARY KEY,
                      node_id TEXT NOT NULL REFERENCES nodes(node_id),
                      space_id TEXT NOT NULL, device_type TEXT, firmware TEXT,
                      state TEXT NOT NULL CHECK (state IN ('free','occupied','unknown')),
                      since TEXT);
CREATE TABLE occupancy_events (id INTEGER PRIMARY KEY,
                      device_id TEXT NOT NULL REFERENCES devices(device_id),
                      state TEXT NOT NULL CHECK (state IN ('free','occupied','unknown')),
                      changed_at TEXT NOT NULL,
                      UNIQUE (device_id, state, changed_at));
"""

SEED = """
INSERT INTO nodes VALUES ('node-floor2', 'library', 1, '2026-11-16T12:10:00Z');
INSERT INTO nodes VALUES ('node-floor3', 'library', 0, '2026-11-16T11:00:00Z');

INSERT INTO devices VALUES ('placemat-0007', 'node-floor2', 'desk-07', 'placemat', '0.1.0', 'occupied', '2026-11-16T12:05:00Z');
INSERT INTO devices VALUES ('placemat-0008', 'node-floor2', 'desk-08', 'placemat', '0.1.0', 'free',     '2026-11-16T12:00:00Z');
INSERT INTO devices VALUES ('placemat-0031', 'node-floor3', 'desk-31', 'placemat', '0.1.0', 'unknown',  '2026-11-16T11:30:00Z');
INSERT INTO devices VALUES ('placemat-0099', 'node-floor2', 'desk-99', NULL, NULL, 'unknown', NULL);

INSERT INTO occupancy_events (device_id, state, changed_at) VALUES
  ('placemat-0007', 'free',     '2026-11-16T12:00:00Z'),
  ('placemat-0008', 'free',     '2026-11-16T12:00:00Z'),
  ('placemat-0031', 'occupied', '2026-11-16T11:00:00Z'),
  ('placemat-0031', 'unknown',  '2026-11-16T11:30:00Z'),
  ('placemat-0007', 'occupied', '2026-11-16T12:05:00Z');
"""


def make_db(path: Path, seed: bool = True) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA + (SEED if seed else ""))
    conn.close()
    return path


@pytest.fixture
def db_path(tmp_path) -> Path:
    return make_db(tmp_path / "studyspace.db")


@pytest.fixture
def client(db_path) -> TestClient:
    return TestClient(create_app(Config(db_path=str(db_path))))
