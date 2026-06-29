# AGENTS.md

このファイルは、このリポジトリで作業する AI/自動化エージェント向けの実装ベースの作業ガイドです。

## 最重要ルール

- ソースコードを正とする（README / `docs/*.md` は参考情報）
- `.gitignore` 対象のファイル・ディレクトリは参照しない
- 挙動変更を行った場合は、関連ドキュメントも必要に応じて更新する
- コードの修正に際しては「最小限の変更量」ではなく、「変更後のコード量が最小になること」を最優先事項とし、場当たり的対応ではなくあるべき姿の美しいコードとなることを心がける
- 意味判定・分類・採用可否のような自然言語理解が必要な処理を、キーワード列挙や場当たり的な文字列マッチで実装しない。既存の LLM 判定基盤（例: `guildbotics/intelligences/functions.py` と `guildbotics/templates/commands/functions/*`）を優先し、必要なら汎用的な判定関数を追加する。

このリポジトリの `.gitignore` では、少なくとも以下を無視対象にしています（抜粋）:

- `tmp/`
- `memo/`
- `.guildbotics/`
- `dist/`
- `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.coverage*`, `coverage.xml`
- `guildbotics/_version.py`

## プロジェクト概要（実装ベース）

- 言語: Python 3.12+
- パッケージ: `guildbotics`
- CLI: Click (`guildbotics.cli:main`)
- 主用途: AI エージェント協調のための CLI / スケジューラ / カスタムコマンド実行基盤

エントリポイント:

- インストール後 CLI: `guildbotics` (`pyproject.toml` の `project.scripts`)
- モジュール側: `guildbotics/cli/__init__.py`

## 重要な実装ポイント

## 1. CLI コマンド

`guildbotics/cli/__init__.py` に主要コマンドがあります。

- `guildbotics start`
- `guildbotics run`
- `guildbotics stop`
- `guildbotics kill`
- `guildbotics version`
- `guildbotics workspace use/current/status`
- `guildbotics member ...`

補足:

- `run` は `--person` または `<command>@<person_id>` でメンバー指定可能
- `workspace use` は active workspace を `~/.guildbotics/data/active-workspace.json` に保存する
- `member` group は `--workspace <dir>` を受け取り、CLI agent / skill 経由の member capability の入口になる
- `start` は PID ファイルを `~/.guildbotics/data/run/scheduler.pid` に保存
- `stop` / `kill` は上記 PID を使ってプロセス停止

## 2. コマンド実行基盤（最重要）

中心実装:

- `guildbotics/drivers/command_runner.py`
- `guildbotics/commands/*`

流れ:

1. `run_command()` が対象メンバーを解決
2. `CommandRunner` がメインコマンドを解決
3. `CommandSpecFactory` が `CommandSpec` を構築
4. 子コマンド（`commands:`）を先に実行
5. 結果を `Context.shared_state` と `Context.pipe` に反映

`Context.pipe` はコマンド間の標準入力/標準出力的な受け渡し文字列として使われます。

### サポートされるコマンド種別（`guildbotics/commands/registry.py`）

- `.md` (`MarkdownCommand`)
- `.py` (`PythonCommand`)
- `.sh` (`ShellScriptCommand`)
- `.yml` / `.yaml` (`YamlCommand`, 定義用)

インライン専用コマンド:

- `print`
- `to_html`
- `to_pdf`

## 3. 設定ファイル解決（実装依存）

設定ファイル解決は `guildbotics/utils/fileio.py` が担当します。

重要事項:

- 優先順は「一次設定 (`GUILDBOTICS_CONFIG_DIR` or cwd の `.guildbotics/config`) → パッケージテンプレート (`guildbotics/templates`)」の2段（`fileio._get_config_path`）。`~/.guildbotics/config` のような home 設定階層は無い（`fileio.py` の `Path.home()` 参照は `~/.guildbotics/data` のデータ用のみ）
- `guildbotics member ...` は `guildbotics/utils/workspace_state.py` も使う。`--workspace` があればその workspace を最優先し、明示的な `GUILDBOTICS_CONFIG_DIR` または cwd の `.guildbotics/config` が無い場合だけ active workspace を適用する
- desktop runtime は workspace 選択時に active workspace を保存し、workspace の `.guildbotics/config` と `.env` から `GUILDBOTICS_CONFIG_DIR` / `GUILDBOTICS_ENV_FILE` を設定する
- ローカライズ対応ファイルは `.<lang>` → `.en` → 素のファイル名の順で探索
- メンバー別コマンドは `team/members/<person_id>/...` を優先し、なければ共通設定へフォールバック

## 4. スケジューラ

中心実装:

- `guildbotics/drivers/task_scheduler.py`
- `guildbotics/drivers/utils.py`

実装上のポイント:

- アクティブなメンバーごとにスレッドを起動
- 各スレッド内で asyncio イベントループを使用
- 定期コマンドと routine コマンドを実行
- 連続エラー数でワーカーループ停止（`consecutive_error_limit`）

既定 routine コマンド（Simple edition）:

- `workflows/ticket_driven_workflow` (`SimpleEdition.get_default_routines()`)

## 5. Edition 切替

`guildbotics/editions/__init__.py#get_edition()` で `GUILDBOTICS_EDITION` を見て Edition を切り替えます。`Edition` は `get_context()` と `get_default_routines()` のみを提供する実行時の抽象です。

- 既定: `simple`
- 実体: `guildbotics/editions/simple/simple_edition.py`
- 設定書き込みロジック（GUI が再利用）: `guildbotics/editions/simple/setup_service.py`

## 開発時の基本コマンド（CI 準拠）

CI (`.github/workflows/ci.yml`) で使われている手順:

```bash
uv sync --extra test --extra dev
uv run --no-sync ruff format --check guildbotics
uv run --no-sync ruff check guildbotics
uv run --no-sync mypy guildbotics
uv run --no-sync pylint guildbotics
uv run --no-sync python -m pytest tests/ --cov=guildbotics --cov-report=xml
```

必要に応じて:

```bash
uv sync --extra dev
```

desktop frontend (`desktop/`) の品質確認:

```bash
cd desktop
npm ci
npm run format:check
npm run lint
npm run typecheck
npm run duplicates
npm run test
```

まとめて実行する場合:

```bash
cd desktop
npm run quality
```

Playwright E2E（実ブラウザ + 実 Local API backend。`npm run quality` / 通常 push CI には含めない）:

```bash
cd desktop
npm ci
npm run e2e:install   # 初回のみ: chromium を取得
npm run e2e           # desktop/e2e/*.spec.ts を headless chromium で実行
```

- harness（`desktop/e2e/start-stack.mjs`）が backend を `uv run python -m guildbotics.app_api` で temp workspace 起動するため、事前にリポジトリルートで `uv sync --extra test --extra dev` 済みであること。
- 詳細・journey 一覧は `desktop/README.md` の「テスト」節と `docs/test_gap_analysis.ja.md` を参照。

desktop packaging / Tauri 変更時の確認:

- `scripts/desktop-build-backend.sh` は PyInstaller で `guildbotics-app-api` と `guildbotics-cli` の 2 本を build し、`desktop/src-tauri/binaries/*-<target>` に配置する
- `scripts/desktop-dev-tauri.sh` は `scripts/desktop-write-dev-binaries.sh` で Local API / CLI の開発用 wrapper を生成する
- Rust/Tauri 側を変更したら `cargo fmt --check`、`cargo check`、必要に応じて `cargo test` を `desktop/src-tauri` で実行する
- sidecar / packaging script を変更したら `bash -n scripts/desktop-build-backend.sh scripts/desktop-build-frontend.sh scripts/desktop-dev-tauri.sh scripts/desktop-write-dev-binaries.sh` と、可能なら `scripts/desktop-build-backend.sh` による smoke を行う

エージェント作業時の品質確認:

- 確認コマンドは、CI とこのファイルに明記された範囲、および変更内容に直接対応する関連テストに限定する。指定範囲より広い確認（例: CI で Ruff 対象外の `tests/` に対する `ruff check`、関連範囲を超える全量 E2E、packaging smoke など）は、ユーザーが明示した場合、または事前に目的・追加コストを説明して承認を得た場合だけ実行する。「念のため」の広範囲チェックを無断で追加しない。
- Python コードを変更したら、原則として `ruff format --check` と `ruff check` と `mypy` と関連 `pytest` を実行してから完了報告する（`ruff check` と `ruff format --check` は別物。整形漏れは CI の `test` ジョブで落ちるため、`ruff format --check` を必ず含める。整形が必要なら `uv run --no-sync ruff format guildbotics` を実行）
- 重複コード確認は `uv run --no-sync pylint guildbotics` を使う（`pyproject.toml` で `duplicate-code` のみ有効化）
- 最低限の確認コマンドは `uv run --no-sync ruff format --check guildbotics`、`uv run --no-sync ruff check guildbotics`、`uv run --no-sync mypy guildbotics`、`uv run --no-sync pylint guildbotics`、`uv run --no-sync python -m pytest ...`
- 型エラーや lint エラーを回避するためだけの `# type: ignore` や noqa は、理由が明確でない限り追加しない

desktop TypeScript 開発時の品質確認:

- Node.js は CI と合わせて 24 系を前提にする
- frontend の依存更新は `desktop/package-lock.json` も更新する
- TypeScript / React コードを変更したら、原則として `npm run format:check`、`npm run lint`、`npm run typecheck`、`npm run duplicates`、関連する `npm run test` を実行してから完了報告する
- 整形が必要な場合は `desktop` で `npm run format` を実行し、Prettier の結果を正とする
- React コンポーネントの挙動変更時は React Testing Library によるコンポーネントテストを追加・更新する
- 純粋関数、入力変換、API payload 生成、trace / scheduler 表示ロジックなどの分岐を変更した場合は Vitest のユニットテストを追加・更新する
- 重複コード抑止は `npm run duplicates` (`jscpd`) を使う。重複検出を避けるためだけの不自然な分割ではなく、UI とロジックの責務が自然に分かれる形へ整理する
- Tauri / Rust 側や生成物を frontend 品質チェックへ巻き込まない。対象は `desktop/src` と frontend 設定ファイルを基本とする
- 実ブラウザ + 実 backend を貫く critical user journey（setup→作成 / scheduler start-stop / command 実行+ストリーム / diagnostics / backend down→retry）を変更したら、`desktop/e2e/*.spec.ts` の該当 Playwright journey を更新する。E2E は `npm run quality` / push CI には含めず、`npm run e2e` で実行する
- E2E に振る舞いパターンを総当たりで持ち込まない。分岐網羅は Vitest の unit / component（mock 境界）に置き、Playwright は jsdom では検証できない実ブラウザ + 実ワイヤ契約（`client.ts ↔ FastAPI ↔ EventBus`）+ 実ファイル書き込みに絞る

## テスト実装の考え方

このリポジトリでは、テストピラミッドに従ってテストを維持する。修正や機能追加の際には「既存テストが通ること」だけでは不十分で、変更した振る舞いを検出できるテストが追加・更新されていることを完了条件に含める。

基本方針:

- Unit test を最も厚くする。純粋関数、入力変換、validator、payload 生成、状態遷移、エラー変換、ファイル解決順はまず unit test で網羅する
- Component / service integration test は、UI 操作、API endpoint、config 書き込み、runtime lifecycle など境界をまたぐ主要 workflow に限定して追加する
- ブラウザ E2E（Playwright, `desktop/e2e/`）は lean-but-real。実ブラウザ engine + 実 Local API backend でしか検証できない critical user journey（setup→実ファイル書き込み、scheduler start/stop、command 実行+`/events` ストリーム、diagnostics、backend down→retry）に絞り、振る舞いパターンの総当たりはしない（分岐網羅は unit / component に委譲）。push CI からは隔離し専用ジョブで回す
- Tauri ネイティブ / packaging smoke は最小限に保ち、実 OS + Tauri runtime が要るもの（sidecar 起動 / `backend_info` / file picker など）は workflow_dispatch / release workflow に隔離する
- LLM、GitHub、Slack、外部 CLI などへの実通信は通常 CI のテストに入れない。既存抽象化、stub、mock、fixture を使い、送信 payload、判定結果、エラー処理を検証する
- snapshot のみで品質を担保しない。ユーザーが観測する文言・状態、生成 request、保存 file/env、publish event、return value を具体的に assert する
- テストコードも本体コードと同じ品質対象とする。重複 fixture や場当たり的 mock が増えた場合は helper / factory へ整理する

変更種別ごとの必須確認:

- 純粋関数、入力変換、validation、parser、serializer を変更したら、正常系・境界値・不正入力の unit test を追加・更新する
- API endpoint、`AppRuntime`、setup service、config 書き込みを変更したら、service unit test と FastAPI `TestClient` integration test の両方を検討する
- scheduler / event listener / websocket / lifecycle を変更したら、状態遷移、二重起動防止、停止、timeout、event/log publish を検証する
- command runner / command spec / command discovery を変更したら、`Context.pipe`、`shared_state`、person-specific fallback、localized file precedence、child command failure を検証する
- desktop の API client を変更したら、URL、method、header、body、query parameter、error response、websocket status を検証する
- desktop の React component を変更したら、React Testing Library でユーザー操作と表示状態を検証する。implementation detail の state ではなく role/text/value/payload を assert する
- desktop の setup / commands / diagnostics / service runtime の workflow を変更したら、component test または mock API integration test を追加・更新する
- desktop の cross-boundary user journey（実 backend を貫く setup / runtime / commands / diagnostics / 起動失敗）を変更したら、`desktop/e2e/` の該当 Playwright spec を追加・更新する（実ブラウザ + 実 backend で検証。総当たりはせず代表 journey に絞る）
- i18n 文言や翻訳キーを変更したら、キー経由の検証を行い、片方の言語だけ欠落しないことを確認する
- bug fix では、先に再現テストまたは同等の failing assertion を追加し、そのテストが修正後に通ることを確認する

テスト追加を省略してよいのは、コメント修正、内部ドキュメントだけの変更、format のみ、型だけの機械的変更など、実行時の振る舞いが変わらないと明確に説明できる場合に限る。その場合も完了報告で「テスト追加不要の理由」を明記する。

## 変更時の実務ルール

- 挙動変更時は、対応テストを `tests/guildbotics/...` に追加・更新する
- コマンド仕様変更時は、`docs/custom_command_guide.en.md` / `docs/custom_command_guide.ja.md` の整合性も確認する
- CLI のオプションやコマンド変更時は、`README.md` / `README.ja.md` の該当箇所も確認する
- ユーザー向け文言を Python にハードコードしない。既存実装に合わせて `guildbotics.utils.i18n_tool.t()` を使い、翻訳キーを `guildbotics/templates/locales/...`（必要なら `*.ja.yml` / `*.en.yml`）へ追加する
- i18n 文言を変更・追加した場合は、既存テストに合わせて翻訳キー経由で検証する（文言直書き前提のテストにしない）
- `Context.pipe` / `shared_state` の更新順序はワークフロー互換性に直結するため、変更時は特に注意する
- コマンド解決順 (`get_person_config_path`, `get_config_path`) を壊さない

## 参照優先度（このリポジトリでの推奨）

実装確認の優先順:

1. `guildbotics/cli/__init__.py`
2. `guildbotics/drivers/command_runner.py`
3. `guildbotics/commands/*`
4. `guildbotics/runtime/context.py`
5. `guildbotics/utils/fileio.py`
6. `tests/guildbotics/...`

ドキュメントは補助として利用し、矛盾があればソースに合わせて修正すること。
