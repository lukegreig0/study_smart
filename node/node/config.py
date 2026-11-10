"""Node configuration, loaded from environment variables.

Every setting has a default so `python -m node` works out of the box against a
local broker. In Docker, everything is passed with `-e NAME=value`.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

# NODE_ID and SITE end up as MQTT topic levels, so keep them boring: no '/',
# no wildcards ('+', '#'), no spaces, no upper case.
_SLUG = re.compile(r"^[a-z0-9-]+$")

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(ValueError):
    """Raised when an environment variable has an invalid value."""


@dataclass(frozen=True)
class Config:
    node_id: str = "node-floor2"
    site: str = "library"
    broker_host: str = "localhost"
    broker_port: int = 1883
    device_sub: str = "studyspace/library/floor2/+/+/status"
    device_timeout_s: float = 6.0  # 3x the simulator's 2 s heartbeat
    keepalive_s: int = 30
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        for name in ("node_id", "site"):
            value = getattr(self, name)
            if not _SLUG.match(value):
                raise ConfigError(
                    f"{name.upper()}={value!r} is invalid: use only lowercase "
                    "letters, digits and hyphens"
                )
        if not self.broker_host:
            raise ConfigError("BROKER_HOST must not be empty")
        if not 0 < self.broker_port <= 65535:
            raise ConfigError(f"BROKER_PORT={self.broker_port} must be in 1..65535")
        if not self.device_sub or "/" not in self.device_sub:
            raise ConfigError(
                f"DEVICE_SUB={self.device_sub!r} must be a topic filter with at "
                "least two levels (the device id is the second-to-last level)"
            )
        for name in ("device_timeout_s", "keepalive_s"):
            if getattr(self, name) <= 0:
                raise ConfigError(f"{name.upper()} must be a positive number")
        if self.log_level not in _LOG_LEVELS:
            raise ConfigError(
                f"LOG_LEVEL={self.log_level!r} must be one of {', '.join(_LOG_LEVELS)}"
            )

    @property
    def base_topic(self) -> str:
        return f"studyspace/v1/{self.site}/{self.node_id}"

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
                raise ConfigError(
                    f"{name}={raw!r} is not a valid {kind.__name__}"
                ) from None

        return cls(
            node_id=get("NODE_ID", defaults.node_id),
            site=get("SITE", defaults.site),
            broker_host=get("BROKER_HOST", defaults.broker_host),
            broker_port=number("BROKER_PORT", defaults.broker_port, int),
            device_sub=get("DEVICE_SUB", defaults.device_sub),
            device_timeout_s=number("DEVICE_TIMEOUT_S", defaults.device_timeout_s, float),
            keepalive_s=number("KEEPALIVE_S", defaults.keepalive_s, int),
            log_level=get("LOG_LEVEL", defaults.log_level).upper(),
        )

    def log_level_number(self) -> int:
        return logging.getLevelNamesMapping()[self.log_level]
