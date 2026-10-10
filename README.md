# MySQL migration and verification

Copy a full MySQL schema and its data from a source database to a target database, then check that the target matches. The two databases use separate login details. The tool is Python and uses PyMySQL, so the `mysql` client is not required.

Schema objects copied:

- Tables, including indexes and foreign keys
- Rows, in batches, skipping generated columns
- Views, triggers, stored routines, and events

Verification compares column and index metadata, foreign keys, row counts, and an order-independent checksum of each base table. It does not write to either database.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Run the tool with `.\.venv\Scripts\python.exe` so PowerShell does not need to allow the activation script. If you prefer an activated shell and script execution is enabled, `.\.venv\Scripts\Activate.ps1` works too.

Edit `.env` with the source and target host, port, user, password, and database. Set `SOURCE_SSL` or `TARGET_SSL` to `true` when the server requires TLS. `.env` is gitignored. Passwords are not printed.

The tool refuses to run when the source and target are the same host, port, and database.

## Commands

```powershell
.\.venv\Scripts\python.exe -m migrator migrate --dry-run
.\.venv\Scripts\python.exe -m migrator migrate
.\.venv\Scripts\python.exe -m migrator verify
.\.venv\Scripts\python.exe -m migrator run --overwrite --batch-size 1000
```

- `migrate` copies schema and data. If the target database already contains tables, views, routines, triggers, or events, the command stops unless you pass `--overwrite`.
- `--dry-run` lists objects and estimated row counts and does not write.
- `--batch-size` is the number of rows per insert (default `1000`).
- `verify` compares structure, row counts, and checksums.
- `run` migrates and then verifies. A dry run stops before verification.
- `--env` points at a different env file. `--report-dir` changes where the JSON report is written (default `reports/`).

`--overwrite` drops every table, view, routine, trigger, and event in the target database, then recreates them from the source. Point it at the database you intend to replace.

## Verification

Structure is compared from `information_schema`, so auto-increment counters and `SHOW CREATE` formatting are not treated as differences. Each base table is checked with `COUNT(*)` and a checksum: `MD5` of each row, reduced with `BIT_XOR`, so row order does not matter. Generated columns are left out of the checksum. Both sessions use UTC.

A JSON report is written under `reports/`. The command prints one line per table and any structure differences.

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | Copy succeeded, or verification matched |
| 1 | Verification found differences |
| 2 | Configuration, connection, or copy failure |

## Privileges

The source user needs `SELECT`, `SHOW VIEW`, `TRIGGER`, and `EVENT` on the source database, plus access to `information_schema`.

The target user needs to create and drop tables, views, routines, triggers, and events, and to insert rows.

## Notes

- Definers are removed, so objects are created as the target user.
- When the database names differ, backtick-quoted source database names inside view, trigger, routine, and event statements are rewritten to the target database.
- Foreign-key and unique checks are turned off for the copy, then turned back on. Triggers are created after the data load, so they do not run during the insert.
- A failed copy can leave a partial target. Re-run with `--overwrite`.
- If MySQL rejects an insert as larger than `max_allowed_packet`, run again with a smaller `--batch-size`.
- Approximate numeric values are checksummed with `CAST(... AS CHAR)`. Use similar MySQL versions on both servers when tables contain floats.
- Row estimates in a dry run come from `information_schema` and can be approximate for InnoDB.
