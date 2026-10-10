"""Command-line entry points for migrate, verify, and run."""

from __future__ import annotations

import argparse
import sys

import pymysql

from migrator.config import ConfigError, load_config
from migrator.connection import DatabaseConnectionError, connect
from migrator.migrate import MigrationError, migrate
from migrator.schema import SchemaError
from migrator.verify import VerificationError, verify


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrator",
        description="Copy a MySQL schema and its data, then verify structure, row counts, and checksums.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate_parser = subparsers.add_parser("migrate", help="Copy schema and data to the target")
    _add_env(migrate_parser)
    _add_migrate_flags(migrate_parser)

    verify_parser = subparsers.add_parser(
        "verify",
        help="Compare structure, row counts, and checksums without writing",
    )
    _add_env(verify_parser)
    _add_report_dir(verify_parser)

    run_parser = subparsers.add_parser("run", help="Migrate, then verify")
    _add_env(run_parser)
    _add_migrate_flags(run_parser)
    _add_report_dir(run_parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.env)
        if args.command == "migrate":
            _run_migrate(config, args)
            return 0
        if args.command == "verify":
            return 0 if _run_verify(config, args) else 1
        if args.command == "run":
            _run_migrate(config, args)
            if args.dry_run:
                print("Dry run finished; verification skipped.")
                return 0
            return 0 if _run_verify(config, args) else 1
    except (
        ConfigError,
        DatabaseConnectionError,
        MigrationError,
        SchemaError,
        VerificationError,
        pymysql.Error,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 2


def _run_migrate(config, args) -> None:
    source = connect(config.source)
    target = connect(config.target)
    try:
        migrate(
            source,
            target,
            config,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
        )
    finally:
        source.close()
        target.close()


def _run_verify(config, args) -> bool:
    source = connect(config.source)
    target = connect(config.target)
    try:
        report = verify(source, target, config, report_dir=args.report_dir)
    finally:
        source.close()
        target.close()
    return bool(report["matched"])


def _add_env(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--env",
        help="Path to a .env file (default: .env in the working directory)",
    )


def _add_migrate_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Drop existing objects in the target database before copying",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List objects and estimated row counts without writing",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Rows per insert batch (default: 1000)",
    )


def _add_report_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--report-dir",
        default="reports",
        help="Directory for the JSON verification report (default: reports)",
    )
