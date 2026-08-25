from __future__ import annotations

from datetime import UTC, datetime, timedelta

from github_activity_db.migrations import initialize_database, utc_now


def test_initial_database_and_repeatable_migration(db, settings):
    initialize_database(db, settings)
    initialize_database(db, settings)
    assert db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
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

