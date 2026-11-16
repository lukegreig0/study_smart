"""All SQL lives here. Nothing else in the host writes a query.

Transactions are the caller's job: ingest.py wraps each message in
`with store.transaction():` so a message is applied completely or not at all.
The methods here never commit on their own.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from .messages import DeviceInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
  node_id   TEXT PRIMARY KEY,
  site      TEXT NOT NULL,
  online    INTEGER NOT NULL,
  last_seen TEXT
);
CREATE TABLE IF NOT EXISTS devices (
  device_id   TEXT PRIMARY KEY,
  node_id     TEXT NOT NULL REFERENCES nodes(node_id),
  space_id    TEXT NOT NULL,
  device_type TEXT,
  firmware    TEXT,
  state       TEXT NOT NULL CHECK (state IN ('free','occupied','unknown')),
  since       TEXT
);
CREATE TABLE IF NOT EXISTS occupancy_events (
  id         INTEGER PRIMARY KEY,
  device_id  TEXT NOT NULL REFERENCES devices(device_id),
  state      TEXT NOT NULL CHECK (state IN ('free','occupied','unknown')),
  changed_at TEXT NOT NULL,
  UNIQUE (device_id, state, changed_at)
);
"""


@dataclass(frozen=True)
class Node:
    node_id: str
    site: str
    online: bool
    last_seen: str | None


@dataclass(frozen=True)
class Device:
    device_id: str
    node_id: str
    space_id: str
    device_type: str | None
    firmware: str | None
    state: str
    since: str | None


class Store:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------ lifecycle

    @classmethod
    def connect(cls, path: str, readonly: bool = False) -> Store:
        """Open the database, creating the schema if needed.

        sqlite3 connections are tied to the thread that created them
        (check_same_thread=True is the default and we keep it): a shared
        connection used from two threads can interleave statements of
        different transactions. The host therefore creates and uses its
        connection only on the main thread; paho's network thread never sees
        it (see mqtt_host.py).
        """
        if readonly:
            # mode=ro fails if the file doesn't exist instead of creating it.
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        else:
            conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        # Foreign keys are off by default in SQLite, per connection.
        conn.execute("PRAGMA foreign_keys=ON")
        if not readonly:
            # WAL lets readers (the future API, `python -m host.show`) read a
            # consistent snapshot while we write, instead of being blocked.
            # It's a property of the file, so it sticks once set. In-memory
            # databases can't use WAL and just report 'memory'.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
        return cls(conn)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """`with conn:` commits on success and rolls back on an exception.
        (It doesn't close the connection, despite how it looks.)"""
        with self.conn:
            yield

    def journal_mode(self) -> str:
        return self.conn.execute("PRAGMA journal_mode").fetchone()[0]

    # --------------------------------------------------------------- nodes

    def get_node(self, node_id: str) -> Node | None:
        row = self.conn.execute(
            "SELECT node_id, site, online, last_seen FROM nodes WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        if row is None:
            return None
        return Node(row["node_id"], row["site"], bool(row["online"]), row["last_seen"])

    def upsert_node(self, node_id: str, site: str, online: bool, last_seen: str) -> None:
        self.conn.execute(
            """
            INSERT INTO nodes (node_id, site, online, last_seen) VALUES (?, ?, ?, ?)
            ON CONFLICT (node_id) DO UPDATE SET
              site = excluded.site, online = excluded.online, last_seen = excluded.last_seen
            """,
            (node_id, site, int(online), last_seen),
        )

    def touch_node(self, node_id: str, last_seen: str) -> None:
        """We heard from a node: it's online and this is when."""
        self.conn.execute(
            "UPDATE nodes SET online = 1, last_seen = ? WHERE node_id = ?",
            (last_seen, node_id),
        )

    def list_online_nodes(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT node_id FROM nodes WHERE online = 1 ORDER BY node_id"
        ).fetchall()
        return [r["node_id"] for r in rows]

    def mark_node_dead(self, node_id: str, now: str) -> list[str]:
        """Node offline; each of its devices that isn't already 'unknown'
        becomes 'unknown' (since=now) with an event row.

        Returns the device ids that changed. Calling it again changes nothing.
        """
        self.conn.execute("UPDATE nodes SET online = 0 WHERE node_id = ?", (node_id,))
        rows = self.conn.execute(
            "SELECT device_id FROM devices WHERE node_id = ? AND state != 'unknown'"
            " ORDER BY device_id",
            (node_id,),
        ).fetchall()
        changed = [r["device_id"] for r in rows]
        for device_id in changed:
            self.set_device_state(device_id, "unknown", now)
        return changed

    # ------------------------------------------------------------- devices

    def upsert_devices(self, node_id: str, devices: Iterable[DeviceInfo]) -> None:
        """Insert new devices as 'unknown' (since NULL, no event), or refresh
        the metadata of known ones. Never touches state/since of existing
        devices: the node's data messages own those."""
        self.conn.executemany(
            """
            INSERT INTO devices (device_id, node_id, space_id, device_type, firmware, state, since)
            VALUES (?, ?, ?, ?, ?, 'unknown', NULL)
            ON CONFLICT (device_id) DO UPDATE SET
              node_id = excluded.node_id, space_id = excluded.space_id,
              device_type = excluded.device_type, firmware = excluded.firmware
            """,
            [(d.id, node_id, d.space_id, d.device_type, d.firmware) for d in devices],
        )

    def get_device(self, device_id: str) -> Device | None:
        row = self.conn.execute(
            "SELECT * FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        return None if row is None else Device(**dict(row))

    def set_device_state(self, device_id: str, state: str, since: str) -> bool:
        """Set the current state and record it in the history.

        INSERT OR IGNORE + the UNIQUE (device_id, state, changed_at)
        constraint make a repeated event a no-op instead of a duplicate row.
        Returns True if a new event row was written.
        """
        self.conn.execute(
            "UPDATE devices SET state = ?, since = ? WHERE device_id = ?",
            (state, since, device_id),
        )
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO occupancy_events (device_id, state, changed_at)"
            " VALUES (?, ?, ?)",
            (device_id, state, since),
        )
        return cur.rowcount == 1

    def list_devices(self) -> list[Device]:
        rows = self.conn.execute(
            "SELECT * FROM devices ORDER BY space_id, device_id"
        ).fetchall()
        return [Device(**dict(r)) for r in rows]

    def list_events(self, device_id: str | None = None) -> list[sqlite3.Row]:
        if device_id is None:
            return self.conn.execute("SELECT * FROM occupancy_events ORDER BY id").fetchall()
        return self.conn.execute(
            "SELECT * FROM occupancy_events WHERE device_id = ? ORDER BY id", (device_id,)
        ).fetchall()
