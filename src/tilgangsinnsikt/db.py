"""Database connection, migration runner and bulk COPY helpers."""

import csv
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from tilgangsinnsikt.config import DatabaseSettings

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"


def connect(settings: DatabaseSettings | None = None, **kwargs: Any) -> psycopg.Connection:
    settings = settings or DatabaseSettings()
    return psycopg.connect(
        host=settings.host,
        port=settings.port,
        user=settings.user,
        password=settings.password.get_secret_value(),
        dbname=settings.db,
        **kwargs,
    )


def migrate(conn: psycopg.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    conn.execute(
        "create table if not exists schema_migration ("
        " version text primary key,"
        " applied_at timestamptz not null default now())"
    )
    applied = {row[0] for row in conn.execute("select version from schema_migration")}
    # Close the implicit transaction so each migration below commits on its own.
    conn.commit()

    newly_applied = []
    for path in sorted(migrations_dir.glob("*.sql")):
        if path.stem in applied:
            continue
        with conn.transaction():
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("insert into schema_migration (version) values (%s)", (path.stem,))
        newly_applied.append(path.stem)
    return newly_applied


def _copy_statement(table: str, columns: Sequence[str], options: str = "") -> sql.Composed:
    return sql.SQL("copy {} ({}) from stdin {}").format(
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, columns)),
        sql.SQL(options),
    )


def copy_rows(
    conn: psycopg.Connection, table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]
) -> int:
    count = 0
    with conn.cursor() as cur, cur.copy(_copy_statement(table, columns)) as copy:
        for row in rows:
            copy.write_row(row)
            count += 1
    return count


def copy_csv(conn: psycopg.Connection, table: str, path: Path, chunk_size: int = 1 << 20) -> int:
    with path.open(encoding="utf-8", newline="") as f:
        columns = next(csv.reader([f.readline()]))
        f.seek(0)
        statement = _copy_statement(table, columns, "with (format csv, header true)")
        with conn.cursor() as cur:
            with cur.copy(statement) as copy:
                while chunk := f.read(chunk_size):
                    copy.write(chunk)
            return cur.rowcount
