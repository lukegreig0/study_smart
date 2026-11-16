"""Entry point: `python -m api`."""

from __future__ import annotations

import logging
import sys

import uvicorn

from .app import create_app
from .config import Config, ConfigError

log = logging.getLogger("api")


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
    log.info("serving %s on http://%s:%s", config.db_path, config.api_host, config.api_port)

    # uvicorn is the web server that runs the FastAPI app. It handles
    # SIGINT/SIGTERM itself: finish in-flight requests, then exit.
    uvicorn.run(
        create_app(config),
        host=config.api_host,
        port=config.api_port,
        log_level=config.log_level.lower(),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
