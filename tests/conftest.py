import json
import uuid
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from tilgangsinnsikt import db
from tilgangsinnsikt.synth import generator

AS_OF = date(2026, 6, 1)


@pytest.fixture(scope="session")
def small_dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("synthetic")
    generator.generate(42, generator.SMALL, AS_OF, out)
    return out


@pytest.fixture
def schema() -> Iterator[str]:
    # A throwaway schema per test keeps tests isolated from any loaded demo data.
    name = f"test_{uuid.uuid4().hex[:12]}"
    with db.connect() as admin:
        admin.execute(sql.SQL("create schema {}").format(sql.Identifier(name)))
    try:
        yield name
    finally:
        with db.connect() as admin:
            admin.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(name)))


@pytest.fixture
def conn(schema: str) -> Iterator[psycopg.Connection]:
    connection = db.connect(options=f"-c search_path={schema}")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def cli_schema(schema: str, monkeypatch: pytest.MonkeyPatch) -> str:
    # CLI commands open their own connections; libpq applies PGOPTIONS to each of them.
    monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema}")
    return schema


@pytest.fixture
def loaded(conn: psycopg.Connection, small_dataset: Path) -> psycopg.Connection:
    db.migrate(conn)
    db.load_csv_dir(conn, small_dataset)
    return conn


@pytest.fixture(scope="session")
def ground_truth(small_dataset: Path) -> dict[str, list[str]]:
    return json.loads((small_dataset / "ground_truth.json").read_text(encoding="utf-8"))["planted"]
