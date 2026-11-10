import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from conftest import TOPIC_7, TS, birth, death, encode, heartbeat
from node import messages
from node.messages import Birth, Death, Heartbeat, ParseError, parse

# ---------------------------------------------------------------- accepted


def test_parse_birth():
    msg = parse(TOPIC_7, encode(birth(occupied=True)))
    assert msg == Birth(
        id="placemat-0007", space_id="desk-07", device_type="placemat",
        firmware="0.1.0", occupied=True, timestamp=TS,
    )


def test_parse_heartbeat_without_occupied():
    assert parse(TOPIC_7, encode(heartbeat())) == Heartbeat("placemat-0007", TS, None)


def test_parse_heartbeat_with_occupied():
    assert parse(TOPIC_7, encode(heartbeat(occupied=False))) == Heartbeat(
        "placemat-0007", TS, False
    )


def test_parse_death():
    assert parse(TOPIC_7, encode(death())) == Death("placemat-0007")


def test_parse_accepts_str_payload():
    assert parse(TOPIC_7, json.dumps(death())) == Death("placemat-0007")


# ---------------------------------------------------------------- rejected


def _without(msg: dict, field: str) -> dict:
    return {k: v for k, v in msg.items() if k != field}


BAD = [
    pytest.param(TOPIC_7, b"", id="empty-payload"),
    pytest.param(TOPIC_7, b"{not json", id="invalid-json"),
    pytest.param(TOPIC_7, b"\xff\xfe", id="not-utf8"),
    pytest.param(TOPIC_7, b"[1, 2]", id="json-not-object"),
    pytest.param("studyspace/library/floor2/desk-07//status", encode(death("")), id="empty-topic-id"),
    pytest.param(TOPIC_7, encode(death("")), id="empty-payload-id"),
    pytest.param("status", encode(death()), id="topic-too-short"),
    pytest.param(TOPIC_7, encode(death("placemat-0008")), id="topic-id-mismatch"),
    pytest.param(TOPIC_7, encode({"type": "reboot", "id": "placemat-0007"}), id="unknown-type"),
    pytest.param(TOPIC_7, encode(_without(death(), "type")), id="missing-type"),
    pytest.param(TOPIC_7, encode(_without(death(), "id")), id="missing-id"),
    pytest.param(TOPIC_7, encode(_without(heartbeat(), "timestamp")), id="heartbeat-missing-timestamp"),
    pytest.param(TOPIC_7, encode(heartbeat(occupied="yes")), id="heartbeat-occupied-not-bool"),
    pytest.param(TOPIC_7, encode(heartbeat(occupied=1)), id="heartbeat-occupied-int"),
    pytest.param(TOPIC_7, encode(birth(schema=2)), id="birth-unsupported-schema"),
    pytest.param(TOPIC_7, encode(birth(space_id=7)), id="birth-space-id-not-str"),
    *[
        pytest.param(TOPIC_7, encode(_without(birth(), f)), id=f"birth-missing-{f}")
        for f in ("schema", "space_id", "device_type", "firmware", "occupied", "timestamp")
    ],
]


@pytest.mark.parametrize("topic,payload", BAD)
def test_parse_rejects(topic, payload):
    with pytest.raises(ParseError):
        parse(topic, payload)


# ---------------------------------------------------------------- northbound


def test_topics():
    assert messages.status_topic("library", "node-floor2") == "studyspace/v1/library/node-floor2/status"
    assert messages.data_topic("library", "node-floor2") == "studyspace/v1/library/node-floor2/data"


def test_node_birth_payload():
    devices = [{"id": "placemat-0007", "space_id": "desk-07",
                "device_type": "placemat", "firmware": "0.1.0"}]
    assert json.loads(messages.node_birth_payload("node-floor2", TS, devices)) == {
        "type": "birth", "id": "node-floor2", "timestamp": TS, "devices": devices,
    }


def test_node_death_payload():
    assert json.loads(messages.node_death_payload("node-floor2")) == {
        "type": "death", "id": "node-floor2",
    }


def test_data_payload():
    devices = {"placemat-0007": {"online": True, "occupied": True, "since": TS},
               "placemat-0009": {"online": False}}
    assert json.loads(
        messages.data_payload("node-floor2", TS, ["placemat-0007"], devices)
    ) == {"id": "node-floor2", "timestamp": TS, "changed": ["placemat-0007"], "devices": devices}


# ---------------------------------------------------------------- time


def test_now_utc_format():
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", messages.now_utc())


def test_format_utc_converts_to_utc():
    plus_two = timezone(timedelta(hours=2))
    assert messages.format_utc(datetime(2026, 11, 10, 14, 0, 5, 999, tzinfo=plus_two)) == TS.replace(":00Z", ":05Z")


def test_format_utc_rejects_naive():
    with pytest.raises(ValueError):
        messages.format_utc(datetime(2026, 11, 10))


def test_no_paho_import():
    import ast, inspect
    from node import registry
    for mod in (messages, registry):
        tree = ast.parse(inspect.getsource(mod))
        names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        assert not any(n.startswith("paho") for n in names), mod.__name__
