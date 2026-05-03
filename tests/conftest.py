"""Shared test fixtures — in-memory SQLite and temporary wiki directories."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from app.db import Database

MIGRATION_SQL = (Path(__file__).parent.parent / "migrations" / "0001_init.sql").read_text()


@pytest.fixture
def db() -> Database:
    """Create an in-memory SQLite database with schema applied."""
    database = Database(Path(":memory:"))
    database.connect()
    # Create db_path as a temp file so the Database doesn't complain
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    database.db_path = db_path
    database._conn = sqlite3.connect(":memory:")
    database._conn.row_factory = sqlite3.Row
    database._conn.execute("PRAGMA journal_mode = WAL")
    database._conn.execute("PRAGMA foreign_keys = ON")
    database._conn.executescript(MIGRATION_SQL)
    yield database
    database.close()


@pytest.fixture
def tmp_wiki(tmp_path: Path) -> Path:
    """Create a temporary wiki directory structure."""
    wiki = tmp_path / "kb"
    wiki.mkdir()
    for sub in ["raw/inbox", "raw/parsed/web", "raw/parsed/pdf", "raw/parsed/youtube",
                "raw/parsed/text", "raw/parsed/voice", "raw/rejected",
                "wiki/entities", "wiki/concepts", "wiki/sources",
                "wiki/syntheses/weekly", "wiki/syntheses/lint"]:
        (wiki / sub).mkdir(parents=True)
    return wiki
