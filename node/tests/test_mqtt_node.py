import threading

import pytest

from conftest import TOPIC_7, TOPIC_9, FakeClient, birth, encode, heartbeat
from node.config import ConfigError, Config
from node.mqtt_node import MqttNode

STATUS = "studyspace/v1/library/node-floor2/status"
DATA = "studyspace/v1/library/node-floor2/data"
NOW_UTC = "2026-11-10T13:00:00Z"


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def node(config, client, clock) -> MqttNode:
    return MqttNode(config, client=client, clock=clock, utc_now=lambda: NOW_UTC)


def drain(node: MqttNode) -> None:
    """Process everything the 'network thread' has queued."""
    while not node._queue.empty():
        node.run_once(wait_s=0)


def connect(node: MqttNode, client: FakeClient) -> None:
    client.fire_connect()
    drain(node)


def kinds(client: FakeClient) -> list[str]:
    return [("birth" if p["topic"] == STATUS and p["payload"]["type"] == "birth"
             else "death" if p["topic"] == STATUS else "data") for p in client.published]


def test_will_is_set_before_connect(node, client):
    node.connect_with_retry(threading.Event())
    assert client.calls.index("will_set") < client.calls.index("connect")
    assert client.will == {
        "topic": STATUS, "payload": {"type": "death", "id": "node-floor2"},
        "qos": 1, "retain": True,
    }


def test_connect_retries_with_backoff(config, clock):
    client = FakeClient(connect_failures=3)
    node = MqttNode(config, client=client, clock=clock)

    class InstantEvent(threading.Event):
        def __init__(self):
            super().__init__()
            self.waits = []

        def wait(self, timeout=None):  # record instead of sleeping
            self.waits.append(timeout)
            return False

    stop = InstantEvent()
    assert node.connect_with_retry(stop) is True
    assert client.calls.count("connect") == 4
    assert stop.waits == [1.0, 2.0, 4.0]


def test_connect_gives_up_when_stopped(config, clock):
    client = FakeClient(connect_failures=99)
    stop = threading.Event()
    stop.set()
    assert MqttNode(config, client=client, clock=clock).connect_with_retry(stop) is False


def test_subscribes_in_on_connect(node, client, config):
    client.fire_connect()
    client.fire_connect()  # reconnect
    assert client.subscriptions == [(config.device_sub, 1)] * 2


def test_birth_then_data_on_connect(node, client):
    connect(node, client)
    assert kinds(client) == ["birth", "data"]
    assert client.published[0]["payload"] == {
        "type": "birth", "id": "node-floor2", "timestamp": NOW_UTC, "devices": [],
    }
    assert client.published[1]["payload"] == {
        "id": "node-floor2", "timestamp": NOW_UTC, "changed": [], "devices": {},
    }


def test_rebirth_on_reconnect(node, client):
    connect(node, client)
    client.deliver(TOPIC_7, encode(birth()))
    drain(node)
    client.published.clear()

    client.fire_disconnect()
    connect(node, client)
    assert kinds(client) == ["birth", "data"]
    assert [d["id"] for d in client.published[0]["payload"]["devices"]] == ["placemat-0007"]


def test_failed_connack_publishes_nothing(node, client):
    client.fire_connect(failure=True)
    drain(node)
    assert client.published == []


def test_all_northbound_publishes_are_retained_qos1(node, client, clock):
    connect(node, client)
    client.deliver(TOPIC_7, encode(birth()))
    client.deliver(TOPIC_7, encode(heartbeat(occupied=True)))
    drain(node)
    clock.advance(100)
    node.run_once(wait_s=0)  # timeout
    node.shutdown()
    assert len(client.published) >= 5
    assert all(p["qos"] == 1 and p["retain"] is True for p in client.published)


def test_new_device_publishes_birth_then_data(node, client):
    connect(node, client)
    client.published.clear()
    client.deliver(TOPIC_7, encode(birth(occupied=True)))
    drain(node)
    assert kinds(client) == ["birth", "data"]
    assert client.published[1]["payload"]["changed"] == ["placemat-0007"]
    assert client.published[1]["payload"]["devices"] == {
        "placemat-0007": {"online": True, "occupied": True, "since": "2026-11-10T12:00:00Z"}
    }


def test_plain_heartbeat_is_not_forwarded(node, client, clock):
    connect(node, client)
    client.deliver(TOPIC_7, encode(birth()))
    drain(node)
    client.published.clear()
    clock.advance(2)
    client.deliver(TOPIC_7, encode(heartbeat()))
    drain(node)
    assert client.published == []


def test_occupancy_change_publishes_data_only(node, client, clock):
    connect(node, client)
    client.deliver(TOPIC_7, encode(birth()))
    client.deliver(TOPIC_9, encode(birth("placemat-0009")))
    drain(node)
    client.published.clear()
    client.deliver(TOPIC_9, encode(heartbeat("placemat-0009", occupied=True)))
    drain(node)
    assert kinds(client) == ["data"]
    payload = client.published[0]["payload"]
    assert payload["changed"] == ["placemat-0009"]
    # Delta: placemat-0007 didn't change, so it isn't in the message.
    assert payload["devices"] == {
        "placemat-0009": {"online": True, "occupied": True, "since": "2026-11-10T12:00:00Z"}
    }


def test_timeout_delta_only_lists_timed_out_device(node, client, clock, config):
    connect(node, client)
    client.deliver(TOPIC_7, encode(birth()))
    drain(node)
    clock.advance(4)
    client.deliver(TOPIC_9, encode(birth("placemat-0009")))
    drain(node)
    client.published.clear()
    clock.advance(config.device_timeout_s - 3)  # 0007 silent >6 s, 0009 only 3 s
    node.run_once(wait_s=0)
    assert client.published[0]["payload"]["devices"] == {"placemat-0007": {"online": False}}


def test_reconnect_publishes_full_snapshot(node, client, clock):
    connect(node, client)
    client.deliver(TOPIC_7, encode(birth()))
    client.deliver(TOPIC_9, encode(birth("placemat-0009", occupied=True)))
    drain(node)
    client.published.clear()
    client.fire_disconnect()
    connect(node, client)
    data = client.published[1]["payload"]
    assert data["changed"] == []
    assert sorted(data["devices"]) == ["placemat-0007", "placemat-0009"]


def test_timeout_publishes_data(node, client, clock, config):
    connect(node, client)
    client.deliver(TOPIC_7, encode(birth()))
    drain(node)
    client.published.clear()
    clock.advance(config.device_timeout_s + 0.5)
    node.run_once(wait_s=0)
    assert kinds(client) == ["data"]
    assert client.published[0]["payload"]["changed"] == ["placemat-0007"]
    assert client.published[0]["payload"]["devices"] == {"placemat-0007": {"online": False}}


def test_no_publish_without_changes(node, client, clock):
    connect(node, client)
    client.deliver(TOPIC_7, encode(birth()))
    drain(node)
    client.published.clear()
    # An hour of steady heartbeats: the device stays online and nothing
    # changes, so nothing is published.
    for _ in range(1800):
        clock.advance(2)
        client.deliver(TOPIC_7, encode(heartbeat()))
        drain(node)
    assert client.published == []


def test_every_data_publish_after_connect_lists_changes(node, client, clock):
    connect(node, client)
    client.published.clear()
    client.deliver(TOPIC_7, encode(birth()))
    client.deliver(TOPIC_7, encode(heartbeat(occupied=True)))
    drain(node)
    clock.advance(100)
    node.run_once(wait_s=0)  # timeout
    data = [p["payload"] for p in client.published if p["topic"] == DATA]
    assert len(data) == 3
    assert all(d["changed"] == ["placemat-0007"] for d in data)


def test_bad_message_is_logged_not_raised(node, client, caplog):
    connect(node, client)
    client.published.clear()
    client.deliver(TOPIC_7, b"{nope")
    client.deliver(TOPIC_7, b"")
    drain(node)
    assert client.published == []
    assert caplog.text.count("rejected message") == 2


def test_on_message_only_queues(node, client):
    client.deliver(TOPIC_7, encode(birth()))
    assert client.published == []
    assert "placemat-0007" not in node.registry
    assert node._queue.qsize() == 1


def test_nothing_published_while_disconnected(node, client, clock):
    connect(node, client)
    client.fire_disconnect()
    drain(node)
    client.published.clear()
    client.deliver(TOPIC_7, encode(birth()))
    drain(node)
    assert client.published == []


def test_death_published_on_shutdown(node, client):
    connect(node, client)
    node.shutdown()
    last = client.published[-1]
    assert last["topic"] == STATUS
    assert last["payload"] == {"type": "death", "id": "node-floor2"}
    assert last["qos"] == 1 and last["retain"] is True
    assert last["info"].waited
    assert client.calls[-3:] == ["publish", "disconnect", "loop_stop"]


def test_run_stops_cleanly(node, client):
    stop = threading.Event()
    stop.set()
    # stop is already set, so connect_with_retry returns False and nothing runs
    node.run(stop)
    assert "connect" not in client.calls


# ---------------------------------------------------------------- config


def test_config_defaults():
    c = Config.from_env({})
    assert c == Config()
    assert c.base_topic == "studyspace/v1/library/node-floor2"


def test_config_from_env():
    c = Config.from_env({"NODE_ID": "node-floor3", "BROKER_PORT": "1884",
                         "DEVICE_TIMEOUT_S": "2.5", "LOG_LEVEL": "debug"})
    assert (c.node_id, c.broker_port, c.device_timeout_s, c.log_level) == (
        "node-floor3", 1884, 2.5, "DEBUG")


@pytest.mark.parametrize("env", [
    {"NODE_ID": "Node_Floor2"},
    {"SITE": "lib/rary"},
    {"BROKER_PORT": "abc"},
    {"BROKER_PORT": "0"},
    {"DEVICE_TIMEOUT_S": "-1"},
    {"KEEPALIVE_S": "0"},
    {"KEEPALIVE_S": "1.5"},
    {"LOG_LEVEL": "LOUD"},
])
def test_config_rejects(env):
    with pytest.raises(ConfigError):
        Config.from_env(env)
