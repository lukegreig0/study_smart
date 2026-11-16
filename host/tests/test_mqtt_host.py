import threading

import pytest

from host.config import Config, ConfigError
from host.mqtt_host import SUBSCRIPTIONS, MqttHost

from .conftest import DATA, NODE, STATUS, birth, data, encode, on


class FakeReasonCode:
    def __init__(self, failure=False):
        self.is_failure = failure

    def __str__(self):
        return "Failure" if self.is_failure else "Success"


class FakeMessage:
    def __init__(self, topic, payload):
        self.topic, self.payload = topic, payload


class FakeClient:
    """Stands in for paho's Client and records calls in order."""

    def __init__(self, connect_failures=0):
        self.calls = []
        self.subscriptions = []
        self.connect_failures = connect_failures
        self.on_connect = self.on_disconnect = self.on_message = None

    def reconnect_delay_set(self, min_delay=1, max_delay=120):
        self.calls.append("reconnect_delay_set")

    def connect(self, host, port, keepalive=60):
        self.calls.append("connect")
        if self.connect_failures > 0:
            self.connect_failures -= 1
            raise ConnectionRefusedError("refused")

    def subscribe(self, topic, qos=0):
        self.calls.append("subscribe")
        self.subscriptions.append((topic, qos))

    def disconnect(self):
        self.calls.append("disconnect")

    def loop_start(self):
        self.calls.append("loop_start")

    def loop_stop(self):
        self.calls.append("loop_stop")

    # Play the network thread.
    def fire_connect(self, failure=False):
        self.on_connect(self, None, {}, FakeReasonCode(failure), None)

    def deliver(self, topic, payload):
        self.on_message(self, None, FakeMessage(topic, payload))


class FakeStore:
    def __init__(self, real):
        self.real, self.closed = real, False

    def __getattr__(self, name):
        return getattr(self.real, name)

    def close(self):
        self.closed = True


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def host(store, client):
    return MqttHost(Config(), store, client=client, clock=lambda: 0.0,
                    utc_now=lambda: "2026-11-10T12:30:00Z")


def test_subscribes_in_on_connect(host, client):
    assert client.subscriptions == []  # nothing before the connection is up
    client.fire_connect()
    assert client.subscriptions == [(t, 1) for t in SUBSCRIPTIONS]
    assert SUBSCRIPTIONS == ("studyspace/v1/+/+/status", "studyspace/v1/+/+/data")


def test_reconnect_resubscribes(host, client):
    client.fire_connect()
    client.fire_connect()
    assert client.subscriptions == [(t, 1) for t in SUBSCRIPTIONS] * 2


def test_refused_connection_does_not_subscribe(host, client):
    client.fire_connect(failure=True)
    assert client.subscriptions == []


def test_on_message_only_queues(host, client, store):
    client.deliver(STATUS, encode(birth()))
    assert store.get_node(NODE) is None  # nothing written on the "network thread"
    host.run_once(wait_s=0)
    assert store.get_node(NODE).online


def test_messages_flow_to_db(host, client, store):
    client.deliver(STATUS, encode(birth()))
    client.deliver(DATA, encode(data({"placemat-0007": on(True)})))
    client.deliver(DATA, b"")  # cleared retained message: rejected, no crash
    for _ in range(3):
        host.run_once(wait_s=0)
    assert store.get_device("placemat-0007").state == "occupied"


def test_connect_retry_then_shutdown_order(store):
    client = FakeClient(connect_failures=2)
    fake_store = FakeStore(store)
    host = MqttHost(Config(), fake_store, client=client)
    stop = threading.Event()
    host._client.loop_start = lambda: (client.calls.append("loop_start"), stop.set())
    # Speed up the backoff: Event.wait returns at once.
    stop.wait = lambda timeout=None: False
    host.run(stop)
    assert client.calls == ["reconnect_delay_set", "connect", "connect", "connect",
                            "loop_start", "disconnect", "loop_stop"]
    assert fake_store.closed


def test_config_from_env():
    cfg = Config.from_env({"BROKER_PORT": "1884", "NODE_TIMEOUT_S": "90", "LOG_LEVEL": "debug"})
    assert (cfg.broker_port, cfg.node_timeout_s, cfg.log_level) == (1884, 90.0, "DEBUG")
    assert Config.from_env({}).db_path == "./studyspace.db"


@pytest.mark.parametrize("env", [
    {"BROKER_PORT": "abc"}, {"BROKER_PORT": "70000"}, {"NODE_TIMEOUT_S": "-1"},
    {"KEEPALIVE_S": "0"}, {"LOG_LEVEL": "LOUD"},
])
def test_config_rejects(env):
    with pytest.raises(ConfigError):
        Config.from_env(env)
