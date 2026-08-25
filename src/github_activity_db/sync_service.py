from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

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


def _subtract_days(value: str, days: int) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed - timedelta(days=days)).isoformat(timespec="seconds").replace("+00:00", "Z")


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
                 client: GitHubClient, *, now: Callable[[], str] = utc_now) -> None:
        self.db = connection
        self.settings = settings
        self.client = client
        self._now = now

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
            (full_name, owner, name, f"https://github.com/{full_name}", self._now()),
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
            self._now(), int(headers["x-ratelimit-limit"]) if headers.get("x-ratelimit-limit", "").isdigit() else None,
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
            (scope_type, scope_key, self._now()),
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def _run_finish(self, run_id: int, result: SyncResult, error: str | None = None) -> None:
        self.db.execute(
            """UPDATE sync_runs SET finished_at=?, status=?, items_seen=?, items_saved=?,
               pages_fetched=?, error=? WHERE id=?""",
            (self._now(), result.status, result.seen, result.saved, result.pages, error, run_id),
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
            (scope_type, scope_key, stage, cursor_url, range_end if success else None,
             range_start, range_end, status, error, _json(metadata) if metadata else None,
             self._now(), 1 if success else 0),
        )

    def _normalize_range(self, since: str | None, until: str | None) -> tuple[str | None, str | None]:
        try:
            start = _iso(since)
        except ValueError as exc:
            raise ValueError(f"--sinceの日時形式が正しくありません: {since}") from exc
        try:
            end = _iso(until)
        except ValueError as exc:
            raise ValueError(f"--untilの日時形式が正しくありません: {until}") from exc
        if start and end and start > end:
            raise ValueError("--sinceは--until以前の日時を指定してください")
        return start, end

    def _stage_range(self, scope_type: str, scope_key: str, stage: str,
                     initial_days: int, explicit_start: str | None,
                     explicit_end: str | None, run_end: str) -> tuple[str, str]:
        state = self._state(scope_type, scope_key, stage)
        if state and state["cursor_url"] and state["range_start"] and state["range_end"]:
            normal_resume = explicit_start is None and explicit_end is None
            explicit_resume = (
                (explicit_start is None or explicit_start == state["range_start"])
                and (explicit_end is None or explicit_end == state["range_end"])
            )
            if normal_resume or explicit_resume:
                return state["range_start"], state["range_end"]
        if explicit_start:
            start = explicit_start
        elif state and state["last_success_at"]:
            last = datetime.fromisoformat(state["last_success_at"].replace("Z", "+00:00"))
            start = (last - timedelta(minutes=self.settings.overlap_minutes)).isoformat(
                timespec="seconds"
            ).replace("+00:00", "Z")
        else:
            start = _subtract_days(run_end, initial_days)
        end = explicit_end or run_end
        if start > end:
            raise ValueError(f"{scope_key} / {stage} の取得開始日時が終了日時より後です")
        return start, end

    def _range_start(self, scope_type: str, scope_key: str, stage: str,
                     initial_days: int, explicit_since: str | None) -> str:
        explicit_start, _ = self._normalize_range(explicit_since, None)
        start, _ = self._stage_range(
            scope_type, scope_key, stage, initial_days, explicit_start, None, self._now()
        )
        return start

    @staticmethod
    def _item_time(item: dict[str, Any], *fields: str) -> str | None:
        for field in fields:
            value = _iso(item.get(field))
            if value:
                return value
        return None

    @classmethod
    def _in_range(cls, item: dict[str, Any], start: str, end: str,
                  *fields: str) -> bool:
        value = cls._item_time(item, *fields)
        return bool(value and start <= value <= end)

    @staticmethod
    def _commit_time(item: dict[str, Any]) -> str | None:
        commit = item.get("commit") or {}
        author = commit.get("author") or {}
        committer = commit.get("committer") or {}
        return _iso(author.get("date") or committer.get("date"))

    def _commit_in_range(self, item: dict[str, Any], start: str, end: str) -> bool:
        value = self._commit_time(item)
        return bool(value and start <= value <= end)

    def _paged(self, *, scope_type: str, scope_key: str, stage: str, path: str,
               params: dict[str, Any] | None, start: str | None, end: str | None,
               saver: Callable[[dict[str, Any]], bool], max_pages: int | None = None,
               conditional: bool = False,
               accept: Callable[[dict[str, Any]], bool] | None = None) -> SyncResult:
        result = SyncResult(f"{scope_type}:{scope_key}:{stage}")
        state = self._state(scope_type, scope_key, stage)
        matching_range = bool(
            state and state["cursor_url"] and state["range_start"] == start and state["range_end"] == end
        )
        url = state["cursor_url"] if matching_range else path
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
            matching_range = bool(
                current and current["range_start"] == start and current["range_end"] == end
            )
            self._set_state(scope_type, scope_key, stage, status="error",
                            cursor_url=current["cursor_url"] if matching_range else None,
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
            run_end = self._now()
            start, end = self._stage_range(
                "person", login, "events", self.settings.people_initial_days,
                None, None, run_end,
            )

            def save(event: dict[str, Any]) -> bool:
                return self._save_event(person_id, account_id, event)

            result = self._paged(
                scope_type="person", scope_key=login, stage="events", path=endpoint,
                params={"per_page": self.settings.per_page}, start=start, end=end,
                saver=save, max_pages=min(3, self.settings.max_pages), conditional=True,
                accept=lambda item: self._in_range(item, start, end, "created_at"),
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
        explicit_start, explicit_end = self._normalize_range(since, until)
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
            run_end = self._now()
            metadata = self._get(f"/repos/{full_name}", conditional=True)
            total.pages += 1
            if metadata.status_code != 304 and isinstance(metadata.data, dict):
                total.saved += int(self._save_repository(repository_id, metadata.data))
            initial_days = watched["initial_days"] or self.settings.repository_initial_days

            def merge(stage_result: SyncResult) -> None:
                total.pages += stage_result.pages
                total.seen += stage_result.seen
                total.saved += stage_result.saved
                if stage_result.status == "incomplete":
                    total.status = "incomplete"
                    total.message = stage_result.message

            remaining = self.settings.max_pages - total.pages
            if remaining > 0:
                start, end = self._stage_range(
                    "repository", full_name, "commits", initial_days,
                    explicit_start, explicit_end, run_end,
                )
                merge(self._paged(
                    scope_type="repository", scope_key=full_name, stage="commits",
                    path=f"/repos/{full_name}/commits",
                    params={"since": start, "until": end, "per_page": self.settings.per_page},
                    start=start, end=end, saver=lambda item: self._save_commit(repository_id, item),
                    accept=lambda item: self._commit_in_range(item, start, end),
                    max_pages=remaining,
                ))
            for stage, endpoint, saver, date_fields in (
                ("issues", "issues", self._save_issue, ("updated_at",)),
                ("pull_requests", "pulls", self._save_pull, ("updated_at",)),
                ("issue_comments", "issues/comments", self._save_issue_comment, ("updated_at",)),
                ("review_comments", "pulls/comments", self._save_review_comment, ("updated_at",)),
                ("releases", "releases", self._save_release, ("published_at", "created_at")),
            ):
                remaining = self.settings.max_pages - total.pages
                if remaining <= 0:
                    total.status = "incomplete"
                    total.message = f"{self.settings.max_pages}ページ上限で中断。次回再開します"
                    break
                stage_start, stage_end = self._stage_range(
                    "repository", full_name, stage, initial_days,
                    explicit_start, explicit_end, run_end,
                )
                params: dict[str, Any] = {"per_page": self.settings.per_page}
                if stage == "issues":
                    params.update({"state": "all", "since": stage_start, "sort": "updated", "direction": "asc"})
                elif stage == "pull_requests":
                    params.update({"state": "all", "sort": "updated", "direction": "desc"})
                elif stage in ("issue_comments", "review_comments"):
                    params.update({"since": stage_start, "sort": "updated", "direction": "asc"})
                accept = lambda item, fields=date_fields, boundary=stage_start, boundary_end=stage_end: (
                    self._in_range(item, boundary, boundary_end, *fields)
                )
                merge(self._paged(
                    scope_type="repository", scope_key=full_name, stage=stage,
                    path=f"/repos/{full_name}/{endpoint}", params=params,
                    start=stage_start, end=stage_end,
                    saver=lambda item, fn=saver: fn(repository_id, item),
                    accept=accept, max_pages=remaining,
                ))
            remaining = self.settings.max_pages - total.pages
            if remaining > 0:
                review_start, review_end = self._stage_range(
                    "repository", full_name, "reviews", initial_days,
                    explicit_start, explicit_end, run_end,
                )
                merge(self._sync_reviews(
                    repository_id, full_name, review_start, review_end, remaining,
                ))
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

    def _next_review_pull(self, repository_id: int, start: str, end: str,
                          after_updated_at: str | None,
                          after_number: int | None) -> sqlite3.Row | None:
        sql = """SELECT number, updated_at FROM pull_requests
                 WHERE repository_id=? AND updated_at>=? AND updated_at<=?"""
        params: list[Any] = [repository_id, start, end]
        if after_updated_at and after_number is not None:
            sql += " AND (updated_at < ? OR (updated_at = ? AND number < ?))"
            params.extend((after_updated_at, after_updated_at, after_number))
        sql += " ORDER BY updated_at DESC, number DESC LIMIT 1"
        return self.db.execute(sql, params).fetchone()

    def _sync_reviews(self, repository_id: int, full_name: str, start: str, end: str,
                      max_pages: int) -> SyncResult:
        result = SyncResult(f"repository:{full_name}:reviews")
        state = self._state("repository", full_name, "reviews")
        matching_range = bool(
            state and state["cursor_url"]
            and state["range_start"] == start and state["range_end"] == end
        )
        cursor = state["cursor_url"] if matching_range else "review-next-pull"
        cursor_meta: dict[str, Any] = {}
        if matching_range and state["metadata_json"]:
            try:
                cursor_meta = json.loads(state["metadata_json"])
            except json.JSONDecodeError:
                cursor_meta = {}
        after_updated_at = cursor_meta.get("updated_at")
        after_number = cursor_meta.get("number")
        pull_number = cursor_meta.get("pull_number")
        pull_updated_at = cursor_meta.get("pull_updated_at")

        try:
            while result.pages < max_pages:
                if cursor == "review-next-pull":
                    candidate = self._next_review_pull(
                        repository_id, start, end, after_updated_at, after_number,
                    )
                    if candidate is None:
                        self._set_state(
                            "repository", full_name, "reviews", status="success",
                            cursor_url=None, range_start=start, range_end=end,
                            success=True, metadata={"complete": True},
                        )
                        self.db.commit()
                        return result
                    pull_number = candidate["number"]
                    pull_updated_at = candidate["updated_at"]
                    cursor = (
                        f"/repos/{full_name}/pulls/{pull_number}/reviews"
                        f"?per_page={self.settings.per_page}"
                    )

                metadata = {
                    "updated_at": after_updated_at,
                    "number": after_number,
                    "pull_number": pull_number,
                    "pull_updated_at": pull_updated_at,
                }
                self._set_state(
                    "repository", full_name, "reviews", status="incomplete",
                    cursor_url=cursor, range_start=start, range_end=end,
                    metadata=metadata,
                )
                self.db.commit()
                response = self._get(cursor)
                result.pages += 1
                for item in response.data if isinstance(response.data, list) else []:
                    result.seen += 1
                    if self._in_range(item, start, end, "submitted_at"):
                        result.saved += int(self._save_review(repository_id, int(pull_number), item))

                if response.next_url:
                    cursor = response.next_url
                    self._set_state(
                        "repository", full_name, "reviews", status="incomplete",
                        cursor_url=cursor, range_start=start, range_end=end,
                        metadata=metadata,
                    )
                else:
                    after_updated_at = str(pull_updated_at)
                    after_number = int(pull_number)
                    pull_number = None
                    pull_updated_at = None
                    cursor = "review-next-pull"
                    self._set_state(
                        "repository", full_name, "reviews", status="incomplete",
                        cursor_url=cursor, range_start=start, range_end=end,
                        metadata={"updated_at": after_updated_at, "number": after_number},
                    )
                self.db.commit()

                if result.pages >= max_pages:
                    if cursor == "review-next-pull" and self._next_review_pull(
                        repository_id, start, end, after_updated_at, after_number,
                    ) is None:
                        self._set_state(
                            "repository", full_name, "reviews", status="success",
                            cursor_url=None, range_start=start, range_end=end,
                            success=True, metadata={"complete": True},
                        )
                        self.db.commit()
                        return result
                    result.status = "incomplete"
                    result.message = f"{max_pages}ページ上限でレビュー収集を中断。次回再開します"
                    return result
            return result
        except Exception as exc:
            self.db.rollback()
            current = self._state("repository", full_name, "reviews")
            self._set_state(
                "repository", full_name, "reviews", status="error",
                cursor_url=current["cursor_url"] if current else cursor,
                range_start=start, range_end=end, error=str(exc),
            )
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
