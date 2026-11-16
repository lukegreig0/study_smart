import json
import logging

import pytest

from host.messages import (
    DeviceReport, NodeBirth, NodeData, NodeDeath, ParseError, normalize_timestamp,
    parse, parse_strict,
)

from .conftest import DATA, NODE, OFF, SITE, STATUS, T0, birth, data, death, encode, on


def test_birth():
    m = parse_strict(STATUS, encode(birth()))
    assert isinstance(m, NodeBirth)
    assert (m.site, m.node_id, m.timestamp) == (SITE, NODE, T0)
    assert [d.id for d in m.devices] == ["placemat-0007", "placemat-0009"]
    assert m.devices[0].space_id == "desk-07"
    assert m.devices[0].firmware == "0.1.0"


def test_birth_with_no_devices():
    assert parse_strict(STATUS, encode(birth(devices=()))).devices == ()


def test_death():
    assert parse_strict(STATUS, encode(death())) == NodeDeath(SITE, NODE)


def test_data():
    m = parse_strict(DATA, encode(data({"placemat-0007": on(True), "placemat-0009": OFF})))
    assert isinstance(m, NodeData)
    assert m.devices["placemat-0007"] == DeviceReport(True, True, T0)
    assert m.devices["placemat-0007"].state == "occupied"
    assert m.devices["placemat-0009"].state == "unknown"
    assert m.changed == ("placemat-0007", "placemat-0009")
    assert not m.is_full_snapshot


def test_full_snapshot_and_free_state():
    m = parse_strict(DATA, encode(data({"placemat-0007": on(False)}, changed=[])))
    assert m.is_full_snapshot
    assert m.devices["placemat-0007"].state == "free"


def test_data_since_is_optional():
    m = parse_strict(DATA, encode(data({"placemat-0007": {"online": True, "occupied": True}})))
    assert m.devices["placemat-0007"].since is None


def test_timestamps_are_normalized():
    assert normalize_timestamp("2026-11-10T12:00:00.123456Z") == "2026-11-10T12:00:00Z"
    with pytest.raises(ValueError):
        normalize_timestamp("2026-11-10T12:00:00+02:00")


def test_parse_accepts_str_payload():
    assert parse(STATUS, json.dumps(death())) == NodeDeath(SITE, NODE)


def _with(base: dict, **changes) -> bytes:
    out = dict(base)
    for k, v in changes.items():
        if v is ...:
            del out[k]
        else:
            out[k] = v
    return encode(out)


REJECTED = [
    ("empty payload", STATUS, b""),
    ("invalid json", STATUS, b"{not json"),
    ("not utf-8", STATUS, b"\xff\xfe"),
    ("json array", STATUS, b"[1, 2]"),
    ("topic id mismatch", STATUS, encode(death("node-floor3"))),
    ("unknown status type", STATUS, _with(death(), type="rebirth")),
    ("missing type", STATUS, _with(death(), type=...)),
    ("missing id", STATUS, _with(death(), id=...)),
    ("birth missing timestamp", STATUS, _with(birth(), timestamp=...)),
    ("birth missing devices", STATUS, _with(birth(), devices=...)),
    ("birth devices not a list", STATUS, _with(birth(), devices={})),
    ("birth device missing space_id", STATUS,
     _with(birth(), devices=[{"id": "placemat-0007"}])),
    ("birth bad device id", STATUS,
     _with(birth(), devices=[{"id": "Placemat_7", "space_id": "desk-07"}])),
    ("bad timestamp", STATUS, _with(birth(), timestamp="yesterday")),
    ("non-utc timestamp", STATUS, _with(birth(), timestamp="2026-11-10T12:00:00+01:00")),
    ("data missing devices", DATA, _with(data({}), devices=...)),
    ("data missing timestamp", DATA, _with(data({}), timestamp=...)),
    ("data devices not an object", DATA, _with(data({}), devices=[])),
    ("data changed not a list", DATA, _with(data({}), changed="placemat-0007")),
    ("data entry missing online", DATA, encode(data({"placemat-0007": {"occupied": True}}))),
    ("data online not bool", DATA, encode(data({"placemat-0007": {"online": 1}}))),
    ("data online missing occupied", DATA, encode(data({"placemat-0007": {"online": True}}))),
    ("data bad since", DATA, encode(data({"placemat-0007": on(True, since="soon")}))),
    ("data bad device id", DATA, encode(data({"PLACEMAT 7": OFF}))),
    ("bad node id in topic", "studyspace/v1/library/Node_2/status", encode(death("Node_2"))),
    ("bad site in topic", "studyspace/v1/Lib/node-floor2/status", encode(death())),
    ("unknown topic kind", "studyspace/v1/library/node-floor2/cmd", encode(death())),
    ("too few levels", "studyspace/v1/node-floor2/status", encode(death())),
    ("wrong prefix", "other/v1/library/node-floor2/status", encode(death())),
]


@pytest.mark.parametrize("topic,payload", [r[1:] for r in REJECTED], ids=[r[0] for r in REJECTED])
def test_rejected(topic, payload, caplog):
    with pytest.raises(ParseError):
        parse_strict(topic, payload)
    # The non-raising wrapper logs and returns None: it never crashes.
    with caplog.at_level(logging.WARNING):
        assert parse(topic, payload) is None
    assert "rejected" in caplog.text
