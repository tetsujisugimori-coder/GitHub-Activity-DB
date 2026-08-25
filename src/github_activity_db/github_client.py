from __future__ import annotations

import email.utils
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

import httpx


class GitHubAPIError(RuntimeError):
    """GitHub API request failed without exposing authentication data."""


class RateLimitError(GitHubAPIError):
    def __init__(self, message: str, *, reset_at: str | None = None,
                 retry_after: int | None = None, headers: dict[str, str] | None = None,
                 status_code: int = 403) -> None:
        super().__init__(message)
        self.reset_at = reset_at
        self.retry_after = retry_after
        self.headers = headers or {}
        self.status_code = status_code


@dataclass(frozen=True)
class APIResponse:
    status_code: int
    data: Any
    headers: dict[str, str]
    next_url: str | None
    url: str


def _utc_from_epoch(value: str | None) -> str | None:
    if not value or not value.isdigit():
        return None
    return datetime.fromtimestamp(int(value), UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _retry_after(value: str | None) -> int | None:
    if not value:
        return None
    if value.isdigit():
        return int(value)
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        return max(0, int((parsed - datetime.now(UTC)).total_seconds()))
    except (TypeError, ValueError):
        return None


def parse_next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        segments = [item.strip() for item in part.split(";")]
        if len(segments) > 1 and 'rel="next"' in segments[1:]:
            return segments[0].strip("<>")
    return None


class GitHubClient:
    base_url = "https://api.github.com"

    def __init__(self, *, token: str | None, api_version: str, user_agent: str,
                 timeout: float = 30, max_retries: int = 3,
                 transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": api_version,
            "User-Agent": user_agent,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(headers=headers, timeout=timeout, transport=transport)
        self.max_retries = max_retries
        self._sleep = sleep
        self.has_token = bool(token)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get(self, path_or_url: str, *, params: dict[str, Any] | None = None,
            etag: str | None = None) -> APIResponse:
        url = path_or_url if path_or_url.startswith("http") else f"{self.base_url}{path_or_url}"
        headers = {"If-None-Match": etag} if etag else None
        response: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.get(url, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt >= self.max_retries:
                    raise GitHubAPIError(f"GitHub APIへの接続に失敗しました: {type(exc).__name__}") from exc
                self._sleep(min(2 ** attempt, 8))
                continue
            if response.status_code >= 500 and attempt < self.max_retries:
                self._sleep(min(2 ** attempt, 8))
                continue
            break
        assert response is not None
        headers_out = {key.lower(): value for key, value in response.headers.items()}
        if response.status_code in (403, 429):
            remaining = headers_out.get("x-ratelimit-remaining")
            retry = _retry_after(headers_out.get("retry-after"))
            reset = _utc_from_epoch(headers_out.get("x-ratelimit-reset"))
            kind = "APIレート制限" if remaining == "0" else "二次レート制限またはアクセス拒否"
            raise RateLimitError(f"GitHub {kind} (HTTP {response.status_code})",
                                 reset_at=reset, retry_after=retry, headers=headers_out,
                                 status_code=response.status_code)
        if response.status_code == 304:
            data: Any = None
        else:
            try:
                data = response.json()
            except ValueError:
                data = None
        if response.status_code >= 400:
            message = data.get("message") if isinstance(data, dict) else "応答を解析できません"
            raise GitHubAPIError(f"GitHub API HTTP {response.status_code}: {message}")
        return APIResponse(
            status_code=response.status_code,
            data=data,
            headers=headers_out,
            next_url=parse_next_link(headers_out.get("link")),
            url=str(response.url),
        )
