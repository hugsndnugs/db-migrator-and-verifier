"""Checksum SQL builder tests. No live MySQL required."""

from __future__ import annotations

import unittest

from migrator.verify import build_checksum_sql


def column(name: str, data_type: str, ordinal: int, *, generated: bool = False) -> dict:
    return {
        "name": name,
        "data_type": data_type,
        "ordinal_position": ordinal,
        "is_generated": generated,
    }


class ChecksumSqlTests(unittest.TestCase):
    def test_checksum_sql_for_simple_table(self):
        sql = build_checksum_sql(
            "users",
            [column("id", "int", 1), column("note", "text", 2)],
        )
        identifier = (
            "CASE WHEN `id` IS NULL THEN '0' ELSE "
            "CONCAT('1', CAST(`id` AS CHAR CHARACTER SET utf8mb4)) END"
        )
        note = (
            "CASE WHEN `note` IS NULL THEN '0' ELSE "
            "CONCAT('1', CAST(`note` AS CHAR CHARACTER SET utf8mb4)) END"
        )
        expected = (
            "SELECT COUNT(*) AS row_count, "
            "BIT_XOR(CAST(CONV(SUBSTRING(MD5(CONCAT_WS(CHAR(31), "
            f"{identifier}, {note}"
            ")), 1, 16), 16, 10) AS UNSIGNED)) AS checksum FROM `users`"
        )
        self.assertEqual(sql, expected)
        self.assertLess(sql.index("`id`"), sql.index("`note`"))

    def test_orders_columns_by_ordinal_position(self):
        sql = build_checksum_sql(
            "users",
            [column("b", "int", 2), column("a", "int", 1)],
        )
        self.assertLess(sql.index("`a`"), sql.index("`b`"))

    def test_excludes_generated_columns(self):
        sql = build_checksum_sql(
            "users",
            [
                column("id", "int", 1),
                column("full_name", "varchar", 2, generated=True),
            ],
        )
        self.assertIn("`id`", sql)
        self.assertNotIn("full_name", sql)

    def test_empty_and_generated_only_tables_have_null_checksum(self):
        empty = build_checksum_sql("empty_table", [])
        generated_only = build_checksum_sql(
            "calc",
            [column("full_name", "varchar", 1, generated=True)],
        )
        self.assertEqual(
            empty,
            "SELECT COUNT(*) AS row_count, NULL AS checksum FROM `empty_table`",
        )
        self.assertEqual(
            generated_only,
            "SELECT COUNT(*) AS row_count, NULL AS checksum FROM `calc`",
        )

    def test_binary_geometry_and_quoted_names(self):
        sql = build_checksum_sql(
            "order",
            [
                column("payload", "blob", 1),
                column("shape", "geometry", 2),
                column("a`b", "int", 3),
            ],
        )
        self.assertIn("CONVERT(HEX(`payload`) USING utf8mb4)", sql)
        self.assertIn("CONVERT(ST_AsText(`shape`) USING utf8mb4)", sql)
        self.assertIn("`a``b`", sql)
        self.assertIn("FROM `order`", sql)
        self.assertIn("BIT_XOR", sql)
        self.assertIn("MD5", sql)


if __name__ == "__main__":
    unittest.main()
