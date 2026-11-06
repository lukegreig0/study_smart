from dataclasses import fields

import pytest

from conftest import REPO_ROOT
from studyspace_node.config import (
    DEFAULT_UI_SOCKET,
    PASSWORD_ENV_VAR,
    ConfigError,
    DistanceSensorConfig,
    MotionSensorConfig,
    MqttConfig,
    NodeConfig,
    OccupancySettings,
    SensorConfig,
    UiConfig,
    WeightSensorConfig,
    load_config,
    parse_config,
)

EXAMPLE = REPO_ROOT / "config.example.toml"


def weight(data_pin=5, clock_pin=6) -> dict:
    return {"data_pin": data_pin, "clock_pin": clock_pin, "tare_offset": 100.0, "scale_per_kg": 2000.0}


def minimal() -> dict:
    return {
        "node": {"id": "n1", "desk_id": "d1", "building": "lib", "room": "f2"},
        "seats": [{"space_id": "seat-1", "weight": weight()}],
        "mqtt": {"host": "broker"},
    }


def parse(raw: dict, env: dict | None = None):
    return parse_config(raw, env={} if env is None else env)


def with_field(section: str, **values) -> dict:
    raw = minimal()
    raw.setdefault(section, {}).update(values)
    return raw


def with_seat(**values) -> dict:
    raw = minimal()
    raw["seats"][0].update(values)
    return raw


def test_loads_the_example_file():
    config = load_config(EXAMPLE, env={})
    assert config.node == NodeConfig("node-07", "desk-07", "library", "floor2")
    assert [s.space_id for s in config.seats] == ["desk-07-a", "desk-07-b", "desk-07-c"]
    assert [s.label for s in config.seats] == ["A", "B", "C"]
    assert [s.position_deg for s in config.seats] == [0, 120, 240]
    assert config.seats[0].weight == WeightSensorConfig(5, 6, 0.0, 21000.0)
    assert config.sensors == SensorConfig(2.0)
    assert config.occupancy == OccupancySettings()
    assert config.ui.socket_path == "/tmp/studyspace-node-ui.sock"
    assert config.mqtt.host == "localhost"
    assert config.mqtt.port == 1883
    assert config.mqtt.heartbeat_s == 60
    assert config.state_topic("desk-07-a") == "studyspace/library/floor2/desk-07-a/state"
    assert config.status_topic == "studyspace/library/floor2/node-07/status"


def test_example_file_documents_every_occupancy_setting():
    text = EXAMPLE.read_text()
    assert all(f.name in text for f in fields(OccupancySettings))


def test_minimal_config_gets_defaults():
    config = parse(minimal())
    assert config.sensors == SensorConfig()
    assert config.occupancy == OccupancySettings()
    assert config.ui == UiConfig(DEFAULT_UI_SOCKET)
    assert config.mqtt == MqttConfig(host="broker")
    assert config.client_id == "n1"
    assert config.seats[0].label == "seat-1"
    assert config.seats[0].position_deg is None


def test_all_fields_are_loaded(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        """
        [node]
        id = "n"
        desk_id = "d"
        building = "b"
        room = "r"
        [[seats]]
        space_id = "s1"
        label = "One"
        position_deg = 90.5
        [seats.weight]
        data_pin = 5
        clock_pin = 6
        tare_offset = 123.5
        scale_per_kg = -2000
        [seats.motion]
        pin = 7
        [seats.distance]
        trigger_pin = 8
        echo_pin = 9
        [sensors]
        poll_interval_s = 0.5
        [occupancy]
        presence_distance_m = 1.0
        confirm_readings = 4
        vacancy_timeout_s = 60
        distance_window = 7
        min_valid_distance_m = 0.05
        max_valid_distance_m = 3.0
        max_consecutive_faults = 9
        presence_weight_kg = 25
        weight_window = 3
        min_valid_weight_kg = -5
        max_valid_weight_kg = 200
        manual_hold_s = 600
        [ui]
        socket_path = "/x/y.sock"
        [mqtt]
        host = "h"
        port = 1234
        topic_prefix = "p"
        username = "u"
        password = "pw"
        tls = true
        heartbeat_s = 30
        client_id = "cid"
        """
    )
    config = load_config(path, env={})
    seat = config.seats[0]
    assert (seat.space_id, seat.label, seat.position_deg) == ("s1", "One", 90.5)
    assert seat.weight == WeightSensorConfig(5, 6, 123.5, -2000)
    assert seat.motion == MotionSensorConfig(7)
    assert seat.distance == DistanceSensorConfig(8, 9)
    assert config.sensors == SensorConfig(0.5)
    assert config.occupancy == OccupancySettings(1.0, 4, 60, 7, 0.05, 3.0, 9, 25, 3, -5, 200, 600)
    assert config.ui == UiConfig("/x/y.sock")
    assert config.mqtt == MqttConfig("h", 1234, "p", "u", "pw", True, 30, "cid")
    assert config.client_id == "cid"


def test_tls_defaults_to_the_tls_port():
    assert parse(with_field("mqtt", tls=True)).mqtt.port == 8883


class TestSeats:
    @pytest.mark.parametrize("count", [1, 2, 6])
    def test_any_number_of_seats(self, count):
        raw = minimal()
        raw["seats"] = [
            {"space_id": f"s{i}", "weight": weight(data_pin=2 * i, clock_pin=2 * i + 1)}
            for i in range(count)
        ]
        assert len(parse(raw).seats) == count

    @pytest.mark.parametrize(
        "sensors",
        [
            {"weight": weight()},
            {"motion": {"pin": 17}},
            {"distance": {"trigger_pin": 23, "echo_pin": 24}},
            {"motion": {"pin": 17}, "distance": {"trigger_pin": 23, "echo_pin": 24}},
            {"weight": weight(), "motion": {"pin": 17}},
        ],
    )
    def test_any_sensor_combination(self, sensors):
        raw = minimal()
        raw["seats"] = [{"space_id": "s", **sensors}]
        seat = parse(raw).seats[0]
        for name in ("weight", "motion", "distance"):
            assert (getattr(seat, name) is not None) == (name in sensors)

    def test_seat_needs_a_sensor(self):
        raw = minimal()
        raw["seats"] = [{"space_id": "s"}]
        with pytest.raises(ConfigError, match=r"\[seats\[0\]\] needs at least one of"):
            parse(raw)

    def test_missing_seats(self):
        raw = minimal()
        del raw["seats"]
        with pytest.raises(ConfigError, match="at least one seat"):
            parse(raw)

    @pytest.mark.parametrize("seats", [[], "x"])
    def test_seats_must_be_a_non_empty_array(self, seats):
        with pytest.raises(ConfigError, match="non-empty array"):
            parse(minimal() | {"seats": seats})

    def test_seat_must_be_a_table(self):
        with pytest.raises(ConfigError, match=r"\[seats\[0\]\] must be a table"):
            parse(minimal() | {"seats": ["x"]})

    def test_duplicate_space_id(self):
        raw = minimal()
        raw["seats"].append({"space_id": "seat-1", "weight": weight(7, 8)})
        with pytest.raises(ConfigError, match="duplicate space_id: seat-1"):
            parse(raw)

    def test_pin_shared_between_seats(self):
        raw = minimal()
        raw["seats"].append({"space_id": "seat-2", "weight": weight(6, 8)})
        with pytest.raises(ConfigError, match=r"used more than once: \[6\]"):
            parse(raw)

    def test_pin_shared_within_a_seat(self):
        with pytest.raises(ConfigError, match="more than once"):
            parse(with_seat(motion={"pin": 5}))

    def test_pin_shared_with_distance_sensor(self):
        with pytest.raises(ConfigError, match="more than once"):
            parse(with_seat(distance={"trigger_pin": 5, "echo_pin": 24}))

    @pytest.mark.parametrize(
        "seat, message",
        [
            ({"space_id": "a/b"}, "space_id"),
            ({"space_id": 7}, "space_id"),
            ({"label": ""}, "label"),
            ({"position_deg": "top"}, "position_deg"),
            ({"typo": 1}, "unknown field.*typo"),
            ({"weight": {**weight(), "data_pin": 99}}, "data_pin"),
            ({"weight": {**weight(), "clock_pin": "6"}}, "clock_pin"),
            ({"weight": {**weight(), "scale_per_kg": 0}}, "scale_per_kg"),
            ({"weight": {**weight(), "tare_offset": "x"}}, "tare_offset"),
            ({"weight": {**weight(), "typo": 1}}, r"seats\[0\]\.weight.*typo"),
            ({"weight": "x"}, "must be a table"),
            ({"motion": {"pin": 99}}, "pin"),
            ({"motion": {"pin": 7, "typo": 1}}, "typo"),
            ({"distance": {"trigger_pin": 7}}, "echo_pin"),
            ({"distance": {"trigger_pin": 7, "echo_pin": 8, "typo": 1}}, "typo"),
        ],
    )
    def test_invalid_seat_fields_named(self, seat, message):
        with pytest.raises(ConfigError, match=message):
            parse(with_seat(**seat))

    def test_missing_weight_field_named(self):
        table = weight()
        del table["tare_offset"]
        with pytest.raises(ConfigError, match=r"\[seats\[0\]\.weight\] missing required field 'tare_offset'"):
            parse(with_seat(weight=table))


class TestMissing:
    @pytest.mark.parametrize("section", ["node", "mqtt"])
    def test_missing_section(self, section):
        raw = minimal()
        del raw[section]
        with pytest.raises(ConfigError, match=rf"missing required section \[{section}\]"):
            parse(raw)

    @pytest.mark.parametrize(
        "section, key",
        [("node", "id"), ("node", "desk_id"), ("node", "building"), ("node", "room"), ("mqtt", "host")],
    )
    def test_missing_field_is_named(self, section, key):
        raw = minimal()
        del raw[section][key]
        with pytest.raises(ConfigError, match=rf"\[{section}\] missing required field '{key}'"):
            parse(raw)

    def test_missing_file(self, tmp_path):
        with pytest.raises(ConfigError, match="cannot read config file"):
            load_config(tmp_path / "nope.toml", env={})

    def test_malformed_toml(self, tmp_path):
        path = tmp_path / "bad.toml"
        path.write_text("[node\nid=")
        with pytest.raises(ConfigError, match="invalid TOML"):
            load_config(path, env={})


class TestInvalid:
    @pytest.mark.parametrize(
        "section, values, message",
        [
            ("node", {"id": ""}, "non-empty string"),
            ("node", {"id": 7}, "non-empty string"),
            ("node", {"desk_id": "a b"}, "desk_id"),
            ("node", {"building": "a/b"}, "building"),
            ("node", {"room": "a b"}, "room"),
            ("mqtt", {"topic_prefix": "a+b"}, "topic_prefix"),
            ("mqtt", {"port": "1883"}, "port"),
            ("mqtt", {"port": True}, "port"),
            ("mqtt", {"port": 0}, "port"),
            ("mqtt", {"port": 70000}, "port"),
            ("mqtt", {"tls": "yes"}, "tls"),
            ("mqtt", {"heartbeat_s": 0}, "heartbeat_s"),
            ("mqtt", {"heartbeat_s": float("nan")}, "heartbeat_s"),
            ("sensors", {"poll_interval_s": 0}, "poll_interval_s"),
            ("sensors", {"poll_interval_s": "1"}, "poll_interval_s"),
            ("occupancy", {"confirm_readings": 0}, "confirm_readings"),
            ("occupancy", {"presence_weight_kg": "heavy"}, "presence_weight_kg"),
            ("ui", {"socket_path": ""}, "socket_path"),
        ],
    )
    def test_invalid_values_name_the_field(self, section, values, message):
        with pytest.raises(ConfigError, match=message):
            parse(with_field(section, **values))

    @pytest.mark.parametrize("section", ["node", "sensors", "occupancy", "ui", "mqtt"])
    def test_unknown_field_rejected(self, section):
        with pytest.raises(ConfigError, match="unknown field.*typo"):
            parse(with_field(section, typo=1))

    def test_old_single_seat_sensor_pins_are_rejected(self):
        with pytest.raises(ConfigError, match="unknown field.*pir_pin"):
            parse(with_field("sensors", pir_pin=17))

    def test_unknown_section_rejected(self):
        with pytest.raises(ConfigError, match="unknown section.*broker"):
            parse(minimal() | {"broker": {}})

    def test_section_must_be_a_table(self):
        with pytest.raises(ConfigError, match=r"\[node\] must be a table"):
            parse(minimal() | {"node": "n1"})

    def test_config_error_is_a_value_error(self):
        assert issubclass(ConfigError, ValueError)


class TestPassword:
    def test_env_var_overrides_file(self):
        raw = with_field("mqtt", username="u", password="from-file")
        assert parse(raw, {PASSWORD_ENV_VAR: "from-env"}).mqtt.password == "from-env"

    def test_env_var_used_without_file_password(self):
        assert parse(with_field("mqtt", username="u"), {PASSWORD_ENV_VAR: "e"}).mqtt.password == "e"

    def test_file_password_used_without_env_var(self):
        raw = with_field("mqtt", username="u", password="from-file")
        assert parse(raw).mqtt.password == "from-file"

    def test_empty_env_var_is_treated_as_unset(self):
        raw = with_field("mqtt", username="u", password="from-file")
        assert parse(raw, {PASSWORD_ENV_VAR: ""}).mqtt.password == "from-file"

    def test_real_environment_is_used_by_default(self, monkeypatch):
        monkeypatch.setenv(PASSWORD_ENV_VAR, "real-env")
        assert parse_config(with_field("mqtt", username="u")).mqtt.password == "real-env"

    def test_password_requires_username(self):
        with pytest.raises(ConfigError, match="requires a username"):
            parse(minimal(), {PASSWORD_ENV_VAR: "e"})

    def test_password_is_not_in_repr(self):
        config = parse(with_field("mqtt", username="u", password="s3cret"))
        assert "s3cret" not in repr(config)


@pytest.mark.parametrize(
    "unit, module", [("studyspace-node", "studyspace_node "), ("studyspace-gui", "studyspace_node.gui ")]
)
def test_systemd_units_run_the_modules(unit, module):
    text = (REPO_ROOT / "deploy" / f"{unit}.service").read_text()
    assert f"-m {module}" in text


def test_gui_unit_is_not_bound_to_the_node_lifecycle():
    text = (REPO_ROOT / "deploy" / "studyspace-gui.service").read_text()
    assert "Requires=" not in text and "BindsTo=" not in text and "PartOf=" not in text
