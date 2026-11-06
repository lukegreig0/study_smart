"""GUI side of the UI socket: connects, reconnects, and hands messages over a queue."""

from __future__ import annotations

import logging
import queue
import socket
import threading
from typing import Any

from .ui_protocol import MAX_LINE_BYTES, Command, ProtocolError, decode, encode

log = logging.getLogger(__name__)

CONNECTED = {"type": "connected"}
DISCONNECTED = {"type": "disconnected"}


class UiClient:
    """Keeps a connection to the node, retrying forever so the GUI survives node restarts."""

    def __init__(self, path: str, retry_s: float = 1.0) -> None:
        self._path = path
        self._retry_s = retry_s
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="ui-client", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)

    def send(self, command: Command) -> bool:
        """Returns False if not connected, in which case nothing was sent."""
        with self._send_lock:
            if self._sock is None:
                return False
            try:
                self._sock.sendall(encode(command.to_message()))
            except OSError as exc:
                log.warning("Could not send command: %s", exc)
                return False
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(0.1)  # also how often the reader checks for stop
                sock.connect(self._path)
            except OSError:
                sock.close()
                self._stop.wait(self._retry_s)
                continue
            with self._send_lock:
                self._sock = sock
            self.messages.put(CONNECTED)
            try:
                self._read_until_closed(sock)
            finally:
                with self._send_lock:
                    self._sock = None
                sock.close()
                self.messages.put(DISCONNECTED)

    def _read_until_closed(self, sock: socket.socket) -> None:
        buffer = bytearray()
        while not self._stop.is_set():
            try:
                data = sock.recv(4096)
            except TimeoutError:
                continue
            except OSError:
                return
            if not data:
                return
            buffer += data
            while (end := buffer.find(b"\n")) != -1:
                line = bytes(buffer[:end])
                del buffer[: end + 1]
                try:
                    self.messages.put(decode(line))
                except ProtocolError as exc:
                    log.warning("Bad message from node: %s", exc)
            if len(buffer) > MAX_LINE_BYTES:
                log.warning("Oversized message from node; reconnecting")
                return
