"""Copy schema objects and table data from the source database to the target."""

from __future__ import annotations

import pymysql
from pymysql.cursors import SSCursor

from migrator.config import AppConfig
from migrator.connection import DatabaseConnectionError, connect_streaming
from migrator.schema import (
    SchemaError,
    fetch_create_statement,
    fetch_snapshot,
    migration_block_reason,
    prepare_create_sql,
    quote_ident,
    quote_ident_param,
)


class MigrationError(Exception):
    """Raised when a copy cannot be started or completed."""


def migrate(
    source_conn,
    target_conn,
    config: AppConfig,
    *,
    overwrite: bool,
    dry_run: bool,
    batch_size: int,
    log=print,
) -> None:
    """Copy schema and data. Existing target objects require `--overwrite`."""
    if batch_size < 1:
        raise MigrationError("--batch-size must be at least 1")

    source_snapshot = fetch_snapshot(source_conn, config.source.database)
    target_snapshot = fetch_snapshot(target_conn, config.target.database)
    blocked = migration_block_reason(
        target_snapshot,
        overwrite=overwrite,
        database=config.target.database,
    )
    if blocked:
        raise MigrationError(blocked)

    if dry_run:
        log(format_dry_run(source_snapshot))
        return

    creates = _load_create_statements(source_conn, source_snapshot)
    total_rows = 0
    try:
        _set_bulk_mode(target_conn, enabled=True)
        _copy_sql_mode(source_conn, target_conn)
        if overwrite:
            log(f"Dropping existing objects in {config.target.database}")
            _drop_objects(target_conn, target_snapshot)
        _create_tables(target_conn, creates["tables"], config)
        total_rows = _copy_all_tables(
            target_conn,
            config,
            source_snapshot,
            batch_size,
            log,
        )
        _create_views(target_conn, creates["views"], config)
        _create_simple_objects(target_conn, "trigger", creates["triggers"], config)
        _create_simple_objects(target_conn, "routine", creates["routines"], config)
        _create_simple_objects(target_conn, "event", creates["events"], config)
    except (pymysql.Error, DatabaseConnectionError) as exc:
        raise MigrationError(
            f"Migration failed: {exc}. The target may be partially copied; "
            "re-run with --overwrite."
        ) from exc
    finally:
        try:
            target_conn.rollback()
        except pymysql.Error:
            pass
        _set_bulk_mode(target_conn, enabled=False)

    log(
        f"Copied {len(source_snapshot['tables'])} tables "
        f"({total_rows} rows), {len(source_snapshot['views'])} views, "
        f"{len(source_snapshot['triggers'])} triggers, "
        f"{len(source_snapshot['routines'])} routines, "
        f"{len(source_snapshot['events'])} events."
    )


def format_dry_run(snapshot: dict) -> str:
    lines = ["Dry run: would copy"]
    for name in sorted(snapshot["tables"]):
        estimate = snapshot["tables"][name].get("estimated_rows")
        if estimate is None:
            lines.append(f"  table {name}")
        else:
            lines.append(f"  table {name} (~{estimate} rows)")
    for key, label in (
        ("views", "view"),
        ("triggers", "trigger"),
        ("routines", "routine"),
        ("events", "event"),
    ):
        for name in sorted(snapshot[key]):
            lines.append(f"  {label} {name}")
    if len(lines) == 1:
        lines.append("  (no objects)")
    return "\n".join(lines)


def _load_create_statements(connection, snapshot: dict) -> dict:
    creates = {"tables": [], "views": [], "triggers": [], "routines": [], "events": []}
    try:
        with connection.cursor() as cursor:
            for name in snapshot["tables"]:
                creates["tables"].append(
                    (name, fetch_create_statement(cursor, "TABLE", name))
                )
            for name in snapshot["views"]:
                creates["views"].append(
                    (name, fetch_create_statement(cursor, "VIEW", name))
                )
            for name in snapshot["triggers"]:
                creates["triggers"].append(
                    (name, fetch_create_statement(cursor, "TRIGGER", name))
                )
            for name, routine in snapshot["routines"].items():
                kind = "PROCEDURE" if routine["routine_type"].upper() == "PROCEDURE" else "FUNCTION"
                creates["routines"].append((name, fetch_create_statement(cursor, kind, name)))
            for name in snapshot["events"]:
                creates["events"].append(
                    (name, fetch_create_statement(cursor, "EVENT", name))
                )
    except (pymysql.Error, SchemaError) as exc:
        raise MigrationError(f"Could not read CREATE statements: {exc}") from exc
    return creates


def _create_tables(connection, tables: list[tuple[str, str]], config: AppConfig) -> None:
    with connection.cursor() as cursor:
        for name, statement in tables:
            sql = prepare_create_sql(statement, config.source.database, config.target.database)
            _execute(cursor, sql, "table", name)
    connection.commit()


def _create_views(connection, views: list[tuple[str, str]], config: AppConfig) -> None:
    pending = [
        (name, prepare_create_sql(statement, config.source.database, config.target.database))
        for name, statement in views
    ]
    errors: dict[str, Exception] = {}
    progress = True
    while pending and progress:
        progress = False
        next_pending = []
        for name, statement in pending:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(statement)
                connection.commit()
            except pymysql.Error as exc:
                try:
                    connection.rollback()
                except pymysql.Error:
                    pass
                errors[name] = exc
                next_pending.append((name, statement))
            else:
                errors.pop(name, None)
                progress = True
        pending = next_pending
    if pending:
        details = "; ".join(f"{name}: {errors[name]}" for name, _ in pending)
        raise MigrationError(
            f"Failed to create views: {details}. "
            "The target may be partially copied; re-run with --overwrite."
        )


def _create_simple_objects(
    connection,
    kind: str,
    objects: list[tuple[str, str]],
    config: AppConfig,
) -> None:
    with connection.cursor() as cursor:
        for name, statement in objects:
            sql = prepare_create_sql(statement, config.source.database, config.target.database)
            _execute(cursor, sql, kind, name)
    connection.commit()


def _copy_all_tables(target_conn, config: AppConfig, snapshot: dict, batch_size: int, log) -> int:
    total = 0
    stream = connect_streaming(config.source)
    try:
        _begin_read_snapshot(stream)
        for name in snapshot["tables"]:
            total += _copy_table(
                stream,
                target_conn,
                name,
                snapshot["tables"][name]["columns"],
                batch_size,
                log,
            )
        stream.rollback()
    finally:
        stream.close()
    return total


def _copy_table(stream, target_conn, table: str, columns: list[dict], batch_size: int, log) -> int:
    insertable = [column for column in columns if not column["is_generated"]]
    if not insertable:
        log(f"copied {table}: 0 rows (no insertable columns)")
        return 0
    selected = ", ".join(quote_ident(column["name"]) for column in insertable)
    inserted = ", ".join(quote_ident_param(column["name"]) for column in insertable)
    placeholders = ", ".join(["%s"] * len(insertable))
    select_sql = f"SELECT {selected} FROM {quote_ident(table)}"
    insert_sql = (
        f"INSERT INTO {quote_ident_param(table)} ({inserted}) VALUES ({placeholders})"
    )
    copied = 0
    with stream.cursor(SSCursor) as source_cursor, target_conn.cursor() as target_cursor:
        source_cursor.execute(select_sql)
        while True:
            rows = source_cursor.fetchmany(batch_size)
            if not rows:
                break
            target_cursor.executemany(insert_sql, list(rows))
            target_conn.commit()
            copied += len(rows)
    log(f"copied {table}: {copied} rows")
    return copied


def _drop_objects(connection, snapshot: dict) -> None:
    with connection.cursor() as cursor:
        for name in snapshot["events"]:
            cursor.execute(f"DROP EVENT IF EXISTS {quote_ident(name)}")
        for name in snapshot["triggers"]:
            cursor.execute(f"DROP TRIGGER IF EXISTS {quote_ident(name)}")
        for name, routine in snapshot["routines"].items():
            kind = "PROCEDURE" if routine["routine_type"].upper() == "PROCEDURE" else "FUNCTION"
            cursor.execute(f"DROP {kind} IF EXISTS {quote_ident(name)}")
        for name in snapshot["views"]:
            cursor.execute(f"DROP VIEW IF EXISTS {quote_ident(name)}")
        for name in snapshot["tables"]:
            cursor.execute(f"DROP TABLE IF EXISTS {quote_ident(name)}")
    connection.commit()


def _execute(cursor, statement: str, kind: str, name: str) -> None:
    try:
        cursor.execute(statement)
    except pymysql.Error as exc:
        raise MigrationError(
            f"Failed to create {kind} {name}: {exc}. "
            "The target may be partially copied; re-run with --overwrite."
        ) from exc


def _set_bulk_mode(connection, *, enabled: bool) -> None:
    flag = "0" if enabled else "1"
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"SET FOREIGN_KEY_CHECKS={flag}")
            cursor.execute(f"SET UNIQUE_CHECKS={flag}")
    except pymysql.Error:
        if enabled:
            raise


def _copy_sql_mode(source_conn, target_conn) -> None:
    with source_conn.cursor() as cursor:
        cursor.execute("SELECT @@SESSION.sql_mode AS sql_mode")
        row = cursor.fetchone() or {}
    lowered = {str(key).lower(): value for key, value in row.items()}
    mode = lowered.get("sql_mode") or ""
    with target_conn.cursor() as cursor:
        cursor.execute("SET SESSION sql_mode = %s", (mode,))


def _begin_read_snapshot(connection) -> None:
    from pymysql.cursors import Cursor

    for statement in (
        "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY",
        "START TRANSACTION WITH CONSISTENT SNAPSHOT",
    ):
        try:
            with connection.cursor(Cursor) as cursor:
                cursor.execute(statement)
            return
        except pymysql.Error:
            try:
                connection.rollback()
            except pymysql.Error:
                pass
