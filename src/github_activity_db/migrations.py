from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from .config import Settings


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (1, "initial_schema", r"""
CREATE TABLE people (
  id INTEGER PRIMARY KEY, display_name TEXT NOT NULL, category TEXT NOT NULL,
  profile_url TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE person_accounts (
  id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  provider TEXT NOT NULL DEFAULT 'github', login TEXT NOT NULL COLLATE NOCASE,
  github_id INTEGER, profile_url TEXT, avatar_url TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(provider, login)
);
CREATE TABLE organizations (
  id INTEGER PRIMARY KEY, login TEXT NOT NULL COLLATE NOCASE UNIQUE,
  display_name TEXT NOT NULL, github_id INTEGER, profile_url TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE repositories (
  id INTEGER PRIMARY KEY, github_id INTEGER UNIQUE, full_name TEXT NOT NULL COLLATE NOCASE UNIQUE,
  owner_login TEXT NOT NULL COLLATE NOCASE, name TEXT NOT NULL, description TEXT,
  html_url TEXT, default_branch TEXT, language TEXT, topics_json TEXT NOT NULL DEFAULT '[]',
  stars INTEGER NOT NULL DEFAULT 0, forks INTEGER NOT NULL DEFAULT 0,
  open_issues INTEGER NOT NULL DEFAULT 0, visibility TEXT,
  discovered_at TEXT NOT NULL, metadata_updated_at TEXT, raw_json TEXT
);
CREATE TABLE watched_repositories (
  repository_id INTEGER PRIMARY KEY REFERENCES repositories(id) ON DELETE CASCADE,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)), initial_days INTEGER,
  added_at TEXT NOT NULL
);
CREATE TABLE person_repository_relations (
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
  relation_type TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  PRIMARY KEY(person_id, repository_id, relation_type)
);
CREATE TABLE events (
  id INTEGER PRIMARY KEY, github_event_id TEXT NOT NULL UNIQUE,
  person_id INTEGER REFERENCES people(id), account_id INTEGER REFERENCES person_accounts(id),
  repository_id INTEGER REFERENCES repositories(id), event_type TEXT NOT NULL,
  action TEXT, title TEXT, body TEXT, html_url TEXT, occurred_at TEXT NOT NULL,
  fetched_at TEXT NOT NULL, raw_json TEXT
);
CREATE TABLE commits (
  id INTEGER PRIMARY KEY, repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
  sha TEXT NOT NULL, person_id INTEGER REFERENCES people(id), author_login TEXT COLLATE NOCASE,
  author_name TEXT, message TEXT NOT NULL, html_url TEXT, committed_at TEXT NOT NULL,
  fetched_at TEXT NOT NULL, raw_json TEXT, UNIQUE(repository_id, sha)
);
CREATE TABLE issues (
  id INTEGER PRIMARY KEY, github_id INTEGER NOT NULL UNIQUE,
  repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
  number INTEGER NOT NULL, person_id INTEGER REFERENCES people(id), author_login TEXT COLLATE NOCASE,
  title TEXT NOT NULL, body TEXT, state TEXT NOT NULL, html_url TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT, raw_json TEXT,
  UNIQUE(repository_id, number)
);
CREATE TABLE pull_requests (
  id INTEGER PRIMARY KEY, github_id INTEGER NOT NULL UNIQUE,
  repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
  number INTEGER NOT NULL, person_id INTEGER REFERENCES people(id), author_login TEXT COLLATE NOCASE,
  title TEXT NOT NULL, body TEXT, state TEXT NOT NULL, draft INTEGER NOT NULL DEFAULT 0,
  merged_at TEXT, html_url TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  closed_at TEXT, raw_json TEXT, UNIQUE(repository_id, number)
);
CREATE TABLE issue_comments (
  id INTEGER PRIMARY KEY, github_id INTEGER NOT NULL UNIQUE,
  repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
  issue_number INTEGER NOT NULL, person_id INTEGER REFERENCES people(id),
  author_login TEXT COLLATE NOCASE, body TEXT NOT NULL, html_url TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, raw_json TEXT
);
CREATE TABLE pull_request_reviews (
  id INTEGER PRIMARY KEY, github_id INTEGER NOT NULL UNIQUE,
  repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
  pull_number INTEGER NOT NULL, person_id INTEGER REFERENCES people(id),
  author_login TEXT COLLATE NOCASE, state TEXT, body TEXT, html_url TEXT,
  submitted_at TEXT, raw_json TEXT
);
CREATE TABLE review_comments (
  id INTEGER PRIMARY KEY, github_id INTEGER NOT NULL UNIQUE,
  repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
  pull_number INTEGER NOT NULL, person_id INTEGER REFERENCES people(id),
  author_login TEXT COLLATE NOCASE, body TEXT NOT NULL, path TEXT, line INTEGER,
  html_url TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, raw_json TEXT
);
CREATE TABLE releases (
  id INTEGER PRIMARY KEY, github_id INTEGER NOT NULL UNIQUE,
  repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
  tag_name TEXT NOT NULL, name TEXT, body TEXT, author_login TEXT COLLATE NOCASE,
  html_url TEXT, draft INTEGER NOT NULL DEFAULT 0, prerelease INTEGER NOT NULL DEFAULT 0,
  published_at TEXT, created_at TEXT NOT NULL, raw_json TEXT
);
CREATE TABLE sync_state (
  id INTEGER PRIMARY KEY, scope_type TEXT NOT NULL, scope_key TEXT NOT NULL COLLATE NOCASE,
  stage TEXT NOT NULL, cursor_url TEXT, last_success_at TEXT, range_start TEXT, range_end TEXT,
  status TEXT NOT NULL DEFAULT 'never', last_error TEXT, metadata_json TEXT,
  updated_at TEXT NOT NULL, UNIQUE(scope_type, scope_key, stage)
);
CREATE TABLE sync_runs (
  id INTEGER PRIMARY KEY, scope_type TEXT NOT NULL, scope_key TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
  items_seen INTEGER NOT NULL DEFAULT 0, items_saved INTEGER NOT NULL DEFAULT 0,
  pages_fetched INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE TABLE api_cache (
  endpoint TEXT PRIMARY KEY, etag TEXT, poll_interval INTEGER,
  last_checked_at TEXT, rate_limit INTEGER, rate_remaining INTEGER,
  rate_reset TEXT, retry_after INTEGER, status_code INTEGER
);
CREATE INDEX idx_accounts_person ON person_accounts(person_id);
CREATE INDEX idx_repositories_owner ON repositories(owner_login);
CREATE INDEX idx_relations_person_last_seen ON person_repository_relations(person_id, last_seen_at DESC);
CREATE INDEX idx_events_person_date ON events(person_id, occurred_at DESC);
CREATE INDEX idx_events_repo_date ON events(repository_id, occurred_at DESC);
CREATE INDEX idx_events_type_date ON events(event_type, occurred_at DESC);
CREATE INDEX idx_commits_person_date ON commits(person_id, committed_at DESC);
CREATE INDEX idx_commits_repo_date ON commits(repository_id, committed_at DESC);
CREATE INDEX idx_issues_repo_updated ON issues(repository_id, updated_at DESC);
CREATE INDEX idx_issues_person_updated ON issues(person_id, updated_at DESC);
CREATE INDEX idx_pulls_repo_updated ON pull_requests(repository_id, updated_at DESC);
CREATE INDEX idx_pulls_person_updated ON pull_requests(person_id, updated_at DESC);
CREATE INDEX idx_issue_comments_person_date ON issue_comments(person_id, created_at DESC);
CREATE INDEX idx_issue_comments_repo_date ON issue_comments(repository_id, created_at DESC);
CREATE INDEX idx_reviews_person_date ON pull_request_reviews(person_id, submitted_at DESC);
CREATE INDEX idx_reviews_repo_date ON pull_request_reviews(repository_id, submitted_at DESC);
CREATE INDEX idx_review_comments_person_date ON review_comments(person_id, created_at DESC);
CREATE INDEX idx_review_comments_repo_date ON review_comments(repository_id, created_at DESC);
CREATE INDEX idx_releases_repo_date ON releases(repository_id, published_at DESC);
CREATE INDEX idx_sync_state_status ON sync_state(status, updated_at DESC);
"""),
)


def migrate(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL
    )""")
    applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    for version, name, sql in MIGRATIONS:
        if version in applied:
            continue
        connection.executescript(sql)
        connection.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            (version, name, utc_now()),
        )
        connection.commit()
    connection.execute("PRAGMA optimize")


def seed_targets(connection: sqlite3.Connection, settings: Settings) -> None:
    now = utc_now()
    with connection:
        for person in settings.people:
            row = connection.execute(
                "SELECT p.id FROM people p JOIN person_accounts a ON a.person_id=p.id "
                "WHERE a.provider='github' AND a.login=? COLLATE NOCASE LIMIT 1",
                (person.accounts[0],),
            ).fetchone() if person.accounts else None
            if row:
                person_id = row[0]
                connection.execute(
                    "UPDATE people SET display_name=?, category=?, updated_at=? WHERE id=?",
                    (person.display_name, person.category, now, person_id),
                )
            else:
                cursor = connection.execute(
                    "INSERT INTO people(display_name, category, created_at, updated_at) VALUES (?,?,?,?)",
                    (person.display_name, person.category, now, now),
                )
                person_id = cursor.lastrowid
            for login in person.accounts:
                connection.execute(
                    """INSERT INTO person_accounts(person_id, provider, login, profile_url, created_at, updated_at)
                       VALUES (?, 'github', ?, ?, ?, ?)
                       ON CONFLICT(provider, login) DO UPDATE SET
                         person_id=excluded.person_id, profile_url=excluded.profile_url, updated_at=excluded.updated_at""",
                    (person_id, login, f"https://github.com/{login}", now, now),
                )
        for org in settings.organizations:
            connection.execute(
                """INSERT INTO organizations(login, display_name, profile_url, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?) ON CONFLICT(login) DO UPDATE SET
                   display_name=excluded.display_name, updated_at=excluded.updated_at""",
                (org.login, org.display_name, f"https://github.com/{org.login}", now, now),
            )
        for repo in settings.repositories:
            owner, name = repo.full_name.split("/", 1)
            connection.execute(
                """INSERT INTO repositories(full_name, owner_login, name, html_url, discovered_at)
                   VALUES (?, ?, ?, ?, ?) ON CONFLICT(full_name) DO NOTHING""",
                (repo.full_name, owner, name, f"https://github.com/{repo.full_name}", now),
            )
            repository_id = connection.execute(
                "SELECT id FROM repositories WHERE full_name=? COLLATE NOCASE", (repo.full_name,)
            ).fetchone()[0]
            if repo.watched:
                connection.execute(
                    """INSERT INTO watched_repositories(repository_id, enabled, initial_days, added_at)
                       VALUES (?, 1, ?, ?) ON CONFLICT(repository_id) DO UPDATE SET
                       enabled=1, initial_days=excluded.initial_days""",
                    (repository_id, repo.initial_days, now),
                )


def initialize_database(connection: sqlite3.Connection, settings: Settings) -> None:
    migrate(connection)
    seed_targets(connection, settings)
