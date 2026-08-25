from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from github_activity_db.db import connect
from github_activity_db.migrations import MIGRATIONS, initialize_database, migrate, utc_now


def _version_one_database(path: Path) -> sqlite3.Connection:
    connection = connect(path)
    connection.execute("""CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL
    )""")
    connection.executescript(MIGRATIONS[0][2])
    connection.execute(
        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (1, ?, ?)",
        (MIGRATIONS[0][1], "2026-08-25T00:00:00Z"),
    )
    connection.commit()
    return connection


def _insert_sync_state(
    connection: sqlite3.Connection,
    stage: str,
    status: str,
    *,
    cursor_url: str | None = None,
    last_success_at: str | None = "2026-08-01T00:00:00Z",
    range_start: str = "2026-08-10T00:00:00Z",
    range_end: str = "2026-08-20T00:00:00Z",
    last_error: str | None = None,
    metadata_json: str | None = None,
) -> None:
    connection.execute(
        """INSERT INTO sync_state(scope_type, scope_key, stage, cursor_url, last_success_at,
               range_start, range_end, status, last_error, metadata_json, updated_at)
           VALUES ('repository', 'example/repo', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            stage,
            cursor_url,
            last_success_at,
            range_start,
            range_end,
            status,
            last_error,
            metadata_json,
            "2026-08-25T00:00:00Z",
        ),
    )


def test_initial_database_and_repeatable_migration(db, settings):
    initialize_database(db, settings)
    initialize_database(db, settings)
    assert db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 3
    assert db.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 1
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_case_insensitive_github_login_uniqueness(db):
    person_id = db.execute("SELECT id FROM people").fetchone()[0]
    now = utc_now()
    try:
        db.execute("""INSERT INTO person_accounts(person_id, provider, login, created_at, updated_at)
                      VALUES (?, 'github', 'GVANROSSUM', ?, ?)""", (person_id, now, now))
    except Exception as exc:
        assert "UNIQUE" in str(exc)
    else:
        raise AssertionError("case-insensitive duplicate was accepted")


def test_expected_indexes_exist(db):
    indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='index'")}
    assert "idx_events_person_date" in indexes
    assert "idx_commits_repo_date" in indexes
    plan = " ".join(str(value) for value in db.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM events WHERE person_id=? ORDER BY occurred_at DESC", (1,)
    ).fetchone())
    assert "idx_events_person_date" in plan


def test_utc_timestamp_format():
    value = utc_now()
    assert value.endswith("Z")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo == UTC


def test_watched_repository_can_be_disabled_and_reenabled(db, settings):
    target = settings.repositories[0]
    repository_id = db.execute("SELECT id FROM repositories WHERE full_name=?", (target.full_name,)).fetchone()[0]
    assert db.execute("SELECT enabled FROM watched_repositories WHERE repository_id=?", (repository_id,)).fetchone()[0] == 1

    disabled = replace(settings, repositories=(replace(target, watched=False, initial_days=45),))
    initialize_database(db, disabled)
    row = db.execute(
        "SELECT enabled, initial_days FROM watched_repositories WHERE repository_id=?", (repository_id,)
    ).fetchone()
    assert tuple(row) == (0, 45)

    enabled = replace(settings, repositories=(replace(target, watched=True, initial_days=60),))
    initialize_database(db, enabled)
    row = db.execute(
        "SELECT enabled, initial_days FROM watched_repositories WHERE repository_id=?", (repository_id,)
    ).fetchone()
    assert tuple(row) == (1, 60)


def test_version_one_upgrade_only_advances_completed_cursorless_states(tmp_path):
    connection = _version_one_database(tmp_path / "version-one.db")
    try:
        _insert_sync_state(connection, "success", "success")
        _insert_sync_state(
            connection,
            "success-without-checkpoint",
            "success",
            last_success_at=None,
        )
        _insert_sync_state(connection, "not-modified", "not_modified")
        _insert_sync_state(connection, "incomplete", "incomplete")
        _insert_sync_state(connection, "error", "error")
        _insert_sync_state(
            connection,
            "success-with-cursor",
            "success",
            cursor_url="https://api.github.test/next",
        )
        connection.commit()

        migrate(connection)

        checkpoints = dict(connection.execute(
            "SELECT stage, last_success_at FROM sync_state ORDER BY stage"
        ))
        assert checkpoints["success"] == "2026-08-20T00:00:00Z"
        assert checkpoints["success-without-checkpoint"] == "2026-08-20T00:00:00Z"
        assert checkpoints["not-modified"] == "2026-08-20T00:00:00Z"
        assert checkpoints["incomplete"] == "2026-08-01T00:00:00Z"
        assert checkpoints["error"] == "2026-08-01T00:00:00Z"
        assert checkpoints["success-with-cursor"] == "2026-08-01T00:00:00Z"
        assert [row[0] for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )] == [1, 2, 3]
    finally:
        connection.close()


def test_migration_three_repairs_polluted_states_and_preserves_resume_data(tmp_path):
    connection = _version_one_database(tmp_path / "polluted-version-two.db")
    try:
        metadata = '{"pull_number":42,"updated_at":"2026-08-12T00:00:00Z"}'
        _insert_sync_state(
            connection,
            "incomplete",
            "incomplete",
            cursor_url="https://api.github.test/next",
            last_success_at="2026-08-20T00:00:00Z",
            last_error="page limit",
            metadata_json=metadata,
        )
        _insert_sync_state(
            connection,
            "error",
            "error",
            last_success_at="2026-08-20T00:00:00Z",
            last_error="temporary failure",
        )
        _insert_sync_state(
            connection,
            "completed-with-cursor",
            "success",
            cursor_url="https://api.github.test/stale",
            last_success_at="2026-08-20T00:00:00Z",
        )
        _insert_sync_state(
            connection,
            "success",
            "success",
            last_success_at="2026-08-20T00:00:00Z",
        )
        _insert_sync_state(
            connection,
            "not-modified",
            "not_modified",
            last_success_at="2026-08-20T00:00:00Z",
        )
        _insert_sync_state(
            connection,
            "unrelated-error",
            "error",
            last_success_at="2026-08-05T00:00:00Z",
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (2, ?, ?)",
            (MIGRATIONS[1][1], "2026-08-25T00:00:00Z"),
        )
        connection.commit()

        migrate(connection)

        checkpoints = dict(connection.execute(
            "SELECT stage, last_success_at FROM sync_state ORDER BY stage"
        ))
        assert checkpoints["incomplete"] == "2026-08-10T00:00:00Z"
        assert checkpoints["error"] == "2026-08-10T00:00:00Z"
        assert checkpoints["completed-with-cursor"] == "2026-08-10T00:00:00Z"
        assert checkpoints["success"] == "2026-08-20T00:00:00Z"
        assert checkpoints["not-modified"] == "2026-08-20T00:00:00Z"
        assert checkpoints["unrelated-error"] == "2026-08-05T00:00:00Z"

        preserved = connection.execute(
            """SELECT cursor_url, range_start, range_end, status, last_error, metadata_json
               FROM sync_state WHERE stage='incomplete'"""
        ).fetchone()
        assert tuple(preserved) == (
            "https://api.github.test/next",
            "2026-08-10T00:00:00Z",
            "2026-08-20T00:00:00Z",
            "incomplete",
            "page limit",
            metadata,
        )
    finally:
        connection.close()


def test_migration_three_is_repeatable(tmp_path):
    connection = _version_one_database(tmp_path / "repeatable-version-two.db")
    try:
        _insert_sync_state(
            connection,
            "error",
            "error",
            last_success_at="2026-08-20T00:00:00Z",
            last_error="temporary failure",
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (2, ?, ?)",
            (MIGRATIONS[1][1], "2026-08-25T00:00:00Z"),
        )
        connection.commit()

        migrate(connection)
        first = tuple(connection.execute(
            "SELECT * FROM sync_state WHERE stage='error'"
        ).fetchone())
        migrate(connection)
        second = tuple(connection.execute(
            "SELECT * FROM sync_state WHERE stage='error'"
        ).fetchone())

        assert second == first
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=3"
        ).fetchone()[0] == 1
    finally:
        connection.close()
