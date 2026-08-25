from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from github_activity_db.config import PersonTarget, RepositoryTarget, Settings
from github_activity_db.db import connect
from github_activity_db.migrations import initialize_database


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        project_root=tmp_path,
        db_path=tmp_path / "activity.db",
        max_pages=2,
        per_page=2,
        max_retries=1,
        people=(PersonTarget("Guido van Rossum", "著名開発者", ("gvanrossum",)),),
        repositories=(RepositoryTarget("python/cpython", True),),
    )


@pytest.fixture
def db(settings: Settings) -> sqlite3.Connection:
    connection = connect(settings.db_path)
    initialize_database(connection, settings)
    yield connection
    connection.close()

