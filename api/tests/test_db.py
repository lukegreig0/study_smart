import sqlite3

import pytest

from api import db

from .conftest import make_db


@pytest.fixture
def conn(db_path):
    with db.Database(str(db_path)).connect() as c:
        yield c


def test_list_devices_sorted_by_space(conn):
    devices = db.list_devices(conn)
    assert [d["space_id"] for d in devices] == ["desk-07", "desk-08", "desk-31", "desk-99"]
    assert devices[0]["node_online"] is True
    assert devices[2]["node_online"] is False


def test_get_device(conn):
    assert db.get_device(conn, "placemat-0008")["state"] == "free"
    assert db.get_device(conn, "nope") is None


def test_history_newest_first_and_since_filter(conn):
    hist = db.device_history(conn, "placemat-0007", None, 100)
    assert [h["state"] for h in hist] == ["occupied", "free"]
    hist = db.device_history(conn, "placemat-0007", "2026-11-16T12:01:00Z", 100)
    assert [h["state"] for h in hist] == ["occupied"]
    assert len(db.device_history(conn, "placemat-0007", None, 1)) == 1


def test_recent_events(conn):
    events = db.recent_events(conn, 3)
    assert [(e["device_id"], e["state"]) for e in events] == [
        ("placemat-0007", "occupied"), ("placemat-0008", "free"), ("placemat-0007", "free"),
    ]
    assert events[0]["space_id"] == "desk-07"


def test_nodes_with_device_count(conn):
    nodes = db.list_nodes(conn)
    assert [(n["node_id"], n["online"], n["device_count"]) for n in nodes] == [
        ("node-floor2", True, 3), ("node-floor3", False, 1),
    ]


def test_state_counts_include_zero(tmp_path):
    with db.Database(str(make_db(tmp_path / "empty.db", seed=False))).connect() as c:
        assert db.state_counts(c) == {"free": 0, "occupied": 0, "unknown": 0}


def test_connection_is_read_only(conn):
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM devices")


def test_missing_file_is_unavailable_and_not_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(db.DatabaseUnavailable):
        with db.Database(str(path)).connect():
            pass
    assert not path.exists()


def test_missing_tables_is_unavailable(tmp_path):
    path = tmp_path / "blank.db"
    sqlite3.connect(path).close()  # creates an empty file with no tables
    with pytest.raises(db.DatabaseUnavailable):
        with db.Database(str(path)).connect() as c:
            db.check(c)
