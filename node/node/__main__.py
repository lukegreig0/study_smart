"""Entry point: `python -m node`."""

from __future__ import annotations

import logging
import signal
import sys
import threading

from .config import Config, ConfigError
from .mqtt_node import MqttNode

log = logging.getLogger("node")


def main() -> int:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        logging.basicConfig(format="%(levelname)s %(message)s")
        log.error("invalid configuration: %s", exc)
        return 2

    logging.basicConfig(
        level=config.log_level_number(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log.info("starting %s (site %s), subscribing to %s",
             config.node_id, config.site, config.device_sub)

    # Ctrl+C sends SIGINT; `docker stop` sends SIGTERM. Either way we only set
    # a flag. The main loop checks it a few times a second and exits, and the
    # `finally` in MqttNode.run() does the clean shutdown. Doing real work
    # inside a signal handler is asking for trouble; setting an Event is safe.
    stop = threading.Event()

    def request_stop(signum, frame):
        log.info("received %s", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    MqttNode(config).run(stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
