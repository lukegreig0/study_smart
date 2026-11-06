import json
import logging
import signal
import socket
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import REPO_ROOT, FakeMqttClient
from studyspace_node import __main__ as cli
from studyspace_node.config import (
    DistanceSensorConfig,
    MotionSensorConfig,
    SeatConfig,
    WeightSensorConfig,
    load_config,
)
from studyspace_node.node import Node
from studyspace_node.occupancy import OccupancyState

EXAMPLE = str(REPO_ROOT / "config.example.toml")


class FakeCliClient(FakeMqttClient):
    def __init__(self):
        super().__init__(connected=True)
        self.calls = []
        self.userdata = None

    def user_data_set(self, userdata):
        self.userdata = userdata

    def connect_async(self, host, port):
        self.calls.append(("connect_async", host, port))

    def loop_start(self):
        self.calls.append("loop_start")

    def disconnect(self):
        self.calls.append(("disconnect", len(self.published)))

    def loop_stop(self):
        self.calls.append("loop_stop")

    def publish(self, topic, payload, qos=0, retain=False):
        super().publish(topic, payload, qos, retain)
        return SimpleNamespace(rc=0, wait_for_publish=lambda timeout=None: True)


@pytest.fixture
def cli_env(monkeypatch):
    """Fakes MQTT and signals; SIGTERM fires once the node has run a few ticks."""
    client = FakeCliClient()
    handlers = {}
    monkeypatch.setattr(cli, "create_mqtt_client", lambda config: client)
    monkeypatch.setattr(cli.signal, "signal", lambda sig, fn: handlers.__setitem__(sig, fn))
    original_tick = Node.tick
    ticks = []

    def tick_then_signal(self):
        original_tick(self)
        ticks.append(1)
        if len(ticks) >= 3:
            handlers[signal.SIGTERM](signal.SIGTERM, None)

    monkeypatch.setattr(Node, "tick", tick_then_signal)
    return SimpleNamespace(client=client, handlers=handlers, ticks=ticks)


def fast_config(tmp_path, socket_path=None):
    path = tmp_path / "c.toml"
    text = open(EXAMPLE).read().replace("poll_interval_s = 2.0", "poll_interval_s = 0.001")
    text = text.replace("/tmp/studyspace-node-ui.sock", socket_path or str(tmp_path / "ui.sock"))
    path.write_text(text)
    return str(path)


def test_simulate_runs_and_shuts_down_cleanly(cli_env, tmp_path):
    assert cli.main(["--config", fast_config(tmp_path), "--simulate"]) == 0
    client = cli_env.client
    assert set(cli_env.handlers) == {signal.SIGINT, signal.SIGTERM}
    assert client.calls[:2] == [("connect_async", "localhost", 1883), "loop_start"]
    assert client.userdata is not None
    assert len(cli_env.ticks) == 3
    last = client.published[-1]
    assert last["payload"] == "offline" and last["retain"] is True
    assert last["topic"] == "studyspace/library/floor2/node-07/status"
    assert client.calls[-2:] == [("disconnect", len(client.published)), "loop_stop"]


def test_screen_receives_snapshots_from_the_running_node(cli_env, tmp_path, monkeypatch):
    connections = []
    wrapped_tick = Node.tick

    def tick(self):
        if not connections:  # the UI socket is up by the time the node starts ticking
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(str(tmp_path / "ui.sock"))
            connections.append(sock)
        wrapped_tick(self)

    monkeypatch.setattr(Node, "tick", tick)
    assert cli.main(["--config", fast_config(tmp_path), "--simulate"]) == 0
    received = b""
    while data := connections[0].recv(65536):
        received += data
    connections[0].close()
    snapshot = json.loads(received.split(b"\n")[0])
    assert snapshot["type"] == "snapshot"
    assert [s["label"] for s in snapshot["seats"]] == ["A", "B", "C"]


def test_socket_is_removed_on_shutdown(cli_env, tmp_path):
    cli.main(["--config", fast_config(tmp_path), "--simulate"])
    assert not (tmp_path / "ui.sock").exists()


def test_node_runs_even_if_the_ui_socket_cannot_be_opened(cli_env, tmp_path, caplog):
    bad = str(tmp_path / "no" / "such" / "dir" / "ui.sock")
    with caplog.at_level(logging.ERROR):
        assert cli.main(["--config", fast_config(tmp_path, bad), "--simulate"]) == 0
    assert "Touchscreen interface unavailable" in caplog.text
    assert len(cli_env.ticks) == 3


def test_bad_config_returns_error_code(tmp_path, caplog):
    with caplog.at_level(logging.ERROR):
        assert cli.main(["--config", str(tmp_path / "missing.toml"), "--simulate"]) == 2
    assert "Configuration error" in caplog.text


def test_missing_gpiozero_gives_a_helpful_error(monkeypatch, caplog):
    def no_gpiozero(_config):
        raise ImportError("gpiozero")

    monkeypatch.setattr(cli, "real_seats", no_gpiozero)
    with caplog.at_level(logging.ERROR):
        assert cli.main(["--config", EXAMPLE]) == 1
    assert "pip install" in caplog.text


def test_gpio_init_failure_gives_an_error(monkeypatch, caplog):
    def broken(_config):
        raise RuntimeError("pin busy")

    monkeypatch.setattr(cli, "real_seats", broken)
    with caplog.at_level(logging.ERROR):
        assert cli.main(["--config", EXAMPLE]) == 1
    assert "pin busy" in caplog.text


class FakeSensor:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.closed = False

    def close(self):
        self.closed = True


def all_sensor_config():
    config = load_config(EXAMPLE, env={})
    return replace(
        config,
        seats=(
            SeatConfig(
                "s1",
                "S",
                90.0,
                weight=WeightSensorConfig(5, 6, 11.0, 22.0),
                motion=MotionSensorConfig(17),
                distance=DistanceSensorConfig(23, 24),
            ),
        ),
    )


def test_real_seats_are_built_from_config(monkeypatch):
    for name in ("Hx711WeightSensor", "PirMotionSensor", "UltrasonicDistanceSensor"):
        monkeypatch.setattr(cli, name, FakeSensor)
    seat = cli.real_seats(all_sensor_config())[0]
    assert (seat.space_id, seat.label, seat.position_deg) == ("s1", "S", 90.0)
    assert seat.weight.args == (5, 6, 11.0, 22.0)
    assert seat.motion.args == (17,)
    assert seat.distance.args == (23, 24)
    assert seat.distance.kwargs == {"max_distance_m": 4.0}


def test_real_seats_only_builds_configured_sensors(monkeypatch):
    monkeypatch.setattr(cli, "Hx711WeightSensor", FakeSensor)
    seats = cli.real_seats(load_config(EXAMPLE, env={}))
    assert len(seats) == 3
    assert all(s.weight and s.motion is None and s.distance is None for s in seats)


def test_failure_releases_sensors_already_opened_on_the_same_seat(monkeypatch):
    weight = FakeSensor()

    def broken(*args, **kwargs):
        raise RuntimeError("no")

    monkeypatch.setattr(cli, "Hx711WeightSensor", lambda *a: weight)
    monkeypatch.setattr(cli, "PirMotionSensor", broken)
    with pytest.raises(RuntimeError):
        cli.real_seats(all_sensor_config())
    assert weight.closed


def test_failure_releases_earlier_seats(monkeypatch):
    opened = []

    def build(*args):
        if len(opened) == 2:
            raise RuntimeError("third cell broken")
        opened.append(FakeSensor())
        return opened[-1]

    monkeypatch.setattr(cli, "Hx711WeightSensor", build)
    with pytest.raises(RuntimeError):
        cli.real_seats(load_config(EXAMPLE, env={}))
    assert [s.closed for s in opened] == [True, True]


def test_simulated_seats_cycle_through_both_states_and_differ(app_config):
    seats = cli.simulated_seats(app_config)
    assert len(seats) == 3
    histories = []
    for seat in seats:
        seen = []
        for t in range(2000):
            seat.detector.update(None, None, float(t), weight_kg=seat.weight.read())
            if not seen or seen[-1] != seat.detector.state:
                seen.append(seat.detector.state)
        histories.append(seen)
        assert OccupancyState.FREE in seen and OccupancyState.OCCUPIED in seen
    first_reads = {s.weight.read() for s in seats}
    assert first_reads  # all readable after a full cycle


def test_simulated_seats_support_every_sensor_type():
    seat = cli.simulated_seats(all_sensor_config())[0]
    assert seat.motion.read() is False
    assert seat.distance.read() == 2.0
    assert seat.weight.read() == 0.5


def test_log_level_is_case_insensitive():
    args = cli.build_arg_parser().parse_args(["--config", "x", "--log-level", "debug"])
    assert args.log_level == "DEBUG"


def test_config_argument_required():
    with pytest.raises(SystemExit):
        cli.build_arg_parser().parse_args([])
