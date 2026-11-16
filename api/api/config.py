"""API configuration, loaded from environment variables.

Defaults are set up for running locally from the api/ folder next to a
locally running host (python -m host in host/ writes host/studyspace.db).
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
    db_path: str = "../host/studyspace.db"
    # 127.0.0.1 = only this machine can reach the API. Use 0.0.0.0 to listen
    # on every network interface (needed inside a container with port mapping).
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if not self.db_path:
            raise ConfigError("DB_PATH must not be empty")
        if not self.api_host:
            raise ConfigError("API_HOST must not be empty")
        if not 0 < self.api_port <= 65535:
            raise ConfigError(f"API_PORT={self.api_port} must be in 1..65535")
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
        defaults = cls()

        def get(name: str, default: str) -> str:
            value = env.get(name, "").strip()
            return value if value else default

        raw_port = get("API_PORT", str(defaults.api_port))
        try:
            port = int(raw_port)
        except ValueError:
            raise ConfigError(f"API_PORT={raw_port!r} is not a valid int") from None

        return cls(
            db_path=get("DB_PATH", defaults.db_path),
            api_host=get("API_HOST", defaults.api_host),
            api_port=port,
            log_level=get("LOG_LEVEL", defaults.log_level).upper(),
        )

    def log_level_number(self) -> int:
        return logging.getLevelNamesMapping()[self.log_level]
