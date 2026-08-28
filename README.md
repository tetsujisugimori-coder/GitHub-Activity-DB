# GitHub Activity DB

GitHub上の人物・組織・リポジトリの**公開活動**を定期的に取得し、ローカルのSQLiteへ蓄積して検索・比較するWebアプリです。外部公開はせず、FastAPIを `127.0.0.1` だけで起動します。

単なるコミット数ではなく、イベント、コミット、Issue、Pull Request、レビュー、コメント、リリースを分けて確認できます。「推定テーマ」は、保存済みのタイトル・メッセージ・コメント冒頭・リポジトリ名から頻出語を機械的に集計したもので、事実の要約ではありません。

## 初期追跡対象

人物は、杉森哲二 (`tetsujisugimori-coder`)、Guido van Rossum (`gvanrossum`)、Fabrice Bellard (`bellard`)、Anders Hejlsberg (`ahejlsberg`)、Linus Torvalds (`torvalds`) です。人物とGitHubアカウントは別テーブルなので、本人性を確認できた場合だけ同一人物へ別アカウントを追加できます。

監視対象は次の9リポジトリです。

- `microsoft/vscode`
- `microsoft/TypeScript`
- `microsoft/PowerToys`
- `bellard/quickjs`
- `bellard/mquickjs`
- `torvalds/linux`
- `python/cpython`
- `microsoft/typeagent-py`
- `gvanrossum/gvanrossum.github.io`

`microsoft` 組織も初期登録します。人物イベントから見つかった別リポジトリは「発見済み」としてDBへ記録しますが、自動では監視対象にしません。

## Guidoの活動を追う仕組み

`gvanrossum` の公開人物イベントを定期取得し、イベントに含まれる活動先を `person_repository_relations` と `repositories` へ記録します。したがって活動先を初期3リポジトリへ固定しません。`python/cpython`、`microsoft/typeagent-py`、`gvanrossum/gvanrossum.github.io` は補助的な監視対象です。新しく発見したリポジトリを詳しく継続収集するには、確認後に `config/targets.toml` へ明示的に追加してください。

## GitHub API上の重要な制約

- 人物イベントAPIで取得できるのは直近30日、最大300イベントです。30日より前の完全な人物活動履歴は復元できません。
- 人物イベントには数時間程度の反映遅延があり得ます。画面にもこの注意を表示します。
- 初回同期は通常リポジトリで直近90日、`torvalds/linux` で直近30日に限定します。
- 1回のリポジトリ同期は、メタデータ取得を含め標準10ページ、1ページ100件までです。全履歴取得機能はありません。
- 各ページをSQLiteへ保存して確定してから次へ進みます。上限時はカーソルを保存し、次回に続きから再開します。
- 通常の増分同期は、直前の成功時刻から5分重ねて取得し、一意制約とUPSERTで重複を除きます。
- ETag / `If-None-Match`、`304 Not Modified`、`X-Poll-Interval`、`Link` ページネーションを扱います。
- 一次レート制限と `403` / `429` / 二次レート制限を通常の通信失敗と区別し、残数・リセット時刻・`Retry-After` を状態へ保存します。
- 初版は逐次実行です。巨大リポジトリに対する高並列・全コメント走査はしません。

Issue APIに現れるPull RequestはIssueとして保存しないため、両者を二重計上しません。レビューは更新期間内のPull Requestに限定し、コメントも期間とページ上限を適用します。

## 必要環境

- Windows 10/11
- PowerShell
- Python 3.12以上

## セットアップ

PowerShellでリポジトリのディレクトリへ移動し、仮想環境を作成します。

```powershell
cd C:\Users\tetsu\Documents\Codex\Github-Activity-DB
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
```

PowerShellの実行ポリシーで有効化できない場合は、以降の `python` を `.\.venv\Scripts\python.exe` に置き換えて実行できます。

### GitHubトークン

トークンなしでも公開APIを試せますが、IP単位の低いレート制限になります。GitHubの Settings → Developer settings → Personal access tokens でトークンを作成し、対象を公開リポジトリの読み取りだけに限定してください。書き込み権限、管理権限、秘密リポジトリへの権限は不要です。

現在のPowerShellセッションだけに設定する例です。

```powershell
$env:GITHUB_TOKEN = "github_pat_..."
```

アプリは `GITHUB_TOKEN` を環境変数からだけ読みます。ソース、SQLite、生JSON、ログには保存しません。本物の値を `.env` や `.env.example` に書かないでください。

## 基本操作

```powershell
python -m github_activity_db init-db
python -m github_activity_db sync --all
python -m github_activity_db sync --people
python -m github_activity_db sync --repos
python -m github_activity_db sync --person gvanrossum
python -m github_activity_db sync --repo python/cpython
python -m github_activity_db sync --repo torvalds/linux --since 2026-08-01
python -m github_activity_db sync --repo python/cpython --since 2026-05-01 --until 2026-06-01
python -m github_activity_db status
python -m github_activity_db report --days 30
python -m github_activity_db serve
python -m github_activity_db mcp
```

Web画面は <http://127.0.0.1:8000> で開きます。外部インターフェースへはバインドしません。

## 試験用MCPサーバー

MCPサーバーは保存済みSQLiteを読み取り専用で開き、次の3ツールをAIクライアントへ公開します。GitHub APIへの同期、任意SQL、DB更新は公開しません。

- `get_database_status`: DB件数、最終同期、未完了・失敗状況
- `list_tracked_people`: 追跡人物と期間内の活動件数
- `search_activities`: 語句・人物・リポジトリ・活動種別による検索

先に通常のCLIでDBを初期化・同期してから起動してください。stdio方式なので、単独実行時に画面が止まって見えるのは、MCPクライアントからの入力を待っている正常な状態です。

```powershell
python -m github_activity_db init-db
python -m github_activity_db sync --person gvanrossum
python -m github_activity_db mcp
```

MCPクライアントには、プロジェクトの仮想環境にあるPythonとモジュールを指定します。設定形式はクライアントごとに異なりますが、基本となる起動情報は次のとおりです。

```json
{
  "command": "C:\\Users\\tetsu\\Documents\\Codex\\Github-Activity-DB\\.venv\\Scripts\\python.exe",
  "args": ["-m", "github_activity_db", "--config", "C:\\Users\\tetsu\\Documents\\Codex\\Github-Activity-DB\\config\\targets.toml", "mcp"]
}
```

公式MCP Inspectorでローカル接続を確認する場合は、Node.js 22.19以上の環境で次を実行します。

```powershell
npx @modelcontextprotocol/inspector .\.venv\Scripts\python.exe -m github_activity_db mcp
```

`status` はDBパスとサイズ、テーブル件数、人物・リポジトリ別最終同期、前回同期、未完了カーソル、API残数とリセット時刻、取得範囲制約を表示します。`--since` / `--until` は、利用者が明示した場合だけ過去範囲を追加取得するための指定です。

## 設定変更

監視対象、人物とアカウント、初回取得日数、ページ数、APIバージョン、DBパス、待受ポートは [`config/targets.toml`](config/targets.toml) で管理します。コードへ対象を追加する必要はありません。

発見済みリポジトリを監視へ昇格する例です。

```toml
[[repositories]]
full_name = "owner/repository"
watched = true
```

設定ファイルを別の場所に置く場合は、全コマンドのサブコマンドより前に `--config` を指定します。

```powershell
python -m github_activity_db --config .\config\targets.toml status
```

## 同期失敗と再開

ページ保存後だけ再開カーソルを進めます。通信失敗時は最後の成功日時を進めず、エラーを `sync_state` と `sync_runs` へ記録します。同じコマンドを再実行すれば、未完了ページがある収集段階は保存済みカーソルから再開します。レート制限時は `status` のリセット時刻または `Retry-After` を確認してから再実行してください。

イベントや各エンティティにはGitHub IDまたは `(repository_id, sha/number)` の一意制約があるため、中断後に同じ範囲を再取得しても重複しません。

## DBのバックアップ

同期処理とWebサーバーを停止してから、DBファイルをコピーしてください。既定のDBは `data/github_activity.db` です。

```powershell
New-Item -ItemType Directory -Force .\backup
Copy-Item .\data\github_activity.db .\backup\github_activity-$(Get-Date -Format yyyyMMdd-HHmmss).db
```

WAL利用中のファイルだけを実行中にコピーすると整合しない可能性があるため、停止後のコピーを推奨します。`data/*.db` は `.gitignore` 対象であり、Gitへコミットしないでください。

## テスト

通常のテストはGitHubへ接続せず、HTTP応答をモックします。

```powershell
python -m pytest
```

DB初期化、マイグレーション再実行、一意制約、ページ上限と再開、ETag/304、ポーリング間隔、レート制限、5xx再試行、失敗時の成功位置、5分重複窓、リポジトリ発見、Guidoの紐付け、Issue/PR分離、UTC、HTMLエスケープ、Webページングを検証します。

実APIの確認は通常テストへ含めていません。`GITHUB_TOKEN` を設定したうえで、明示的に `python -m github_activity_db sync --person gvanrossum` などを実行してください。

## データベース構成

- 人物: `people`, `person_accounts`
- 対象: `organizations`, `repositories`, `watched_repositories`, `person_repository_relations`
- 活動: `events`, `commits`, `issues`, `pull_requests`, `issue_comments`, `pull_request_reviews`, `review_comments`, `releases`
- 運用: `sync_state`, `sync_runs`, `api_cache`, `schema_migrations`

日時はUTC ISO 8601で保存し、画面表示時だけPCのローカル時刻へ変換します。SQLiteは外部キー制約とWALを有効化し、一覧で使う人物・リポジトリ・種別・日時へインデックスを設定しています。APIの生JSONは通常表示列と分け、認証ヘッダーは保存しません。

## 現時点で完全には取得できない情報

GitHub APIの保持期間より前の人物イベント、削除済み・非公開化された内容、非公開リポジトリ、APIが公開しない情報、同期開始前に30日窓から消えたイベントは取得できません。巨大リポジトリの全履歴も意図的に対象外です。長期的な変化を確認するには、早めに初回同期し、その後定期的に同期してローカルDBへ蓄積してください。
