"""Processing rules: how a parsed message changes the database.

No paho, no clock reads. Every call gets two times passed in:
  now_utc   host wall clock as an ISO string, for anything stored
  now_mono  host time.monotonic(), only for the node liveness timer
            (wall clocks can jump; monotonic clocks can't)

Everything here is idempotent: applying the same message twice leaves the
database as it was (apart from nodes.last_seen, which records when we last
heard from a node). That matters because on every (re)start or reconnect the
broker replays each node's retained birth/death and latest data message.

Timestamp comparisons are plain string comparisons: every stored timestamp
has the same fixed-width "YYYY-MM-DDTHH:MM:SSZ" form (messages.format_utc),
and for that form string order == time order.
"""

from __future__ import annotations

import logging

from .messages import Message, NodeBirth, NodeData, NodeDeath
from .store import Store

log = logging.getLogger(__name__)


class Ingest:
    """Applies messages to a Store and tracks node liveness.

    The only in-memory state is `_heard`: when (monotonic) we last heard from
    each online node. It deliberately isn't stored: monotonic readings mean
    nothing after a restart.
    """

    def __init__(self, store: Store, node_timeout_s: float = 0.0) -> None:
        self.store = store
        self.node_timeout_s = node_timeout_s  # <= 0 disables the timeout
        self._heard: dict[str, float] = {}

    # ---------------------------------------------------------------- input

    def apply(self, msg: Message, now_utc: str, now_mono: float) -> None:
        if isinstance(msg, NodeBirth):
            self._birth(msg, now_utc, now_mono)
        elif isinstance(msg, NodeData):
            self._data(msg, now_utc, now_mono)
        elif isinstance(msg, NodeDeath):
            self._death(msg, now_utc)
        else:
            raise TypeError(f"not a host message: {msg!r}")

    def _birth(self, msg: NodeBirth, now_utc: str, now_mono: float) -> None:
        with self.store.transaction():
            # The node row must exist first: devices reference it.
            self.store.upsert_node(msg.node_id, msg.site, online=True, last_seen=now_utc)
            self.store.upsert_devices(msg.node_id, msg.devices)
        self._heard[msg.node_id] = now_mono
        log.info("node %s born with %d device(s)", msg.node_id, len(msg.devices))

    def _death(self, msg: NodeDeath, now_utc: str) -> None:
        self._heard.pop(msg.node_id, None)
        if self.store.get_node(msg.node_id) is None:
            # Typically a retained death from a node we've never seen born.
            log.info("death of unknown node %s ignored", msg.node_id)
            return
        self._mark_dead(msg.node_id, now_utc, reason="death")

    def _mark_dead(self, node_id: str, now_utc: str, reason: str) -> None:
        with self.store.transaction():
            changed = self.store.mark_node_dead(node_id, now_utc)
        if changed:
            log.info("node %s dead (%s): %s -> unknown", node_id, reason, ", ".join(changed))
        else:
            log.debug("node %s dead (%s), nothing changed", node_id, reason)

    def _data(self, msg: NodeData, now_utc: str, now_mono: float) -> None:
        node = self.store.get_node(msg.node_id)
        if node is None:
            log.warning("data from unknown node %s ignored (waiting for its birth)", msg.node_id)
            return
        if not node.online:
            # Sparkplug rule: after a death, a node's next message is always a
            # birth (the node publishes birth + full snapshot on every
            # connect). Data from a dead node is therefore stale; in practice
            # it's the broker replaying the retained data message after a
            # retained death, and applying it would bring a dead room back.
            log.info("data from offline node %s ignored until its next birth", msg.node_id)
            return

        log.debug("data from %s: %s, changed=%s", msg.node_id,
                  "full snapshot" if msg.is_full_snapshot else "delta", list(msg.changed))

        with self.store.transaction():
            self.store.touch_node(msg.node_id, now_utc)
            for device_id, report in msg.devices.items():
                self._apply_report(msg, device_id, report)
        self._heard[msg.node_id] = now_mono

    def _apply_report(self, msg: NodeData, device_id: str, report) -> None:
        dev = self.store.get_device(device_id)
        if dev is None:
            log.warning("device %s in data from %s is unknown; skipped until its birth",
                        device_id, msg.node_id)
            return
        if dev.node_id != msg.node_id:
            log.warning("device %s reported by %s but belongs to %s; skipped",
                        device_id, msg.node_id, dev.node_id)
            return

        # Diff against what we have. We don't trust "changed": a message we
        # missed would leave us wrong forever, while the diff self-corrects
        # on the next full snapshot.
        new_state = report.state
        if new_state == dev.state:
            return

        # Stale/out-of-order check, on the *message* time: a message generated
        # before our current state was established can't override it.
        if dev.since is not None and msg.timestamp < dev.since:
            log.info("stale data for %s ignored (message %s < stored since %s)",
                     device_id, msg.timestamp, dev.since)
            return

        # When did the change happen? The entry's "since" (the device's own
        # occupancy-change time) if it has one, else the message time. But
        # "since" can legitimately be older than our stored since: a device
        # occupied since 12:00 drops offline at 12:05 (-> unknown, since
        # 12:05) and comes back at 12:06 still reporting since=12:00. Its
        # transition unknown -> occupied happened at 12:06, so in that case
        # we use the message time, which keeps the history in time order.
        if report.since is not None and (dev.since is None or report.since >= dev.since):
            changed_at = report.since
        else:
            changed_at = msg.timestamp

        self.store.set_device_state(device_id, new_state, changed_at)
        log.info("%s (%s): %s -> %s at %s", device_id, dev.space_id, dev.state,
                 new_state, changed_at)

    # ------------------------------------------------------------- liveness

    def check_node_timeouts(self, now_utc: str, now_mono: float) -> list[str]:
        """Treat online nodes that have been silent for more than
        node_timeout_s as dead. Returns the node ids marked dead now.

        A node that's online in the database but that we haven't heard from
        this session (we just started) gets its timer started now, so it has
        a full timeout period to show up (its retained birth usually arrives
        immediately anyway).
        """
        if self.node_timeout_s <= 0:
            return []
        dead = []
        for node_id in self.store.list_online_nodes():
            last = self._heard.setdefault(node_id, now_mono)
            if now_mono - last > self.node_timeout_s:
                log.warning("node %s silent for %.0fs, treating as dead", node_id, now_mono - last)
                self._heard.pop(node_id)
                self._mark_dead(node_id, now_utc, reason="timeout")
                dead.append(node_id)
        return dead
