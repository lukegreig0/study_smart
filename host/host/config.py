"""Host configuration, loaded from environment variables.

Every setting has a default so `python -m host` works out of the box against a
local broker. In Docker, everything is passed with `-e NAME=value`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(ValueError):
    """Raised when an environment variable has an invalid value."""


@dataclass(frozen=True)
class Config:
    broker_host: str = "localhost"
    broker_port: int = 1883
    client_id: str = "host-primary"
    db_path: str = "./studyspace.db"
    # 0 disables the host-side node timeout. See README "Node liveness": the
    # node only publishes when something changes, so a quiet room would look
    # like a dead node. The broker's Last Will is the primary death signal.
    node_timeout_s: float = 0.0
    keepalive_s: int = 30
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if not self.broker_host:
            raise ConfigError("BROKER_HOST must not be empty")
        if not 0 < self.broker_port <= 65535:
            raise ConfigError(f"BROKER_PORT={self.broker_port} must be in 1..65535")
        if not self.client_id:
            raise ConfigError("CLIENT_ID must not be empty")
        if not self.db_path:
            raise ConfigError("DB_PATH must not be empty")
        if self.node_timeout_s < 0:
            raise ConfigError(
                f"NODE_TIMEOUT_S={self.node_timeout_s} must be >= 0 (0 disables it)"
            )
        if self.keepalive_s <= 0:
            raise ConfigError(f"KEEPALIVE_S={self.keepalive_s} must be a positive integer")
        if self.log_level not in _LOG_LEVELS:
            raise ConfigError(
                f"LOG_LEVEL={self.log_level!r} must be one of {', '.join(_LOG_LEVELS)}"
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        """Build a Config from `env` (defaults to os.environ).

        Unset or empty variables fall back to the dataclass default.
        """
        env = os.environ if env is None else env
        defaults = cls()  # valid by construction, used for fallbacks

        def get(name: str, default: str) -> str:
            value = env.get(name, "").strip()
            return value if value else default

        def number(name: str, default: float, kind: type) -> float | int:
            raw = get(name, str(default))
            try:
                return kind(raw)
            except ValueError:
                raise ConfigError(f"{name}={raw!r} is not a valid {kind.__name__}") from None

        return cls(
            broker_host=get("BROKER_HOST", defaults.broker_host),
            broker_port=number("BROKER_PORT", defaults.broker_port, int),
            client_id=get("CLIENT_ID", defaults.client_id),
            db_path=get("DB_PATH", defaults.db_path),
            node_timeout_s=number("NODE_TIMEOUT_S", defaults.node_timeout_s, float),
            keepalive_s=number("KEEPALIVE_S", defaults.keepalive_s, int),
            log_level=get("LOG_LEVEL", defaults.log_level).upper(),
        )

    def log_level_number(self) -> int:
        return logging.getLevelNamesMapping()[self.log_level]
