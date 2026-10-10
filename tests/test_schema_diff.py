"""Schema diff, identifier, and configuration tests. No live MySQL required."""

from __future__ import annotations

import os
import tempfile
import unittest
from copy import deepcopy
from io import StringIO
from pathlib import Path
from contextlib import redirect_stderr

from migrator.cli import main
from migrator.config import ConfigError, load_config
from migrator.migrate import format_dry_run
from migrator.schema import (
    diff_schema,
    empty_snapshot,
    is_generated_column,
    migration_block_reason,
    quote_ident,
    quote_ident_param,
    retarget_database,
    scrub_database,
    strip_definer,
)


def sample_table() -> dict:
    return {
        "engine": "InnoDB",
        "collation": "utf8mb4_general_ci",
        "charset": "utf8mb4",
        "estimated_rows": 10,
        "columns": [
            {
                "name": "id",
                "ordinal_position": 1,
                "column_type": "int",
                "data_type": "int",
                "is_nullable": "NO",
                "column_default": None,
                "extra": "auto_increment",
                "character_set": None,
                "collation": None,
                "is_generated": False,
            }
        ],
        "indexes": [
            {
                "name": "PRIMARY",
                "non_unique": False,
                "index_type": "BTREE",
                "columns": [{"name": "id", "sub_part": None, "collation": "A"}],
            }
        ],
        "foreign_keys": [],
    }


def snapshot_with_users(table: dict | None = None) -> dict:
    snapshot = empty_snapshot()
    snapshot["tables"]["users"] = sample_table() if table is None else table
    return snapshot


class SchemaDiffTests(unittest.TestCase):
    def test_identical_snapshots_match(self):
        source = snapshot_with_users()
        target = snapshot_with_users()
        target["tables"]["users"]["estimated_rows"] = 999
        self.assertEqual(diff_schema(source, target), [])

    def test_missing_and_extra_tables(self):
        source = snapshot_with_users()
        target = empty_snapshot()
        self.assertEqual(
            diff_schema(source, target),
            [{"object_type": "table", "name": "users", "issue": "missing_on_target"}],
        )
        extra = snapshot_with_users()
        extra["tables"]["audit"] = sample_table()
        self.assertEqual(
            diff_schema(source, extra),
            [{"object_type": "table", "name": "audit", "issue": "extra_on_target"}],
        )

    def test_column_type_mismatch(self):
        source = snapshot_with_users()
        target = snapshot_with_users()
        target["tables"]["users"]["columns"][0]["column_type"] = "bigint"
        self.assertEqual(
            diff_schema(source, target),
            [
                {
                    "object_type": "column",
                    "name": "users.id",
                    "issue": "attribute_mismatch",
                    "field": "column_type",
                    "source": "int",
                    "target": "bigint",
                }
            ],
        )

    def test_missing_index(self):
        source = snapshot_with_users()
        target = snapshot_with_users()
        target["tables"]["users"]["indexes"] = []
        self.assertEqual(
            diff_schema(source, target),
            [{"object_type": "index", "name": "users.PRIMARY", "issue": "missing_on_target"}],
        )

    def test_foreign_key_delete_rule_mismatch(self):
        source = snapshot_with_users()
        target = snapshot_with_users()
        foreign_key = {
            "name": "fk_parent",
            "columns": ["parent_id"],
            "referenced_table": "parents",
            "referenced_columns": ["id"],
            "update_rule": "CASCADE",
            "delete_rule": "RESTRICT",
        }
        source["tables"]["users"]["foreign_keys"] = [deepcopy(foreign_key)]
        changed = deepcopy(foreign_key)
        changed["delete_rule"] = "CASCADE"
        target["tables"]["users"]["foreign_keys"] = [changed]
        self.assertEqual(
            diff_schema(source, target),
            [
                {
                    "object_type": "foreign_key",
                    "name": "users.fk_parent",
                    "issue": "attribute_mismatch",
                    "field": "delete_rule",
                    "source": "RESTRICT",
                    "target": "CASCADE",
                }
            ],
        )

    def test_view_definition_mismatch(self):
        source = empty_snapshot()
        target = empty_snapshot()
        source["views"]["active"] = {
            "check_option": "NONE",
            "security_type": "DEFINER",
            "is_updatable": "NO",
            "definition": "select `id` from `users`",
            "columns": ["id"],
        }
        target["views"]["active"] = deepcopy(source["views"]["active"])
        target["views"]["active"]["definition"] = "select `id` from `accounts`"
        differences = diff_schema(source, target)
        self.assertEqual(len(differences), 1)
        self.assertEqual(differences[0]["object_type"], "view")
        self.assertEqual(differences[0]["field"], "definition")


class SchemaHelperTests(unittest.TestCase):
    def test_quote_ident_escapes_backticks(self):
        self.assertEqual(quote_ident("users"), "`users`")
        self.assertEqual(quote_ident("a`b"), "`a``b`")
        self.assertEqual(quote_ident_param("a%b"), "`a%%b`")

    def test_strip_definer(self):
        statement = (
            "CREATE ALGORITHM=UNDEFINED DEFINER=`root`@`localhost` "
            "SQL SECURITY DEFINER VIEW `v` AS SELECT 1"
        )
        self.assertEqual(
            strip_definer(statement),
            "CREATE ALGORITHM=UNDEFINED SQL SECURITY DEFINER VIEW `v` AS SELECT 1",
        )
        plain = "CREATE DEFINER=root@localhost PROCEDURE p() BEGIN END"
        self.assertEqual(strip_definer(plain), "CREATE PROCEDURE p() BEGIN END")
        self.assertEqual(strip_definer("CREATE VIEW `v` AS SELECT 1"), "CREATE VIEW `v` AS SELECT 1")

    def test_retarget_database(self):
        statement = "CREATE VIEW `v` AS SELECT * FROM `prod`.`users`"
        self.assertEqual(
            retarget_database(statement, "prod", "stage"),
            "CREATE VIEW `v` AS SELECT * FROM `stage`.`users`",
        )
        self.assertEqual(retarget_database(statement, "prod", "prod"), statement)
        self.assertEqual(
            retarget_database("SELECT `Prod`.`users`.`id`", "prod", "stage"),
            "SELECT `stage`.`users`.`id`",
        )

    def test_scrub_database_hides_schema_name(self):
        self.assertEqual(
            scrub_database("select `prod`.`users`.`id` from `prod`.`users`", "prod"),
            "select `db`.`users`.`id` from `db`.`users`",
        )

    def test_generated_column_detection_ignores_default_generated(self):
        self.assertFalse(is_generated_column("DEFAULT_GENERATED", None))
        self.assertFalse(is_generated_column("", ""))
        self.assertTrue(is_generated_column("VIRTUAL GENERATED", None))
        self.assertTrue(is_generated_column("", "(`first` + `last`)"))

    def test_migration_block_reason(self):
        empty = empty_snapshot()
        self.assertIsNone(migration_block_reason(empty, overwrite=False, database="staging"))
        occupied = empty_snapshot()
        occupied["tables"]["users"] = sample_table()
        occupied["views"]["active"] = {}
        occupied["views"]["closed"] = {}
        occupied["triggers"]["users_bi"] = {}
        self.assertIsNone(migration_block_reason(occupied, overwrite=True, database="staging"))
        self.assertEqual(
            migration_block_reason(occupied, overwrite=False, database="staging"),
            "Target database 'staging' already contains objects (1 table, 2 views, 1 trigger). "
            "Re-run with --overwrite to replace them.",
        )

    def test_dry_run_lists_estimates(self):
        snapshot = snapshot_with_users()
        snapshot["views"]["active"] = {}
        text = format_dry_run(snapshot)
        self.assertIn("table users (~10 rows)", text)
        self.assertIn("view active", text)
        self.assertIn("(no objects)", format_dry_run(empty_snapshot()))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._previous = os.environ.copy()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._previous)

    def test_loads_two_profiles_and_hides_password(self):
        path = self._write_env(
            "\n".join(
                [
                    "SOURCE_HOST=source.example",
                    "SOURCE_USER=reader",
                    "SOURCE_PASSWORD=secret-source",
                    "SOURCE_DATABASE=app",
                    "SOURCE_SSL=true",
                    "TARGET_HOST=target.example",
                    "TARGET_PORT=3307",
                    "TARGET_USER=writer",
                    "TARGET_PASSWORD=secret-target",
                    "TARGET_DATABASE=app",
                    "TARGET_SSL=false",
                ]
            )
        )
        config = load_config(str(path))
        self.assertEqual(config.source.port, 3306)
        self.assertTrue(config.source.ssl)
        self.assertEqual(config.target.port, 3307)
        self.assertFalse(config.target.ssl)
        self.assertNotIn("secret-source", repr(config.source))
        self.assertIn("***", repr(config.source))

    def test_rejects_same_database_ignoring_case(self):
        path = self._write_env(
            "\n".join(
                [
                    "SOURCE_HOST=localhost",
                    "SOURCE_USER=reader",
                    "SOURCE_DATABASE=app",
                    "TARGET_HOST=LOCALHOST",
                    "TARGET_USER=writer",
                    "TARGET_DATABASE=App",
                ]
            )
        )
        with self.assertRaises(ConfigError) as raised:
            load_config(str(path))
        self.assertIn("same database", str(raised.exception))

    def test_rejects_missing_settings_and_bad_port(self):
        missing = self._write_env("SOURCE_USER=reader\n")
        with self.assertRaises(ConfigError) as raised:
            load_config(str(missing))
        self.assertIn("SOURCE_HOST", str(raised.exception))

        bad_port = self._write_env(
            "\n".join(
                [
                    "SOURCE_HOST=source.example",
                    "SOURCE_PORT=nope",
                    "SOURCE_USER=reader",
                    "SOURCE_DATABASE=app",
                    "TARGET_HOST=target.example",
                    "TARGET_USER=writer",
                    "TARGET_DATABASE=other",
                ]
            )
        )
        with self.assertRaises(ConfigError) as raised:
            load_config(str(bad_port))
        self.assertIn("SOURCE_PORT", str(raised.exception))

    def test_cli_returns_config_error_without_connecting(self):
        buffer = StringIO()
        with redirect_stderr(buffer):
            code = main(["migrate", "--env", "does-not-exist.env"])
        self.assertEqual(code, 2)
        self.assertIn("Environment file not found", buffer.getvalue())

    def _write_env(self, contents: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / ".env"
        path.write_text(contents, encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
