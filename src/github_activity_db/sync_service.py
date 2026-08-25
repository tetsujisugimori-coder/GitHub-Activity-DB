from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Iterable

from .config import Settings
from .github_client import APIResponse, GitHubAPIError, GitHubClient, RateLimitError
from .migrations import utc_now


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _login(item: dict[str, Any] | None) -> str | None:
    return item.get("login") if item else None


def _iso(value: str | None) -> str | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class SyncResult:
    scope: str
    status: str = "success"
    pages: int = 0
    seen: int = 0
    saved: int = 0
    message: str = ""


class SyncService:
    def __init__(self, connection: sqlite3.Connection, settings: Settings,
                 client: GitHubClient) -> None:
        self.db = connection
        self.settings = settings
        self.client = client

    def _person_for_login(self, login: str | None) -> tuple[int | None, int | None]:
        if not login:
            return None, None
        row = self.db.execute(
            "SELECT person_id, id FROM person_accounts WHERE provider='github' AND login=? COLLATE NOCASE",
            (login,),
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def _repository(self, full_name: str) -> int:
        row = self.db.execute(
            "SELECT id FROM repositories WHERE full_name=? COLLATE NOCASE", (full_name,)
        ).fetchone()
        if row:
            return row[0]
        owner, name = full_name.split("/", 1)
        cursor = self.db.execute(
            "INSERT INTO repositories(full_name, owner_login, name, html_url, discovered_at) VALUES (?,?,?,?,?)",
            (full_name, owner, name, f"https://github.com/{full_name}", utc_now()),
        )
        return int(cursor.lastrowid)

    def _cache(self, endpoint: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM api_cache WHERE endpoint=?", (endpoint,)).fetchone()

    def _record_api(self, endpoint: str, response: APIResponse) -> None:
        headers = response.headers
        reset = headers.get("x-ratelimit-reset")
        reset_iso = None
        if reset and reset.isdigit():
            reset_iso = datetime.fromtimestamp(int(reset), UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        values = (
            endpoint, headers.get("etag"), int(headers["x-poll-interval"]) if headers.get("x-poll-interval", "").isdigit() else None,
            utc_now(), int(headers["x-ratelimit-limit"]) if headers.get("x-ratelimit-limit", "").isdigit() else None,
            int(headers["x-ratelimit-remaining"]) if headers.get("x-ratelimit-remaining", "").isdigit() else None,
            reset_iso, int(headers["retry-after"]) if headers.get("retry-after", "").isdigit() else None,
            response.status_code,
        )
        self.db.execute(
            """INSERT INTO api_cache(endpoint, etag, poll_interval, last_checked_at, rate_limit,
                   rate_remaining, rate_reset, retry_after, status_code) VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(endpoint) DO UPDATE SET
                 etag=COALESCE(excluded.etag, api_cache.etag),
                 poll_interval=COALESCE(excluded.poll_interval, api_cache.poll_interval),
                 last_checked_at=excluded.last_checked_at,
                 rate_limit=COALESCE(excluded.rate_limit, api_cache.rate_limit),
                 rate_remaining=COALESCE(excluded.rate_remaining, api_cache.rate_remaining),
                 rate_reset=COALESCE(excluded.rate_reset, api_cache.rate_reset),
                 retry_after=excluded.retry_after, status_code=excluded.status_code""",
            values,
        )

    def _get(self, url: str, *, params: dict[str, Any] | None = None,
             conditional: bool = False) -> APIResponse:
        cache = self._cache(url)
        etag = cache["etag"] if conditional and cache else None
        try:
            response = self.client.get(url, params=params, etag=etag)
        except RateLimitError as exc:
            self._record_api(url, APIResponse(exc.status_code, None, exc.headers, None, url))
            self.db.commit()
            raise
        self._record_api(url, response)
        return response

    def _run_start(self, scope_type: str, scope_key: str) -> int:
        cursor = self.db.execute(
            "INSERT INTO sync_runs(scope_type, scope_key, started_at, status) VALUES (?,?,?,'running')",
            (scope_type, scope_key, utc_now()),
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def _run_finish(self, run_id: int, result: SyncResult, error: str | None = None) -> None:
        self.db.execute(
            """UPDATE sync_runs SET finished_at=?, status=?, items_seen=?, items_saved=?,
               pages_fetched=?, error=? WHERE id=?""",
            (utc_now(), result.status, result.seen, result.saved, result.pages, error, run_id),
        )
        self.db.commit()

    def _state(self, scope_type: str, scope_key: str, stage: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM sync_state WHERE scope_type=? AND scope_key=? COLLATE NOCASE AND stage=?",
            (scope_type, scope_key, stage),
        ).fetchone()

    def _set_state(self, scope_type: str, scope_key: str, stage: str, *,
                   status: str, cursor_url: str | None, range_start: str | None,
                   range_end: str | None, success: bool = False,
                   error: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        self.db.execute(
            """INSERT INTO sync_state(scope_type, scope_key, stage, cursor_url, last_success_at,
                   range_start, range_end, status, last_error, metadata_json, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(scope_type, scope_key, stage) DO UPDATE SET
                 cursor_url=excluded.cursor_url,
                 last_success_at=CASE WHEN ? THEN excluded.last_success_at ELSE sync_state.last_success_at END,
                 range_start=excluded.range_start, range_end=excluded.range_end,
                 status=excluded.status, last_error=excluded.last_error,
                 metadata_json=COALESCE(excluded.metadata_json, sync_state.metadata_json),
                 updated_at=excluded.updated_at""",
            (scope_type, scope_key, stage, cursor_url, utc_now() if success else None,
             range_start, range_end, status, error, _json(metadata) if metadata else None,
             utc_now(), 1 if success else 0),
        )

    def _range_start(self, scope_type: str, scope_key: str, stage: str,
                     initial_days: int, explicit_since: str | None) -> str:
        if explicit_since:
            return _iso(explicit_since) or explicit_since
        state = self._state(scope_type, scope_key, stage)
        if state and state["last_success_at"]:
            last = datetime.fromisoformat(state["last_success_at"].replace("Z", "+00:00"))
            return (last - timedelta(minutes=self.settings.overlap_minutes)).isoformat(timespec="seconds").replace("+00:00", "Z")
        return _days_ago(initial_days)

    def _paged(self, *, scope_type: str, scope_key: str, stage: str, path: str,
               params: dict[str, Any] | None, start: str | None, end: str | None,
               saver: Callable[[dict[str, Any]], bool], max_pages: int | None = None,
               resume: bool = True, conditional: bool = False,
               accept: Callable[[dict[str, Any]], bool] | None = None) -> SyncResult:
        result = SyncResult(f"{scope_type}:{scope_key}:{stage}")
        state = self._state(scope_type, scope_key, stage)
        matching_explicit_range = bool(
            state and state["cursor_url"] and state["range_start"] == start and state["range_end"] == end
        )
        url = state["cursor_url"] if state and state["cursor_url"] and (resume or matching_explicit_range) else path
        request_params = None if url != path else params
        page_limit = max_pages or self.settings.max_pages
        try:
            while url and result.pages < page_limit:
                response = self._get(url, params=request_params,
                                     conditional=conditional and result.pages == 0)
                request_params = None
                result.pages += 1
                if response.status_code == 304:
                    self._set_state(scope_type, scope_key, stage, status="not_modified",
                                    cursor_url=None, range_start=start, range_end=end, success=True)
                    self.db.commit()
                    result.message = "変更なし"
                    return result
                items = response.data if isinstance(response.data, list) else []
                for item in items:
                    result.seen += 1
                    if accept is None or accept(item):
                        result.saved += int(saver(item))
                url = response.next_url
                incomplete = bool(url)
                self._set_state(scope_type, scope_key, stage,
                                status="incomplete" if incomplete else "success",
                                cursor_url=url, range_start=start, range_end=end,
                                success=not incomplete)
                self.db.commit()
                if not url:
                    break
            if url:
                result.status = "incomplete"
                result.message = f"{page_limit}ページ上限で中断。次回再開します"
            return result
        except Exception as exc:
            self.db.rollback()
            current = self._state(scope_type, scope_key, stage)
            self._set_state(scope_type, scope_key, stage, status="error",
                            cursor_url=current["cursor_url"] if current else None,
                            range_start=start, range_end=end, error=str(exc))
            self.db.commit()
            raise

    def sync_person(self, login: str) -> SyncResult:
        person_id, account_id = self._person_for_login(login)
        if not person_id:
            raise ValueError(f"未登録の人物アカウントです: {login}")
        run_id = self._run_start("person", login)
        result = SyncResult(f"person:{login}")
        try:
            endpoint = f"/users/{login}/events/public"
            cache = self._cache(endpoint)
            if cache and cache["poll_interval"] and cache["last_checked_at"]:
                checked = datetime.fromisoformat(cache["last_checked_at"].replace("Z", "+00:00"))
                if datetime.now(UTC) < checked + timedelta(seconds=cache["poll_interval"]):
                    result.status = "skipped"
                    result.message = "X-Poll-Intervalの待機中"
                    self._run_finish(run_id, result)
                    return result
            start = _days_ago(self.settings.people_initial_days)

            def save(event: dict[str, Any]) -> bool:
                return self._save_event(person_id, account_id, event)

            result = self._paged(
                scope_type="person", scope_key=login, stage="events", path=endpoint,
                params={"per_page": self.settings.per_page}, start=start, end=utc_now(),
                saver=save, max_pages=min(3, self.settings.max_pages), conditional=True,
            )
            self._run_finish(run_id, result)
            return result
        except Exception as exc:
            result.status = "rate_limited" if isinstance(exc, RateLimitError) else "failed"
            self._run_finish(run_id, result, str(exc))
            raise

    def _save_event(self, person_id: int, account_id: int, event: dict[str, Any]) -> bool:
        repo_name = (event.get("repo") or {}).get("name")
        repository_id = self._repository(repo_name) if repo_name and "/" in repo_name else None
        payload = event.get("payload") or {}
        event_type = event.get("type") or "UnknownEvent"
        action = payload.get("action") or payload.get("ref_type")
        obj = payload.get("pull_request") or payload.get("issue") or payload.get("comment") or payload.get("release") or {}
        title = obj.get("title") or payload.get("ref")
        body = obj.get("body") or obj.get("message")
        html_url = obj.get("html_url")
        if not html_url and repo_name:
            html_url = f"https://github.com/{repo_name}"
        occurred = _iso(event.get("created_at")) or utc_now()
        before = self.db.total_changes
        self.db.execute(
            """INSERT INTO events(github_event_id, person_id, account_id, repository_id,
                   event_type, action, title, body, html_url, occurred_at, fetched_at, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(github_event_id) DO UPDATE SET
                 action=excluded.action, title=excluded.title, body=excluded.body,
                 html_url=excluded.html_url, raw_json=excluded.raw_json""",
            (str(event["id"]), person_id, account_id, repository_id, event_type, action,
             title, body, html_url, occurred, utc_now(), _json(event)),
        )
        if repository_id:
            self.db.execute(
                """INSERT INTO person_repository_relations(person_id, repository_id, relation_type,
                       first_seen_at, last_seen_at) VALUES (?,?,'event',?,?)
                   ON CONFLICT(person_id, repository_id, relation_type) DO UPDATE SET
                     last_seen_at=MAX(last_seen_at, excluded.last_seen_at)""",
                (person_id, repository_id, occurred, occurred),
            )
        return self.db.total_changes > before

    def sync_repository(self, full_name: str, *, since: str | None = None,
                        until: str | None = None) -> SyncResult:
        repository_id = self._repository(full_name)
        watched = self.db.execute(
            "SELECT initial_days FROM watched_repositories WHERE repository_id=? AND enabled=1",
            (repository_id,),
        ).fetchone()
        if not watched:
            raise ValueError(f"発見済みですが監視対象ではありません: {full_name}")
        run_id = self._run_start("repository", full_name)
        total = SyncResult(f"repository:{full_name}")
        try:
            metadata = self._get(f"/repos/{full_name}", conditional=True)
            total.pages += 1
            if metadata.status_code != 304 and isinstance(metadata.data, dict):
                total.saved += int(self._save_repository(repository_id, metadata.data))
            initial_days = watched["initial_days"] or self.settings.repository_initial_days
            start = self._range_start("repository", full_name, "commits", initial_days, since)
            end = _iso(until) if until else utc_now()
            if since and not until:
                prior_range = self.db.execute(
                    """SELECT range_end FROM sync_state WHERE scope_type='repository'
                       AND scope_key=? COLLATE NOCASE AND range_start=? AND cursor_url IS NOT NULL
                       ORDER BY updated_at DESC LIMIT 1""", (full_name, start),
                ).fetchone()
                if prior_range and prior_range["range_end"]:
                    end = prior_range["range_end"]
            resume = since is None and until is None
            def merge(stage_result: SyncResult) -> None:
                total.pages += stage_result.pages
                total.seen += stage_result.seen
                total.saved += stage_result.saved
                if stage_result.status == "incomplete":
                    total.status = "incomplete"
                    total.message = stage_result.message

            remaining = self.settings.max_pages - total.pages
            if remaining > 0:
                merge(self._paged(
                    scope_type="repository", scope_key=full_name, stage="commits",
                    path=f"/repos/{full_name}/commits",
                    params={"since": start, "until": end, "per_page": self.settings.per_page},
                    start=start, end=end, saver=lambda item: self._save_commit(repository_id, item),
                    resume=resume, max_pages=remaining,
                ))
            for stage, endpoint, saver, date_field in (
                ("issues", "issues", self._save_issue, "updated_at"),
                ("pull_requests", "pulls", self._save_pull, "updated_at"),
                ("issue_comments", "issues/comments", self._save_issue_comment, "updated_at"),
                ("review_comments", "pulls/comments", self._save_review_comment, "updated_at"),
                ("releases", "releases", self._save_release, "published_at"),
            ):
                remaining = self.settings.max_pages - total.pages
                if remaining <= 0:
                    total.status = "incomplete"
                    total.message = f"{self.settings.max_pages}ページ上限で中断。次回再開します"
                    break
                stage_start = self._range_start("repository", full_name, stage, initial_days, since)
                params: dict[str, Any] = {"per_page": self.settings.per_page}
                if stage == "issues":
                    params.update({"state": "all", "since": stage_start, "sort": "updated", "direction": "asc"})
                elif stage == "pull_requests":
                    params.update({"state": "all", "sort": "updated", "direction": "desc"})
                elif stage in ("issue_comments", "review_comments"):
                    params.update({"since": stage_start, "sort": "updated", "direction": "asc"})
                accept = lambda item, field=date_field, boundary=stage_start: not item.get(field) or item[field] >= boundary
                merge(self._paged(
                    scope_type="repository", scope_key=full_name, stage=stage,
                    path=f"/repos/{full_name}/{endpoint}", params=params,
                    start=stage_start, end=end,
                    saver=lambda item, fn=saver: fn(repository_id, item),
                    resume=resume, accept=accept, max_pages=remaining,
                ))
            remaining = self.settings.max_pages - total.pages
            if remaining > 0:
                merge(self._sync_reviews(repository_id, full_name, start, end, remaining, resume))
            else:
                total.status = "incomplete"
                total.message = f"{self.settings.max_pages}ページ上限で中断。次回再開します"
            self._run_finish(run_id, total)
            return total
        except Exception as exc:
            total.status = "rate_limited" if isinstance(exc, RateLimitError) else "failed"
            self._run_finish(run_id, total, str(exc))
            raise

    def _save_repository(self, repository_id: int, item: dict[str, Any]) -> bool:
        before = self.db.total_changes
        self.db.execute(
            """UPDATE repositories SET github_id=?, full_name=?, owner_login=?, name=?, description=?,
               html_url=?, default_branch=?, language=?, topics_json=?, stars=?, forks=?, open_issues=?,
               visibility=?, metadata_updated_at=?, raw_json=? WHERE id=?""",
            (item.get("id"), item["full_name"], item["owner"]["login"], item["name"],
             item.get("description"), item.get("html_url"), item.get("default_branch"),
             item.get("language"), _json(item.get("topics", [])), item.get("stargazers_count", 0),
             item.get("forks_count", 0), item.get("open_issues_count", 0), item.get("visibility"),
             _iso(item.get("updated_at")), _json(item), repository_id),
        )
        return self.db.total_changes > before

    def _save_commit(self, repository_id: int, item: dict[str, Any]) -> bool:
        author = item.get("author") or item.get("committer")
        login = _login(author)
        person_id, _ = self._person_for_login(login)
        commit = item.get("commit") or {}
        author_info = commit.get("author") or {}
        committed = _iso(author_info.get("date") or (commit.get("committer") or {}).get("date")) or utc_now()
        before = self.db.total_changes
        self.db.execute(
            """INSERT INTO commits(repository_id, sha, person_id, author_login, author_name,
                   message, html_url, committed_at, fetched_at, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(repository_id, sha) DO UPDATE SET person_id=excluded.person_id,
                 author_login=excluded.author_login, author_name=excluded.author_name,
                 message=excluded.message, html_url=excluded.html_url, raw_json=excluded.raw_json""",
            (repository_id, item["sha"], person_id, login, author_info.get("name"),
             commit.get("message", ""), item.get("html_url"), committed, utc_now(), _json(item)),
        )
        self._touch_relation(person_id, repository_id, "commit", committed)
        return self.db.total_changes > before

    def _save_issue(self, repository_id: int, item: dict[str, Any]) -> bool:
        if "pull_request" in item:
            return False
        login = _login(item.get("user")); person_id, _ = self._person_for_login(login)
        before = self.db.total_changes
        self.db.execute(
            """INSERT INTO issues(github_id, repository_id, number, person_id, author_login,
                   title, body, state, html_url, created_at, updated_at, closed_at, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(repository_id, number) DO UPDATE SET
                 person_id=excluded.person_id, author_login=excluded.author_login, title=excluded.title,
                 body=excluded.body, state=excluded.state, html_url=excluded.html_url,
                 updated_at=excluded.updated_at, closed_at=excluded.closed_at, raw_json=excluded.raw_json""",
            (item["id"], repository_id, item["number"], person_id, login, item.get("title", ""),
             item.get("body"), item.get("state", "open"), item.get("html_url"), _iso(item.get("created_at")) or utc_now(),
             _iso(item.get("updated_at")) or utc_now(), _iso(item.get("closed_at")), _json(item)),
        )
        self._touch_relation(person_id, repository_id, "issue", _iso(item.get("updated_at")) or utc_now())
        return self.db.total_changes > before

    def _save_pull(self, repository_id: int, item: dict[str, Any]) -> bool:
        login = _login(item.get("user")); person_id, _ = self._person_for_login(login)
        before = self.db.total_changes
        self.db.execute(
            """INSERT INTO pull_requests(github_id, repository_id, number, person_id, author_login,
                   title, body, state, draft, merged_at, html_url, created_at, updated_at, closed_at, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(repository_id, number) DO UPDATE SET
                 person_id=excluded.person_id, author_login=excluded.author_login, title=excluded.title,
                 body=excluded.body, state=excluded.state, draft=excluded.draft,
                 merged_at=excluded.merged_at, html_url=excluded.html_url,
                 updated_at=excluded.updated_at, closed_at=excluded.closed_at, raw_json=excluded.raw_json""",
            (item["id"], repository_id, item["number"], person_id, login, item.get("title", ""),
             item.get("body"), item.get("state", "open"), int(bool(item.get("draft"))), _iso(item.get("merged_at")),
             item.get("html_url"), _iso(item.get("created_at")) or utc_now(),
             _iso(item.get("updated_at")) or utc_now(), _iso(item.get("closed_at")), _json(item)),
        )
        self._touch_relation(person_id, repository_id, "pull_request", _iso(item.get("updated_at")) or utc_now())
        return self.db.total_changes > before

    def _save_issue_comment(self, repository_id: int, item: dict[str, Any]) -> bool:
        login = _login(item.get("user")); person_id, _ = self._person_for_login(login)
        issue_number = int((item.get("issue_url") or "0").rstrip("/").split("/")[-1])
        before = self.db.total_changes
        self.db.execute(
            """INSERT INTO issue_comments(github_id, repository_id, issue_number, person_id,
                   author_login, body, html_url, created_at, updated_at, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(github_id) DO UPDATE SET
                 person_id=excluded.person_id, body=excluded.body, html_url=excluded.html_url,
                 updated_at=excluded.updated_at, raw_json=excluded.raw_json""",
            (item["id"], repository_id, issue_number, person_id, login, item.get("body", ""),
             item.get("html_url"), _iso(item.get("created_at")) or utc_now(),
             _iso(item.get("updated_at")) or utc_now(), _json(item)),
        )
        self._touch_relation(person_id, repository_id, "issue_comment", _iso(item.get("updated_at")) or utc_now())
        return self.db.total_changes > before

    def _save_review_comment(self, repository_id: int, item: dict[str, Any]) -> bool:
        login = _login(item.get("user")); person_id, _ = self._person_for_login(login)
        pull_number = int((item.get("pull_request_url") or "0").rstrip("/").split("/")[-1])
        before = self.db.total_changes
        self.db.execute(
            """INSERT INTO review_comments(github_id, repository_id, pull_number, person_id,
                   author_login, body, path, line, html_url, created_at, updated_at, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(github_id) DO UPDATE SET
                 person_id=excluded.person_id, body=excluded.body, path=excluded.path,
                 line=excluded.line, html_url=excluded.html_url, updated_at=excluded.updated_at,
                 raw_json=excluded.raw_json""",
            (item["id"], repository_id, pull_number, person_id, login, item.get("body", ""),
             item.get("path"), item.get("line"), item.get("html_url"),
             _iso(item.get("created_at")) or utc_now(), _iso(item.get("updated_at")) or utc_now(), _json(item)),
        )
        self._touch_relation(person_id, repository_id, "review_comment", _iso(item.get("updated_at")) or utc_now())
        return self.db.total_changes > before

    def _save_review(self, repository_id: int, pull_number: int, item: dict[str, Any]) -> bool:
        login = _login(item.get("user")); person_id, _ = self._person_for_login(login)
        before = self.db.total_changes
        self.db.execute(
            """INSERT INTO pull_request_reviews(github_id, repository_id, pull_number, person_id,
                   author_login, state, body, html_url, submitted_at, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(github_id) DO UPDATE SET
                 person_id=excluded.person_id, state=excluded.state, body=excluded.body,
                 html_url=excluded.html_url, submitted_at=excluded.submitted_at, raw_json=excluded.raw_json""",
            (item["id"], repository_id, pull_number, person_id, login, item.get("state"), item.get("body"),
             item.get("html_url"), _iso(item.get("submitted_at")), _json(item)),
        )
        self._touch_relation(person_id, repository_id, "review", _iso(item.get("submitted_at")) or utc_now())
        return self.db.total_changes > before

    def _save_release(self, repository_id: int, item: dict[str, Any]) -> bool:
        before = self.db.total_changes
        self.db.execute(
            """INSERT INTO releases(github_id, repository_id, tag_name, name, body, author_login,
                   html_url, draft, prerelease, published_at, created_at, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(github_id) DO UPDATE SET
                 tag_name=excluded.tag_name, name=excluded.name, body=excluded.body,
                 html_url=excluded.html_url, draft=excluded.draft, prerelease=excluded.prerelease,
                 published_at=excluded.published_at, raw_json=excluded.raw_json""",
            (item["id"], repository_id, item.get("tag_name", ""), item.get("name"), item.get("body"),
             _login(item.get("author")), item.get("html_url"), int(bool(item.get("draft"))),
             int(bool(item.get("prerelease"))), _iso(item.get("published_at")),
             _iso(item.get("created_at")) or utc_now(), _json(item)),
        )
        return self.db.total_changes > before

    def _sync_reviews(self, repository_id: int, full_name: str, start: str, end: str,
                      max_pages: int, resume: bool) -> SyncResult:
        result = SyncResult(f"repository:{full_name}:reviews")
        state = self._state("repository", full_name, "reviews")
        cursor_meta: dict[str, Any] = {}
        matching_range = bool(state and state["range_start"] == start and state["range_end"] == end)
        if state and state["cursor_url"] and state["metadata_json"] and (resume or matching_range):
            try:
                cursor_meta = json.loads(state["metadata_json"])
            except json.JSONDecodeError:
                cursor_meta = {}
        sql = """SELECT number, updated_at FROM pull_requests
                 WHERE repository_id=? AND updated_at>=?"""
        params: list[Any] = [repository_id, start]
        if cursor_meta.get("updated_at") and cursor_meta.get("number") is not None:
            sql += " AND (updated_at < ? OR (updated_at = ? AND number < ?))"
            params.extend((cursor_meta["updated_at"], cursor_meta["updated_at"], cursor_meta["number"]))
        sql += " ORDER BY updated_at DESC, number DESC LIMIT ?"
        params.append(max_pages + 1)
        candidates = self.db.execute(sql, params).fetchall()
        pulls = candidates[:max_pages]
        has_more = len(candidates) > max_pages
        try:
            for row in pulls:
                response = self._get(
                    f"/repos/{full_name}/pulls/{row['number']}/reviews",
                    params={"per_page": self.settings.per_page},
                )
                result.pages += 1
                for item in response.data if isinstance(response.data, list) else []:
                    result.seen += 1
                    result.saved += int(self._save_review(repository_id, row["number"], item))
                self.db.commit()
            if has_more and pulls:
                last = pulls[-1]
                result.status = "incomplete"
                result.message = f"{max_pages}ページ上限でレビュー収集を中断。次回再開します"
                self._set_state("repository", full_name, "reviews", status="incomplete",
                                cursor_url="review-keyset", range_start=start, range_end=end,
                                metadata={"updated_at": last["updated_at"], "number": last["number"]})
            else:
                self._set_state("repository", full_name, "reviews", status="success", cursor_url=None,
                                range_start=start, range_end=end, success=True, metadata={"complete": True})
            self.db.commit()
            return result
        except Exception as exc:
            self.db.rollback()
            self._set_state("repository", full_name, "reviews", status="error", cursor_url=None,
                            range_start=start, range_end=end, error=str(exc))
            self.db.commit()
            raise

    def _touch_relation(self, person_id: int | None, repository_id: int,
                        relation: str, at: str) -> None:
        if person_id is None:
            return
        self.db.execute(
            """INSERT INTO person_repository_relations(person_id, repository_id, relation_type,
                   first_seen_at, last_seen_at) VALUES (?,?,?,?,?)
               ON CONFLICT(person_id, repository_id, relation_type) DO UPDATE SET
                 last_seen_at=MAX(last_seen_at, excluded.last_seen_at)""",
            (person_id, repository_id, relation, at, at),
        )

    def sync_people(self, login: str | None = None) -> list[SyncResult]:
        rows = self.db.execute(
            "SELECT login FROM person_accounts WHERE provider='github' " +
            ("AND login=? COLLATE NOCASE " if login else "") + "ORDER BY login",
            (login,) if login else (),
        ).fetchall()
        return [self.sync_person(row["login"]) for row in rows]

    def sync_repositories(self, full_name: str | None = None, *, since: str | None = None,
                          until: str | None = None) -> list[SyncResult]:
        sql = """SELECT r.full_name FROM repositories r JOIN watched_repositories w ON w.repository_id=r.id
                 WHERE w.enabled=1"""
        params: tuple[Any, ...] = ()
        if full_name:
            sql += " AND r.full_name=? COLLATE NOCASE"
            params = (full_name,)
        sql += " ORDER BY r.full_name"
        rows = self.db.execute(sql, params).fetchall()
        if full_name and not rows:
            raise ValueError(f"監視対象リポジトリではありません: {full_name}")
        return [self.sync_repository(row["full_name"], since=since, until=until) for row in rows]
