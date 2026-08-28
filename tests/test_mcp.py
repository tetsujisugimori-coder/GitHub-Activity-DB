from __future__ import annotations

import asyncio
import sqlite3

from mcp import Client

from github_activity_db.mcp_server import build_server
from github_activity_db.mcp_tools import get_database_status, list_tracked_people, search_activities
from github_activity_db.db import connect_readonly
from github_activity_db.migrations import utc_now


def _insert_commit(db) -> None:
    person_id = db.execute("SELECT id FROM people WHERE display_name='Guido van Rossum'").fetchone()[0]
    repository_id = db.execute("SELECT id FROM repositories WHERE full_name='python/cpython'").fetchone()[0]
    db.execute(
        """INSERT INTO commits(repository_id, sha, person_id, author_login, author_name,
                   message, html_url, committed_at, fetched_at)
           VALUES (?, 'abc123', ?, 'gvanrossum', 'Guido van Rossum',
                   'Improve asyncio documentation', 'https://github.com/python/cpython/commit/abc123', ?, ?)""",
        (repository_id, person_id, utc_now(), utc_now()),
    )
    db.commit()


def test_read_only_mcp_queries(db, settings):
    _insert_commit(db)

    status = get_database_status(settings)
    people = list_tracked_people(settings, days=30)
    activities = search_activities(settings, query="asyncio", person="gvanrossum")

    assert status["access"] == "read-only"
    assert status["activities"] == 1
    assert people["people"][0]["activity_count"] == 1
    assert activities["count"] == 1
    assert activities["activities"][0]["title"] == "Improve asyncio documentation"


def test_mcp_protocol_lists_and_calls_tools(db, settings):
    _insert_commit(db)
    server = build_server(lambda: settings)

    async def exercise_server():
        async with Client(server, raise_exceptions=True) as client:
            listed = await client.list_tools()
            names = {tool.name for tool in listed.tools}
            assert names == {
                "get_database_status",
                "list_tracked_people",
                "search_activities",
            }
            result = await client.call_tool(
                "search_activities",
                {"query": "asyncio", "repository": "python/cpython"},
            )
            assert result.is_error is False
            assert result.structured_content["count"] == 1

    asyncio.run(exercise_server())


def test_mcp_rejects_unsafe_ranges(db, settings):
    try:
        search_activities(settings, days=0)
    except ValueError as exc:
        assert "1～365" in str(exc)
    else:
        raise AssertionError("invalid day range was accepted")


def test_mcp_database_connection_rejects_writes(db, settings):
    readonly = connect_readonly(settings.db_path)
    try:
        with readonly:
            readonly.execute(
                "UPDATE people SET display_name='changed' WHERE display_name='Guido van Rossum'"
            )
    except sqlite3.OperationalError as exc:
        assert "readonly" in str(exc).lower()
    else:
        raise AssertionError("read-only MCP connection accepted a write")
    finally:
        readonly.close()
