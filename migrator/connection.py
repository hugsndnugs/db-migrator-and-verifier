"""Open MySQL connections for the source and target databases."""

from __future__ import annotations

import pymysql
from pymysql.cursors import DictCursor, SSCursor

from migrator.config import DatabaseConfig


class DatabaseConnectionError(Exception):
    """Raised when a database connection cannot be opened."""


def connect(config: DatabaseConfig):
    """Open a buffered connection and pin the session time zone to UTC."""
    return _connect(config, cursorclass=DictCursor)


def connect_streaming(config: DatabaseConfig):
    """Open an unbuffered connection for streaming table reads."""
    return _connect(config, cursorclass=SSCursor)


def _connect(config: DatabaseConfig, *, cursorclass):
    kwargs = {
        "host": config.host,
        "port": config.port,
        "user": config.user,
        "password": config.password,
        "database": config.database,
        "charset": "utf8mb4",
        "cursorclass": cursorclass,
        "autocommit": False,
        "connect_timeout": 10,
        "init_command": "SET time_zone = '+00:00'",
    }
    if config.ssl:
        # A non-empty dict makes PyMySQL require TLS. Certificate checks stay off
        # unless the server is configured with a CA outside this tool.
        kwargs["ssl"] = {"check_hostname": False}
    try:
        return pymysql.connect(**kwargs)
    except pymysql.Error as exc:
        raise DatabaseConnectionError(
            f"Could not connect to {config.host}:{config.port}/{config.database} "
            f"as {config.user}: {exc}"
        ) from exc
