# シークレット保存方式（SecretStore）

2026-07 実装。ユーザー向けの説明は README の「7.2. Secret Storage / シークレットの保存」を正とする。
このメモは実装者向けに構成要素と決定事項だけを記録する。

## 保存モデル

- シークレット = LLM プロバイダ API キー（`models/<provider>/default.yml` の `api_key_env`）と、
  person シークレット（`Person.SECRET_ENV_SUFFIXES` = `GITHUB_ACCESS_TOKEN` / `GITHUB_PRIVATE_KEY` /
  `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN`）。
  `*_GITHUB_INSTALLATION_ID` などの ID・パス類は非シークレットとして `.env` に残す。
- `<ID>_GITHUB_PRIVATE_KEY` は GitHub App の PEM **内容**。`secrets migrate` と setup が
  `*_GITHUB_PRIVATE_KEY_PATH` のファイルを読んでストアへコピーし、パスのエントリも `.env` から
  取り除く（ファイル削除だけユーザーに委ねる）。content 未保存（パスが読めない等）の場合のみ
  パスが `.env` に残り、従来どおりファイルから読む。desktop の member フォームは
  `has_github_private_key`（snapshot）を見て、保存済みならパス空欄を許容する。
  verify もパス未設定時は content の有無で判定する。
- バックエンドは 2 種類（`guildbotics/utils/secret_store.py`）:
  - `keyring`: OS キーチェーン（`keyring` ライブラリ）。ワークスペースには
    `.guildbotics/config/secrets.yml`（`backend` / `store_id` / `keys`。値は持たない）だけを置く。
    キーチェーンのサービス名は `GuildBotics/<store_id>` で、ワークスペースを移動しても同一マシン内では追従する。
  - `env-file`: 従来のワークスペース `.env`。既存ワークスペースの既定であり、ヘッドレス環境の正式経路。
- バックエンド決定: `GUILDBOTICS_SECRETS_BACKEND` 環境変数 > `secrets.yml` の `backend` > レガシー（env-file）。
  新規プロジェクトセットアップ（`SimpleProjectSetupService.write_project`）だけが
  `create_default=True` で keyring を既定選択し、`secrets.yml` を作成して固定する。
  既存ワークスペースの切替は `guildbotics secrets migrate` のみが行う。

## 値の解決

実行時の読み出しは従来どおり `os.environ` 経由（`Person.get_secret`、agno モデル、AI CLI ツール子プロセス）。
プロセス起動時に以下の優先順位で環境へ注入する:

1. 実環境変数（そのまま勝つ）
2. OS キーチェーン（`env_loader.read_workspace_secrets`）
3. `.env`（`env_loader.load_guildbotics_env` / app_api `_load_workspace_env`）

app_api はワークスペース切替時に注入済みキーを除去する既存機構（`_loaded_dotenv_keys`）へ
キーチェーン由来のキーも合流させる。上書きも自分が注入したキーに限定し、親プロセスから
継承した実環境変数は保持する（優先順位の一貫性のため）。

例外として `*_GITHUB_PRIVATE_KEY`（`secret_store.is_environment_secret` が False）は環境変数へ
注入しない。AI CLI ツール子プロセスは `os.environ` を丸ごと継承するため、App 秘密鍵を環境に
載せると全エージェントプロセスへ漏れる。消費側（`github_utils.get_person_private_key_pem`）が
`env_loader.workspace_secret_store()` 経由で使用時にだけ読み出し、無ければ従来の
`*_GITHUB_PRIVATE_KEY_PATH` ファイルへフォールバックする。

## 書き込み経路

- `setup_service` は keyring バックエンドのとき、provider キーと person トークンをストアへ書き、
  `.env` には非シークレットだけを書く。person の rename / delete はストア側のキーも移動・削除する。
- GuildBotics が書く `.env` は常に `0600`。`write_env_text` は `mkstemp`（作成時点で 0600）+
  `os.replace` の atomic 書き込みで、他ユーザーから読める瞬間を作らない。
- dotenv 直列化は `format_env_line` に集約。改行・引用符等を含む値（PEM 等）はダブルクォート+
  エスケープで書き、`dotenv_values` で完全往復する。
- 引越しは `guildbotics secrets export` / `import`（dotenv 形式、エクスポートは一時ファイル前提）。

## テスト方針

- `tests/conftest.py` の autouse fixture が `GUILDBOTICS_SECRETS_BACKEND=env-file` を強制し、
  開発機の実キーチェーンに触れない。keyring 経路は `fake_keyring` fixture（インメモリバックエンド +
  強制解除）で検証する。desktop e2e harness（`start-stack.mjs`）も `env-file` を強制する。
