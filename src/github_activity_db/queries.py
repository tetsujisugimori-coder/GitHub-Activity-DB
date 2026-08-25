from __future__ import annotations

import re
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any


ACTIVITY_UNION = """
SELECT 'event' kind, event_type subtype, occurred_at activity_at, person_id, repository_id,
       COALESCE(title, action, event_type) title, body, html_url, '人物イベントAPI' source
FROM events
UNION ALL
SELECT 'commit', 'Commit', committed_at, person_id, repository_id,
       substr(message, 1, instr(message || char(10), char(10)) - 1), message, html_url, 'Commits API'
FROM commits
UNION ALL
SELECT 'issue', 'Issue', updated_at, person_id, repository_id, title, body, html_url, 'Issues API'
FROM issues
UNION ALL
SELECT 'pull_request', 'Pull Request', updated_at, person_id, repository_id, title, body, html_url, 'Pulls API'
FROM pull_requests
UNION ALL
SELECT 'issue_comment', 'Issue comment', updated_at, person_id, repository_id,
       'Issue #' || issue_number || ' のコメント', body, html_url, 'Issue comments API'
FROM issue_comments
UNION ALL
SELECT 'review', 'Pull request review', COALESCE(submitted_at, ''), person_id, repository_id,
       'Pull Request #' || pull_number || ' のレビュー', body, html_url, 'Pull request reviews API'
FROM pull_request_reviews
UNION ALL
SELECT 'review_comment', 'Review comment', updated_at, person_id, repository_id,
       'Pull Request #' || pull_number || ' のレビューコメント', body, html_url, 'Review comments API'
FROM review_comments
"""


def cutoff(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds").replace("+00:00", "Z")


def counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = ("people", "person_accounts", "organizations", "repositories", "events", "commits",
              "issues", "pull_requests", "issue_comments", "pull_request_reviews", "review_comments", "releases")
    return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}


def dashboard(connection: sqlite3.Connection) -> dict[str, Any]:
    union = f"({ACTIVITY_UNION})"
    count_7 = connection.execute(f"SELECT COUNT(*) FROM {union} WHERE activity_at>=?", (cutoff(7),)).fetchone()[0]
    count_30 = connection.execute(f"SELECT COUNT(*) FROM {union} WHERE activity_at>=?", (cutoff(30),)).fetchone()[0]
    people = connection.execute(
        f"""SELECT p.id, p.display_name, COUNT(a.kind) count FROM people p
            LEFT JOIN {union} a ON a.person_id=p.id AND a.activity_at>=?
            GROUP BY p.id ORDER BY count DESC, p.display_name""", (cutoff(30),)
    ).fetchall()
    repositories = connection.execute(
        f"""SELECT r.id, r.full_name, COUNT(a.kind) count FROM repositories r
            LEFT JOIN {union} a ON a.repository_id=r.id AND a.activity_at>=?
            GROUP BY r.id ORDER BY count DESC, r.full_name LIMIT 12""", (cutoff(30),)
    ).fetchall()
    types = connection.execute(
        f"SELECT kind, COUNT(*) count FROM {union} WHERE activity_at>=? GROUP BY kind ORDER BY count DESC",
        (cutoff(30),),
    ).fetchall()
    last_sync = connection.execute(
        "SELECT MAX(finished_at) FROM sync_runs WHERE status IN ('success','incomplete')"
    ).fetchone()[0]
    errors = connection.execute(
        "SELECT * FROM sync_runs WHERE status IN ('failed','rate_limited') ORDER BY started_at DESC LIMIT 8"
    ).fetchall()
    rate = connection.execute(
        "SELECT rate_remaining, rate_limit, rate_reset FROM api_cache WHERE rate_remaining IS NOT NULL ORDER BY last_checked_at DESC LIMIT 1"
    ).fetchone()
    return {"count_7": count_7, "count_30": count_30, "people": people,
            "repositories": repositories, "types": types, "last_sync": last_sync,
            "errors": errors, "rate": rate}


def people_list(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        f"""SELECT p.id, p.display_name, p.category, GROUP_CONCAT(DISTINCT a.login) logins,
             (SELECT COUNT(*) FROM ({ACTIVITY_UNION}) x WHERE x.person_id=p.id AND x.activity_at>=?) count_7,
             (SELECT COUNT(*) FROM ({ACTIVITY_UNION}) x WHERE x.person_id=p.id AND x.activity_at>=?) count_30,
             (SELECT GROUP_CONCAT(full_name, ', ') FROM (
                SELECT DISTINCT r.full_name FROM person_repository_relations pr
                JOIN repositories r ON r.id=pr.repository_id WHERE pr.person_id=p.id
                ORDER BY pr.last_seen_at DESC LIMIT 4)) recent_repositories,
             (SELECT MAX(last_success_at) FROM sync_state s JOIN person_accounts pa
                ON pa.login=s.scope_key COLLATE NOCASE WHERE s.scope_type='person' AND pa.person_id=p.id) last_sync
           FROM people p LEFT JOIN person_accounts a ON a.person_id=p.id
           GROUP BY p.id ORDER BY p.display_name""", (cutoff(7), cutoff(30))
    ).fetchall()


def activity_page(connection: sqlite3.Connection, *, person_id: int | None = None,
                  repository_id: int | None = None, kind: str | None = None,
                  repository_filter: int | None = None, days: int = 30,
                  page: int = 1, per_page: int = 30) -> tuple[list[sqlite3.Row], int]:
    clauses = ["a.activity_at>=?"]
    params: list[Any] = [cutoff(days)]
    if person_id is not None:
        clauses.append("a.person_id=?"); params.append(person_id)
    repo = repository_filter if repository_filter is not None else repository_id
    if repo is not None:
        clauses.append("a.repository_id=?"); params.append(repo)
    if kind:
        clauses.append("(a.kind=? OR a.subtype=?)"); params.extend((kind, kind))
    where = " AND ".join(clauses)
    total = connection.execute(
        f"SELECT COUNT(*) FROM ({ACTIVITY_UNION}) a WHERE {where}", params
    ).fetchone()[0]
    rows = connection.execute(
        f"""SELECT a.*, r.full_name, p.display_name FROM ({ACTIVITY_UNION}) a
            LEFT JOIN repositories r ON r.id=a.repository_id
            LEFT JOIN people p ON p.id=a.person_id
            WHERE {where} ORDER BY a.activity_at DESC LIMIT ? OFFSET ?""",
        (*params, per_page, (max(page, 1) - 1) * per_page),
    ).fetchall()
    return rows, total


STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "into", "fix", "add", "update",
    "remove", "use", "using", "github", "pull", "request", "issue", "comment", "merge",
    "です", "ます", "する", "した", "ため", "から", "この", "その", "こと", "対応", "修正", "追加",
}
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{2,}|[ぁ-んァ-ヶ一-龠]{2,}")


def estimated_topics(rows: list[sqlite3.Row], limit: int = 12) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    examples: dict[str, str | None] = {}
    for row in rows:
        text = f"{row['title'] or ''} {(row['body'] or '')[:300]} {row['full_name'] or ''}"
        for token in TOKEN_RE.findall(text):
            normalized = token.lower()
            if normalized in STOPWORDS or len(normalized) < 3:
                continue
            counter[normalized] += 1
            examples.setdefault(normalized, row["html_url"])
    return [{"term": term, "count": count, "url": examples.get(term)}
            for term, count in counter.most_common(limit)]


def comparison(connection: sqlite3.Connection, days: int,
               person_ids: list[int], repository_ids: list[int]) -> dict[str, Any]:
    kinds = ("commit", "issue", "pull_request", "review", "issue_comment", "review_comment", "event")

    def grouped(column: str, ids: list[int], table: str, label: str) -> list[dict[str, Any]]:
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"""SELECT o.id, o.{label} label, a.kind, COUNT(*) count FROM {table} o
                CROSS JOIN ({' UNION ALL '.join('SELECT ? kind' for _ in kinds)}) k
                LEFT JOIN ({ACTIVITY_UNION}) a ON a.{column}=o.id AND a.kind=k.kind AND a.activity_at>=?
                WHERE o.id IN ({marks}) GROUP BY o.id, k.kind ORDER BY o.{label}, k.kind""",
            (*kinds, cutoff(days), *ids),
        ).fetchall()
        grouped_rows: dict[int, dict[str, Any]] = {}
        for row in rows:
            record = grouped_rows.setdefault(row["id"], {"label": row["label"], "counts": {}})
            record["counts"][row["kind"]] = row["count"]
        return list(grouped_rows.values())

    return {"people": grouped("person_id", person_ids, "people", "display_name"),
            "repositories": grouped("repository_id", repository_ids, "repositories", "full_name"),
            "kinds": kinds}

