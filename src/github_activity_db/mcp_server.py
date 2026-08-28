from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .config import Settings, load_settings
from . import mcp_tools


def build_server(settings_loader: Callable[[], Settings] = load_settings) -> MCPServer:
    server = MCPServer(
        "github-activity-db",
        instructions=(
            "ローカルSQLiteに保存済みのGitHub公開活動を読み取るサーバーです。"
            "同期や書き込みは行いません。検索前に必要ならDB状態を確認してください。"
        ),
    )

    @server.tool()
    def get_database_status() -> dict[str, Any]:
        """保存済みDBの件数、最終同期、未完了・失敗状況を確認します。"""
        try:
            return mcp_tools.get_database_status(settings_loader())
        except (OSError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool()
    def list_tracked_people(days: int = 30) -> dict[str, Any]:
        """追跡対象の人物と、指定期間内の活動件数を一覧表示します。"""
        try:
            return mcp_tools.list_tracked_people(settings_loader(), days)
        except (OSError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool()
    def search_activities(
        query: str = "",
        person: str = "",
        repository: str = "",
        kind: str = "",
        days: int = 30,
        limit: int = 20,
    ) -> dict[str, Any]:
        """保存済み活動を語句・人物・リポジトリ・活動種別で絞り込みます。"""
        try:
            return mcp_tools.search_activities(
                settings_loader(), query=query, person=person, repository=repository,
                kind=kind, days=days, limit=limit,
            )
        except (OSError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    return server


mcp = build_server()


def run_mcp_server(settings: Settings | None = None) -> None:
    server = build_server(lambda: settings) if settings is not None else mcp
    server.run(transport="stdio")


def main() -> None:
    run_mcp_server()
