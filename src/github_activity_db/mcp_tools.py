from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any

from .config import Settings
from .db import connect_readonly
from .queries import ACTIVITY_UNION, cutoff


KINDS = {
    "event", "commit", "issue", "pull_request", "issue_comment", "review", "review_comment"
}


def _open(settings: Settings) -> sqlite3.Connection:
    if not settings.db_path.is_file():
        raise ValueError(
            "SQLite DBがありません。先に `python -m github_activity_db init-db` を実行してください。"
        )
    return connect_readonly(settings.db_path)


def _validate_days(days: int) -> None:
    if not 1 <= days <= 365:
        raise ValueError("daysは1～365で指定してください。")


def get_database_status(settings: Settings) -> dict[str, Any]:
    """Return a compact, non-sensitive summary of the local activity database."""
    with closing(_open(settings)) as connection:
        activity_count = connection.execute(
            f"SELECT COUNT(*) FROM ({ACTIVITY_UNION})"
        ).fetchone()[0]
        last_sync = connection.execute(
            "SELECT MAX(finished_at) FROM sync_runs WHERE status IN ('success', 'incomplete')"
        ).fetchone()[0]
        incomplete = connection.execute(
            "SELECT COUNT(*) FROM sync_state WHERE status='incomplete'"
        ).fetchone()[0]
        failures = connection.execute(
            "SELECT COUNT(*) FROM sync_runs WHERE status IN ('failed', 'rate_limited')"
        ).fetchone()[0]
        return {
            "database_exists": True,
            "size_bytes": settings.db_path.stat().st_size,
            "people": connection.execute("SELECT COUNT(*) FROM people").fetchone()[0],
            "repositories": connection.execute("SELECT COUNT(*) FROM repositories").fetchone()[0],
            "activities": activity_count,
            "last_sync_at": last_sync,
            "incomplete_syncs": incomplete,
            "failed_or_rate_limited_syncs": failures,
            "access": "read-only",
        }


def list_tracked_people(settings: Settings, days: int = 30) -> dict[str, Any]:
    """List configured people and their activity counts for a recent period."""
    _validate_days(days)
    with closing(_open(settings)) as connection:
        rows = connection.execute(
            f"""SELECT p.display_name, p.category,
                       (SELECT GROUP_CONCAT(pa.login)
                          FROM person_accounts pa WHERE pa.person_id=p.id) AS github_accounts,
                       (SELECT COUNT(*) FROM ({ACTIVITY_UNION}) a
                          WHERE a.person_id=p.id AND a.activity_at>=?) AS activity_count,
                       (SELECT MAX(a.activity_at) FROM ({ACTIVITY_UNION}) a
                          WHERE a.person_id=p.id AND a.activity_at>=?) AS latest_activity_at
                FROM people p
                ORDER BY activity_count DESC, p.display_name""",
            (cutoff(days), cutoff(days)),
        ).fetchall()
        return {
            "days": days,
            "people": [
                {
                    "display_name": row["display_name"],
                    "category": row["category"],
                    "github_accounts": (row["github_accounts"] or "").split(",") if row["github_accounts"] else [],
                    "activity_count": row["activity_count"],
                    "latest_activity_at": row["latest_activity_at"],
                }
                for row in rows
            ],
        }


def search_activities(
    settings: Settings,
    query: str = "",
    person: str = "",
    repository: str = "",
    kind: str = "",
    days: int = 30,
    limit: int = 20,
) -> dict[str, Any]:
    """Search recent saved activities with optional person, repository, and kind filters."""
    _validate_days(days)
    if not 1 <= limit <= 50:
        raise ValueError("limitは1～50で指定してください。")
    if kind and kind not in KINDS:
        raise ValueError("kindが不正です。利用可能: " + ", ".join(sorted(KINDS)))

    clauses = ["a.activity_at>=?"]
    params: list[Any] = [cutoff(days)]
    if query.strip():
        clauses.append("(a.title LIKE ? OR a.body LIKE ? OR r.full_name LIKE ?)")
        pattern = f"%{query.strip()}%"
        params.extend((pattern, pattern, pattern))
    if person.strip():
        clauses.append("(p.display_name=? COLLATE NOCASE OR pa.login=? COLLATE NOCASE)")
        params.extend((person.strip(), person.strip()))
    if repository.strip():
        clauses.append("r.full_name=? COLLATE NOCASE")
        params.append(repository.strip())
    if kind:
        clauses.append("a.kind=?")
        params.append(kind)

    where = " AND ".join(clauses)
    with closing(_open(settings)) as connection:
        rows = connection.execute(
            f"""SELECT a.kind, a.subtype, a.activity_at, a.title,
                       substr(COALESCE(a.body, ''), 1, 500) AS body_excerpt,
                       a.html_url, a.source, p.display_name, r.full_name
                FROM ({ACTIVITY_UNION}) a
                LEFT JOIN people p ON p.id=a.person_id
                LEFT JOIN person_accounts pa ON pa.person_id=p.id
                LEFT JOIN repositories r ON r.id=a.repository_id
                WHERE {where}
                GROUP BY a.kind, a.subtype, a.activity_at, a.title, a.body,
                         a.html_url, a.source, p.display_name, r.full_name
                ORDER BY a.activity_at DESC
                LIMIT ?""",
            (*params, limit),
        ).fetchall()
        return {
            "filters": {
                "query": query, "person": person, "repository": repository,
                "kind": kind, "days": days, "limit": limit,
            },
            "count": len(rows),
            "activities": [dict(row) for row in rows],
        }
