from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from github_activity_db.github_client import GitHubAPIError, GitHubClient, RateLimitError
from github_activity_db.migrations import utc_now
from github_activity_db.sync_service import SyncService


def event(event_id: str, repo: str = "new-owner/new-repo", body: str = "hello"):
    return {"id": event_id, "type": "IssueCommentEvent", "created_at": "2026-08-20T10:00:00Z",
            "repo": {"name": repo}, "payload": {"action": "created", "comment": {
                "body": body, "html_url": f"https://github.com/{repo}/issues/1#issuecomment-1"}}}


def make_service(db, settings, handler, *, retries=0):
    api = GitHubClient(token=None, api_version=settings.api_version, user_agent="test",
                       max_retries=retries, transport=httpx.MockTransport(handler), sleep=lambda _: None)
    return SyncService(db, settings, api), api


def test_duplicate_event_and_discovered_repo_is_not_watched(db, settings):
    service, api = make_service(db, settings, lambda request: httpx.Response(200, json=[event("1"), event("1")]))
    with api:
        service.sync_person("gvanrossum")
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    repo = db.execute("SELECT id FROM repositories WHERE full_name='new-owner/new-repo'").fetchone()
    assert repo
    assert db.execute("SELECT COUNT(*) FROM watched_repositories WHERE repository_id=?", (repo[0],)).fetchone()[0] == 0
    linked = db.execute("""SELECT p.display_name FROM events e JOIN people p ON p.id=e.person_id
                           WHERE e.github_event_id='1'""").fetchone()[0]
    assert linked == "Guido van Rossum"


def test_etag_is_saved_and_304_preserves_single_event(db, settings):
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, headers={"ETag": '"v1"'}, json=[event("1")])
        assert request.headers["if-none-match"] == '"v1"'
        return httpx.Response(304, headers={"ETag": '"v1"'})
    service, api = make_service(db, settings, handler)
    with api:
        service.sync_person("gvanrossum")
        result = service.sync_person("gvanrossum")
    assert result.message == "変更なし"
    assert db.execute("SELECT etag FROM api_cache WHERE endpoint='/users/gvanrossum/events/public'").fetchone()[0] == '"v1"'
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_poll_interval_is_honored(db, settings):
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"X-Poll-Interval": "3600"}, json=[])
    service, api = make_service(db, settings, handler)
    with api:
        service.sync_person("gvanrossum")
        result = service.sync_person("gvanrossum")
    assert result.status == "skipped"
    assert calls == 1


def test_rate_limit_headers_are_saved(db, settings):
    def handler(request):
        return httpx.Response(403, headers={"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "0",
                                            "X-RateLimit-Reset": "1800000000"},
                              json={"message": "rate limit"})
    service, api = make_service(db, settings, handler)
    with api, pytest.raises(RateLimitError):
        service.sync_person("gvanrossum")
    cached = db.execute("SELECT rate_remaining, rate_limit FROM api_cache").fetchone()
    assert tuple(cached) == (0, 60)


def test_page_limit_and_resume(db, settings):
    settings = replace(settings, max_pages=1)
    urls = []
    def handler(request):
        urls.append(str(request.url))
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[event("2")])
        return httpx.Response(200, headers={"Link": '<https://api.github.com/users/gvanrossum/events/public?page=2>; rel="next"'},
                              json=[event("1")])
    service, api = make_service(db, settings, handler)
    with api:
        first = service.sync_person("gvanrossum")
        second = service.sync_person("gvanrossum")
    assert first.status == "incomplete"
    assert "page=2" in urls[1]
    assert second.status == "success"
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
    state = db.execute("SELECT cursor_url FROM sync_state WHERE stage='events'").fetchone()
    assert state[0] is None


def test_failure_does_not_advance_last_success(db, settings):
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=[event("1")])
        return httpx.Response(500, json={"message": "temporary"})
    service, api = make_service(db, settings, handler)
    with api:
        service.sync_person("gvanrossum")
        before = db.execute("SELECT last_success_at FROM sync_state WHERE stage='events'").fetchone()[0]
        with pytest.raises(GitHubAPIError):
            service.sync_person("gvanrossum")
    state = db.execute("SELECT last_success_at, status FROM sync_state WHERE stage='events'").fetchone()
    assert state[0] == before
    assert state[1] == "error"


def test_five_minute_overlap(db, settings):
    service, api = make_service(db, settings, lambda request: httpx.Response(200, json=[]))
    last = "2026-08-25T10:00:00Z"
    service._set_state("repository", "python/cpython", "commits", status="success", cursor_url=None,
                       range_start="2026-08-01T00:00:00Z", range_end=last, success=True)
    db.execute("UPDATE sync_state SET last_success_at=?", (last,)); db.commit()
    assert service._range_start("repository", "python/cpython", "commits", 90, None) == "2026-08-25T09:55:00Z"
    api.close()


def test_duplicate_commit_and_issue_pr_are_not_double_counted(db, settings):
    service, api = make_service(db, settings, lambda request: httpx.Response(200, json=[]))
    repo_id = db.execute("SELECT id FROM repositories WHERE full_name='python/cpython'").fetchone()[0]
    commit = {"sha": "abc", "html_url": "https://github.com/python/cpython/commit/abc",
              "author": {"login": "gvanrossum"}, "commit": {"message": "Improve parser",
              "author": {"name": "Guido", "date": "2026-08-20T00:00:00Z"}}}
    service._save_commit(repo_id, commit); service._save_commit(repo_id, commit)
    issue_as_pr = {"id": 10, "number": 5, "title": "PR", "body": "", "state": "open",
                   "user": {"login": "gvanrossum"}, "created_at": "2026-08-20T00:00:00Z",
                   "updated_at": "2026-08-20T00:00:00Z", "pull_request": {"url": "x"}}
    assert service._save_issue(repo_id, issue_as_pr) is False
    service._save_pull(repo_id, issue_as_pr)
    db.commit(); api.close()
    assert db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM pull_requests").fetchone()[0] == 1
