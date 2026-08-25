from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PersonTarget:
    display_name: str
    category: str
    accounts: tuple[str, ...]


@dataclass(frozen=True)
class OrganizationTarget:
    login: str
    display_name: str


@dataclass(frozen=True)
class RepositoryTarget:
    full_name: str
    watched: bool = True
    initial_days: int | None = None


@dataclass(frozen=True)
class Settings:
    project_root: Path
    db_path: Path
    api_version: str = "2022-11-28"
    user_agent: str = "GitHub-Activity-DB/0.1"
    request_timeout_seconds: float = 30.0
    max_retries: int = 3
    max_pages: int = 10
    per_page: int = 100
    people_initial_days: int = 30
    repository_initial_days: int = 90
    linux_initial_days: int = 30
    overlap_minutes: int = 5
    host: str = "127.0.0.1"
    port: int = 8000
    people: tuple[PersonTarget, ...] = field(default_factory=tuple)
    organizations: tuple[OrganizationTarget, ...] = field(default_factory=tuple)
    repositories: tuple[RepositoryTarget, ...] = field(default_factory=tuple)

    @property
    def github_token(self) -> str | None:
        return os.environ.get("GITHUB_TOKEN") or None


def _find_project_root(config_path: Path) -> Path:
    return config_path.resolve().parent.parent


def load_settings(path: str | Path | None = None) -> Settings:
    config_path = Path(path or os.environ.get("GITHUB_ACTIVITY_CONFIG", "config/targets.toml"))
    if not config_path.is_absolute():
        config_path = (Path.cwd() / config_path).resolve()
    with config_path.open("rb") as handle:
        raw: dict[str, Any] = tomllib.load(handle)
    app = raw.get("app", {})
    project_root = _find_project_root(config_path)
    db_path = Path(app.get("db_path", "data/github_activity.db"))
    if not db_path.is_absolute():
        db_path = project_root / db_path
    people = tuple(
        PersonTarget(p["display_name"], p["category"], tuple(p.get("accounts", ())))
        for p in raw.get("people", ())
    )
    organizations = tuple(
        OrganizationTarget(o["login"], o.get("display_name", o["login"]))
        for o in raw.get("organizations", ())
    )
    repositories = tuple(
        RepositoryTarget(r["full_name"], bool(r.get("watched", True)), r.get("initial_days"))
        for r in raw.get("repositories", ())
    )
    keys = {
        "api_version", "user_agent", "request_timeout_seconds", "max_retries",
        "max_pages", "per_page", "people_initial_days", "repository_initial_days",
        "linux_initial_days", "overlap_minutes", "host", "port",
    }
    values = {key: app[key] for key in keys if key in app}
    return Settings(project_root, db_path, people=people, organizations=organizations,
                    repositories=repositories, **values)

