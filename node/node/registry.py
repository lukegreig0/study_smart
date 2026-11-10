"""The node's picture of its devices. Pure logic: no MQTT, no clocks.

Every method that depends on time takes `now`, a reading from the node's
monotonic clock (time.monotonic()). We never use the device's own timestamp
for liveness: device clocks can be wrong, and a stale retained message carries
an old timestamp anyway. Device timestamps are only passed through as "since".
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from .messages import Birth, Death, Heartbeat, Message

log = logging.getLogger(__name__)


@dataclass
class Device:
    id: str
    space_id: str
    device_type: str
    firmware: str
    occupied: bool
    since: str  # device timestamp of the last occupancy change
    birth_timestamp: str  # timestamp of the birth we last accepted
    last_seen: float  # node monotonic clock
    online: bool = True

    def metadata(self) -> dict:
        return {
            "id": self.id,
            "space_id": self.space_id,
            "device_type": self.device_type,
            "firmware": self.firmware,
        }


@dataclass
class Events:
    """What a message (or a timeout check) changed.

    changed: device ids whose data-topic state changed -> publish data.
    rebirth: the device list itself changed -> publish the node birth again.
    """

    changed: list[str] = field(default_factory=list)
    rebirth: bool = False

    def __bool__(self) -> bool:
        return bool(self.changed) or self.rebirth


class Registry:
    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s
        self._devices: dict[str, Device] = {}

    def __contains__(self, device_id: str) -> bool:
        return device_id in self._devices

    def get(self, device_id: str) -> Device | None:
        return self._devices.get(device_id)

    # ---------------------------------------------------------------- input

    def handle(self, msg: Message, now: float) -> Events:
        if isinstance(msg, Birth):
            return self._birth(msg, now)
        if isinstance(msg, Heartbeat):
            return self._heartbeat(msg, now)
        if isinstance(msg, Death):
            return self._death(msg)
        raise TypeError(f"not a southbound message: {msg!r}")

    def _birth(self, msg: Birth, now: float) -> Events:
        dev = self._devices.get(msg.id)
        if dev is None:
            # A birth may be a stale retained one from before we started.
            # Register it anyway; if the device is really gone it will not
            # heartbeat and check_timeouts() marks it offline.
            self._devices[msg.id] = Device(
                id=msg.id,
                space_id=msg.space_id,
                device_type=msg.device_type,
                firmware=msg.firmware,
                occupied=msg.occupied,
                since=msg.timestamp,
                birth_timestamp=msg.timestamp,
                last_seen=now,
            )
            log.info("device %s registered (space %s)", msg.id, msg.space_id)
            return Events(changed=[msg.id], rebirth=True)

        if msg.timestamp == dev.birth_timestamp:
            # Exactly the birth we already processed. This happens when we
            # reconnect and the broker re-delivers the retained birth. Its
            # "occupied" is the value at boot time and may be out of date
            # (later changes arrive in non-retained heartbeats), so applying
            # it would undo real changes. It also says nothing about whether
            # the device is alive right now, so don't touch last_seen.
            log.debug("ignoring re-delivered birth from %s", msg.id)
            return Events()

        # The device rebooted or reconnected and sent a fresh birth.
        events = Events()
        before = dev.metadata()
        dev.space_id, dev.device_type, dev.firmware = (
            msg.space_id, msg.device_type, msg.firmware,
        )
        if dev.metadata() != before:
            events.rebirth = True  # the device list in our birth is stale
        if dev.occupied != msg.occupied:
            dev.occupied = msg.occupied
            dev.since = msg.timestamp
            events.changed.append(msg.id)
        if not dev.online:
            dev.online = True
            if msg.id not in events.changed:
                events.changed.append(msg.id)
        dev.birth_timestamp = msg.timestamp
        dev.last_seen = now
        log.info("device %s re-born", msg.id)
        return events

    def _heartbeat(self, msg: Heartbeat, now: float) -> Events:
        dev = self._devices.get(msg.id)
        if dev is None:
            # We can't describe a device we never saw a birth for (no
            # space_id, no occupancy). It will be picked up at its next birth.
            log.warning("heartbeat from unknown device %s ignored", msg.id)
            return Events()

        dev.last_seen = now
        events = Events()
        if not dev.online:
            dev.online = True
            events.changed.append(msg.id)
            log.info("device %s back online", msg.id)
        if msg.occupied is not None and msg.occupied != dev.occupied:
            dev.occupied = msg.occupied
            dev.since = msg.timestamp
            if msg.id not in events.changed:
                events.changed.append(msg.id)
            log.info("device %s occupied=%s", msg.id, msg.occupied)
        return events

    def _death(self, msg: Death) -> Events:
        dev = self._devices.get(msg.id)
        if dev is None:
            # Typically a retained death from a previous session: the broker
            # hands it to us on subscribe. Nothing to mark offline.
            log.info("death from unknown device %s ignored", msg.id)
            return Events()
        if not dev.online:
            return Events()
        dev.online = False
        log.info("device %s died", msg.id)
        return Events(changed=[msg.id])

    def check_timeouts(self, now: float) -> list[str]:
        """Mark devices offline that have been silent for too long.

        Returns the ids that went offline in this call. A device that is
        already offline is not reported again.
        """
        went_offline = []
        for dev in self._devices.values():
            if dev.online and now - dev.last_seen > self.timeout_s:
                dev.online = False
                went_offline.append(dev.id)
                log.info(
                    "device %s timed out (silent for %.1fs)", dev.id, now - dev.last_seen
                )
        return went_offline

    # --------------------------------------------------------------- output

    def snapshot(self, ids: Iterable[str] | None = None) -> dict[str, dict]:
        """The "devices" object of the data payload, sorted by id.

        With `ids`, only those devices are included (a delta); without, every
        known device (the full room).
        """
        wanted = self._devices if ids is None else set(ids) & self._devices.keys()
        out: dict[str, dict] = {}
        for dev_id in sorted(wanted):
            dev = self._devices[dev_id]
            if dev.online:
                out[dev_id] = {"online": True, "occupied": dev.occupied, "since": dev.since}
            else:
                # When a device is offline we don't know if the desk is in use,
                # so we report nothing rather than a stale value.
                out[dev_id] = {"online": False}
        return out

    def birth_devices(self) -> list[dict]:
        """The "devices" list of the node birth: every known device, online or not."""
        return [self._devices[d].metadata() for d in sorted(self._devices)]
