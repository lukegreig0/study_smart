import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from studyspace_node.messages import (
    PayloadError,
    StateMessage,
    build_payload,
    parse_payload,
    state_topic,
    status_topic,
    validate_segment,
)
from studyspace_node.occupancy import OccupancySource, OccupancyState

WHEN = datetime(2026, 11, 6, 9, 30, 15, 123456, tzinfo=UTC)


def payload(**overrides) -> str:
    args = {
        "node_id": "node-07",
        "desk_id": "desk-07",
        "space_id": "desk-07-a",
        "state": OccupancyState.FREE,
        "healthy": True,
        "reason": "change",
        "source": OccupancySource.SENSOR,
        "timestamp": WHEN,
    }
    return build_payload(**(args | overrides))


def valid_dict() -> dict:
    return json.loads(payload())


def test_state_topic_format():
    assert state_topic("studyspace", "library", "floor2", "desk-07") == (
        "studyspace/library/floor2/desk-07/state"
    )


def test_status_topic_format():
    assert status_topic("studyspace", "library", "floor2", "node-07") == (
        "studyspace/library/floor2/node-07/status"
    )


@pytest.mark.parametrize("build", [state_topic, status_topic])
@pytest.mark.parametrize("position", range(4))
@pytest.mark.parametrize(
    "bad", ["", "a/b", "a+b", "+", "a#", "#", "a b", " a", "a\t", "a\nb", "a b", "a\0b", None, 7]
)
def test_invalid_segments_rejected(build, position, bad):
    segments = ["studyspace", "library", "floor2", "desk-07"]
    segments[position] = bad
    with pytest.raises(ValueError):
        build(*segments)


def test_validate_segment_returns_value_and_names_field():
    assert validate_segment("room", "floor-2_b.c") == "floor-2_b.c"
    with pytest.raises(ValueError, match="room"):
        validate_segment("room", "a/b")


def test_payload_fields_and_types():
    data = json.loads(payload(state=OccupancyState.OCCUPIED, healthy=False, reason="heartbeat"))
    assert data == {
        "schema": 1,
        "node_id": "node-07",
        "desk_id": "desk-07",
        "space_id": "desk-07-a",
        "state": "occupied",
        "healthy": False,
        "reason": "heartbeat",
        "source": "sensor",
        "timestamp": "2026-11-06T09:30:15.123456Z",
    }
    assert type(data["schema"]) is int
    assert type(data["healthy"]) is bool


def test_payload_contains_no_sensor_data():
    assert set(valid_dict()) == {
        "schema", "node_id", "desk_id", "space_id", "state", "healthy", "reason", "source", "timestamp",
    }


def test_timestamp_is_converted_to_utc():
    plus_two = WHEN.astimezone(timezone(timedelta(hours=2)))
    assert json.loads(payload(timestamp=plus_two))["timestamp"] == "2026-11-06T09:30:15.123456Z"


def test_naive_timestamp_rejected():
    with pytest.raises(ValueError, match="timezone"):
        payload(timestamp=datetime(2026, 11, 6, 9, 30))


def test_unknown_state_cannot_be_built():
    with pytest.raises(ValueError):
        payload(state=OccupancyState.UNKNOWN)


def test_bad_reason_cannot_be_built():
    with pytest.raises(ValueError):
        payload(reason="because")


def test_bad_source_cannot_be_built():
    with pytest.raises(ValueError):
        payload(source="guess")


def test_manual_source_round_trips():
    assert parse_payload(payload(source=OccupancySource.MANUAL)).source is OccupancySource.MANUAL


@pytest.mark.parametrize("state", [OccupancyState.FREE, OccupancyState.OCCUPIED])
@pytest.mark.parametrize("healthy", [True, False])
@pytest.mark.parametrize("reason", ["change", "heartbeat"])
def test_round_trip(state, healthy, reason):
    message = parse_payload(payload(state=state, healthy=healthy, reason=reason))
    assert message == StateMessage(
        schema=1,
        node_id="node-07",
        desk_id="desk-07",
        space_id="desk-07-a",
        state=state,
        healthy=healthy,
        reason=reason,
        source=OccupancySource.SENSOR,
        timestamp=WHEN,
    )
    assert message.timestamp.utcoffset() == timedelta(0)


def test_parse_accepts_bytes_as_received_from_mqtt():
    assert parse_payload(payload().encode()).node_id == "node-07"


def test_parse_normalises_offset_timestamps_to_utc():
    data = valid_dict() | {"timestamp": "2026-11-06T11:30:15+02:00"}
    assert parse_payload(json.dumps(data)).timestamp == datetime(2026, 11, 6, 9, 30, 15, tzinfo=UTC)


def test_parse_ignores_unknown_extra_fields():
    data = valid_dict() | {"future_field": 1}
    assert parse_payload(json.dumps(data)).space_id == "desk-07-a"


@pytest.mark.parametrize(
    "raw",
    ["", "not json", "[]", "null", "42", b"\xff\xfe", None],
)
def test_parse_rejects_non_object_payloads(raw):
    with pytest.raises(PayloadError):
        parse_payload(raw)


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema": 2},
        {"schema": "1"},
        {"schema": True},
        {"schema": None},
        {"state": "unknown"},
        {"state": "FREE"},
        {"state": "busy"},
        {"state": 1},
        {"healthy": "true"},
        {"healthy": 1},
        {"reason": "because"},
        {"reason": None},
        {"node_id": ""},
        {"node_id": 7},
        {"space_id": ""},
        {"space_id": None},
        {"desk_id": ""},
        {"desk_id": 7},
        {"source": "guess"},
        {"source": None},
        {"timestamp": "yesterday"},
        {"timestamp": 1730000000},
        {"timestamp": "2026-11-06T09:30:15"},  # no offset
    ],
)
def test_parse_rejects_bad_fields(overrides):
    with pytest.raises(PayloadError):
        parse_payload(json.dumps(valid_dict() | overrides))


@pytest.mark.parametrize("field", list(valid_dict()))
def test_parse_rejects_missing_fields(field):
    data = valid_dict()
    del data[field]
    with pytest.raises(PayloadError, match=field):
        parse_payload(json.dumps(data))


def test_payload_error_is_a_value_error():
    assert issubclass(PayloadError, ValueError)
