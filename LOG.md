# 作業ログ

## 2026-08-25 — 初版実装とGit登録

### 実装

- Python、FastAPI、Jinja2、SQLiteによるローカルWebアプリ「GitHub Activity DB」を新規実装した。
- 初期人物5名、Microsoft組織、監視対象リポジトリ9件を `config/targets.toml` から登録できるようにした。
- 人物イベント、コミット、Issue、Pull Request、Issueコメント、レビュー、レビューコメント、リリースの保存処理を実装した。
- SQLiteのマイグレーション、WAL、外部キー、一意制約、検索用インデックスを実装した。
- ETag、304、`X-Poll-Interval`、Linkページネーション、5xx再試行、レート制限、5分の重複取得窓、同期再開カーソルを実装した。
- 人物イベントから発見したリポジトリは「発見済み」として保存し、自動では監視対象にしない構成とした。
- ダッシュボード、人物一覧・詳細、リポジトリ一覧・詳細、比較画面、推定テーマ表示を実装した。
- CLIの `init-db`、`sync`、`status`、`report`、`serve` を実装した。
- 日本語README、`.env.example`、`.gitignore`、PowerShell向け操作手順を整備した。

### 検証結果

- モック自動テスト: `19 passed`、失敗なし。
- `pip check`: `No broken requirements found`。
- FastAPI TestClientで主要ページのHTTP 200を確認した。
- Uvicornを `127.0.0.1:8000` で実際に起動し、ルートページのHTTP 200を確認した。
- FastAPI TestClient由来の非推奨警告が1件あるが、テスト失敗ではない。

### GitHub API同期

- `gvanrossum` の人物イベント同期を実行し、次の結果を確認した。

```text
person:gvanrossum:events: success / pages=2 seen=100 saved=100
```

- この同期時点ではCLIに `GITHUB_TOKENが未設定` の警告が表示されており、公開APIの未認証枠で取得された。
- その後、利用者がPowerShellのプロセス環境変数 `GITHUB_TOKEN` を設定した。
- トークン文字列そのものは、ソース、DB、ログ、Gitへ保存していない。
- トークン認証後の再同期結果は、まだ作業ログ上では未確認。

### Git

- 空だった作業ディレクトリを `main` ブランチのGitリポジトリとして初期化した。
- `.venv`、`.env`、SQLite DB、ログ、キャッシュ、テスト一時ファイルを `.gitignore` へ登録した。
- ステージ済みファイルに実トークン形式の文字列がないことを確認した。
- 初回コミットを作成した。

初版実装をルートコミットとして記録した。

### GitHubへのプッシュ

- GitHub CLIは `tetsujisugimori-coder` アカウントでログイン済み。
- 次の同名リポジトリがGitHub上に存在し、空の公開リポジトリであることを確認した。

```text
https://github.com/tetsujisugimori-coder/GitHub-Activity-DB
visibility: PUBLIC
```

- 公開リポジトリへのソース公開について利用者の明示承認を得た。
- 上記URLを `origin` として登録し、`main` ブランチを公開プッシュした。
- GitHub上の可視性が `PUBLIC`、デフォルトブランチが `main` であることを確認した。
- ローカルの `main` は `origin/main` を追跡するよう設定した。
- 初回実装、作業ログ追加、公開プッシュ記録の順でコミットした。

### 次回候補

1. トークン認証済み状態で `sync --person gvanrossum` を再実行し、警告が出ないことを確認する。
2. 必要に応じてWindowsタスクスケジューラで定期同期を設定する。
3. 今後の変更もテスト後にコミットし、`origin/main` へプッシュする。
