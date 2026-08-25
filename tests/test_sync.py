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


def make_service(db, settings, handler, *, retries=0, now=None):
    api = GitHubClient(token=None, api_version=settings.api_version, user_agent="test",
                       max_retries=retries, transport=httpx.MockTransport(handler), sleep=lambda _: None)
    kwargs = {"now": now} if now else {}
    return SyncService(db, settings, api, **kwargs), api


def repository_metadata():
    return {
        "id": 81598961, "full_name": "python/cpython", "owner": {"login": "python"},
        "name": "cpython", "description": "Python", "html_url": "https://github.com/python/cpython",
        "default_branch": "main", "language": "Python", "topics": ["python"],
        "stargazers_count": 1, "forks_count": 1, "open_issues_count": 1,
        "visibility": "public", "updated_at": "2026-08-10T00:00:00Z",
    }


def review(review_id: int, submitted_at: str):
    return {"id": review_id, "user": {"login": "gvanrossum"}, "state": "APPROVED",
            "body": "review", "html_url": f"https://github.com/review/{review_id}",
            "submitted_at": submitted_at}


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


def test_resume_keeps_range_end_checkpoint_and_next_overlap(db, settings):
    settings = replace(settings, max_pages=1)
    clock = {"value": "2026-08-25T10:00:00Z"}
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                headers={"Link": '<https://api.github.com/users/gvanrossum/events/public?page=2>; rel="next"'},
                json=[event("range-1")],
            )
        if calls == 2:
            return httpx.Response(200, json=[event("range-2")])
        newest = event("range-3")
        newest["created_at"] = "2026-08-25T10:20:00Z"
        return httpx.Response(200, json=[newest])

    service, api = make_service(db, settings, handler, now=lambda: clock["value"])
    with api:
        first = service.sync_person("gvanrossum")
        first_state = db.execute("SELECT * FROM sync_state WHERE stage='events'").fetchone()
        assert first.status == "incomplete"
        assert first_state["range_end"] == "2026-08-25T10:00:00Z"
        assert first_state["last_success_at"] is None

        clock["value"] = "2026-08-25T10:30:00Z"
        second = service.sync_person("gvanrossum")
        second_state = db.execute("SELECT * FROM sync_state WHERE stage='events'").fetchone()
        assert second.status == "success"
        assert second_state["range_end"] == "2026-08-25T10:00:00Z"
        assert second_state["last_success_at"] == "2026-08-25T10:00:00Z"

        clock["value"] = "2026-08-25T10:40:00Z"
        service.sync_person("gvanrossum")
        third_state = db.execute("SELECT * FROM sync_state WHERE stage='events'").fetchone()
        assert third_state["range_start"] == "2026-08-25T09:55:00Z"
        assert third_state["range_end"] == "2026-08-25T10:40:00Z"
    assert db.execute("SELECT COUNT(*) FROM events WHERE github_event_id='range-3'").fetchone()[0] == 1


def test_explicit_range_resume_reuses_original_end(db, settings):
    service, api = make_service(db, settings, lambda request: httpx.Response(200, json=[]),
                                now=lambda: "2026-08-25T10:30:00Z")
    service._set_state(
        "repository", "python/cpython", "commits", status="incomplete",
        cursor_url="https://api.github.com/next", range_start="2026-08-01T00:00:00Z",
        range_end="2026-08-10T00:00:00Z",
    )
    db.commit()
    start, end = service._stage_range(
        "repository", "python/cpython", "commits", 90,
        "2026-08-01T00:00:00Z", None, "2026-08-25T10:30:00Z",
    )
    api.close()
    assert (start, end) == ("2026-08-01T00:00:00Z", "2026-08-10T00:00:00Z")


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


def test_until_filters_every_repository_activity_type(db, settings):
    settings = replace(settings, max_pages=20, per_page=100)
    inside = "2026-08-05T12:00:00Z"
    outside = "2026-08-11T12:00:00Z"

    def commit(sha, at):
        return {"sha": sha, "html_url": f"https://github.com/commit/{sha}",
                "author": {"login": "gvanrossum"},
                "commit": {"message": sha, "author": {"name": "Guido", "date": at}}}

    def issue(item_id, number, at):
        return {"id": item_id, "number": number, "title": f"issue-{number}", "body": "",
                "state": "open", "user": {"login": "gvanrossum"}, "html_url": "https://github.com/issue",
                "created_at": at, "updated_at": at, "closed_at": None}

    def pull(item_id, number, at):
        item = issue(item_id, number, at)
        item.update({"draft": False, "merged_at": None})
        return item

    def issue_comment(item_id, number, at):
        return {"id": item_id, "issue_url": f"https://api.github.com/issues/{number}",
                "user": {"login": "gvanrossum"}, "body": "comment", "html_url": "https://github.com/comment",
                "created_at": at, "updated_at": at}

    def review_comment(item_id, number, at):
        return {"id": item_id, "pull_request_url": f"https://api.github.com/pulls/{number}",
                "user": {"login": "gvanrossum"}, "body": "review comment", "path": "x.py", "line": 1,
                "html_url": "https://github.com/review-comment", "created_at": at, "updated_at": at}

    def release(item_id, at):
        return {"id": item_id, "tag_name": f"v{item_id}", "name": f"v{item_id}", "body": "",
                "author": {"login": "gvanrossum"}, "html_url": "https://github.com/release",
                "draft": False, "prerelease": False, "published_at": at, "created_at": at}

    def handler(request):
        path = request.url.path
        if path == "/repos/python/cpython":
            return httpx.Response(200, json=repository_metadata())
        if path.endswith("/commits"):
            return httpx.Response(200, json=[commit("inside", inside), commit("outside", outside)])
        if path.endswith("/issues/comments"):
            return httpx.Response(200, json=[issue_comment(31, 1, inside), issue_comment(32, 2, outside)])
        if path.endswith("/pulls/comments"):
            return httpx.Response(200, json=[review_comment(41, 1, inside), review_comment(42, 2, outside)])
        if path.endswith("/pulls/1/reviews"):
            return httpx.Response(200, json=[review(51, inside), review(52, outside)])
        if path.endswith("/issues"):
            return httpx.Response(200, json=[issue(11, 1, inside), issue(12, 2, outside)])
        if path.endswith("/pulls"):
            return httpx.Response(200, json=[pull(21, 1, inside), pull(22, 2, outside)])
        if path.endswith("/releases"):
            return httpx.Response(200, json=[release(61, inside), release(62, outside)])
        raise AssertionError(f"unexpected URL: {request.url}")

    service, api = make_service(db, settings, handler, now=lambda: "2026-08-25T00:00:00Z")
    with api:
        service.sync_repository(
            "python/cpython", since="2026-08-01T00:00:00Z", until="2026-08-10T00:00:00Z",
        )
    for table in ("commits", "issues", "pull_requests", "issue_comments",
                  "review_comments", "pull_request_reviews", "releases"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1, table


def test_invalid_explicit_range_is_rejected_before_api_call(db, settings):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    service, api = make_service(db, settings, handler)
    with api, pytest.raises(ValueError, match="--sinceは--until以前"):
        service.sync_repository(
            "python/cpython", since="2026-08-11T00:00:00Z", until="2026-08-10T00:00:00Z",
        )
    assert calls == 0


def test_review_link_pagination_resumes_with_fixed_range(db, settings):
    settings = replace(settings, per_page=100)
    service = None
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[review(72, "2026-08-06T00:00:00Z")])
        return httpx.Response(
            200,
            headers={"Link": '<https://api.github.com/repos/python/cpython/pulls/1/reviews?page=2>; rel="next"'},
            json=[review(71, "2026-08-05T00:00:00Z")],
        )

    service, api = make_service(db, settings, handler, now=lambda: "2026-08-25T00:00:00Z")
    repo_id = db.execute("SELECT id FROM repositories WHERE full_name='python/cpython'").fetchone()[0]
    service._save_pull(repo_id, {"id": 70, "number": 1, "title": "PR", "body": "", "state": "open",
        "draft": False, "merged_at": None, "user": {"login": "gvanrossum"},
        "html_url": "https://github.com/pr/1", "created_at": "2026-08-05T00:00:00Z",
        "updated_at": "2026-08-05T00:00:00Z", "closed_at": None})
    db.commit()
    with api:
        first = service._sync_reviews(
            repo_id, "python/cpython", "2026-08-01T00:00:00Z", "2026-08-10T00:00:00Z", 1,
        )
        state = db.execute("SELECT * FROM sync_state WHERE stage='reviews'").fetchone()
        assert first.status == "incomplete"
        assert "page=2" in state["cursor_url"]
        assert state["last_success_at"] is None
        second = service._sync_reviews(
            repo_id, "python/cpython", "2026-08-01T00:00:00Z", "2026-08-10T00:00:00Z", 1,
        )
    state = db.execute("SELECT * FROM sync_state WHERE stage='reviews'").fetchone()
    assert second.status == "success"
    assert state["last_success_at"] == "2026-08-10T00:00:00Z"
    assert db.execute("SELECT COUNT(*) FROM pull_request_reviews").fetchone()[0] == 2
    assert calls == 2
