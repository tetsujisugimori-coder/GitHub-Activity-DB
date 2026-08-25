from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from github_activity_db.migrations import initialize_database, migrate, utc_now


def test_initial_database_and_repeatable_migration(db, settings):
    initialize_database(db, settings)
    initialize_database(db, settings)
    assert db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 2
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


def test_checkpoint_migration_uses_saved_range_end(db):
    db.execute("DELETE FROM schema_migrations WHERE version=2")
    db.execute(
        """INSERT INTO sync_state(scope_type, scope_key, stage, cursor_url, last_success_at,
               range_start, range_end, status, updated_at)
           VALUES ('repository', 'example/repo', 'commits', NULL, ?, ?, ?, 'success', ?)""",
        ("2026-08-25T10:30:00Z", "2026-08-01T00:00:00Z",
         "2026-08-25T10:00:00Z", "2026-08-25T10:30:00Z"),
    )
    db.commit()
    migrate(db)
    checkpoint = db.execute(
        "SELECT last_success_at FROM sync_state WHERE scope_key='example/repo'"
    ).fetchone()[0]
    assert checkpoint == "2026-08-25T10:00:00Z"
