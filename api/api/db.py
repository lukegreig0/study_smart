"""All SQL lives here, and all of it is read-only.

The host is the only writer of the database (see host/host/store.py, which
owns the schema). The API opens the same file in SQLite's read-only mode, so
even a bug in here can't change the data.

One connection per request
--------------------------
FastAPI runs plain `def` endpoints on a pool of worker threads, so two
requests can run at the same time on different threads. A sqlite3
connection mustn't be shared between threads (same reason as in the host), so
every request opens its own connection and closes it at the end. For a local
SQLite file that costs well under a millisecond.

Because the host uses WAL mode, our readers never block its writes and see a
consistent snapshot even while it's writing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

STATES = ("free", "occupied", "unknown")


class DatabaseUnavailable(Exception):
    """The database file isn't there yet (the host hasn't started) or has no
    tables yet. Reported as 503 Service Unavailable: try again later."""


class Database:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if not self.path.is_file():
            raise DatabaseUnavailable(f"database {self.path} does not exist yet")
        # mode=ro: read-only, and never create the file. as_uri() also takes
        # care of escaping odd characters in the path.
        uri = self.path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                raise DatabaseUnavailable("database has no tables yet") from exc
            raise
        finally:
            conn.close()


# --------------------------------------------------------------------------
# Queries. Each takes an open connection and returns plain dicts, which
# FastAPI turns into JSON.
# --------------------------------------------------------------------------

_DEVICE_COLUMNS = """
    d.device_id, d.space_id, d.node_id, d.device_type, d.firmware,
    d.state, d.since, n.online AS node_online
"""


def _device(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["node_online"] = bool(out["node_online"])
    return out


def list_devices(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        f"SELECT {_DEVICE_COLUMNS} FROM devices d JOIN nodes n USING (node_id)"
        " ORDER BY d.space_id, d.device_id"
    ).fetchall()
    return [_device(r) for r in rows]


def get_device(conn: sqlite3.Connection, device_id: str) -> dict | None:
    row = conn.execute(
        f"SELECT {_DEVICE_COLUMNS} FROM devices d JOIN nodes n USING (node_id)"
        " WHERE d.device_id = ?",
        (device_id,),
    ).fetchone()
    return None if row is None else _device(row)


def device_history(
    conn: sqlite3.Connection, device_id: str, since: str | None, limit: int
) -> list[dict]:
    """A device's state changes, newest first.

    `since` is compared as a string; that works because the host stores
    every timestamp in the same fixed-width 'YYYY-MM-DDTHH:MM:SSZ' form, and
    the API normalises `since` to that form before calling this.
    """
    sql = "SELECT state, changed_at FROM occupancy_events WHERE device_id = ?"
    params: list = [device_id]
    if since is not None:
        sql += " AND changed_at >= ?"
        params.append(since)
    sql += " ORDER BY changed_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def recent_events(conn: sqlite3.Connection, limit: int) -> list[dict]:
    """The latest state changes across every device, newest first."""
    rows = conn.execute(
        """
        SELECT e.device_id, d.space_id, e.state, e.changed_at
        FROM occupancy_events e JOIN devices d USING (device_id)
        ORDER BY e.changed_at DESC, e.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_nodes(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT n.node_id, n.site, n.online, n.last_seen,
               (SELECT COUNT(*) FROM devices d WHERE d.node_id = n.node_id) AS device_count
        FROM nodes n
        ORDER BY n.node_id
        """
    ).fetchall()
    return [{**dict(r), "online": bool(r["online"])} for r in rows]


def state_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many devices are in each state; every state is present, even at 0."""
    counts = dict.fromkeys(STATES, 0)
    for row in conn.execute("SELECT state, COUNT(*) AS n FROM devices GROUP BY state"):
        counts[row["state"]] = row["n"]
    return counts


def check(conn: sqlite3.Connection) -> None:
    """Cheap query that fails if the schema isn't there (used by /health)."""
    conn.execute("SELECT 1 FROM devices LIMIT 1").fetchall()
