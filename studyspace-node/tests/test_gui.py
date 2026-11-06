"""The view model needs no display; rendering runs on SDL's headless driver if available."""

import ast
import subprocess
import sys
import time

import pytest

from conftest import REPO_ROOT
from studyspace_node import gui
from studyspace_node.gui import DeskViewModel, Layout, seat_status
from studyspace_node.ui_protocol import MARK_OCCUPIED, MARK_VACANT, Command

LAYOUT = Layout(800, 480)
NOW = 1000.0


def seat(space_id="a", label="A", state="free", source="sensor", healthy=True, hold=0.0, position=None):
    return {
        "space_id": space_id,
        "label": label,
        "position_deg": position,
        "state": state,
        "source": source,
        "healthy": healthy,
        "hold_remaining_s": hold,
    }


def snapshot(*seats, mqtt=True, poll=2.0):
    return {
        "type": "snapshot",
        "node_id": "n",
        "desk_id": "desk-07",
        "mqtt_connected": mqtt,
        "poll_interval_s": poll,
        "seats": list(seats),
    }


THREE = (seat("a", "A", position=0), seat("b", "B", position=120), seat("c", "C", position=240))


@pytest.fixture
def model():
    m = DeskViewModel(LAYOUT, idle_after_s=60)
    m.handle({"type": "connected"}, NOW)
    m.handle(snapshot(*THREE), NOW)
    m.tap(0, 0, NOW)  # wake the screen
    return m


def center_of(model, index):
    return model.layout.seat_center(model.seats, index)


def button_center(rect):
    return (rect[0] + rect[2] // 2, rect[1] + rect[3] // 2)


class TestLayout:
    def test_seats_follow_configured_positions(self):
        top = LAYOUT.seat_center([seat(position=0)], 0)
        right = LAYOUT.seat_center([seat(position=90)], 0)
        cx, cy = LAYOUT.desk_center
        assert top[0] == cx and top[1] < cy
        assert right[0] > cx and right[1] == cy

    @pytest.mark.parametrize("count", [1, 2, 3, 4, 6])
    def test_unpositioned_seats_are_spaced_without_overlap_and_on_screen(self, count):
        seats = [seat(str(i)) for i in range(count)]
        centers = [LAYOUT.seat_center(seats, i) for i in range(count)]
        r = LAYOUT.seat_radius
        for i, (x, y) in enumerate(centers):
            assert r <= x <= LAYOUT.panel[0] - r
            assert r <= y <= LAYOUT.height - r
            for ox, oy in centers[i + 1 :]:
                assert (x - ox) ** 2 + (y - oy) ** 2 > (2 * r) ** 2

    def test_hit_test(self):
        seats = list(THREE)
        x, y = LAYOUT.seat_center(seats, 1)
        assert LAYOUT.seat_at(seats, x, y) == 1
        assert LAYOUT.seat_at(seats, x + LAYOUT.seat_radius - 1, y) == 1
        assert LAYOUT.seat_at(seats, *LAYOUT.desk_center) is None

    def test_buttons_are_on_screen_and_do_not_overlap(self):
        o, v = LAYOUT.occupied_button, LAYOUT.vacant_button
        assert o[1] + o[3] <= v[1]
        assert v[1] + v[3] <= LAYOUT.height
        assert o[0] >= LAYOUT.panel[0]
        assert o[3] >= 48  # comfortably finger-sized


class TestSeatStatus:
    @pytest.mark.parametrize(
        "kwargs, expected",
        [
            ({"state": "unknown"}, ("unknown", "Starting")),
            ({"state": "free"}, ("free", "Free")),
            ({"state": "occupied"}, ("occupied", "Occupied")),
            ({"state": "occupied", "source": "manual", "hold": 1800}, ("held", "Held 30 min")),
            ({"state": "occupied", "source": "manual", "hold": 61}, ("held", "Held 2 min")),
            ({"state": "occupied", "source": "manual", "hold": 1}, ("held", "Held 1 min")),
        ],
    )
    def test_status(self, kwargs, expected):
        assert seat_status(seat(**kwargs)) == expected


class TestConnectionState:
    def test_not_live_until_a_snapshot_arrives(self):
        m = DeskViewModel(LAYOUT)
        assert not m.is_live(NOW)
        m.handle({"type": "connected"}, NOW)
        assert not m.is_live(NOW)
        m.handle(snapshot(*THREE), NOW)
        assert m.is_live(NOW)

    def test_disconnect_discards_old_data(self, model):
        model.selected = "a"
        model.handle({"type": "disconnected"}, NOW)
        assert not model.is_live(NOW)
        assert model.seats == []
        assert model.selected is None

    def test_hung_node_goes_stale(self, model):
        assert model.is_live(NOW + 10)
        assert not model.is_live(NOW + 10.1)

    def test_stale_limit_scales_with_slow_polling(self):
        m = DeskViewModel(LAYOUT)
        m.handle({"type": "connected"}, NOW)
        m.handle(snapshot(*THREE, poll=30.0), NOW)
        assert m.is_live(NOW + 90)
        assert not m.is_live(NOW + 91)

    def test_unknown_message_types_are_ignored(self, model):
        model.handle({"type": "future"}, NOW)
        model.handle({}, NOW)
        assert model.is_live(NOW)


class TestTaps:
    def test_tap_seat_selects_it_and_sends_nothing(self, model):
        assert model.tap(*center_of(model, 1), NOW) is None
        assert model.selected == "b"
        assert model.selected_seat()["label"] == "B"

    def test_tap_elsewhere_deselects(self, model):
        model.tap(*center_of(model, 1), NOW)
        assert model.tap(*LAYOUT.desk_center, NOW) is None
        assert model.selected is None

    def test_buttons_do_nothing_with_no_seat_selected(self, model):
        assert model.tap(*button_center(LAYOUT.occupied_button), NOW) is None

    @pytest.mark.parametrize(
        "rect, name", [(LAYOUT.occupied_button, MARK_OCCUPIED), (LAYOUT.vacant_button, MARK_VACANT)]
    )
    def test_buttons_send_a_command_for_the_selected_seat(self, model, rect, name):
        model.tap(*center_of(model, 2), NOW)
        command = model.tap(*button_center(rect), NOW)
        assert command == Command(1, name, "c")

    def test_command_ids_increase(self, model):
        model.tap(*center_of(model, 0), NOW)
        first = model.tap(*button_center(LAYOUT.occupied_button), NOW)
        second = model.tap(*button_center(LAYOUT.occupied_button), NOW)
        assert second.id == first.id + 1

    def test_seat_that_vanishes_from_snapshot_cannot_be_commanded(self, model):
        model.tap(*center_of(model, 2), NOW)
        model.handle(snapshot(seat("a")), NOW)
        assert model.tap(*button_center(LAYOUT.occupied_button), NOW) is None

    def test_no_commands_when_not_live(self, model):
        model.tap(*center_of(model, 0), NOW)
        model.handle({"type": "disconnected"}, NOW)
        assert model.tap(*button_center(LAYOUT.occupied_button), NOW) is None

    def test_no_commands_when_stale(self, model):
        model.tap(*center_of(model, 0), NOW)
        assert model.tap(*button_center(LAYOUT.occupied_button), NOW + 20) is None

    def test_selection_times_out(self, model):
        model.tap(*center_of(model, 0), NOW)
        model.tick(NOW + 29)
        assert model.selected == "a"
        model.tick(NOW + 30)
        assert model.selected is None


class TestIdle:
    def test_idle_at_startup(self):
        assert DeskViewModel(LAYOUT).is_idle(NOW)

    def test_idle_after_timeout(self, model):
        assert not model.is_idle(NOW + 59)
        assert model.is_idle(NOW + 60)

    def test_first_touch_only_wakes(self, model):
        model.handle(snapshot(*THREE), NOW + 100)
        assert model.is_idle(NOW + 100)
        assert model.tap(*center_of(model, 0), NOW + 100) is None
        assert model.selected is None
        assert not model.is_idle(NOW + 100)
        model.tap(*center_of(model, 0), NOW + 101)
        assert model.selected == "a"


class TestResults:
    def press(self, model, rect=LAYOUT.vacant_button):
        model.tap(*center_of(model, 0), NOW)
        return model.tap(*button_center(rect), NOW)

    def test_success_shows_toast_and_clears_selection(self, model):
        command = self.press(model)
        model.handle({"type": "result", "id": command.id, "ok": True, "error": None}, NOW)
        assert model.toast == "Marked vacant"
        assert model.selected is None

    def test_refusal_shows_the_nodes_reason_and_keeps_selection(self, model):
        command = self.press(model)
        model.handle({"type": "result", "id": command.id, "ok": False, "error": "Someone is sitting here"}, NOW)
        assert model.toast == "Someone is sitting here"
        assert model.selected == "a"

    def test_refusal_without_a_reason_still_says_something(self, model):
        command = self.press(model)
        model.handle({"type": "result", "id": command.id, "ok": False, "error": None}, NOW)
        assert model.toast

    def test_result_for_unknown_command_is_ignored(self, model):
        model.handle({"type": "result", "id": 999, "ok": True}, NOW)
        assert model.toast is None

    def test_toast_expires(self, model):
        command = self.press(model)
        model.handle({"type": "result", "id": command.id, "ok": True}, NOW)
        model.tick(NOW + 3.9)
        assert model.toast is not None
        model.tick(NOW + 4)
        assert model.toast is None

    def test_send_failure_is_shown(self, model):
        command = self.press(model)
        model.send_failed(command, NOW)
        assert "node" in model.toast


def test_free_summary(model):
    assert model.free_summary() == "3 of 3 free"
    model.handle(snapshot(seat("a", state="occupied"), seat("b"), seat("c", state="unknown")), NOW)
    assert model.free_summary() == "1 of 3 free"


class TestIndependence:
    GUI_MODULES = ["gui.py", "ui_client.py", "ui_protocol.py"]

    @pytest.mark.parametrize("name", GUI_MODULES)
    def test_gui_source_does_not_import_node_internals(self, name):
        tree = ast.parse((REPO_ROOT / "src" / "studyspace_node" / name).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        forbidden = {"occupancy", "sensors", "publisher", "node", "messages", "config", "gpiozero", "paho"}
        assert not {i for i in imported if i.split(".")[0] in forbidden}

    def test_importing_gui_loads_no_sensor_or_mqtt_code(self):
        code = (
            "import sys, studyspace_node.gui, studyspace_node.ui_client;"
            "bad = [m for m in sys.modules if m.startswith(('paho', 'gpiozero'))"
            " or m.endswith(('.occupancy', '.sensors', '.publisher', '.node'))];"
            "sys.exit(repr(bad) if bad else 0)"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


class TestArgs:
    def test_defaults(self):
        args = gui.build_arg_parser().parse_args([])
        assert args.socket == gui.DEFAULT_SOCKET
        assert args.size == (800, 480)
        assert args.windowed is False

    def test_size(self):
        assert gui.build_arg_parser().parse_args(["--size", "1024x600"]).size == (1024, 600)

    @pytest.mark.parametrize("bad", ["big", "0x5", "10", "ax b"])
    def test_bad_size(self, bad):
        with pytest.raises(SystemExit):
            gui.build_arg_parser().parse_args(["--size", bad])

    def test_missing_pygame_gives_a_helpful_error(self, monkeypatch, caplog):
        monkeypatch.setitem(sys.modules, "pygame", None)
        assert gui.main([]) == 1
        assert "pip install" in caplog.text


class TestRender:
    @pytest.fixture
    def pg(self, monkeypatch):
        monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
        pygame = pytest.importorskip("pygame")
        pygame.init()
        yield pygame
        pygame.quit()

    @pytest.fixture
    def surface(self, pg):
        return pg.display.set_mode((800, 480))

    @pytest.mark.parametrize(
        "kwargs, colour",
        [
            ({"state": "free"}, "free"),
            ({"state": "occupied"}, "occupied"),
            ({"state": "occupied", "source": "manual", "hold": 600}, "held"),
            ({"state": "unknown"}, "unknown"),
        ],
    )
    def test_seat_colour_reflects_state(self, pg, surface, model, kwargs, colour):
        model.handle(snapshot(seat("a", position=0, **kwargs)), NOW)
        gui.render(pg, surface, model, NOW)
        x, y = model.layout.seat_center(model.seats, 0)
        edge = (x, y - model.layout.seat_radius + 4)  # clear of the text
        assert surface.get_at(edge)[:3] == gui._COLOURS[colour]

    def test_not_live_draws_no_seats(self, pg, surface, model):
        center = center_of(model, 0)
        model.handle({"type": "disconnected"}, NOW)
        gui.render(pg, surface, model, NOW)
        assert surface.get_at(center)[:3] == gui._BACKGROUND

    def test_not_available_message_is_never_dimmed(self, pg, surface):
        fresh = DeskViewModel(LAYOUT)
        assert fresh.is_idle(NOW)
        gui.render(pg, surface, fresh, NOW)
        assert surface.get_at((LAYOUT.panel[0] + 2, 2))[:3] == gui._PANEL

    def test_every_view_renders(self, pg, surface, model):
        model.handle(snapshot(seat("a", healthy=False, position=0), seat("b", position=120), mqtt=False), NOW)
        gui.render(pg, surface, model, NOW)
        model.tap(*center_of(model, 0), NOW)
        gui.render(pg, surface, model, NOW)
        model._show_toast("Hello", NOW)
        gui.render(pg, surface, model, NOW)

    def test_idle_dims_but_still_shows_state(self, pg, surface, model):
        x, y = center_of(model, 0)
        edge = (x, y - model.layout.seat_radius + 4)
        gui.render(pg, surface, model, NOW)
        bright = surface.get_at(edge)[:3]
        gui.render(pg, surface, model, NOW + 9)
        assert surface.get_at(edge)[:3] == bright
        model.idle_after_s = 5
        gui.render(pg, surface, model, NOW + 9)
        dimmed = surface.get_at(edge)[:3]
        assert dimmed != bright and sum(dimmed) > 0


class TestRunLoop:
    """gui.run() against SDL's headless driver with no node present."""

    @pytest.fixture
    def pg(self, monkeypatch):
        monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
        return pytest.importorskip("pygame")

    def args(self, tmp_path, *extra):
        return gui.build_arg_parser().parse_args(["--socket", str(tmp_path / "none.sock"), *extra])

    def run_events(self, pg, monkeypatch, tmp_path, events, *extra):
        """Posts events on the first frame and QUIT on the next."""
        frames = []
        real_render = gui.render

        def render(pygame, surface, model, now):
            real_render(pygame, surface, model, now)
            frames.append(model)
            if len(frames) == 1:
                for event in events():
                    pygame.event.post(event)
            elif len(frames) == 2:
                pygame.event.post(pygame.event.Event(pygame.QUIT))

        monkeypatch.setattr(gui, "render", render)
        assert gui.run(self.args(tmp_path, *extra)) == 0
        return frames

    def test_quits_cleanly_with_no_node_running(self, pg, monkeypatch, tmp_path):
        frames = self.run_events(pg, monkeypatch, tmp_path, lambda: [], "--windowed")
        assert not frames[0].connected

    def test_fullscreen_mode(self, pg, monkeypatch, tmp_path):
        self.run_events(pg, monkeypatch, tmp_path, lambda: [])

    def test_touch_when_no_node_is_harmless(self, pg, monkeypatch, tmp_path):
        tap = lambda: [pg.event.Event(pg.MOUSEBUTTONDOWN, pos=(10, 10), button=1)]
        frames = self.run_events(pg, monkeypatch, tmp_path, tap, "--windowed")
        assert not frames[0].is_idle(time.monotonic())

    def test_main_runs_the_loop(self, pg, monkeypatch, tmp_path):
        monkeypatch.setattr(gui, "run", lambda args: 7)
        assert gui.main(["--socket", str(tmp_path / "x")]) == 7

    def test_failed_send_is_reported_to_the_model(self, pg, monkeypatch, tmp_path):
        from studyspace_node import ui_client

        class DeadClient(ui_client.UiClient):
            def send(self, command):
                return False

        monkeypatch.setattr(ui_client, "UiClient", DeadClient)
        reported = []

        class Model(DeskViewModel):
            def tap(self, x, y, now):
                return Command(1, MARK_VACANT, "a")

            def send_failed(self, command, now):
                reported.append(command)

        monkeypatch.setattr(gui, "DeskViewModel", Model)
        tap = lambda: [pg.event.Event(pg.MOUSEBUTTONDOWN, pos=(10, 10), button=1)]
        self.run_events(pg, monkeypatch, tmp_path, tap, "--windowed")
        assert reported == [Command(1, MARK_VACANT, "a")]
