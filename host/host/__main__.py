"""Entry point: `python -m host`."""

from __future__ import annotations

import logging
import signal
import sqlite3
import sys
import threading

from .config import Config, ConfigError
from .mqtt_host import MqttHost
from .store import Store

log = logging.getLogger("host")


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

    try:
        store = Store.connect(config.db_path)
    except sqlite3.Error as exc:
        log.error("cannot open database %s: %s", config.db_path, exc)
        return 2
    log.info("database %s (journal_mode=%s), broker %s:%s, node timeout %s",
             config.db_path, store.journal_mode(), config.broker_host, config.broker_port,
             f"{config.node_timeout_s:g}s" if config.node_timeout_s > 0 else "off")

    # Ctrl+C sends SIGINT; `docker stop` sends SIGTERM. The handler only sets
    # an Event; the main loop sees it within one queue timeout and the
    # `finally` in MqttHost.run() does the real shutdown. Doing real work in a
    # signal handler is asking for trouble; setting an Event is safe.
    stop = threading.Event()

    def request_stop(signum, frame):
        log.info("received %s", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    MqttHost(config, store).run(stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
