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
def conn() -> Iterator[psycopg.Connection]:
    # A throwaway schema per test keeps tests isolated from any loaded demo data.
    schema = f"test_{uuid.uuid4().hex[:12]}"
    with db.connect() as admin:
        admin.execute(sql.SQL("create schema {}").format(sql.Identifier(schema)))
    connection = db.connect(options=f"-c search_path={schema}")
    try:
        yield connection
    finally:
        connection.close()
        with db.connect() as admin:
            admin.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(schema)))
