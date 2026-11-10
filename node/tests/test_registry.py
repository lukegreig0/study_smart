import pytest

from node.messages import Birth, Death, Heartbeat
from node.registry import Events, Registry

T0 = 1000.0
TIMEOUT = 6.0
TS0 = "2026-11-10T12:00:00Z"
TS1 = "2026-11-10T12:00:04Z"
TS2 = "2026-11-10T12:00:08Z"


def make_birth(device_id="placemat-0007", occupied=False, timestamp=TS0, firmware="0.1.0"):
    return Birth(device_id, "desk-07", "placemat", firmware, occupied, timestamp)


@pytest.fixture
def reg() -> Registry:
    r = Registry(TIMEOUT)
    r.handle(make_birth(), T0)
    return r


def test_birth_registers_device_and_requests_rebirth():
    r = Registry(TIMEOUT)
    events = r.handle(make_birth(occupied=True), T0)
    assert events == Events(changed=["placemat-0007"], rebirth=True)
    assert r.snapshot() == {"placemat-0007": {"online": True, "occupied": True, "since": TS0}}
    assert r.birth_devices() == [
        {"id": "placemat-0007", "space_id": "desk-07", "device_type": "placemat", "firmware": "0.1.0"}
    ]


def test_second_new_device_requests_rebirth(reg):
    assert reg.handle(make_birth("placemat-0009"), T0).rebirth is True
    assert [d["id"] for d in reg.birth_devices()] == ["placemat-0007", "placemat-0009"]


def test_heartbeat_without_occupied_emits_nothing(reg):
    assert not reg.handle(Heartbeat("placemat-0007", TS1), T0 + 2)


def test_heartbeat_with_changed_occupied_emits_change_and_updates_since(reg):
    events = reg.handle(Heartbeat("placemat-0007", TS1, occupied=True), T0 + 2)
    assert events == Events(changed=["placemat-0007"])
    assert reg.snapshot()["placemat-0007"] == {"online": True, "occupied": True, "since": TS1}


def test_heartbeat_with_same_occupied_emits_nothing(reg):
    assert not reg.handle(Heartbeat("placemat-0007", TS1, occupied=False), T0 + 2)
    assert reg.snapshot()["placemat-0007"]["since"] == TS0


def test_timeout_boundary_marks_offline_once(reg):
    assert reg.check_timeouts(T0 + TIMEOUT - 0.01) == []
    assert reg.check_timeouts(T0 + TIMEOUT + 0.01) == ["placemat-0007"]
    assert reg.check_timeouts(T0 + TIMEOUT + 5) == []  # not repeated
    assert reg.snapshot()["placemat-0007"] == {"online": False}


def test_heartbeat_resets_timeout(reg):
    reg.handle(Heartbeat("placemat-0007", TS1), T0 + 5)
    assert reg.check_timeouts(T0 + TIMEOUT + 1) == []


def test_heartbeat_after_timeout_brings_device_back(reg):
    reg.check_timeouts(T0 + TIMEOUT + 1)
    events = reg.handle(Heartbeat("placemat-0007", TS2), T0 + TIMEOUT + 2)
    assert events == Events(changed=["placemat-0007"])
    assert reg.snapshot()["placemat-0007"] == {"online": True, "occupied": False, "since": TS0}


def test_heartbeat_after_timeout_with_occupied_change_lists_device_once(reg):
    reg.check_timeouts(T0 + TIMEOUT + 1)
    events = reg.handle(Heartbeat("placemat-0007", TS2, occupied=True), T0 + TIMEOUT + 2)
    assert events.changed == ["placemat-0007"]


def test_death_marks_offline(reg):
    assert reg.handle(Death("placemat-0007"), T0 + 1) == Events(changed=["placemat-0007"])
    assert reg.snapshot() == {"placemat-0007": {"online": False}}
    # Still listed in the birth: known devices stay known.
    assert [d["id"] for d in reg.birth_devices()] == ["placemat-0007"]
    # A second death changes nothing.
    assert not reg.handle(Death("placemat-0007"), T0 + 2)


@pytest.mark.parametrize("msg", [Death("placemat-0099"), Heartbeat("placemat-0099", TS1, True)])
def test_message_from_unknown_device_is_ignored(reg, msg):
    assert not reg.handle(msg, T0 + 1)
    assert "placemat-0099" not in reg


def test_redelivered_retained_birth_is_ignored(reg):
    # Occupancy changed via heartbeat after boot...
    reg.handle(Heartbeat("placemat-0007", TS1, occupied=True), T0 + 2)
    # ...then we reconnect and the broker re-sends the old retained birth.
    assert not reg.handle(make_birth(occupied=False, timestamp=TS0), T0 + 3)
    assert reg.snapshot()["placemat-0007"]["occupied"] is True


def test_new_birth_from_known_device_updates_state(reg):
    reg.handle(Death("placemat-0007"), T0 + 1)
    events = reg.handle(make_birth(occupied=True, timestamp=TS2), T0 + 2)
    assert events == Events(changed=["placemat-0007"])
    assert reg.snapshot()["placemat-0007"] == {"online": True, "occupied": True, "since": TS2}


def test_new_birth_with_new_firmware_requests_rebirth(reg):
    events = reg.handle(make_birth(timestamp=TS2, firmware="0.2.0"), T0 + 2)
    assert events.rebirth is True
    assert reg.birth_devices()[0]["firmware"] == "0.2.0"


def test_snapshot_of_selected_ids(reg):
    reg.handle(make_birth("placemat-0009", occupied=True, timestamp=TS1), T0)
    assert reg.snapshot(["placemat-0009"]) == {
        "placemat-0009": {"online": True, "occupied": True, "since": TS1}
    }
    assert reg.snapshot([]) == {}
    assert reg.snapshot(["placemat-0099"]) == {}  # unknown ids are skipped


def test_snapshot_format_with_mixed_devices(reg):
    reg.handle(make_birth("placemat-0009", occupied=True, timestamp=TS1), T0 + 4)
    reg.check_timeouts(T0 + TIMEOUT + 1)  # 0007 times out, 0009 doesn't
    assert reg.snapshot() == {
        "placemat-0007": {"online": False},
        "placemat-0009": {"online": True, "occupied": True, "since": TS1},
    }
