"""Read MySQL schema metadata and compare two snapshots."""

from __future__ import annotations

import re


class SchemaError(Exception):
    """Raised when schema metadata cannot be read."""


_DEFINER_RE = re.compile(
    r"\s*DEFINER\s*=\s*"
    r"(?:`(?:[^`]|``)+`|'(?:[^'\\]|\\.|'')*'|[A-Za-z0-9._$-]+)"
    r"\s*@\s*"
    r"(?:`(?:[^`]|``)+`|'(?:[^'\\]|\\.|'')*'|[A-Za-z0-9._%$-]+)",
    re.IGNORECASE,
)

_TABLE_ATTRS = ("engine", "collation", "charset")
_COLUMN_ATTRS = (
    "ordinal_position",
    "column_type",
    "is_nullable",
    "column_default",
    "extra",
    "character_set",
    "collation",
)
_INDEX_ATTRS = ("non_unique", "index_type", "columns")
_FOREIGN_KEY_ATTRS = (
    "columns",
    "referenced_table",
    "referenced_columns",
    "update_rule",
    "delete_rule",
)
_VIEW_ATTRS = ("check_option", "security_type", "is_updatable", "definition", "columns")
_TRIGGER_ATTRS = ("table_name", "timing", "event", "orientation", "statement")
_ROUTINE_ATTRS = (
    "routine_type",
    "return_type",
    "is_deterministic",
    "sql_data_access",
    "security_type",
    "definition",
    "parameters",
)
_EVENT_ATTRS = (
    "event_type",
    "interval_value",
    "interval_field",
    "status",
    "on_completion",
    "definition",
)

_COLUMNS_SQL = """
SELECT TABLE_NAME AS table_name,
       COLUMN_NAME AS column_name,
       ORDINAL_POSITION AS ordinal_position,
       COLUMN_TYPE AS column_type,
       DATA_TYPE AS data_type,
       IS_NULLABLE AS is_nullable,
       COLUMN_DEFAULT AS column_default,
       EXTRA AS extra,
       CHARACTER_SET_NAME AS character_set_name,
       COLLATION_NAME AS collation_name,
       GENERATION_EXPRESSION AS generation_expression
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = %s
ORDER BY TABLE_NAME, ORDINAL_POSITION
"""

_COLUMNS_SQL_LEGACY = """
SELECT TABLE_NAME AS table_name,
       COLUMN_NAME AS column_name,
       ORDINAL_POSITION AS ordinal_position,
       COLUMN_TYPE AS column_type,
       DATA_TYPE AS data_type,
       IS_NULLABLE AS is_nullable,
       COLUMN_DEFAULT AS column_default,
       EXTRA AS extra,
       CHARACTER_SET_NAME AS character_set_name,
       COLLATION_NAME AS collation_name
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = %s
ORDER BY TABLE_NAME, ORDINAL_POSITION
"""


def empty_snapshot() -> dict:
    return {
        "tables": {},
        "views": {},
        "triggers": {},
        "routines": {},
        "events": {},
    }


def quote_ident(name: str) -> str:
    """Quote a MySQL identifier with backticks."""
    escaped = name.replace("`", "``")
    return f"`{escaped}`"


def quote_ident_param(name: str) -> str:
    """Quote an identifier used in a statement PyMySQL formats with `%s` placeholders."""
    return quote_ident(name).replace("%", "%%")


def strip_definer(statement: str) -> str:
    """Remove DEFINER clauses so objects are created as the target user."""
    return _DEFINER_RE.sub("", statement)


def retarget_database(statement: str, source_db: str, target_db: str) -> str:
    """Rewrite backtick-quoted references from the source database name to the target."""
    if source_db == target_db:
        return statement
    pattern = re.compile(re.escape(quote_ident(source_db)), re.IGNORECASE)
    return pattern.sub(quote_ident(target_db), statement)


def prepare_create_sql(statement: str, source_db: str, target_db: str) -> str:
    return retarget_database(strip_definer(statement), source_db, target_db)


def normalize_sql(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return " ".join(str(value).split())


def scrub_database(value: str | bytes | None, database: str) -> str:
    """Normalize SQL text and hide the schema name so two databases can be compared."""
    text = normalize_sql(value)
    if not database:
        return text
    pattern = re.compile(re.escape(quote_ident(database)), re.IGNORECASE)
    return pattern.sub("`db`", text)


def is_generated_column(extra: str | None, generation_expression: str | None) -> bool:
    extra_text = (extra or "").upper()
    if "VIRTUAL GENERATED" in extra_text or "STORED GENERATED" in extra_text:
        return True
    if generation_expression is None:
        return False
    return str(generation_expression).strip() != ""


def migration_block_reason(snapshot: dict, *, overwrite: bool, database: str) -> str | None:
    """Return an error when the target is not empty and overwrite is off."""
    if overwrite:
        return None
    counts = []
    for label, key in (
        ("table", "tables"),
        ("view", "views"),
        ("trigger", "triggers"),
        ("routine", "routines"),
        ("event", "events"),
    ):
        count = len(snapshot.get(key, {}))
        if count:
            noun = label if count == 1 else f"{label}s"
            counts.append(f"{count} {noun}")
    if not counts:
        return None
    joined = ", ".join(counts)
    return (
        f"Target database {database!r} already contains objects ({joined}). "
        "Re-run with --overwrite to replace them."
    )


def diff_schema(source: dict, target: dict) -> list[dict]:
    """Compare two schema snapshots. Result entries are JSON-serializable."""
    differences = []
    differences.extend(
        _diff_named_maps("table", source["tables"], target["tables"], _diff_table)
    )
    differences.extend(
        _diff_named_maps(
            "view",
            source["views"],
            target["views"],
            lambda name, left, right: _diff_fields("view", name, left, right, _VIEW_ATTRS),
        )
    )
    differences.extend(
        _diff_named_maps(
            "trigger",
            source["triggers"],
            target["triggers"],
            lambda name, left, right: _diff_fields("trigger", name, left, right, _TRIGGER_ATTRS),
        )
    )
    differences.extend(
        _diff_named_maps(
            "routine",
            source["routines"],
            target["routines"],
            lambda name, left, right: _diff_fields("routine", name, left, right, _ROUTINE_ATTRS),
        )
    )
    differences.extend(
        _diff_named_maps(
            "event",
            source["events"],
            target["events"],
            lambda name, left, right: _diff_fields("event", name, left, right, _EVENT_ATTRS),
        )
    )
    return differences


def fetch_snapshot(connection, database: str) -> dict:
    """Read tables, views, routines, triggers, and events from information_schema."""
    import pymysql

    snapshot = empty_snapshot()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT t.TABLE_NAME AS table_name,
                       t.ENGINE AS engine,
                       t.TABLE_COLLATION AS table_collation,
                       c.CHARACTER_SET_NAME AS character_set_name,
                       t.TABLE_ROWS AS estimated_rows
                FROM information_schema.TABLES t
                LEFT JOIN information_schema.COLLATIONS c
                  ON c.COLLATION_NAME = t.TABLE_COLLATION
                WHERE t.TABLE_SCHEMA = %s AND t.TABLE_TYPE = 'BASE TABLE'
                ORDER BY t.TABLE_NAME
                """,
                (database,),
            )
            for row in _rows(cursor):
                snapshot["tables"][row["table_name"]] = {
                    "engine": row["engine"],
                    "collation": row["table_collation"],
                    "charset": row["character_set_name"],
                    "estimated_rows": _optional_int(row["estimated_rows"]),
                    "columns": [],
                    "indexes": [],
                    "foreign_keys": [],
                }

            cursor.execute(
                """
                SELECT TABLE_NAME AS table_name,
                       VIEW_DEFINITION AS view_definition,
                       CHECK_OPTION AS check_option,
                       IS_UPDATABLE AS is_updatable,
                       SECURITY_TYPE AS security_type
                FROM information_schema.VIEWS
                WHERE TABLE_SCHEMA = %s
                ORDER BY TABLE_NAME
                """,
                (database,),
            )
            for row in _rows(cursor):
                snapshot["views"][row["table_name"]] = {
                    "check_option": row["check_option"],
                    "security_type": row["security_type"],
                    "is_updatable": row["is_updatable"],
                    "definition": scrub_database(row["view_definition"], database),
                    "columns": [],
                }

            _attach_columns(cursor, database, snapshot)
            _attach_indexes(cursor, database, snapshot)
            _attach_foreign_keys(cursor, database, snapshot)
            _attach_triggers(cursor, database, snapshot)
            _attach_routines(cursor, database, snapshot)
            _attach_events(cursor, database, snapshot)
    except pymysql.Error as exc:
        raise SchemaError(f"Could not read schema for {database!r}: {exc}") from exc
    return snapshot


def fetch_create_statement(cursor, kind: str, name: str) -> str:
    """Run SHOW CREATE for a fixed object kind and return the SQL text."""
    if kind not in {"TABLE", "VIEW", "TRIGGER", "PROCEDURE", "FUNCTION", "EVENT"}:
        raise SchemaError(f"Unsupported SHOW CREATE kind {kind}")
    cursor.execute(f"SHOW CREATE {kind} {quote_ident(name)}")
    row = cursor.fetchone()
    if not row:
        raise SchemaError(f"No CREATE statement returned for {kind} {name}")
    return _extract_create_sql(row)


def _attach_columns(cursor, database: str, snapshot: dict) -> None:
    import pymysql

    try:
        cursor.execute(_COLUMNS_SQL, (database,))
    except pymysql.Error as exc:
        if not exc.args or exc.args[0] != 1054:
            raise
        cursor.execute(_COLUMNS_SQL_LEGACY, (database,))
    for row in _rows(cursor):
        table_name = row["table_name"]
        generated = is_generated_column(row.get("extra"), row.get("generation_expression"))
        column = {
            "name": row["column_name"],
            "ordinal_position": int(row["ordinal_position"]),
            "column_type": row["column_type"],
            "data_type": row["data_type"],
            "is_nullable": row["is_nullable"],
            "column_default": _text_or_none(row["column_default"]),
            "extra": row["extra"] or "",
            "character_set": row["character_set_name"],
            "collation": row["collation_name"],
            "is_generated": generated,
        }
        if table_name in snapshot["tables"]:
            snapshot["tables"][table_name]["columns"].append(column)
        elif table_name in snapshot["views"]:
            snapshot["views"][table_name]["columns"].append(column["name"])


def _attach_indexes(cursor, database: str, snapshot: dict) -> None:
    cursor.execute(
        """
        SELECT TABLE_NAME AS table_name,
               INDEX_NAME AS index_name,
               NON_UNIQUE AS non_unique,
               SEQ_IN_INDEX AS seq_in_index,
               COLUMN_NAME AS column_name,
               SUB_PART AS sub_part,
               COLLATION AS collation,
               INDEX_TYPE AS index_type
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = %s
        ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
        """,
        (database,),
    )
    grouped: dict[tuple[str, str], dict] = {}
    for row in _rows(cursor):
        table_name = row["table_name"]
        if table_name not in snapshot["tables"]:
            continue
        key = (table_name, row["index_name"])
        index = grouped.get(key)
        if index is None:
            index = {
                "name": row["index_name"],
                "non_unique": bool(row["non_unique"]),
                "index_type": row["index_type"],
                "columns": [],
            }
            grouped[key] = index
            snapshot["tables"][table_name]["indexes"].append(index)
        index["columns"].append(
            {
                "name": row["column_name"],
                "sub_part": _optional_int(row["sub_part"]),
                "collation": row["collation"],
            }
        )


def _attach_foreign_keys(cursor, database: str, snapshot: dict) -> None:
    cursor.execute(
        """
        SELECT k.TABLE_NAME AS table_name,
               k.CONSTRAINT_NAME AS constraint_name,
               k.COLUMN_NAME AS column_name,
               k.ORDINAL_POSITION AS ordinal_position,
               k.REFERENCED_TABLE_NAME AS referenced_table_name,
               k.REFERENCED_COLUMN_NAME AS referenced_column_name,
               r.UPDATE_RULE AS update_rule,
               r.DELETE_RULE AS delete_rule
        FROM information_schema.KEY_COLUMN_USAGE k
        JOIN information_schema.REFERENTIAL_CONSTRAINTS r
          ON r.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA
         AND r.TABLE_NAME = k.TABLE_NAME
         AND r.CONSTRAINT_NAME = k.CONSTRAINT_NAME
        WHERE k.TABLE_SCHEMA = %s
          AND k.REFERENCED_TABLE_NAME IS NOT NULL
        ORDER BY k.TABLE_NAME, k.CONSTRAINT_NAME, k.ORDINAL_POSITION
        """,
        (database,),
    )
    grouped: dict[tuple[str, str], dict] = {}
    for row in _rows(cursor):
        table_name = row["table_name"]
        if table_name not in snapshot["tables"]:
            continue
        key = (table_name, row["constraint_name"])
        foreign_key = grouped.get(key)
        if foreign_key is None:
            foreign_key = {
                "name": row["constraint_name"],
                "columns": [],
                "referenced_table": row["referenced_table_name"],
                "referenced_columns": [],
                "update_rule": row["update_rule"],
                "delete_rule": row["delete_rule"],
            }
            grouped[key] = foreign_key
            snapshot["tables"][table_name]["foreign_keys"].append(foreign_key)
        foreign_key["columns"].append(row["column_name"])
        foreign_key["referenced_columns"].append(row["referenced_column_name"])


def _attach_triggers(cursor, database: str, snapshot: dict) -> None:
    cursor.execute(
        """
        SELECT TRIGGER_NAME AS trigger_name,
               EVENT_OBJECT_TABLE AS event_object_table,
               ACTION_TIMING AS action_timing,
               EVENT_MANIPULATION AS event_manipulation,
               ACTION_ORIENTATION AS action_orientation,
               ACTION_STATEMENT AS action_statement
        FROM information_schema.TRIGGERS
        WHERE TRIGGER_SCHEMA = %s
        ORDER BY TRIGGER_NAME
        """,
        (database,),
    )
    for row in _rows(cursor):
        snapshot["triggers"][row["trigger_name"]] = {
            "table_name": row["event_object_table"],
            "timing": row["action_timing"],
            "event": row["event_manipulation"],
            "orientation": row["action_orientation"],
            "statement": scrub_database(row["action_statement"], database),
        }


def _attach_routines(cursor, database: str, snapshot: dict) -> None:
    cursor.execute(
        """
        SELECT ROUTINE_NAME AS routine_name,
               ROUTINE_TYPE AS routine_type,
               DTD_IDENTIFIER AS dtd_identifier,
               IS_DETERMINISTIC AS is_deterministic,
               SQL_DATA_ACCESS AS sql_data_access,
               SECURITY_TYPE AS security_type,
               ROUTINE_DEFINITION AS routine_definition
        FROM information_schema.ROUTINES
        WHERE ROUTINE_SCHEMA = %s
        ORDER BY ROUTINE_NAME
        """,
        (database,),
    )
    for row in _rows(cursor):
        snapshot["routines"][row["routine_name"]] = {
            "routine_type": row["routine_type"],
            "return_type": _text_or_none(row["dtd_identifier"]),
            "is_deterministic": row["is_deterministic"],
            "sql_data_access": row["sql_data_access"],
            "security_type": row["security_type"],
            "definition": scrub_database(row["routine_definition"], database),
            "parameters": [],
        }

    cursor.execute(
        """
        SELECT SPECIFIC_NAME AS specific_name,
               ORDINAL_POSITION AS ordinal_position,
               PARAMETER_MODE AS parameter_mode,
               PARAMETER_NAME AS parameter_name,
               DTD_IDENTIFIER AS dtd_identifier
        FROM information_schema.PARAMETERS
        WHERE SPECIFIC_SCHEMA = %s
        ORDER BY SPECIFIC_NAME, ORDINAL_POSITION
        """,
        (database,),
    )
    for row in _rows(cursor):
        if int(row["ordinal_position"]) == 0:
            continue
        routine = snapshot["routines"].get(row["specific_name"])
        if routine is None:
            continue
        routine["parameters"].append(
            {
                "ordinal_position": int(row["ordinal_position"]),
                "mode": row["parameter_mode"],
                "name": row["parameter_name"],
                "data_type": row["dtd_identifier"],
            }
        )


def _attach_events(cursor, database: str, snapshot: dict) -> None:
    cursor.execute(
        """
        SELECT EVENT_NAME AS event_name,
               EVENT_TYPE AS event_type,
               INTERVAL_VALUE AS interval_value,
               INTERVAL_FIELD AS interval_field,
               STATUS AS status,
               ON_COMPLETION AS on_completion,
               EVENT_DEFINITION AS event_definition
        FROM information_schema.EVENTS
        WHERE EVENT_SCHEMA = %s
        ORDER BY EVENT_NAME
        """,
        (database,),
    )
    for row in _rows(cursor):
        interval = row["interval_value"]
        snapshot["events"][row["event_name"]] = {
            "event_type": row["event_type"],
            "interval_value": None if interval is None else str(interval),
            "interval_field": row["interval_field"],
            "status": row["status"],
            "on_completion": row["on_completion"],
            "definition": scrub_database(row["event_definition"], database),
        }


def _diff_table(name: str, source: dict, target: dict) -> list[dict]:
    differences = _diff_fields("table", name, source, target, _TABLE_ATTRS)
    differences.extend(
        _diff_children("column", name, source["columns"], target["columns"], _COLUMN_ATTRS)
    )
    differences.extend(
        _diff_children("index", name, source["indexes"], target["indexes"], _INDEX_ATTRS)
    )
    differences.extend(
        _diff_children(
            "foreign_key",
            name,
            source["foreign_keys"],
            target["foreign_keys"],
            _FOREIGN_KEY_ATTRS,
        )
    )
    return differences


def _diff_named_maps(object_type: str, source_map: dict, target_map: dict, compare) -> list[dict]:
    differences = []
    for name in sorted(set(source_map) - set(target_map)):
        differences.append(
            {"object_type": object_type, "name": name, "issue": "missing_on_target"}
        )
    for name in sorted(set(target_map) - set(source_map)):
        differences.append(
            {"object_type": object_type, "name": name, "issue": "extra_on_target"}
        )
    for name in sorted(set(source_map) & set(target_map)):
        differences.extend(compare(name, source_map[name], target_map[name]))
    return differences


def _diff_children(
    object_type: str,
    parent: str,
    source_items: list[dict],
    target_items: list[dict],
    fields: tuple[str, ...],
) -> list[dict]:
    source_map = {item["name"]: item for item in source_items}
    target_map = {item["name"]: item for item in target_items}
    differences = []
    for name in sorted(set(source_map) - set(target_map)):
        differences.append(
            {
                "object_type": object_type,
                "name": f"{parent}.{name}",
                "issue": "missing_on_target",
            }
        )
    for name in sorted(set(target_map) - set(source_map)):
        differences.append(
            {
                "object_type": object_type,
                "name": f"{parent}.{name}",
                "issue": "extra_on_target",
            }
        )
    for name in sorted(set(source_map) & set(target_map)):
        differences.extend(
            _diff_fields(
                object_type,
                f"{parent}.{name}",
                source_map[name],
                target_map[name],
                fields,
            )
        )
    return differences


def _diff_fields(
    object_type: str,
    name: str,
    source: dict,
    target: dict,
    fields: tuple[str, ...],
) -> list[dict]:
    differences = []
    for field in fields:
        if source.get(field) != target.get(field):
            differences.append(
                {
                    "object_type": object_type,
                    "name": name,
                    "issue": "attribute_mismatch",
                    "field": field,
                    "source": source.get(field),
                    "target": target.get(field),
                }
            )
    return differences


def _rows(cursor) -> list[dict]:
    return [_lower_keys(row) for row in cursor.fetchall()]


def _lower_keys(row: dict) -> dict:
    return {str(key).lower(): value for key, value in row.items()}


def _extract_create_sql(row: dict) -> str:
    lowered = _lower_keys(row)
    for key, value in lowered.items():
        if value and (key.startswith("create ") or key == "sql original statement"):
            if isinstance(value, bytes):
                return value.decode("utf-8")
            return str(value)
    raise SchemaError("SHOW CREATE did not include a statement")


def _text_or_none(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _optional_int(value):
    if value is None:
        return None
    return int(value)
