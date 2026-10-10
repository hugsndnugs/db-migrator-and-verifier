"""Load source and target connection settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when connection settings are missing or inconsistent."""


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    ssl: bool

    def __repr__(self) -> str:
        return (
            f"DatabaseConfig(host={self.host!r}, port={self.port!r}, user={self.user!r}, "
            f"password='***', database={self.database!r}, ssl={self.ssl!r})"
        )


@dataclass(frozen=True)
class AppConfig:
    source: DatabaseConfig
    target: DatabaseConfig


def load_config(env_path: str | None = None) -> AppConfig:
    """Load both database profiles from the environment or a `.env` file."""
    if env_path:
        path = Path(env_path)
        if not path.is_file():
            raise ConfigError(f"Environment file not found: {path}")
        load_dotenv(path, override=True)
    else:
        default_path = Path.cwd() / ".env"
        if default_path.is_file():
            load_dotenv(default_path, override=True)

    source = _database_config("SOURCE")
    target = _database_config("TARGET")
    if _is_same_database(source, target):
        raise ConfigError(
            "Source and target refer to the same database "
            f"({source.host}:{source.port}/{source.database}). "
            "Refusing to copy a database onto itself."
        )
    return AppConfig(source=source, target=target)


def _database_config(prefix: str) -> DatabaseConfig:
    host = _required(f"{prefix}_HOST")
    user = _required(f"{prefix}_USER")
    database = _required(f"{prefix}_DATABASE")
    return DatabaseConfig(
        host=host,
        port=_port(f"{prefix}_PORT", os.environ.get(f"{prefix}_PORT")),
        user=user,
        password=os.environ.get(f"{prefix}_PASSWORD", ""),
        database=database,
        ssl=_bool_flag(f"{prefix}_SSL", os.environ.get(f"{prefix}_SSL")),
    )


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required setting {name}")
    return value


def _port(name: str, raw: str | None) -> int:
    text = (raw or "").strip()
    if not text:
        return 3306
    try:
        port = int(text)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ConfigError(f"{name} must be between 1 and 65535")
    return port


def _bool_flag(name: str, raw: str | None) -> bool:
    text = (raw or "").strip().lower()
    if text in {"", "0", "false", "no", "off"}:
        return False
    if text in {"1", "true", "yes", "on"}:
        return True
    raise ConfigError(f"{name} must be true or false")


def _is_same_database(source: DatabaseConfig, target: DatabaseConfig) -> bool:
    return (
        source.host.casefold() == target.host.casefold()
        and source.port == target.port
        and source.database.casefold() == target.database.casefold()
    )
