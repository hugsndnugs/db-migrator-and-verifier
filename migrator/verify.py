"""Compare structure, row counts, and order-independent checksums."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from migrator.config import AppConfig
from migrator.schema import diff_schema, fetch_snapshot, quote_ident


class VerificationError(Exception):
    """Raised when a verification report cannot be written."""


_BINARY_TYPES = {
    "binary",
    "varbinary",
    "tinyblob",
    "blob",
    "mediumblob",
    "longblob",
    "bit",
}
_GEOMETRY_TYPES = {
    "geometry",
    "point",
    "linestring",
    "polygon",
    "multipoint",
    "multilinestring",
    "multipolygon",
    "geometrycollection",
    "geomcollection",
}


def build_checksum_sql(table: str, columns: list[dict]) -> str:
    """Build a count plus an order-independent checksum for one base table.

    Each row is hashed with MD5. The first 16 hex characters are XOR-ed together
    so row order does not change the result. Generated columns are excluded.
    """
    usable = [column for column in columns if not column.get("is_generated")]
    usable.sort(key=lambda column: column.get("ordinal_position", 0))
    quoted_table = quote_ident(table)
    if not usable:
        return f"SELECT COUNT(*) AS row_count, NULL AS checksum FROM {quoted_table}"
    pieces = ", ".join(
        checksum_value_expr(column["name"], column["data_type"]) for column in usable
    )
    row_hash = f"MD5(CONCAT_WS(CHAR(31), {pieces}))"
    checksum = (
        f"BIT_XOR(CAST(CONV(SUBSTRING({row_hash}, 1, 16), 16, 10) AS UNSIGNED))"
    )
    return f"SELECT COUNT(*) AS row_count, {checksum} AS checksum FROM {quoted_table}"


def checksum_value_expr(name: str, data_type: str) -> str:
    """Render one column as a stable string, with NULL distinct from any value."""
    quoted = quote_ident(name)
    kind = (data_type or "").lower()
    if kind in _BINARY_TYPES:
        rendered = f"CONVERT(HEX({quoted}) USING utf8mb4)"
    elif kind in _GEOMETRY_TYPES:
        rendered = f"CONVERT(ST_AsText({quoted}) USING utf8mb4)"
    else:
        rendered = f"CAST({quoted} AS CHAR CHARACTER SET utf8mb4)"
    return f"CASE WHEN {quoted} IS NULL THEN '0' ELSE CONCAT('1', {rendered}) END"


def verify(source_conn, target_conn, config: AppConfig, *, report_dir: str = "reports", log=print) -> dict:
    """Compare both databases, print a summary, and write a JSON report."""
    source_snapshot = fetch_snapshot(source_conn, config.source.database)
    target_snapshot = fetch_snapshot(target_conn, config.target.database)
    structure_differences = diff_schema(source_snapshot, target_snapshot)

    common_tables = sorted(
        set(source_snapshot["tables"]) & set(target_snapshot["tables"])
    )
    table_results = []
    data_differences = []
    for name in common_tables:
        source_stats = _table_stats(source_conn, name, source_snapshot["tables"][name]["columns"])
        target_stats = _table_stats(target_conn, name, target_snapshot["tables"][name]["columns"])
        matched = (
            source_stats["row_count"] == target_stats["row_count"]
            and source_stats["checksum"] == target_stats["checksum"]
        )
        result = {
            "name": name,
            "source_rows": source_stats["row_count"],
            "target_rows": target_stats["row_count"],
            "source_checksum": _checksum_text(source_stats["checksum"]),
            "target_checksum": _checksum_text(target_stats["checksum"]),
            "match": matched,
        }
        table_results.append(result)
        if not matched:
            data_differences.append(
                {
                    "object_type": "table_data",
                    "name": name,
                    "issue": "data_mismatch",
                    "source_rows": result["source_rows"],
                    "target_rows": result["target_rows"],
                    "source_checksum": result["source_checksum"],
                    "target_checksum": result["target_checksum"],
                }
            )

    differences = structure_differences + data_differences
    report = {
        "matched": not differences,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "checksum": "bit_xor_md5",
        "source": _endpoint(config.source),
        "target": _endpoint(config.target),
        "structure_differences": structure_differences,
        "tables": table_results,
        "differences": differences,
    }
    path = _write_report(report, report_dir)
    log(format_report(report, path))
    return report


def format_report(report: dict, path: Path | None = None) -> str:
    lines = []
    if path is not None:
        lines.append(f"Verification report: {path}")
    for table in report["tables"]:
        status = "match" if table["match"] else "differ"
        lines.append(
            f"{table['name']}: rows {table['source_rows']}/{table['target_rows']} "
            f"checksum {table['source_checksum']}/{table['target_checksum']} {status}"
        )
    for difference in report["structure_differences"]:
        lines.append(
            f"{difference['object_type']} {difference['name']}: {difference['issue']}"
            + (f" ({difference['field']})" if difference.get("field") else "")
        )
    if report["matched"]:
        lines.append("RESULT: databases match")
    else:
        lines.append(f"RESULT: {len(report['differences'])} difference(s) found")
    return "\n".join(lines)


def _table_stats(connection, table: str, columns: list[dict]) -> dict:
    sql = build_checksum_sql(table, columns)
    with connection.cursor() as cursor:
        cursor.execute(sql)
        row = cursor.fetchone()
    if row is None:
        raise VerificationError(f"Checksum query returned no row for {table}")
    lowered = {str(key).lower(): value for key, value in row.items()}
    return {
        "row_count": int(lowered["row_count"]),
        "checksum": None if lowered["checksum"] is None else int(lowered["checksum"]),
    }


def _checksum_text(value: int | None) -> str | None:
    if value is None:
        return None
    return str(value)


def _endpoint(config) -> dict:
    return {
        "host": config.host,
        "port": config.port,
        "user": config.user,
        "database": config.database,
    }


def _write_report(report: dict, report_dir: str) -> Path:
    directory = Path(report_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = directory / f"verify-{stamp}.json"
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except OSError as exc:
        raise VerificationError(f"Could not write verification report: {exc}") from exc
    return path
