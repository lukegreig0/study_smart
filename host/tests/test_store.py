import sqlite3

import pytest

from host.messages import DeviceInfo
from host.store import Store

from .conftest import NODE, T0

D7 = DeviceInfo("placemat-0007", "desk-07", "placemat", "0.1.0")


def tables(store):
    rows = store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def test_schema_created(store):
    assert {"nodes", "devices", "occupancy_events"} <= tables(store)
    assert store.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_connect_twice_keeps_data(tmp_path):
    path = str(tmp_path / "x.db")
    s = Store.connect(path)
    with s.transaction():
        s.upsert_node(NODE, "library", True, T0)
    s.close()
    s = Store.connect(path)  # CREATE TABLE IF NOT EXISTS: no error, data kept
    assert s.get_node(NODE).online
    s.close()


def test_wal_for_file_database(tmp_path):
    s = Store.connect(str(tmp_path / "studyspace.db"))
    assert s.journal_mode() == "wal"
    s.close()


def test_upsert_node(store):
    store.upsert_node(NODE, "library", True, T0)
    store.upsert_node(NODE, "library", False, "2026-11-10T12:05:00Z")
    node = store.get_node(NODE)
    assert (node.online, node.last_seen) == (False, "2026-11-10T12:05:00Z")
    assert store.get_node("nope") is None


def test_upsert_devices_new_and_existing(store):
    store.upsert_node(NODE, "library", True, T0)
    store.upsert_devices(NODE, [D7])
    dev = store.get_device("placemat-0007")
    assert (dev.state, dev.since, dev.space_id) == ("unknown", None, "desk-07")
    assert store.list_events() == []  # new devices get no event

    store.set_device_state("placemat-0007", "occupied", T0)
    store.upsert_devices(NODE, [DeviceInfo("placemat-0007", "desk-08", "placemat", "0.2.0")])
    dev = store.get_device("placemat-0007")
    # Metadata refreshed, state untouched.
    assert (dev.space_id, dev.firmware, dev.state, dev.since) == ("desk-08", "0.2.0", "occupied", T0)


def test_device_needs_existing_node(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_devices("ghost-node", [D7])


def test_state_check_constraint(store):
    store.upsert_node(NODE, "library", True, T0)
    store.upsert_devices(NODE, [D7])
    with pytest.raises(sqlite3.IntegrityError):
        store.set_device_state("placemat-0007", "maybe", T0)


def test_duplicate_event_is_a_noop(store):
    store.upsert_node(NODE, "library", True, T0)
    store.upsert_devices(NODE, [D7])
    assert store.set_device_state("placemat-0007", "occupied", T0) is True
    assert store.set_device_state("placemat-0007", "occupied", T0) is False
    assert len(store.list_events("placemat-0007")) == 1


def test_mark_node_dead(store):
    store.upsert_node(NODE, "library", True, T0)
    store.upsert_devices(NODE, [D7, DeviceInfo("placemat-0009", "desk-09", None, None)])
    store.set_device_state("placemat-0007", "occupied", T0)
    later = "2026-11-10T12:10:00Z"
    assert store.mark_node_dead(NODE, later) == ["placemat-0007"]  # 0009 already unknown
    assert not store.get_node(NODE).online
    assert store.get_device("placemat-0007").since == later
    assert store.mark_node_dead(NODE, later) == []


def test_transaction_rolls_back(store):
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction():
            store.upsert_node(NODE, "library", True, T0)
            store.upsert_devices("ghost-node", [D7])
    assert store.get_node(NODE) is None


def test_readonly_connect(tmp_path):
    path = str(tmp_path / "ro.db")
    with pytest.raises(sqlite3.OperationalError):
        Store.connect(path, readonly=True)  # doesn't create the file
    Store.connect(path).close()
    ro = Store.connect(path, readonly=True)
    assert ro.list_devices() == []
    with pytest.raises(sqlite3.OperationalError):
        ro.upsert_node(NODE, "library", True, T0)
    ro.close()


def test_show_prints_table(tmp_path, capsys):
    from host import show

    path = str(tmp_path / "show.db")
    s = Store.connect(path)
    with s.transaction():
        s.upsert_node(NODE, "library", True, T0)
        s.upsert_devices(NODE, [D7])
        s.set_device_state("placemat-0007", "occupied", T0)
    s.close()

    assert show.main([path]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["space", "device", "node", "state", "since"]
    assert out[2].split() == ["desk-07", "placemat-0007", NODE, "occupied", T0]
