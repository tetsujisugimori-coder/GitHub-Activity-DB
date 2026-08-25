from __future__ import annotations

from fastapi.testclient import TestClient

from github_activity_db.migrations import utc_now
from github_activity_db.web import create_app, local_time


def test_local_time_conversion():
    rendered = local_time("2026-08-25T00:00:00Z")
    assert rendered.startswith("2026-08-25")
    assert rendered != "2026-08-25T00:00:00Z"


def test_html_is_escaped_and_activity_is_paginated(db, settings):
    person_id = db.execute("SELECT id FROM people").fetchone()[0]
    account_id = db.execute("SELECT id FROM person_accounts").fetchone()[0]
    repo_id = db.execute("SELECT id FROM repositories").fetchone()[0]
    now = utc_now()
    for number in range(31):
        db.execute("""INSERT INTO events(github_event_id, person_id, account_id, repository_id,
            event_type, title, body, occurred_at, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (str(number), person_id, account_id, repo_id, "IssueCommentEvent", f"event {number}",
             "<script>alert('x')</script>", now, now))
    db.commit()
    client = TestClient(create_app(settings))
    first = client.get(f"/people/{person_id}?days=30")
    second = client.get(f"/people/{person_id}?days=30&page=2")
    assert first.status_code == second.status_code == 200
    assert "&lt;script&gt;" in first.text
    assert "<script>alert" not in first.text
    assert "1 / 2" in first.text
    assert "2 / 2" in second.text


def test_all_primary_pages_render(db, settings):
    client = TestClient(create_app(settings))
    for path in ("/", "/people", "/repositories", "/compare"):
        response = client.get(path)
        assert response.status_code == 200
        assert "GitHub Activity DB" in response.text
    assert client.get("/compare?person=1&repo=1&days=30").status_code == 200
