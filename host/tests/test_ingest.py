import pytest

from host.ingest import Ingest

from .conftest import NODE, OFF, birth, data, death, msg, on

T1 = "2026-11-10T12:01:00Z"
T2 = "2026-11-10T12:02:00Z"
T3 = "2026-11-10T12:03:00Z"
HOST_NOW = "2026-11-10T12:30:00Z"
D7, D9 = "placemat-0007", "placemat-0009"


def state(store, device_id):
    d = store.get_device(device_id)
    return d.state, d.since


def events(store, device_id=None):
    return [(e["device_id"], e["state"], e["changed_at"]) for e in store.list_events(device_id)]


def apply(ingest, payload, now_utc=HOST_NOW, now_mono=0.0, node_id=NODE):
    ingest.apply(msg(payload, node_id), now_utc, now_mono)


@pytest.fixture
def born(ingest):
    """Birth + first full snapshot: 0007 occupied since T1, 0009 free since T0."""
    apply(ingest, birth())
    apply(ingest, data({D7: on(True, T1), D9: on(False)}, timestamp=T1, changed=[]))
    return ingest


def test_birth_creates_node_and_unknown_devices(ingest, store):
    apply(ingest, birth(), now_utc=HOST_NOW)
    node = store.get_node(NODE)
    assert (node.online, node.last_seen, node.site) == (True, HOST_NOW, "library")
    assert state(store, D7) == ("unknown", None)
    assert events(store) == []


def test_birth_then_snapshot_sets_states(born, store):
    assert state(store, D7) == ("occupied", T1)
    assert state(store, D9) == ("free", "2026-11-10T12:00:00Z")
    assert events(store) == [(D7, "occupied", T1), (D9, "free", "2026-11-10T12:00:00Z")]


def test_same_snapshot_twice_adds_nothing(born, store):
    before = events(store)
    apply(born, data({D7: on(True, T1), D9: on(False)}, timestamp=T1, changed=[]))
    assert events(store) == before


def test_rebirth_does_not_touch_state(born, store):
    apply(born, birth(timestamp=T2))
    assert state(store, D7) == ("occupied", T1)


def test_delta_only_touches_listed_devices(born, store):
    apply(born, data({D9: on(True, T2)}, timestamp=T2))
    assert state(store, D9) == ("occupied", T2)
    assert state(store, D7) == ("occupied", T1)


def test_refresh_corrects_missed_change(born, store):
    # We missed the delta where 0007 became free; the next full snapshot
    # says so with "changed": []. The diff catches it.
    apply(born, data({D7: on(False, T2), D9: on(False)}, timestamp=T3, changed=[]))
    assert state(store, D7) == ("free", T2)
    assert events(store, D7) == [(D7, "occupied", T1), (D7, "free", T2)]
    assert len(events(store, D9)) == 1


def test_changed_list_is_not_trusted(born, store):
    # "changed" lies (names nobody); the diff still applies the change.
    apply(born, data({D7: on(False, T2)}, timestamp=T2, changed=[D9]))
    assert state(store, D7) == ("free", T2)


def test_stale_snapshot_ignored(born, store):
    apply(born, data({D7: on(False, T2)}, timestamp=T2))
    # An older message arrives late (out of order): since and timestamp < stored since.
    apply(born, data({D7: on(True, T1)}, timestamp=T1))
    assert state(store, D7) == ("free", T2)
    assert len(events(store, D7)) == 2


def test_offline_becomes_unknown(born, store):
    apply(born, data({D7: OFF}, timestamp=T2))
    assert state(store, D7) == ("unknown", T2)  # no "since": snapshot timestamp
    assert events(store, D7)[-1] == (D7, "unknown", T2)


def test_back_online_with_old_since_uses_message_time(born, store):
    apply(born, data({D7: OFF}, timestamp=T2))
    # Back online, still occupied since T1 (before it went offline at T2).
    apply(born, data({D7: on(True, T1)}, timestamp=T3))
    assert state(store, D7) == ("occupied", T3)
    assert events(store, D7) == [(D7, "occupied", T1), (D7, "unknown", T2), (D7, "occupied", T3)]


def test_since_missing_uses_message_time(born, store):
    apply(born, data({D9: {"online": True, "occupied": True}}, timestamp=T2))
    assert state(store, D9) == ("occupied", T2)


def test_node_death_only_affects_that_node(born, store):
    apply(born, birth(node_id="node-floor3", devices=["placemat-0031"]), node_id="node-floor3")
    apply(born, data({"placemat-0031": on(True, T1)}, node_id="node-floor3", timestamp=T1),
          node_id="node-floor3")

    apply(born, death(), now_utc=HOST_NOW)
    assert not store.get_node(NODE).online
    assert state(store, D7) == ("unknown", HOST_NOW)
    assert state(store, D9) == ("unknown", HOST_NOW)
    assert state(store, "placemat-0031") == ("occupied", T1)
    assert store.get_node("node-floor3").online

    n = len(events(store))
    apply(born, death(), now_utc="2026-11-10T12:31:00Z")  # retained replay
    assert len(events(store)) == n


def test_death_of_unknown_node(ingest, store):
    apply(ingest, death("node-ghost"), node_id="node-ghost")
    assert store.get_node("node-ghost") is None


def test_data_after_death_ignored_until_birth(born, store):
    # Retained replay order on startup for a dead node: death, then its last data.
    apply(born, death())
    apply(born, data({D7: on(False, T3)}, timestamp=T3))
    assert state(store, D7) == ("unknown", HOST_NOW)
    assert not store.get_node(NODE).online

    # Node comes back: birth then a full snapshot.
    later = "2026-11-10T12:40:00Z"
    apply(born, birth(timestamp=later))
    apply(born, data({D7: on(False, later), D9: on(False, later)}, timestamp=later, changed=[]))
    assert state(store, D7) == ("free", later)


def test_device_without_birth_is_skipped(born, store):
    apply(born, data({"placemat-0099": on(True), D7: on(False, T2)}, timestamp=T2))
    assert store.get_device("placemat-0099") is None
    assert state(store, D7) == ("free", T2)  # the rest of the message still applies


def test_data_from_unknown_node_is_skipped(ingest, store):
    apply(ingest, data({D7: on(True)}))
    assert store.get_node(NODE) is None


def test_device_reported_by_other_node_is_skipped(born, store):
    apply(born, birth(node_id="node-floor3", devices=[]), node_id="node-floor3")
    apply(born, data({D7: on(False, T2)}, node_id="node-floor3", timestamp=T2),
          node_id="node-floor3")
    assert state(store, D7) == ("occupied", T1)


def test_startup_replay_adds_no_events(tmp_path):
    from host.store import Store

    path = str(tmp_path / "s.db")
    first = Store.connect(path)
    run1 = Ingest(first)
    snapshot = data({D7: on(True, T1), D9: on(False)}, timestamp=T1, changed=[])
    run1.apply(msg(birth()), HOST_NOW, 0.0)
    run1.apply(msg(snapshot), HOST_NOW, 0.0)
    before = [tuple(r) for r in first.list_events()]
    first.close()

    # Host restarts; the broker replays the retained birth and data.
    second = Store.connect(path)
    run2 = Ingest(second)
    run2.apply(msg(birth()), "2026-11-10T13:00:00Z", 0.0)
    run2.apply(msg(snapshot), "2026-11-10T13:00:00Z", 0.0)
    assert [tuple(r) for r in second.list_events()] == before
    assert state(second, D7) == ("occupied", T1)
    second.close()


# ----------------------------------------------------------------- timeouts

def test_node_timeout_boundary(store):
    ing = Ingest(store, node_timeout_s=90)
    ing.apply(msg(birth()), HOST_NOW, 1000.0)
    ing.apply(msg(data({D7: on(True, T1)}, timestamp=T1)), HOST_NOW, 1010.0)

    assert ing.check_node_timeouts(HOST_NOW, 1100.0) == []  # exactly 90 s: not yet
    assert store.get_node(NODE).online
    assert ing.check_node_timeouts("2026-11-10T12:32:00Z", 1100.5) == [NODE]
    assert not store.get_node(NODE).online
    assert state(store, D7) == ("unknown", "2026-11-10T12:32:00Z")

    n = len(events(store))
    assert ing.check_node_timeouts(HOST_NOW, 2000.0) == []  # dead exactly once
    assert len(events(store)) == n


def test_message_resets_timeout(store):
    ing = Ingest(store, node_timeout_s=90)
    ing.apply(msg(birth()), HOST_NOW, 0.0)
    ing.apply(msg(data({D7: on(True, T1)}, timestamp=T1)), HOST_NOW, 80.0)
    assert ing.check_node_timeouts(HOST_NOW, 150.0) == []
    assert ing.check_node_timeouts(HOST_NOW, 170.5) == [NODE]


def test_timeout_clock_starts_for_nodes_online_at_startup(store):
    Ingest(store).apply(msg(birth()), HOST_NOW, 0.0)  # previous run
    ing = Ingest(store, node_timeout_s=90)  # restart: no monotonic history
    assert ing.check_node_timeouts(HOST_NOW, 5000.0) == []  # timer starts now
    assert ing.check_node_timeouts(HOST_NOW, 5090.5) == [NODE]


def test_timeout_disabled(store):
    ing = Ingest(store, node_timeout_s=0)
    ing.apply(msg(birth()), HOST_NOW, 0.0)
    assert ing.check_node_timeouts(HOST_NOW, 1e9) == []
    assert store.get_node(NODE).online
