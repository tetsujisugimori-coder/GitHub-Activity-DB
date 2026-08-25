from __future__ import annotations

import httpx
import pytest

from github_activity_db.github_client import GitHubClient, RateLimitError, parse_next_link


def client(handler, *, retries=1, sleeps=None):
    return GitHubClient(token="secret-value", api_version="2022-11-28", user_agent="test",
                        max_retries=retries, transport=httpx.MockTransport(handler),
                        sleep=(sleeps.append if sleeps is not None else lambda _: None))


def test_link_pagination_parser():
    link = '<https://api.github.com/items?page=2>; rel="next", <https://api.github.com/items?page=4>; rel="last"'
    assert parse_next_link(link) == "https://api.github.com/items?page=2"


def test_rate_limit_is_distinct_and_token_not_exposed():
    def handler(request):
        assert request.headers["authorization"] == "Bearer secret-value"
        return httpx.Response(403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1800000000"},
                              json={"message": "API rate limit exceeded"})
    with client(handler) as api:
        with pytest.raises(RateLimitError) as error:
            api.get("/rate_limit")
    assert error.value.reset_at
    assert "secret-value" not in str(error.value)


def test_5xx_is_retried_with_backoff():
    calls = 0
    sleeps = []
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503 if calls == 1 else 200, json=[])
    with client(handler, sleeps=sleeps) as api:
        response = api.get("/events")
    assert response.status_code == 200
    assert calls == 2
    assert sleeps == [1]


def test_304_not_modified():
    def handler(request):
        assert request.headers["if-none-match"] == '"abc"'
        return httpx.Response(304, headers={"ETag": '"abc"'})
    with client(handler) as api:
        response = api.get("/events", etag='"abc"')
    assert response.status_code == 304
    assert response.data is None

