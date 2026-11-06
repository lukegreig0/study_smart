"""Node side of the UI socket. Runs on its own thread so a GUI can never stall the node."""

from __future__ import annotations

import logging
import os
import select
import socket
import stat
import threading
from collections.abc import Callable
from typing import Any

from .ui_protocol import MAX_LINE_BYTES, Command, ProtocolError, decode, encode, parse_command, result_message

log = logging.getLogger(__name__)

MAX_CLIENTS = 8
MAX_OUTBOUND_BYTES = 256 * 1024  # a client this far behind has stopped reading

Reply = Callable[[dict[str, Any]], None]


class _Client:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.inbound = bytearray()
        self.outbound = bytearray()


class UiServer:
    """Pushes snapshots to connected GUIs and passes commands to ``on_command``.

    ``on_command(command, reply)`` is called on the server thread and must not block.
    """

    def __init__(self, path: str, on_command: Callable[[Command, Reply], None]) -> None:
        self._path = path
        self._on_command = on_command
        self._lock = threading.Lock()
        self._clients: list[_Client] = []
        self._latest: bytes | None = None
        self._stopping = False
        self._listener: socket.socket | None = None
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)
        self._thread = threading.Thread(target=self._serve, name="ui-server", daemon=True)

    def start(self) -> None:
        """Bind and start serving; raises OSError if the socket can't be created."""
        self._remove_stale_socket()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(self._path)
            os.chmod(self._path, 0o660)  # the GUI runs as a separate user in our group
            listener.listen()
        except OSError:
            listener.close()
            raise
        listener.setblocking(False)
        self._listener = listener
        self._thread.start()

    def stop(self) -> None:
        self._stopping = True
        self._wake()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
        if self._listener is not None:
            self._listener.close()
            self._remove_stale_socket()
        self._wake_r.close()
        self._wake_w.close()

    def publish(self, message: dict[str, Any]) -> None:
        """Send to every client, and to any client connecting later. Never blocks."""
        data = encode(message)
        with self._lock:
            self._latest = data
            for client in self._clients:
                client.outbound += data
        self._wake()

    def _remove_stale_socket(self) -> None:
        try:
            mode = os.lstat(self._path).st_mode
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(mode):
            raise OSError(f"{self._path} exists and is not a socket; refusing to replace it")
        os.unlink(self._path)

    def _wake(self) -> None:
        try:
            self._wake_w.send(b"\0")
        except OSError:  # buffer full means it's already awake; closed means stopping
            pass

    def _serve(self) -> None:
        assert self._listener is not None
        while not self._stopping:
            # Checked here, not after writing: select never reports a stuck client
            # as writable, so that path would never run for the client it's meant for.
            with self._lock:
                stuck = [c for c in self._clients if len(c.outbound) > MAX_OUTBOUND_BYTES]
            for client in stuck:
                self._drop(client, "not reading")
            with self._lock:
                clients = list(self._clients)
                writers = [c.sock for c in clients if c.outbound]
            readable, writable, _ = select.select(
                [self._listener, self._wake_r, *(c.sock for c in clients)], writers, [], 1.0
            )
            if self._wake_r in readable:
                try:
                    self._wake_r.recv(4096)
                except BlockingIOError:
                    pass
            if self._listener in readable:
                self._accept()
            for client in clients:
                if client.sock in readable and not self._read(client):
                    continue
                if client.sock in writable:
                    self._write(client)
        with self._lock:
            clients, self._clients = self._clients, []
        for client in clients:
            client.sock.close()

    def _accept(self) -> None:
        try:
            sock, _ = self._listener.accept()
        except OSError:
            return
        with self._lock:
            if len(self._clients) >= MAX_CLIENTS:
                log.warning("Too many UI clients; rejecting connection")
                sock.close()
                return
            sock.setblocking(False)
            client = _Client(sock)
            if self._latest is not None:
                client.outbound += self._latest
            self._clients.append(client)
        log.info("UI client connected")

    def _drop(self, client: _Client, reason: str) -> None:
        with self._lock:
            if client not in self._clients:
                return
            self._clients.remove(client)
        client.sock.close()
        log.info("UI client disconnected: %s", reason)

    def _read(self, client: _Client) -> bool:
        try:
            data = client.sock.recv(4096)
        except BlockingIOError:
            return True
        except OSError as exc:
            self._drop(client, str(exc))
            return False
        if not data:
            self._drop(client, "closed")
            return False
        client.inbound += data
        while (end := client.inbound.find(b"\n")) != -1:
            line = bytes(client.inbound[:end])
            del client.inbound[: end + 1]
            self._handle_line(client, line)
        if len(client.inbound) > MAX_LINE_BYTES:
            self._drop(client, "line too long")
            return False
        return True

    def _handle_line(self, client: _Client, line: bytes) -> None:
        def reply(message: dict[str, Any]) -> None:
            self._send(client, message)

        command_id = None
        try:
            message = decode(line)
            command_id = message.get("id")
            command = parse_command(message)
        except ProtocolError as exc:
            log.warning("Bad message from UI client: %s", exc)
            reply(result_message(command_id if type(command_id) is int else None, str(exc)))
            return
        self._on_command(command, reply)

    def _send(self, client: _Client, message: dict[str, Any]) -> None:
        with self._lock:
            if client in self._clients:
                client.outbound += encode(message)
        self._wake()

    def _write(self, client: _Client) -> None:
        with self._lock:
            pending = bytes(client.outbound)
        try:
            sent = client.sock.send(pending)
        except BlockingIOError:
            sent = 0
        except OSError as exc:
            self._drop(client, str(exc))
            return
        with self._lock:
            del client.outbound[:sent]
