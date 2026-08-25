from __future__ import annotations

import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Settings, load_settings
from .db import connect
from .migrations import initialize_database
from .queries import activity_page, comparison, dashboard, estimated_topics, people_list


PACKAGE_DIR = Path(__file__).parent


def local_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    except ValueError:
        return value


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    connection = connect(settings.db_path)
    initialize_database(connection, settings)
    connection.close()
    app = FastAPI(title="GitHub Activity DB", docs_url=None, redoc_url=None)
    app.state.settings = settings
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    templates.env.filters["localtime"] = local_time
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")

    @contextmanager
    def database() -> Iterator[sqlite3.Connection]:
        db = connect(settings.db_path)
        try:
            yield db
        finally:
            db.close()

    def render(request: Request, name: str, context: dict) -> HTMLResponse:
        common = {"request": request, "token_missing": not bool(settings.github_token),
                  "limits_note": "人物イベントは直近30日・最大300件で、反映に数時間遅れる場合があります。30日より前の完全な履歴は取得できません。"}
        return templates.TemplateResponse(request=request, name=name, context={**common, **context})

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        with database() as db:
            return render(request, "dashboard.html", {"data": dashboard(db)})

    @app.get("/people", response_class=HTMLResponse)
    def people(request: Request) -> HTMLResponse:
        with database() as db:
            return render(request, "people.html", {"people": people_list(db)})

    @app.get("/people/{person_id}", response_class=HTMLResponse)
    def person_detail(request: Request, person_id: int, days: int = Query(30, ge=1, le=365),
                      kind: str | None = None, repository_id: int | None = None,
                      page: int = Query(1, ge=1)) -> HTMLResponse:
        with database() as db:
            person = db.execute("""SELECT p.*, GROUP_CONCAT(a.login, ', ') logins,
                MIN(a.profile_url) profile_url FROM people p LEFT JOIN person_accounts a ON a.person_id=p.id
                WHERE p.id=? GROUP BY p.id""", (person_id,)).fetchone()
            if not person:
                raise HTTPException(404, "人物が見つかりません")
            rows, total = activity_page(db, person_id=person_id, kind=kind,
                                        repository_filter=repository_id, days=days, page=page)
            repos = db.execute("""SELECT DISTINCT r.id, r.full_name FROM person_repository_relations pr
                JOIN repositories r ON r.id=pr.repository_id WHERE pr.person_id=? ORDER BY r.full_name""",
                (person_id,)).fetchall()
            coverage = db.execute("""SELECT MIN(range_start) range_start, MAX(range_end) range_end,
                MAX(last_success_at) last_sync FROM sync_state s JOIN person_accounts a
                ON a.login=s.scope_key COLLATE NOCASE WHERE s.scope_type='person' AND a.person_id=?""",
                (person_id,)).fetchone()
            return render(request, "person_detail.html", {
                "person": person, "activities": rows, "total": total, "pages": max(1, math.ceil(total / 30)),
                "page": page, "days": days, "kind": kind or "", "repository_id": repository_id,
                "repositories": repos, "topics": estimated_topics(rows), "coverage": coverage,
            })

    @app.get("/repositories", response_class=HTMLResponse)
    def repositories(request: Request) -> HTMLResponse:
        with database() as db:
            rows = db.execute("""SELECT r.*, w.enabled watched,
                (SELECT COUNT(*) FROM commits c WHERE c.repository_id=r.id) commit_count,
                (SELECT MAX(last_success_at) FROM sync_state s WHERE s.scope_type='repository'
                    AND s.scope_key=r.full_name COLLATE NOCASE) last_sync
                FROM repositories r LEFT JOIN watched_repositories w ON w.repository_id=r.id
                ORDER BY watched DESC, r.full_name""").fetchall()
            return render(request, "repositories.html", {"repositories": rows})

    @app.get("/repositories/{repository_id}", response_class=HTMLResponse)
    def repository_detail(request: Request, repository_id: int,
                          days: int = Query(30, ge=1, le=365), kind: str | None = None,
                          page: int = Query(1, ge=1)) -> HTMLResponse:
        with database() as db:
            repository = db.execute("SELECT * FROM repositories WHERE id=?", (repository_id,)).fetchone()
            if not repository:
                raise HTTPException(404, "リポジトリが見つかりません")
            rows, total = activity_page(db, repository_id=repository_id, kind=kind, days=days, page=page)
            contributors = db.execute("""SELECT p.id, p.display_name, COUNT(*) activity_count
                FROM person_repository_relations pr JOIN people p ON p.id=pr.person_id
                WHERE pr.repository_id=? GROUP BY p.id ORDER BY activity_count DESC""",
                (repository_id,)).fetchall()
            releases = db.execute("SELECT * FROM releases WHERE repository_id=? ORDER BY published_at DESC LIMIT 20",
                                  (repository_id,)).fetchall()
            return render(request, "repository_detail.html", {
                "repository": repository, "activities": rows, "total": total,
                "pages": max(1, math.ceil(total / 30)), "page": page, "days": days,
                "kind": kind or "", "contributors": contributors, "releases": releases,
            })

    @app.get("/compare", response_class=HTMLResponse)
    def compare(request: Request, days: int = Query(30),
                person: list[int] = Query(default=[]), repo: list[int] = Query(default=[])) -> HTMLResponse:
        if days not in (7, 30, 90):
            days = 30
        with database() as db:
            people_options = db.execute("SELECT id, display_name FROM people ORDER BY display_name").fetchall()
            repo_options = db.execute("SELECT id, full_name FROM repositories ORDER BY full_name").fetchall()
            data = comparison(db, days, person, repo)
            return render(request, "compare.html", {"data": data, "days": days,
                          "people_options": people_options, "repo_options": repo_options,
                          "selected_people": person, "selected_repos": repo})

    return app
