"""Desk touchscreen: a separate program that shows the node's state and sends commands.

    studyspace-gui --socket /run/studyspace-node/ui.sock

It holds no occupancy logic, and imports neither the sensor nor the MQTT code. If it
crashes the node carries on, and if the node stops the screen says so and reconnects.

``DeskViewModel`` and the layout are pure and tested without a display; only
``render`` and ``run`` touch pygame.
"""

from __future__ import annotations

import argparse
import itertools
import logging
import math
import signal
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .ui_protocol import MARK_OCCUPIED, MARK_VACANT, Command

log = logging.getLogger("studyspace_node.gui")

DEFAULT_SOCKET = "/run/studyspace-node/ui.sock"
MIN_STALE_AFTER_S = 10.0
TOAST_S = 4.0
SELECTION_TIMEOUT_S = 30.0
FPS = 10

Rect = tuple[int, int, int, int]  # x, y, width, height


def _in_rect(rect: Rect, x: int, y: int) -> bool:
    return rect[0] <= x < rect[0] + rect[2] and rect[1] <= y < rect[1] + rect[3]


@dataclass(frozen=True)
class Layout:
    """Screen geometry: the desk diagram on the left, the action panel on the right."""

    width: int
    height: int

    @property
    def panel(self) -> Rect:
        panel_width = self.width // 3
        return (self.width - panel_width, 0, panel_width, self.height)

    @property
    def _diagram_size(self) -> int:
        return min(self.width - self.panel[2], self.height)

    @property
    def seat_radius(self) -> int:
        return max(28, self._diagram_size // 9)

    @property
    def _orbit(self) -> int:
        """Distance from the desk centre to each seat centre, keeping seats on screen."""
        return self._diagram_size // 2 - self.seat_radius - 6

    @property
    def desk_center(self) -> tuple[int, int]:
        return ((self.width - self.panel[2]) // 2, self.height // 2)

    @property
    def desk_radius(self) -> int:
        return max(16, self._orbit - self.seat_radius - 8)

    def seat_center(self, seats: Sequence[dict[str, Any]], index: int) -> tuple[int, int]:
        degrees = seats[index].get("position_deg")
        if degrees is None:
            degrees = index * 360 / len(seats)
        cx, cy = self.desk_center
        angle = math.radians(degrees)
        return (round(cx + self._orbit * math.sin(angle)), round(cy - self._orbit * math.cos(angle)))

    def seat_at(self, seats: Sequence[dict[str, Any]], x: int, y: int) -> int | None:
        for index in range(len(seats)):
            sx, sy = self.seat_center(seats, index)
            if (x - sx) ** 2 + (y - sy) ** 2 <= self.seat_radius**2:
                return index
        return None

    @property
    def occupied_button(self) -> Rect:
        px, _, pw, _ = self.panel
        return (px + 12, self.height // 2 - 8, pw - 24, self.height // 5)

    @property
    def vacant_button(self) -> Rect:
        x, y, w, h = self.occupied_button
        return (x, y + h + 12, w, h)


def seat_status(seat: dict[str, Any]) -> tuple[str, str]:
    """Returns (style, text) for a seat from a snapshot."""
    if seat["state"] == "unknown":
        return "unknown", "Starting"
    if seat["source"] == "manual":
        minutes = math.ceil(seat.get("hold_remaining_s", 0) / 60)
        return "held", f"Held {minutes} min"
    if seat["state"] == "occupied":
        return "occupied", "Occupied"
    return "free", "Free"


class DeskViewModel:
    """Everything the screen shows, derived from node messages and taps."""

    def __init__(self, layout: Layout, idle_after_s: float = 60.0) -> None:
        self.layout = layout
        self.idle_after_s = idle_after_s
        self.connected = False
        self.snapshot: dict[str, Any] | None = None
        self.selected: str | None = None
        self.toast: str | None = None
        self._toast_until = 0.0
        self._snapshot_at = 0.0
        self._last_touch = -math.inf
        self._selected_at = 0.0
        self._ids = itertools.count(1)
        self._pending: dict[int, str] = {}

    @property
    def seats(self) -> list[dict[str, Any]]:
        return self.snapshot["seats"] if self.snapshot else []

    def handle(self, message: dict[str, Any], now: float) -> None:
        kind = message.get("type")
        if kind == "connected":
            self.connected = True
        elif kind == "disconnected":
            self.connected = False
            self.snapshot = None
            self.selected = None
            self._pending.clear()
        elif kind == "snapshot":
            self.snapshot = message
            self._snapshot_at = now
        elif kind == "result":
            self._handle_result(message, now)

    def _handle_result(self, message: dict[str, Any], now: float) -> None:
        action = self._pending.pop(message.get("id"), None)
        if action is None:
            return
        if message.get("ok"):
            self.selected = None
            self._show_toast(action, now)
        else:
            self._show_toast(message.get("error") or "Could not do that", now)

    def _show_toast(self, text: str, now: float) -> None:
        self.toast = text
        self._toast_until = now + TOAST_S

    def tick(self, now: float) -> None:
        if self.toast and now >= self._toast_until:
            self.toast = None
        if self.selected and now - self._selected_at >= SELECTION_TIMEOUT_S:
            self.selected = None

    def is_live(self, now: float) -> bool:
        """True if what's on screen can be trusted: connected, with recent data."""
        if not self.connected or self.snapshot is None:
            return False
        stale_after = max(MIN_STALE_AFTER_S, 3 * self.snapshot.get("poll_interval_s", 0))
        return now - self._snapshot_at <= stale_after

    def is_idle(self, now: float) -> bool:
        return now - self._last_touch >= self.idle_after_s

    def selected_seat(self) -> dict[str, Any] | None:
        return next((s for s in self.seats if s["space_id"] == self.selected), None)

    def free_summary(self) -> str:
        free = sum(1 for s in self.seats if s["state"] == "free")
        return f"{free} of {len(self.seats)} free"

    def tap(self, x: int, y: int, now: float) -> Command | None:
        """Handle a touch; returns a command for the node if the tap asked for one."""
        was_idle = self.is_idle(now)
        self._last_touch = now
        if was_idle or not self.is_live(now):
            return None  # the first touch only wakes the screen

        selected = self.selected_seat()
        if selected is not None:
            if _in_rect(self.layout.occupied_button, x, y):
                return self._command(MARK_OCCUPIED, selected, "Marked occupied")
            if _in_rect(self.layout.vacant_button, x, y):
                return self._command(MARK_VACANT, selected, "Marked vacant")

        index = self.layout.seat_at(self.seats, x, y)
        if index is not None:
            self.selected = self.seats[index]["space_id"]
            self._selected_at = now
        else:
            self.selected = None
        return None

    def _command(self, name: str, seat: dict[str, Any], done_text: str) -> Command:
        command = Command(next(self._ids), name, seat["space_id"])
        self._pending[command.id] = done_text
        return command

    def send_failed(self, command: Command, now: float) -> None:
        self._pending.pop(command.id, None)
        self._show_toast("Could not reach the node", now)


_COLOURS = {
    "free": (46, 160, 90),
    "occupied": (200, 60, 60),
    "held": (222, 150, 40),
    "unknown": (110, 115, 125),
}
_BACKGROUND = (22, 24, 30)
_PANEL = (36, 39, 48)
_DESK = (92, 72, 52)
_TEXT = (240, 240, 240)
_MUTED = (165, 170, 180)
_DIM_ALPHA = 150


def _draw_text(pygame: Any, surface: Any, text: str, size: int, center: tuple[int, int], colour=_TEXT) -> None:
    image = pygame.font.Font(None, size).render(text, True, colour)
    surface.blit(image, image.get_rect(center=center))


def _draw_panel(pygame: Any, surface: Any, model: DeskViewModel, now: float) -> None:
    layout = model.layout
    px, _, pw, height = layout.panel
    center_x = px + pw // 2
    pygame.draw.rect(surface, _PANEL, layout.panel)

    if not model.is_live(now):
        _draw_text(pygame, surface, "Not available", 36, (center_x, height // 2 - 20))
        _draw_text(pygame, surface, "Waiting for the node", 26, (center_x, height // 2 + 16), _MUTED)
        return

    seat = model.selected_seat()
    if seat is None:
        _draw_text(pygame, surface, model.free_summary(), 44, (center_x, height // 3))
        _draw_text(pygame, surface, "Tap a seat to mark it", 26, (center_x, height // 3 + 42), _MUTED)
    else:
        _draw_text(pygame, surface, f"Seat {seat['label']}", 44, (center_x, height // 5))
        _draw_text(pygame, surface, seat_status(seat)[1], 30, (center_x, height // 5 + 40), _MUTED)
        for rect, text, style in (
            (layout.occupied_button, "Mark occupied", "occupied"),
            (layout.vacant_button, "Mark vacant", "free"),
        ):
            pygame.draw.rect(surface, _COLOURS[style], rect, border_radius=10)
            _draw_text(pygame, surface, text, 30, (rect[0] + rect[2] // 2, rect[1] + rect[3] // 2))

    if not model.snapshot.get("mqtt_connected", True):
        _draw_text(pygame, surface, "Network offline", 22, (center_x, height - 18), _MUTED)


def render(pygame: Any, surface: Any, model: DeskViewModel, now: float) -> None:
    """Draw one frame of the model onto ``surface``."""
    layout = model.layout
    live = model.is_live(now)
    surface.fill(_BACKGROUND)

    pygame.draw.circle(surface, _DESK, layout.desk_center, layout.desk_radius)
    if live:
        desk_id = model.snapshot.get("desk_id", "")
        _draw_text(pygame, surface, desk_id, 28, layout.desk_center)

        for index, seat in enumerate(model.seats):
            center = layout.seat_center(model.seats, index)
            style, text = seat_status(seat)
            pygame.draw.circle(surface, _COLOURS[style], center, layout.seat_radius)
            if seat["space_id"] == model.selected:
                pygame.draw.circle(surface, _TEXT, center, layout.seat_radius + 4, width=4)
            _draw_text(pygame, surface, seat["label"], 40, (center[0], center[1] - 12))
            _draw_text(pygame, surface, text, 22, (center[0], center[1] + 16))
            if not seat.get("healthy", True):
                _draw_text(pygame, surface, "Sensor fault", 20, (center[0], center[1] + layout.seat_radius + 14))

    _draw_panel(pygame, surface, model, now)

    if model.toast:
        x, y = layout.desk_center
        image = pygame.font.Font(None, 30).render(model.toast, True, _TEXT)
        box = image.get_rect(center=(x, layout.height - 30)).inflate(24, 14)
        pygame.draw.rect(surface, (0, 0, 0), box, border_radius=8)
        surface.blit(image, image.get_rect(center=box.center))

    # Dim rather than blank when idle: people walking up still need to see what's free.
    # Never dim a "not available" message, which needs to stay readable.
    if live and model.is_idle(now):
        shade = pygame.Surface(surface.get_size(), pygame.SRCALPHA)
        shade.fill((0, 0, 0, _DIM_ALPHA))
        surface.blit(shade, (0, 0))


def _parse_size(text: str) -> tuple[int, int]:
    try:
        width, height = (int(part) for part in text.lower().split("x"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected WIDTHxHEIGHT, got {text!r}") from None
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError(f"size must be positive, got {text!r}")
    return width, height


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="studyspace-gui", description=__doc__.split("\n")[0])
    parser.add_argument("--socket", default=DEFAULT_SOCKET, help=f"node UI socket (default: {DEFAULT_SOCKET})")
    parser.add_argument("--windowed", action="store_true", help="run in a window instead of fullscreen")
    parser.add_argument("--size", type=_parse_size, default=(800, 480), help="window size (default: 800x480)")
    parser.add_argument("--idle-s", type=float, default=60.0, help="dim after this many seconds untouched")
    parser.add_argument(
        "--log-level",
        default="INFO",
        type=str.upper,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    return parser


def run(args: argparse.Namespace) -> int:
    import pygame

    from .ui_client import UiClient

    pygame.init()
    if args.windowed:
        surface = pygame.display.set_mode(args.size)
    else:
        surface = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        pygame.mouse.set_visible(False)
    pygame.display.set_caption("Study space")

    model = DeskViewModel(Layout(*surface.get_size()), idle_after_s=args.idle_s)
    client = UiClient(args.socket)
    client.start()

    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
    frame_clock = pygame.time.Clock()
    try:
        while not stopping:
            now = time.monotonic()
            while not client.messages.empty():
                model.handle(client.messages.get_nowait(), now)
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    stopping.append(True)
                # SDL also reports touches as mouse presses, so this covers the touchscreen.
                elif event.type == pygame.MOUSEBUTTONDOWN:
                    command = model.tap(*event.pos, now)
                    if command is not None and not client.send(command):
                        model.send_failed(command, now)
            model.tick(now)
            render(pygame, surface, model, now)
            pygame.display.flip()
            frame_clock.tick(FPS)
    except KeyboardInterrupt:
        pass
    finally:
        client.stop()
        pygame.quit()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        import pygame  # noqa: F401
    except ImportError:
        log.error('pygame-ce is not installed. Run: pip install ".[gui]"')
        return 1
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
