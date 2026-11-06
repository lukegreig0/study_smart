import json
import logging
import threading
from dataclasses import replace

import pytest

from conftest import FIXED_TIME, FakeClock, FakeMqttClient, NodeRig
from studyspace_node.messages import parse_payload
from studyspace_node.node import Node, Seat
from studyspace_node.occupancy import OccupancyDetector, OccupancySettings, OccupancyState
from studyspace_node.publisher import Publisher
from studyspace_node.sensors import FakeWeightSensor
from studyspace_node.ui_protocol import MARK_OCCUPIED, MARK_VACANT, Command

STATE_TOPIC = "studyspace/library/floor2/desk-07-a/state"
CONFIRM = OccupancySettings().confirm_readings


def messages(rig):
    return [json.loads(p) for p in rig.client.payloads]


def reasons(rig):
    return [m["reason"] for m in messages(rig)]


def become_free(rig):
    rig.ticks(CONFIRM)
    assert rig.detector.state is OccupancyState.FREE


def become_occupied(rig):
    rig.motion.extend([True] * CONFIRM)
    rig.ticks(CONFIRM)
    assert rig.detector.state is OccupancyState.OCCUPIED


class TestPublishOnChange:
    def test_no_publish_while_unknown(self, rig):
        rig.ticks(CONFIRM - 1)
        assert rig.detector.state is OccupancyState.UNKNOWN
        assert rig.client.published == []

    def test_first_known_state_is_published_as_change(self, rig):
        become_free(rig)
        assert len(rig.client.published) == 1
        message = rig.client.published[0]
        assert (message["topic"], message["qos"], message["retain"]) == (STATE_TOPIC, 1, True)

    def test_payload_is_contract_compliant(self, rig):
        become_free(rig)
        parsed = parse_payload(rig.client.payloads[0])
        assert parsed.node_id == "node-07"
        assert parsed.desk_id == "desk-07"
        assert parsed.space_id == "desk-07-a"
        assert parsed.state is OccupancyState.FREE
        assert parsed.healthy is True
        assert parsed.reason == "change"
        assert parsed.source == "sensor"
        assert parsed.timestamp == FIXED_TIME

    def test_transition_publishes_immediately(self, rig):
        rig.motion.extend([False] * CONFIRM + [True] * CONFIRM)
        rig.ticks(CONFIRM * 2)
        assert [m["state"] for m in messages(rig)] == ["free", "occupied"]
        assert reasons(rig) == ["change", "change"]

    def test_no_republish_between_heartbeats(self, rig):
        become_free(rig)
        rig.ticks(10)
        assert len(rig.client.published) == 1

    def test_weight_reading_reaches_the_detector(self, rig):
        rig.weight.extend([70.0] * CONFIRM)
        rig.ticks(CONFIRM)
        assert rig.detector.state is OccupancyState.OCCUPIED


class TestHeartbeat:
    def test_boundary(self, rig):
        become_free(rig)
        rig.clock.advance(59.999)
        rig.node.tick()
        assert reasons(rig) == ["change"]
        rig.clock.advance(0.001)
        rig.node.tick()
        assert reasons(rig) == ["change", "heartbeat"]

    def test_repeats_at_the_configured_interval(self, rig):
        become_free(rig)
        rig.ticks(180)
        assert reasons(rig) == ["change", "heartbeat", "heartbeat", "heartbeat"]

    def test_respects_configured_interval(self, app_config, client, clock):
        config = replace(app_config, mqtt=replace(app_config.mqtt, heartbeat_s=10))
        rig = NodeRig(config, client, clock)
        become_free(rig)
        rig.ticks(10)
        assert reasons(rig) == ["change", "heartbeat"]

    def test_change_restarts_the_heartbeat_interval(self, rig):
        rig.motion.extend([False] * (CONFIRM + 50) + [True] * (CONFIRM + 59))
        rig.ticks(CONFIRM + 50 + CONFIRM)
        assert reasons(rig) == ["change", "change"]
        rig.ticks(59)
        assert reasons(rig) == ["change", "change"]
        rig.motion.extend([True])
        rig.ticks(1)
        assert reasons(rig) == ["change", "change", "heartbeat"]

    def test_no_heartbeat_while_unknown(self, rig):
        rig.motion.extend([True, False] * 100)  # never confirms either way
        rig.ticks(200)
        assert rig.detector.state is OccupancyState.UNKNOWN
        assert rig.client.published == []

    def test_heartbeat_carries_current_state(self, rig):
        become_occupied(rig)
        rig.distance.extend([0.5])
        rig.ticks(60)
        assert messages(rig)[-1]["state"] == "occupied"
        assert messages(rig)[-1]["reason"] == "heartbeat"


class TestHealth:
    def fail_all(self, rig, count):
        error = [OSError("dead")] * count
        rig.motion.extend(error + [False])
        rig.distance.extend(error + [2.0])
        rig.weight.extend(error + [0.0])

    def test_flip_to_unhealthy_is_published_and_then_back(self, rig):
        become_free(rig)
        limit = rig.config.occupancy.max_consecutive_faults
        self.fail_all(rig, limit)
        before = len(rig.client.published)
        rig.ticks(limit - 1)
        assert len(rig.client.published) == before
        rig.ticks(1)
        assert messages(rig)[-1]["healthy"] is False
        assert messages(rig)[-1]["reason"] == "heartbeat"
        assert messages(rig)[-1]["state"] == "free"
        rig.ticks(1)
        assert messages(rig)[-1]["healthy"] is True
        assert messages(rig)[-1]["reason"] == "heartbeat"

    def test_unhealthy_while_unknown_publishes_nothing_but_logs(self, rig, caplog):
        limit = rig.config.occupancy.max_consecutive_faults
        self.fail_all(rig, limit + 5)
        with caplog.at_level(logging.WARNING):
            rig.ticks(limit + 5)
        assert rig.detector.healthy is False
        assert rig.client.published == []
        assert "healthy=False" in caplog.text

    def test_first_publish_after_unhealthy_start_reports_recovered_health(self, rig):
        self.fail_all(rig, 6)
        rig.ticks(6 + CONFIRM)
        assert [m["healthy"] for m in messages(rig)] == [True]


class TestSensorFailures:
    def test_motion_exception_does_not_crash_and_others_are_used(self, rig):
        rig.motion.extend([RuntimeError("gpio")])
        rig.ticks(CONFIRM)
        assert rig.detector.state is OccupancyState.FREE
        assert rig.detector.healthy

    def test_weight_exception_does_not_crash_and_others_are_used(self, rig):
        rig.weight.extend([RuntimeError("hx711")])
        rig.motion.extend([True])
        rig.ticks(CONFIRM)
        assert rig.detector.state is OccupancyState.OCCUPIED

    def test_all_raising_holds_state(self, rig):
        become_free(rig)
        rig.motion.extend([ValueError("x")])
        rig.distance.extend([ValueError("y")])
        rig.weight.extend([ValueError("z")])
        rig.ticks(3)
        assert rig.detector.state is OccupancyState.FREE
        assert rig.detector.consecutive_faults == 3

    def test_failure_logged_once_then_recovery_logged(self, rig, caplog):
        rig.distance.extend([OSError("no echo")] * 3 + [2.0])
        with caplog.at_level(logging.DEBUG, logger="studyspace_node.node"):
            rig.ticks(4)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "distance sensor read failed" in warnings[0].getMessage()
        assert "distance sensor recovered" in caplog.text


class TestRawDataPrivacy:
    def test_raw_readings_are_never_published(self, rig):
        rig.distance.extend([1.2345] * 10)
        rig.weight.extend([71.2345] * 10)
        rig.ticks(10)
        assert rig.client.published
        assert all("1.2345" not in p for p in rig.client.payloads)

    def test_raw_readings_are_not_sent_to_the_screen(self, rig):
        rig.weight.extend([71.2345] * 10)
        rig.ticks(10)
        assert "71.2345" not in json.dumps(rig.node.snapshot())


class TestOfflineBehaviour:
    def test_state_is_buffered_then_delivered_on_flush(self, rig):
        rig.client.connected = False
        become_free(rig)
        rig.ticks(120)
        assert rig.client.published == []
        assert len(rig.publisher.pending) == 1  # heartbeats collapse by topic
        rig.client.connected = True
        rig.publisher.flush()
        assert len(rig.client.published) == 1


def weight_seat(space_id, settings=None):
    return Seat(
        space_id=space_id,
        label=space_id[-1].upper(),
        detector=OccupancyDetector(settings),
        weight=FakeWeightSensor(default=0.0),
    )


class TestMultipleSeats:
    @pytest.fixture
    def seats(self):
        return [weight_seat("desk-07-a"), weight_seat("desk-07-b"), weight_seat("desk-07-c")]

    @pytest.fixture
    def node(self, seats, app_config, client, clock):
        return Node(seats, Publisher(client), app_config, clock=clock, wall_clock=lambda: FIXED_TIME)

    def run_ticks(self, node, clock, count):
        for _ in range(count):
            clock.advance(1.0)
            node.tick()

    def test_each_seat_publishes_to_its_own_topic(self, node, clock, client):
        self.run_ticks(node, clock, CONFIRM)
        assert [m["topic"] for m in client.published] == [
            "studyspace/library/floor2/desk-07-a/state",
            "studyspace/library/floor2/desk-07-b/state",
            "studyspace/library/floor2/desk-07-c/state",
        ]
        assert {json.loads(p)["desk_id"] for p in client.payloads} == {"desk-07"}

    def test_seats_are_independent(self, node, seats, clock, client):
        seats[1].weight.extend([70.0] * 20)
        self.run_ticks(node, clock, CONFIRM)
        states = {json.loads(p)["space_id"]: json.loads(p)["state"] for p in client.payloads}
        assert states == {"desk-07-a": "free", "desk-07-b": "occupied", "desk-07-c": "free"}

    def test_one_failing_seat_does_not_affect_the_others(self, node, seats, clock, client):
        seats[0].weight.extend([OSError("dead")])
        self.run_ticks(node, clock, CONFIRM + 5)
        assert seats[0].detector.healthy is False
        assert seats[0].detector.state is OccupancyState.UNKNOWN
        assert seats[1].detector.state is OccupancyState.FREE
        assert seats[1].detector.healthy is True

    def test_offline_buffer_keeps_every_seat(self, node, clock, client):
        client.connected = False
        self.run_ticks(node, clock, CONFIRM + 120)
        client.connected = True
        node._publisher.flush()
        assert len(client.published) == 3

    def test_close_closes_every_sensor(self):
        closed = []

        class Sensor:
            def __init__(self, name):
                self.name = name

            def close(self):
                closed.append(self.name)

        seat = Seat("s", OccupancyDetector(), motion=Sensor("m"), weight=Sensor("w"))
        seat.close()
        assert closed == ["m", "w"]


class TestCommands:
    def submit(self, rig, name, space_id="desk-07-a", command_id=1):
        replies = []
        rig.node.submit_command(Command(command_id, name, space_id), replies.append)
        rig.node.process_commands()
        return replies

    def test_mark_occupied_publishes_immediately_without_a_tick(self, rig):
        become_free(rig)
        replies = self.submit(rig, MARK_OCCUPIED)
        assert replies == [{"type": "result", "id": 1, "ok": True, "error": None}]
        last = messages(rig)[-1]
        assert (last["state"], last["source"], last["reason"]) == ("occupied", "manual", "change")

    def test_hold_expiry_is_published_as_sensor_free(self, rig):
        become_free(rig)
        self.submit(rig, MARK_OCCUPIED)
        rig.ticks(1800)
        last = messages(rig)[-1]
        assert (last["state"], last["source"], last["reason"]) == ("free", "sensor", "change")

    def test_mark_occupied_while_already_occupied_publishes_the_source_change(self, rig):
        become_occupied(rig)
        count = len(rig.client.published)
        self.submit(rig, MARK_OCCUPIED)
        assert len(rig.client.published) == count + 1
        last = messages(rig)[-1]
        assert (last["state"], last["source"], last["reason"]) == ("occupied", "manual", "heartbeat")

    def test_mark_vacant_after_someone_left_publishes_free(self, rig):
        become_occupied(rig)
        rig.motion.extend([False])
        rig.ticks(1)  # sensor now reads empty, vacancy timeout still running
        assert rig.detector.state is OccupancyState.OCCUPIED
        replies = self.submit(rig, MARK_VACANT)
        assert replies[0]["ok"] is True
        assert messages(rig)[-1]["state"] == "free"

    def test_mark_vacant_refused_when_someone_is_seated(self, rig):
        rig.weight.extend([70.0])
        rig.ticks(CONFIRM)
        count = len(rig.client.published)
        replies = self.submit(rig, MARK_VACANT)
        assert replies[0]["ok"] is False
        assert "sitting" in replies[0]["error"]
        assert len(rig.client.published) == count

    def test_unknown_seat_is_an_error_not_a_crash(self, rig):
        replies = self.submit(rig, MARK_OCCUPIED, space_id="nope")
        assert replies[0]["ok"] is False
        assert "nope" in replies[0]["error"]

    def test_full_queue_replies_busy(self, rig):
        replies = []
        for i in range(40):
            rig.node.submit_command(Command(i, MARK_OCCUPIED, "desk-07-a"), replies.append)
        assert [r["id"] for r in replies] == list(range(32, 40))
        assert all(r["ok"] is False for r in replies)

    def test_commands_are_not_applied_until_processed_on_the_node_thread(self, rig):
        become_free(rig)
        rig.node.submit_command(Command(1, MARK_OCCUPIED, "desk-07-a"), lambda _: None)
        assert rig.detector.state is OccupancyState.FREE


class TestSnapshot:
    def test_contents(self, rig):
        become_free(rig)
        rig.detector.mark_occupied(rig.clock.now)
        rig.clock.advance(100)
        assert rig.node.snapshot() == {
            "type": "snapshot",
            "node_id": "node-07",
            "desk_id": "desk-07",
            "mqtt_connected": True,
            "poll_interval_s": 1.0,
            "seats": [
                {
                    "space_id": "desk-07-a",
                    "label": "A",
                    "position_deg": None,
                    "state": "occupied",
                    "source": "manual",
                    "healthy": True,
                    "hold_remaining_s": 1700.0,
                }
            ],
        }

    def test_unknown_is_shown_to_the_screen(self, rig):
        assert rig.node.snapshot()["seats"][0]["state"] == "unknown"

    def test_sent_on_every_tick_and_after_commands(self, rig):
        sent = []
        rig.node.attach_ui(sent.append)
        rig.ticks(2)
        assert len(sent) == 2
        rig.node.submit_command(Command(1, MARK_OCCUPIED, "desk-07-a"), lambda _: None)
        rig.node.process_commands()
        assert len(sent) == 3
        assert sent[-1]["seats"][0]["source"] == "manual"

    def test_no_snapshot_without_commands_processed(self, rig):
        sent = []
        rig.node.attach_ui(sent.append)
        rig.node.process_commands()
        assert sent == []

    def test_works_with_no_ui_attached(self, rig):
        rig.ticks(1)
        rig.node.process_commands()


class AdvancingStop(threading.Event):
    """A stop event whose wait() advances the fake clock, and stops after a limit."""

    def __init__(self, clock, limit):
        super().__init__()
        self.clock = clock
        self.limit = limit
        self.timeouts = []

    def wait(self, timeout=None):
        self.timeouts.append(timeout)
        self.clock.advance(timeout)
        if len(self.timeouts) >= self.limit:
            self.set()
        return self.is_set()


class TestRun:
    def test_exits_immediately_if_stop_already_set(self, rig):
        stop = threading.Event()
        stop.set()
        rig.node.run(stop)
        assert rig.client.published == []

    def test_ticks_at_the_poll_interval(self, rig):
        ticks = []
        original = rig.node.tick
        rig.node.tick = lambda: (ticks.append(rig.clock.now), original())
        start = rig.clock.now
        rig.node.run(AdvancingStop(rig.clock, 45))
        assert len(ticks) == 5
        assert ticks == pytest.approx([start + i for i in range(5)])

    def test_commands_are_handled_between_ticks(self, app_config, client, clock):
        config = replace(app_config, sensors=replace(app_config.sensors, poll_interval_s=3600))
        rig = NodeRig(config, client, clock)
        replies = []
        stop = AdvancingStop(clock, 3)

        original_wait = stop.wait

        def wait(timeout=None):
            if len(stop.timeouts) == 0:
                rig.node.submit_command(Command(1, MARK_OCCUPIED, "desk-07-a"), replies.append)
            return original_wait(timeout)

        stop.wait = wait
        rig.node.run(stop)
        assert len(replies) == 1
        assert max(stop.timeouts) <= 0.1  # never sleeps through the whole poll interval

    def test_stop_interrupts_the_wait(self, app_config, client):
        config = replace(app_config, sensors=replace(app_config.sensors, poll_interval_s=3600))
        rig = NodeRig(config, client, FakeClock())
        stop = threading.Event()
        ticked = threading.Event()
        original_tick = rig.node.tick
        rig.node.tick = lambda: (original_tick(), ticked.set())
        thread = threading.Thread(target=rig.node.run, args=(stop,), daemon=True)
        thread.start()
        assert ticked.wait(timeout=5)
        stop.set()
        thread.join(timeout=5)
        assert not thread.is_alive()

    def test_loop_survives_unexpected_tick_error(self, rig, caplog):
        calls = []

        def bad_tick():
            calls.append(1)
            raise RuntimeError("surprise")

        rig.node.tick = bad_tick
        with caplog.at_level(logging.ERROR):
            rig.node.run(AdvancingStop(rig.clock, 25))
        assert len(calls) >= 2
        assert "Unexpected error" in caplog.text


@pytest.mark.parametrize("step", [0.5, 1.0, 7.0])
def test_end_to_end_cycle_through_a_seat_session(rig, step):
    """free -> sits down -> stands up -> free after the vacancy timeout."""
    timeout = rig.config.occupancy.vacancy_timeout_s
    leaving_ticks = int(timeout / step) + 6  # + median window catch-up
    rig.weight.extend([0.0] * CONFIRM + [70.0] * (CONFIRM + 10) + [0.0])
    rig.ticks(CONFIRM + CONFIRM + 10 + leaving_ticks, step=step)
    changes = [m["state"] for m in messages(rig) if m["reason"] == "change"]
    assert changes == ["free", "occupied", "free"]
