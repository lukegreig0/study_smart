"""UI protocol, plus the real server and client talking over a real Unix socket."""

import json
import queue
import socket
import stat
import threading
import time
from types import SimpleNamespace

import pytest

from conftest import FakeMqttClient
from studyspace_node.gui import DeskViewModel, Layout
from studyspace_node.node import Node, Seat
from studyspace_node.occupancy import OccupancyDetector, OccupancySettings
from studyspace_node.publisher import Publisher
from studyspace_node.sensors import FakeWeightSensor
from studyspace_node.ui_client import CONNECTED, DISCONNECTED, UiClient
from studyspace_node.ui_protocol import (
    MARK_OCCUPIED,
    MARK_VACANT,
    MAX_LINE_BYTES,
    Command,
    ProtocolError,
    decode,
    encode,
    parse_command,
    result_message,
)
from studyspace_node.ui_server import MAX_CLIENTS, MAX_OUTBOUND_BYTES, UiServer

TIMEOUT = 5


class TestProtocol:
    def test_encode_decode_round_trip(self):
        line = encode({"type": "x", "n": 1})
        assert line.endswith(b"\n") and line.count(b"\n") == 1
        assert decode(line) == {"type": "x", "n": 1}

    @pytest.mark.parametrize("line", [b"not json", b"[]", b"1", b"\xff"])
    def test_decode_rejects_bad_lines(self, line):
        with pytest.raises(ProtocolError):
            decode(line)

    @pytest.mark.parametrize("name", [MARK_OCCUPIED, MARK_VACANT])
    def test_command_round_trip(self, name):
        command = Command(7, name, "desk-07-a")
        assert parse_command(decode(encode(command.to_message()))) == command

    @pytest.mark.parametrize(
        "message",
        [
            {},
            {"type": "snapshot"},
            {"type": "command", "command": MARK_VACANT, "space_id": "s"},
            {"type": "command", "id": "1", "command": MARK_VACANT, "space_id": "s"},
            {"type": "command", "id": True, "command": MARK_VACANT, "space_id": "s"},
            {"type": "command", "id": 1, "command": "explode", "space_id": "s"},
            {"type": "command", "id": 1, "command": MARK_VACANT},
            {"type": "command", "id": 1, "command": MARK_VACANT, "space_id": ""},
            {"type": "command", "id": 1, "command": MARK_VACANT, "space_id": 3},
        ],
    )
    def test_parse_command_rejects_bad_commands(self, message):
        with pytest.raises(ProtocolError):
            parse_command(message)

    def test_result_message(self):
        assert result_message(1) == {"type": "result", "id": 1, "ok": True, "error": None}
        assert result_message(2, "no") == {"type": "result", "id": 2, "ok": False, "error": "no"}


@pytest.fixture
def received():
    return queue.Queue()


@pytest.fixture
def server(tmp_path, received):
    def on_command(command, reply):
        received.put(command)
        reply(result_message(command.id))

    instance = UiServer(str(tmp_path / "ui.sock"), on_command)
    instance.start()
    yield instance
    instance.stop()


@pytest.fixture
def client(tmp_path, server):
    instance = UiClient(str(tmp_path / "ui.sock"), retry_s=0.01)
    instance.start()
    yield instance
    instance.stop()


def raw_connection(server):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(TIMEOUT)
    sock.connect(server._path)
    return sock


def next_line(sock, buffer=None):
    buffer = bytearray() if buffer is None else buffer
    while b"\n" not in buffer:
        data = sock.recv(4096)
        if not data:
            raise ConnectionError("closed")
        buffer += data
    line, _, rest = bytes(buffer).partition(b"\n")
    buffer[:] = rest
    return json.loads(line)


def next_message(client, kind=None):
    while True:
        message = client.messages.get(timeout=TIMEOUT)
        if kind is None or message["type"] == kind:
            return message


class TestServerAndClient:
    def test_client_reports_connected(self, client):
        assert client.messages.get(timeout=TIMEOUT) == CONNECTED

    def test_snapshot_reaches_client(self, server, client):
        next_message(client, "connected")
        server.publish({"type": "snapshot", "n": 1})
        assert next_message(client, "snapshot")["n"] == 1

    def test_new_client_gets_latest_snapshot_immediately(self, server, tmp_path):
        server.publish({"type": "snapshot", "n": 1})
        server.publish({"type": "snapshot", "n": 2})
        client = UiClient(str(tmp_path / "ui.sock"), retry_s=0.01)
        client.start()
        try:
            assert next_message(client, "snapshot")["n"] == 2
        finally:
            client.stop()

    def test_command_round_trip_with_result(self, client, received):
        next_message(client, "connected")
        assert client.send(Command(9, MARK_VACANT, "desk-07-a")) is True
        assert received.get(timeout=TIMEOUT) == Command(9, MARK_VACANT, "desk-07-a")
        assert next_message(client, "result") == result_message(9)

    def test_send_while_disconnected_returns_false(self, tmp_path):
        client = UiClient(str(tmp_path / "nothing-here.sock"))
        assert client.send(Command(1, MARK_VACANT, "s")) is False

    def test_client_retries_until_the_node_appears(self, tmp_path):
        path = str(tmp_path / "ui.sock")
        client = UiClient(path, retry_s=0.01)
        client.start()
        server = UiServer(path, lambda c, r: None)
        server.start()
        try:
            assert next_message(client, "connected") == CONNECTED
        finally:
            client.stop()
            server.stop()

    def test_client_reports_node_stopping_then_reconnects(self, tmp_path):
        path = str(tmp_path / "ui.sock")
        first = UiServer(path, lambda c, r: None)
        first.start()
        client = UiClient(path, retry_s=0.01)
        client.start()
        try:
            next_message(client, "connected")
            first.stop()
            assert next_message(client, "disconnected") == DISCONNECTED
            second = UiServer(path, lambda c, r: None)
            second.start()
            try:
                assert next_message(client, "connected") == CONNECTED
            finally:
                second.stop()
        finally:
            client.stop()

    def test_client_ignores_bad_lines_from_the_node(self, tmp_path):
        path = str(tmp_path / "ui.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen()
        client = UiClient(path, retry_s=0.01)
        client.start()
        try:
            conn, _ = listener.accept()
            conn.sendall(b"garbage\n" + encode({"type": "snapshot", "n": 3}))
            assert next_message(client, "snapshot")["n"] == 3
            conn.sendall(b"x" * (MAX_LINE_BYTES + 1))  # no newline: client must hang up
            assert next_message(client, "disconnected") == DISCONNECTED
            conn.close()
        finally:
            client.stop()
            listener.close()


class TestServerRobustness:
    def test_socket_permissions(self, server, tmp_path):
        mode = (tmp_path / "ui.sock").stat().st_mode
        assert stat.S_ISSOCK(mode)
        assert stat.S_IMODE(mode) == 0o660

    def test_replaces_a_stale_socket(self, tmp_path):
        path = tmp_path / "ui.sock"
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(path))
        stale.close()
        server = UiServer(str(path), lambda c, r: None)
        server.start()
        server.stop()
        assert not path.exists()

    def test_refuses_to_replace_a_regular_file(self, tmp_path):
        path = tmp_path / "ui.sock"
        path.write_text("important")
        with pytest.raises(OSError, match="not a socket"):
            UiServer(str(path), lambda c, r: None).start()
        assert path.read_text() == "important"

    def test_bad_directory_raises(self, tmp_path):
        with pytest.raises(OSError):
            UiServer(str(tmp_path / "missing" / "ui.sock"), lambda c, r: None).start()

    @pytest.mark.parametrize(
        "line, expected_id",
        [
            (b"not json\n", None),
            (b"[1,2]\n", None),
            (b'{"type":"command","id":4,"command":"explode","space_id":"s"}\n', 4),
            (b'{"type":"command","id":"x","command":"mark_vacant","space_id":"s"}\n', None),
        ],
    )
    def test_bad_input_gets_an_error_reply_and_server_survives(self, server, received, line, expected_id):
        sock = raw_connection(server)
        sock.sendall(line)
        reply = next_line(sock)
        assert reply["ok"] is False and reply["id"] == expected_id
        assert received.empty()
        sock.sendall(encode(Command(5, MARK_VACANT, "s").to_message()))
        assert next_line(sock)["ok"] is True
        sock.close()

    def test_oversized_line_disconnects_only_that_client(self, server):
        bad = raw_connection(server)
        good = raw_connection(server)
        try:
            bad.sendall(b"x" * (MAX_LINE_BYTES + 5000))
        except OSError:
            pass
        try:
            assert bad.recv(4096) == b""
        except ConnectionResetError:  # how a Unix socket reports close with unread data
            pass
        bad.close()
        server.publish({"type": "snapshot"})
        assert next_line(good)["type"] == "snapshot"
        good.close()

    def test_two_commands_in_one_packet_both_handled(self, server, received):
        sock = raw_connection(server)
        sock.sendall(
            encode(Command(1, MARK_VACANT, "a").to_message())
            + encode(Command(2, MARK_VACANT, "b").to_message())
        )
        assert received.get(timeout=TIMEOUT).id == 1
        assert received.get(timeout=TIMEOUT).id == 2
        sock.close()

    def test_command_split_across_packets(self, server, received):
        sock = raw_connection(server)
        data = encode(Command(1, MARK_VACANT, "a").to_message())
        sock.sendall(data[:10])
        sock.sendall(data[10:])
        assert received.get(timeout=TIMEOUT).id == 1
        sock.close()

    def test_reply_after_client_left_is_harmless(self, tmp_path):
        replies = queue.Queue()
        server = UiServer(str(tmp_path / "ui.sock"), lambda c, r: replies.put(r))
        server.start()
        try:
            sock = raw_connection(server)
            sock.sendall(encode(Command(1, MARK_VACANT, "a").to_message()))
            reply = replies.get(timeout=TIMEOUT)
            sock.close()
            good = raw_connection(server)
            server.publish({"type": "snapshot"})
            assert next_line(good)["type"] == "snapshot"  # server has processed the close
            reply(result_message(1))
            server.publish({"type": "snapshot", "n": 2})
            assert next_line(good, bytearray())["type"] == "snapshot"
            good.close()
        finally:
            server.stop()

    def test_client_limit_rejects_extra_connections(self, server):
        clients = [raw_connection(server) for _ in range(MAX_CLIENTS)]
        server.publish({"type": "snapshot"})
        for sock in clients:
            assert next_line(sock)["type"] == "snapshot"
        extra = raw_connection(server)
        assert extra.recv(4096) == b""
        extra.close()
        for sock in clients:
            sock.close()

    def test_publish_never_blocks_and_stuck_client_is_dropped(self, server):
        stuck = raw_connection(server)  # never reads
        good = raw_connection(server)
        server.publish({"type": "snapshot"})
        buffer = bytearray()
        assert next_line(good, buffer)["type"] == "snapshot"
        padding = "x" * 8192
        for i in range(MAX_OUTBOUND_BYTES // 2048):
            server.publish({"type": "snapshot", "n": i, "padding": padding})
            assert next_line(good, buffer)["n"] == i
        stuck.settimeout(TIMEOUT)
        total = 0
        while data := stuck.recv(65536):  # server closed it: drains what was buffered, then EOF
            total += len(data)
        assert total < MAX_OUTBOUND_BYTES * 4
        stuck.close()
        good.close()

    def test_stop_with_a_client_connected(self, tmp_path):
        server = UiServer(str(tmp_path / "ui.sock"), lambda c, r: None)
        server.start()
        sock = raw_connection(server)
        server.stop()
        assert sock.recv(4096) == b""
        sock.close()

    def test_publish_after_stop_is_harmless(self, tmp_path):
        server = UiServer(str(tmp_path / "ui.sock"), lambda c, r: None)
        server.start()
        server.stop()
        server.publish({"type": "snapshot"})


class TestNodeToScreen:
    """Real Node, UiServer, UiClient and view model together; only sensors and MQTT are fake."""

    @pytest.fixture
    def desk(self, tmp_path, app_config):
        from dataclasses import replace

        config = replace(app_config, sensors=replace(app_config.sensors, poll_interval_s=0.01))
        settings = OccupancySettings(confirm_readings=1, weight_window=1)
        seats = [
            Seat("desk-07-a", OccupancyDetector(settings), "A", weight=FakeWeightSensor(default=0.0)),
            Seat("desk-07-b", OccupancyDetector(settings), "B", weight=FakeWeightSensor(default=70.0)),
        ]
        mqtt = FakeMqttClient()
        node = Node(seats, Publisher(mqtt), config)
        path = str(tmp_path / "ui.sock")
        server = UiServer(path, node.submit_command)
        server.start()
        node.attach_ui(server.publish)
        stop = threading.Event()
        thread = threading.Thread(target=node.run, args=(stop,), daemon=True)
        thread.start()
        client = UiClient(path, retry_s=0.01)
        client.start()
        model = DeskViewModel(Layout(800, 480))
        model.tap(0, 0, time.monotonic())  # wake
        yield SimpleNamespace(client=client, model=model, mqtt=mqtt)
        client.stop()
        stop.set()
        thread.join(timeout=5)
        server.stop()

    def pump(self, desk, until):
        for _ in range(2000):
            desk.model.handle(desk.client.messages.get(timeout=TIMEOUT), time.monotonic())
            if until():
                return
        pytest.fail("condition not reached")

    def seat(self, desk, space_id):
        return next((s for s in desk.model.seats if s["space_id"] == space_id), None)

    def press(self, desk, space_id, rect):
        model = desk.model
        index = [s["space_id"] for s in model.seats].index(space_id)
        now = time.monotonic()
        model.tap(*model.layout.seat_center(model.seats, index), now)
        command = model.tap(rect[0] + 1, rect[1] + 1, now)
        assert desk.client.send(command)

    def test_screen_shows_node_state(self, desk):
        self.pump(desk, lambda: (self.seat(desk, "desk-07-b") or {}).get("state") == "occupied")
        assert self.seat(desk, "desk-07-a")["state"] == "free"
        assert desk.model.free_summary() == "1 of 2 free"

    def test_mark_occupied_from_the_screen_reaches_mqtt(self, desk):
        self.pump(desk, lambda: (self.seat(desk, "desk-07-a") or {}).get("state") == "free")
        self.press(desk, "desk-07-a", desk.model.layout.occupied_button)
        self.pump(desk, lambda: desk.model.toast == "Marked occupied")
        self.pump(desk, lambda: self.seat(desk, "desk-07-a")["source"] == "manual")
        published = [json.loads(p) for p in desk.mqtt.payloads]
        last = [m for m in published if m["space_id"] == "desk-07-a"][-1]
        assert (last["state"], last["source"]) == ("occupied", "manual")

    def test_node_refusal_reaches_the_screen(self, desk):
        self.pump(desk, lambda: (self.seat(desk, "desk-07-b") or {}).get("state") == "occupied")
        self.press(desk, "desk-07-b", desk.model.layout.vacant_button)
        self.pump(desk, lambda: desk.model.toast == "Someone is sitting here")
        assert self.seat(desk, "desk-07-b")["state"] == "occupied"
