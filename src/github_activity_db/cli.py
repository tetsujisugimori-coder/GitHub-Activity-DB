from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import uvicorn

from .config import Settings, load_settings
from .db import connect
from .github_client import GitHubAPIError, GitHubClient
from .migrations import initialize_database
from .queries import ACTIVITY_UNION, counts, cutoff
from .sync_service import SyncResult, SyncService


def _client(settings: Settings) -> GitHubClient:
    if not settings.github_token:
        print("警告: GITHUB_TOKENが未設定です。公開APIは利用できますが、低いレート制限が適用されます。", file=sys.stderr)
    return GitHubClient(token=settings.github_token, api_version=settings.api_version,
                        user_agent=settings.user_agent, timeout=settings.request_timeout_seconds,
                        max_retries=settings.max_retries)


def _print_results(results: list[SyncResult]) -> None:
    for result in results:
        suffix = f" - {result.message}" if result.message else ""
        print(f"{result.scope}: {result.status} / pages={result.pages} seen={result.seen} saved={result.saved}{suffix}")


def init_db(settings: Settings) -> None:
    connection = connect(settings.db_path)
    try:
        initialize_database(connection, settings)
    finally:
        connection.close()
    print(f"DBを初期化しました: {settings.db_path}")


def sync_command(args: argparse.Namespace, settings: Settings) -> int:
    connection = connect(settings.db_path)
    initialize_database(connection, settings)
    try:
        with _client(settings) as client:
            service = SyncService(connection, settings, client)
            results: list[SyncResult] = []
            if args.person:
                results.extend(service.sync_people(args.person))
            elif args.repo:
                results.extend(service.sync_repositories(args.repo, since=args.since, until=args.until))
            else:
                if args.all or args.people:
                    results.extend(service.sync_people())
                if args.all or args.repos:
                    results.extend(service.sync_repositories(since=args.since, until=args.until))
            _print_results(results)
        return 0
    except (GitHubAPIError, ValueError) as exc:
        print(f"同期を安全に停止しました: {exc}", file=sys.stderr)
        return 2
    finally:
        connection.close()


def status(settings: Settings) -> None:
    connection = connect(settings.db_path)
    initialize_database(connection, settings)
    try:
        size = settings.db_path.stat().st_size if settings.db_path.exists() else 0
        print(f"DB: {settings.db_path}")
        print(f"サイズ: {size:,} bytes")
        print("登録件数: " + ", ".join(f"{key}={value}" for key, value in counts(connection).items()))
        print("\n人物ごとの最終同期:")
        for row in connection.execute("""SELECT a.login, MAX(s.last_success_at) last_sync FROM person_accounts a
            LEFT JOIN sync_state s ON s.scope_type='person' AND s.scope_key=a.login COLLATE NOCASE
            GROUP BY a.id ORDER BY a.login"""):
            print(f"  {row['login']}: {row['last_sync'] or '未同期'}")
        print("\nリポジトリごとの最終同期:")
        for row in connection.execute("""SELECT r.full_name, MAX(s.last_success_at) last_sync FROM repositories r
            JOIN watched_repositories w ON w.repository_id=r.id AND w.enabled=1
            LEFT JOIN sync_state s ON s.scope_type='repository' AND s.scope_key=r.full_name COLLATE NOCASE
            GROUP BY r.id ORDER BY r.full_name"""):
            print(f"  {row['full_name']}: {row['last_sync'] or '未同期'}")
        last = connection.execute("SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT 1").fetchone()
        print(f"\n前回同期: {last['status']} ({last['started_at']})" if last else "\n前回同期: なし")
        pending = connection.execute("SELECT scope_key, stage, cursor_url FROM sync_state WHERE status='incomplete'").fetchall()
        print(f"未完了ページネーション: {len(pending)}件")
        for row in pending:
            print(f"  {row['scope_key']} / {row['stage']}")
        rate = connection.execute("""SELECT rate_remaining, rate_limit, rate_reset FROM api_cache
            WHERE rate_remaining IS NOT NULL ORDER BY last_checked_at DESC LIMIT 1""").fetchone()
        print(f"API残り回数: {rate['rate_remaining']}/{rate['rate_limit']} / reset={rate['rate_reset']}" if rate else "API残り回数: 未取得")
        print("取得範囲: 人物イベントは直近30日・最大300件。通常リポジトリは初回90日、torvalds/linuxは30日。")
    finally:
        connection.close()


def report(settings: Settings, days: int) -> None:
    connection = connect(settings.db_path)
    initialize_database(connection, settings)
    try:
        print(f"直近{days}日の活動レポート")
        for row in connection.execute(
            f"""SELECT p.display_name, a.kind, COUNT(a.kind) count FROM people p
                LEFT JOIN ({ACTIVITY_UNION}) a ON a.person_id=p.id AND a.activity_at>=?
                GROUP BY p.id, a.kind ORDER BY p.display_name, count DESC""", (cutoff(days),)
        ):
            print(f"  {row['display_name']}: {row['kind'] or '活動なし'}={row['count']}")
    finally:
        connection.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m github_activity_db")
    parser.add_argument("--config", help="targets.tomlのパス")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db")
    sync = sub.add_parser("sync")
    mode = sync.add_mutually_exclusive_group(required=True)
    mode.add_argument("--all", action="store_true")
    mode.add_argument("--people", action="store_true")
    mode.add_argument("--repos", action="store_true")
    mode.add_argument("--person")
    mode.add_argument("--repo")
    sync.add_argument("--since", help="ISO 8601またはYYYY-MM-DD")
    sync.add_argument("--until", help="ISO 8601またはYYYY-MM-DD")
    sub.add_parser("status")
    report_parser = sub.add_parser("report")
    report_parser.add_argument("--days", type=int, default=30, choices=(7, 30, 90))
    sub.add_parser("serve")
    sub.add_parser("mcp")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(args.config)
    if args.command == "init-db":
        init_db(settings); return 0
    if args.command == "sync":
        return sync_command(args, settings)
    if args.command == "status":
        status(settings); return 0
    if args.command == "report":
        report(settings, args.days); return 0
    if args.command == "serve":
        uvicorn.run("github_activity_db.web:create_app", factory=True,
                    host=settings.host, port=settings.port, reload=False)
        return 0
    if args.command == "mcp":
        from .mcp_server import run_mcp_server

        run_mcp_server(settings)
        return 0
    return 1
