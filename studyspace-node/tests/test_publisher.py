import logging
from types import SimpleNamespace

import pytest

from conftest import FakeMqttClient
from studyspace_node.publisher import Publisher, create_mqtt_client, publish_offline


def test_publishes_with_qos1_and_retain(client):
    assert Publisher(client).publish("t/a", "1") is True
    assert client.published == [{"topic": "t/a", "payload": "1", "qos": 1, "retain": True}]


def test_buffers_while_offline():
    client = FakeMqttClient(connected=False)
    publisher = Publisher(client)
    assert publisher.publish("t/a", "1") is False
    assert client.published == []
    assert publisher.pending == [("t/a", "1")]


def test_flushes_in_order_on_reconnect():
    client = FakeMqttClient(connected=False)
    publisher = Publisher(client)
    for topic in "abc":
        publisher.publish(f"t/{topic}", topic)
    client.connected = True
    assert publisher.flush() == 3
    assert [m["topic"] for m in client.published] == ["t/a", "t/b", "t/c"]
    assert all(m["qos"] == 1 and m["retain"] is True for m in client.published)
    assert publisher.pending == []


def test_flush_while_offline_keeps_buffer():
    client = FakeMqttClient(connected=False)
    publisher = Publisher(client)
    publisher.publish("t/a", "1")
    assert publisher.flush() == 0
    assert publisher.pending == [("t/a", "1")]


def test_flush_with_nothing_buffered(client):
    assert Publisher(client).flush() == 0


def test_buffer_bound_drops_oldest(caplog):
    client = FakeMqttClient(connected=False)
    publisher = Publisher(client, max_buffer=2)
    with caplog.at_level(logging.WARNING):
        for topic in "abc":
            publisher.publish(f"t/{topic}", topic)
    assert publisher.pending == [("t/b", "b"), ("t/c", "c")]
    assert "t/a" in caplog.text


def test_default_buffer_bound_is_100():
    client = FakeMqttClient(connected=False)
    publisher = Publisher(client)
    for i in range(150):
        publisher.publish(f"t/{i}", str(i))
    pending = publisher.pending
    assert len(pending) == 100
    assert pending[0] == ("t/50", "50")


def test_per_topic_dedup_keeps_only_latest():
    client = FakeMqttClient(connected=False)
    publisher = Publisher(client)
    publisher.publish("t/a", "old")
    publisher.publish("t/a", "new")
    assert publisher.pending == [("t/a", "new")]


def test_dedup_moves_topic_to_newest_position():
    client = FakeMqttClient(connected=False)
    publisher = Publisher(client)
    publisher.publish("t/a", "a1")
    publisher.publish("t/b", "b1")
    publisher.publish("t/a", "a2")
    assert publisher.pending == [("t/b", "b1"), ("t/a", "a2")]


def test_dedup_protects_a_topic_from_being_dropped_as_oldest():
    client = FakeMqttClient(connected=False)
    publisher = Publisher(client, max_buffer=2)
    publisher.publish("t/a", "a1")
    publisher.publish("t/b", "b1")
    publisher.publish("t/a", "a2")  # at capacity, but must not evict anything
    assert publisher.pending == [("t/b", "b1"), ("t/a", "a2")]


def test_publish_after_reconnect_flushes_older_messages_first():
    client = FakeMqttClient(connected=False)
    publisher = Publisher(client)
    publisher.publish("t/a", "a")
    client.connected = True
    assert publisher.publish("t/b", "b") is True
    assert client.payloads == ["a", "b"]


def test_publish_after_reconnect_never_sends_superseded_message():
    client = FakeMqttClient(connected=False)
    publisher = Publisher(client)
    publisher.publish("t/a", "old")
    client.connected = True
    publisher.publish("t/a", "new")
    assert client.payloads == ["new"]


def test_client_exception_buffers_instead_of_raising(client):
    publisher = Publisher(client)
    client.publish_error = OSError("socket gone")
    assert publisher.publish("t/a", "1") is False
    assert publisher.pending == [("t/a", "1")]
    client.publish_error = None
    assert publisher.flush() == 1
    assert client.payloads == ["1"]


def test_nonzero_rc_buffers(client):
    publisher = Publisher(client)
    client.result = SimpleNamespace(rc=4)  # MQTT_ERR_NO_CONN
    assert publisher.publish("t/a", "1") is False
    assert publisher.pending == [("t/a", "1")]


def test_zero_rc_counts_as_sent(client):
    client.result = SimpleNamespace(rc=0)
    assert Publisher(client).publish("t/a", "1") is True


def test_failed_flush_preserves_order_of_remaining_messages():
    class FailsOnB(FakeMqttClient):
        def publish(self, topic, payload, qos=0, retain=False):
            if topic == "t/b" and not self.heal:
                raise OSError("boom")
            return super().publish(topic, payload, qos, retain)

        heal = False

    client = FailsOnB(connected=False)
    publisher = Publisher(client)
    for topic in "abc":
        publisher.publish(f"t/{topic}", topic)
    client.connected = True
    assert publisher.flush() == 1
    assert publisher.pending == [("t/b", "b"), ("t/c", "c")]
    client.heal = True
    assert publisher.flush() == 2
    assert client.payloads == ["a", "b", "c"]


def test_is_connected_reflects_the_client():
    client = FakeMqttClient(connected=False)
    publisher = Publisher(client)
    assert publisher.is_connected() is False
    client.connected = True
    assert publisher.is_connected() is True


def test_invalid_buffer_size_rejected(client):
    with pytest.raises(ValueError):
        Publisher(client, max_buffer=0)


# --- create_mqtt_client -----------------------------------------------------


class RecordingPahoClient:
    """Stands in for paho.mqtt.client.Client to capture how the factory configures it."""

    def __init__(self, api_version, client_id=""):
        self.api_version = api_version
        self.client_id = client_id
        self.credentials = None
        self.tls = False
        self.will = None
        self.reconnect_delay = None
        self.published = []
        self.on_connect = None
        self.on_disconnect = None

    def username_pw_set(self, username, password=None):
        self.credentials = (username, password)

    def tls_set(self):
        self.tls = True

    def will_set(self, topic, payload, qos, retain):
        self.will = (topic, payload, qos, retain)

    def reconnect_delay_set(self, min_delay, max_delay):
        self.reconnect_delay = (min_delay, max_delay)

    def publish(self, topic, payload, qos, retain):
        self.published.append((topic, payload, qos, retain))


@pytest.fixture
def paho_client(monkeypatch):
    import paho.mqtt.client as mqtt

    monkeypatch.setattr(mqtt, "Client", RecordingPahoClient)
    return mqtt


def with_mqtt(config, **changes):
    from dataclasses import replace

    return replace(config, mqtt=replace(config.mqtt, **changes))


STATUS_TOPIC = "studyspace/library/floor2/node-07/status"


def test_factory_uses_v2_callback_api_and_last_will(paho_client, app_config):
    client = create_mqtt_client(app_config)
    assert client.api_version is paho_client.CallbackAPIVersion.VERSION2
    assert client.client_id == "node-07"
    assert client.will == (STATUS_TOPIC, "offline", 1, True)
    low, high = client.reconnect_delay
    assert 0 < low < high


def test_factory_is_anonymous_and_plaintext_by_default(paho_client, app_config):
    client = create_mqtt_client(app_config)
    assert client.credentials is None
    assert client.tls is False


def test_factory_applies_credentials_tls_and_client_id(paho_client, app_config):
    config = with_mqtt(app_config, username="u", password="p", tls=True, client_id="custom")
    client = create_mqtt_client(config)
    assert client.credentials == ("u", "p")
    assert client.tls is True
    assert client.client_id == "custom"


def test_on_connect_publishes_online_then_flushes(paho_client, app_config):
    client = create_mqtt_client(app_config)
    events = []
    client.publish = lambda topic, payload, qos, retain: events.append((topic, payload, qos, retain))
    userdata = SimpleNamespace(flush=lambda: events.append("flush"))
    client.on_connect(client, userdata, None, SimpleNamespace(is_failure=False), None)
    assert events == [(STATUS_TOPIC, "online", 1, True), "flush"]


def test_on_connect_without_publisher_still_announces_online(paho_client, app_config):
    client = create_mqtt_client(app_config)
    client.on_connect(client, None, None, SimpleNamespace(is_failure=False), None)
    assert client.published == [(STATUS_TOPIC, "online", 1, True)]


def test_refused_connection_does_not_publish_or_flush(paho_client, app_config, caplog):
    client = create_mqtt_client(app_config)
    flushed = []
    userdata = SimpleNamespace(flush=lambda: flushed.append(True))
    with caplog.at_level(logging.ERROR):
        client.on_connect(client, userdata, None, SimpleNamespace(is_failure=True), None)
    assert client.published == [] and flushed == []
    assert "refused" in caplog.text


@pytest.mark.parametrize("is_failure, level", [(True, logging.WARNING), (False, logging.INFO)])
def test_on_disconnect_log_level_reflects_cause(paho_client, app_config, caplog, is_failure, level):
    client = create_mqtt_client(app_config)
    with caplog.at_level(logging.INFO):
        client.on_disconnect(client, None, None, SimpleNamespace(is_failure=is_failure), None)
    assert [r.levelno for r in caplog.records] == [level]


def test_factory_works_against_the_real_paho_api(app_config):
    """No network: catches constructor or setter signature drift in paho."""
    client = create_mqtt_client(with_mqtt(app_config, username="u", password="p", tls=True))
    assert client.is_connected() is False


# --- publish_offline --------------------------------------------------------


class OfflineClient(FakeMqttClient):
    def __init__(self, connected=True, wait_error=None):
        super().__init__(connected)
        self.waited = []
        self.wait_error = wait_error
        self.result = self

    def wait_for_publish(self, timeout=None):
        self.waited.append(timeout)
        if self.wait_error:
            raise self.wait_error


def test_publish_offline_sends_retained_and_waits():
    client = OfflineClient()
    publish_offline(client, STATUS_TOPIC, timeout_s=1.5)
    assert client.published == [
        {"topic": STATUS_TOPIC, "payload": "offline", "qos": 1, "retain": True}
    ]
    assert client.waited == [1.5]


def test_publish_offline_skips_when_disconnected():
    client = OfflineClient(connected=False)
    publish_offline(client, STATUS_TOPIC)
    assert client.published == []


@pytest.mark.parametrize("error", [RuntimeError("no conn"), ValueError("queue full")])
def test_publish_offline_tolerates_send_errors(error, caplog):
    client = OfflineClient(wait_error=error)
    with caplog.at_level(logging.WARNING):
        publish_offline(client, STATUS_TOPIC)
    assert "offline" in caplog.text
