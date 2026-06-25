# GuildBotics Desktop GUI 実装方針

## 目的

GuildBotics の既存 CLI 体験を維持しながら、macOS 向けのデスクトップ GUI を追加する。GUI は既存 Python 実行基盤を再実装せず、Local API 経由で利用する。

## 決定事項

- リポジトリは分割せず、monorepo として管理する。
- Python 側は既存 `guildbotics` package を継続し、GUI 用境界として `guildbotics/app_api/` を追加する。
- Desktop 側は `desktop/` に Tauri v2 + TypeScript frontend として追加する。
- v1 GUI の正式対応は macOS Apple Silicon arm64 のみとする。
- v1 の配布物は signed + notarized DMG を直接配布する。
- v1 では自動更新を実装せず、手動更新を前提にする。
- Python backend は desktop app に完全同梱する。
- UI と backend は `127.0.0.1` の Local API で接続し、REST と WebSocket を使う。
- Local API は起動時 session token を必須にし、GUI からの接続だけを許可する。
- Codex CLI、Antigravity CLI、Claude Code などの外部 CLI agent は同梱しない。GUI は検出、状態表示、設定支援、verify のみを提供する。
- secrets は v1 では既存 CLI 互換の `.env` を正とする。OS Keychain 対応は将来課題とする。
- Setup UI は Wizard 専用画面を別に作らず、単一の設定画面に初回ガイド層を重ねる。
- GitHub 連携そのものは任意だが、初期設定では「使う / 使わない」の判断を明示入力にする。Setup の導線は `プロジェクト → LLM・CLIエージェント → GitHub → メンバー` の順に統一する。
- Desktop frontend は Mantine v7、React Router、TanStack Query、lucide-react を標準構成にする。
- GUI の workspace は backend sidecar の cwd として扱う。API request ごとに workspace path を渡す設計にはしない。

## サポート方針

| 環境                      | GUI v1   | CLI      |
| ------------------------- | -------- | -------- |
| macOS Apple Silicon arm64 | 正式対応 | 継続対応 |
| macOS Intel               | 対象外   | 継続対応 |
| Linux                     | 対象外   | 継続対応 |
| Windows                   | 対象外   | 将来検討 |

GUI 非対応環境でも、既存 CLI が fallback として動作することを維持する。

## 構成

```text
GuildBotics/
  guildbotics/
    app_api/       # Desktop GUI 用 Local API daemon
    cli/           # 既存 Click CLI
    commands/      # 既存 command framework
    drivers/       # 既存 scheduler / command runner
    runtime/       # 既存 context / factories
  desktop/
    src/           # TypeScript frontend
    src-tauri/     # Tauri shell / sidecar packaging
```

## Local API 境界

v1 skeleton では以下の API を提供する。

- `GET /health`: backend の起動確認。
- `GET /config/status`: `.env`、config、storage path の状態確認。
- `GET /team`: project と members の概要。
- `POST /commands/run`: 既存 command runner の実行。
- `GET /scheduler/status`: GUI sidecar 内 runtime（scheduler / events）の状態確認。
- `POST /scheduler/start`: GUI sidecar 内 runtime の起動。`only` 未指定時は CLI `start` と同じく scheduler と event listener runner の両方を起動する。
- `POST /scheduler/stop`: GUI sidecar 内 runtime の停止。
- `POST /verify`: GUI 向けの非対話 verify。
- `WS /events`: runtime event stream。
- `WS /logs`: log stream。

すべての endpoint は session token を要求する。REST は `X-GuildBotics-Session-Token` header、WebSocket は `token` query parameter を使う。

REST endpoint の error response は以下の JSON 形状に統一する。

```json
{
  "code": "machine_readable_error_code",
  "message": "Human readable summary.",
  "context": {}
}
```

frontend は `message` を表示し、分岐や復旧導線には `code` と `context` を使う。FastAPI 標準の `detail` 文字列には依存しない。

`/scheduler/*` は historical name を維持するが、response は scheduler と event listener runner の両方を含む runtime lifecycle model とする。各 runtime は `starting`、`running`、`stopping`、`stopped`、`failed` の state を持つ。

## Packaging 方針

- Tauri sidecar として Python backend binary を同梱する。
- v1 の sidecar build target は `aarch64-apple-darwin` のみとする。
- DMG の署名と notarization を release workflow の完了条件にする。
- external CLI agent は同梱せず、ユーザー環境の PATH と設定を利用する。

## 大項目 TODO

1. Desktop 方針ドキュメントを追加する。
2. Python service 層を作り、CLI 対話ロジックから設定生成処理を切り出す。
3. Local API daemon を追加し、CLI 相当機能を API 経由で呼べるようにする。
4. WebSocket で logs/events/progress を流す仕組みを作る。
5. Tauri + TypeScript frontend の skeleton を追加する。
6. 初期セットアップ、設定確認、member 管理、command 実行、scheduler 管理の画面を作る。
7. Python backend sidecar の Mac arm64 packaging を作る。
8. signed + notarized DMG の release workflow を作る。
9. GUI サポート外環境でも CLI が壊れていないことを CI で保証する。

## 現在の到達点

このドキュメント作成時点で、以下は着手済みである。

- `guildbotics/app_api/` に FastAPI ベースの Local API skeleton を追加済み。
- `GET /health`、`GET /config/status`、`GET /team`、`POST /commands/run`、scheduler start/stop/status、`POST /verify`、`WS /events`、`WS /logs` の入口を追加済み。
- REST endpoint の成功 response model と共通 error response model を定義済み。
- API error は `code`、`message`、`context` の JSON shape に統一済み。
- `guildbotics/cli/simple/setup_service.py` に project / member 設定ファイル生成 service を追加し、`config init` / `config add` のファイル生成部分を再利用可能にした。
- GitHub Project URL、GitHub repository URL、GitHub App URL、GitHub user 解決の service 境界を追加済み。
- `POST /commands/run` は request id を返し、同時実行を v1 では 1 件に制限済み。
- command 実行中の `command.started`、`command.log`、`command.finished`、`command.failed` を `WS /events` に流す実装を追加済み。
- scheduler / event listener runner の lifecycle 管理を `RuntimeLifecycleService` へ分離済み。
- `/scheduler/start` は `only` 未指定で scheduler と event listener runner の両方を起動し、`only=scheduler|events` で片方だけ起動する。
- GUI sidecar 内 runtime は CLI daemon の pidfile を作成せず、sidecar process 内の thread / runner として管理する。
- `desktop/` に Tauri v2 + React + TanStack Query の skeleton を追加済み。
- Desktop frontend に Mantine v7、React Router、`@mantine/form`、zod、i18next / react-i18next を導入済み。
- GUI 表示言語は `localStorage` の app preference として管理し、`project.yml` の `language`（エージェントの既定言語）とは分離済み。新規 project 作成時の `language` 初期値だけ GUI 表示言語に合わせる。
- Setup UI は「単一設定画面 + 初回ガイド層」として追加済み。
- Setup UI の初回フィードバックを反映し、保存 CTA を「設定ファイルを書き込む」に明確化、language / config location に明示ラベルを追加、Tauri 実行時の作業ディレクトリ選択を OS dialog に変更済み。
- Setup UI の「次へ」は当該セクションの必須入力が満たされた場合のみ有効化し、未入力のまま進めないように更新済み。
- `GET /intelligences/cli-agents/detection` を追加し、CLI エージェントの検出済み/未検出を UI で表示し、未検出は選択不可に更新済み。
- 初期セットアップでは `シークレット` セクションを必須導線から外し、コアは `プロジェクト / LLM・CLIエージェント / メンバー` の 3 セクションに整理済み。`シークレット` は設定後画面で管理する。
- 初期セットアップ時の `.env` 書き込み方式は UI で選択させず自動固定（`.env` 既存時は `append`、未存在時は `overwrite`）に更新済み。
- FastAPI の `RequestValidationError` を `ApiError` 形式（`code/message/context`）へ統一変換済み。
- `POST /config/init` と `POST /config/members` を追加し、GUI から setup service を呼べるようにした。
- `GET /config/project` と `PUT /config/project` を追加し、既存設定のフォーム hydrate と非破壊更新（`project.yml`・default mapping・`.env` キーマージ）を実装済み。
- 既存設定の保存導線は `PUT /config/project` を使うよう frontend を更新済み（初回作成のみ `POST /config/init`）。
- `POST /config/members/resolve` を追加し、member 追加フォームから GitHub identity 解決を呼び出せるようにした。
- Setup UI の `MembersSection` を placeholder から更新し、member 一覧表示と `config add` 相当の追加フォーム（種別、identity 解決、roles、active、GitHub 認証入力）を実装済み。
- `GET /config/members/{person_id}`、`PUT /config/members/{person_id}`、`DELETE /config/members/{person_id}` を追加し、member の詳細取得・更新・削除を API 化した。
- `SimplePersonSetupService` に member 読み取り/更新/削除を追加し、person rename 時の directory 移動と `.env` の person-secret キー差し替え（GitHub/Slack）を実装済み。
- Setup UI の `MembersSection` で追加だけでなく編集/削除を実装し、タブ構成（基本 / GitHub 認証 / Slack）で member 単位設定を更新できるようにした。
- `GET /scheduler/routines` を追加し、routine ごとの `requires_github` を frontend に提供するようにした。
- runtime の scheduler start に GitHub 連携ガードを追加し、`ticket_driven_workflow` を GitHub 未設定のまま起動できないようにした。
- Overview/Commands 画面に routine 選択、GitHub 未設定ガード表示、member 指定実行、events/logs viewer を追加した。
- GitHub 未設定でも `project.yml` を生成できるように `SimpleProjectSetupService` を更新済み。
- Setup UI から GitHub 未設定の fresh workspace に project config と `.env` を生成できることを確認済み。
- frontend API client に `WS /events` の subscription helper を追加し、command runner 画面で実行イベントを表示するようにした。
- `desktop/src/api/backend.ts` で Tauri sidecar 起動と health check の入口を追加済み。
- `.github/workflows/desktop-macos.yml` に macOS arm64 desktop artifact build を実装済み。`desktop/sidecar/guildbotics-app-api.spec` ベースで PyInstaller sidecar を build し、sidecar health smoke test、optional な signing / notarization step を経て `tauri build` で DMG を生成する。secrets 未設定なら unsigned DMG、secrets 設定済みなら signed + notarized DMG を生成する。ローカル実機で unsigned DMG 生成まで検証済み。
- `to_pdf` の WeasyPrint import を実行時 lazy import にし、PDF を使わない command 実行が native dependency 不足に巻き込まれないようにした。
- `GET /config/intelligences` と `PUT /config/intelligences` を追加し、チーム既定と member override の `model_mapping.yml` / `brain_mapping.yml` / `cli_agent_mapping.yml` / `models/*` / `cli_agents/*` を GUI から読み書きできるようにした。
- Setup UI の `LLM・CLIエージェント` 詳細設定から「準備中」表示を削除し、機能割り当て、モデル定義、CLI エージェント定義を編集できるようにした。
- member 編集フォームに `LLM・CLIエージェント` タブを追加し、チーム既定の継承 / member 個別 override を GUI から切り替えられるようにした。

既知の制約:

- `POST /verify` は GUI 向けの軽量・非対話 check として実装済み。既存 `config verify` と異なり、外部サービス変更や対話 prompt は行わない。
- `POST /scheduler/stop` は v1 では sidecar 内 runtime 全体を停止する。個別 runtime の停止 UI は未実装。
- `WS /logs` は global log stream の skeleton であり、UI 統合は未整備。
- macOS signing / notarization secrets は未設定。workflow は secrets 未設定時に unsigned DMG を生成する。secrets 名は `APPLE_CERTIFICATE` / `APPLE_CERTIFICATE_PASSWORD` / `APPLE_KEYCHAIN_PASSWORD` / `APPLE_SIGNING_IDENTITY` / `APPLE_ID` / `APPLE_PASSWORD` / `APPLE_TEAM_ID`。
- packaged GUI sidecar は `weasyprint` を同梱しないため、`to_pdf` 系の PDF 変換は GUI からは使えない（native dependency を入れた CLI が fallback）。sidecar は PyInstaller onefile のため初回起動時の自己展開に約 10 秒かかる。
- local full pytest は WeasyPrint native dependency がない環境では `tests/guildbotics/drivers/commands/test_to_pdf_command.py` が失敗する。

## セッション分割 TODO

各セッションは、1 つの実装セッションで完了できる大きさを目安にする。前のセッションが未完了の場合は、次のセッションに進まない。

実施順序の明確化:

- UI 実装の完了を先に達成するため、`advanced intelligence 設定 UI` を packaging より先に実施する。
- よって実施順は `Session 8 (advanced intelligence)` → `Session 8.1 (GUI 向け設定検証 UX)` → `Session 8.2 (Overview 運用画面)` → `Session 8.3 (Commands 実行画面)` → `Session 8.4 (メンバー別巡回設定)` → `Session 9 (packaging)` → `Session 10 (CI/fallback)` とする。

### Session 1: Local API contract を安定化する

状態: 完了。

目的:

- GUI が依存できる REST response と error response の形を固定する。
- 既存 CLI の内部例外を GUI 向けに安全な API error へ変換する。

主な作業:

- `guildbotics/app_api/models.py` に共通 error model と endpoint ごとの response model を追加する。
- `guildbotics/app_api/api.py` の error handling を統一する。
- `CommandError`、person 未指定、person 不明、config 不備、validation error の HTTP status と JSON shape を決める。
- `docs/desktop_app_plan.ja.md` の Local API 境界に確定 schema 概要を反映する。

完了条件:

- すべての REST endpoint が成功時・失敗時とも Pydantic model で表現されている。
- frontend 側が `detail` の自由形式文字列に依存しない。
- 既存 CLI の user-facing message は変更しない。

確認:

```bash
uv run --no-sync ruff format --check guildbotics
uv run --no-sync ruff check guildbotics
uv run --no-sync mypy guildbotics
uv run --no-sync python -m pytest tests/guildbotics/app_api
```

### Session 2: GUI 用 setup service を完成させる

状態: 完了。

目的:

- `config init` と `config add` 相当のファイル生成を、CLI 対話に依存しない service として切り出す。
- CLI と GUI が同じ service を呼ぶ状態にする。

主な作業:

- `ProjectSetupInput` / `SimpleProjectSetupService` に不足している validation を追加する。
- member 追加用の `PersonSetupInput` と service を追加する。
- GitHub username / GitHub App URL / project URL / repository URL の parse 処理を service 側へ移動する。
- `.env` 追記処理は secrets の値を返さず、file action と masked summary のみ返す。
- `SimpleSetupTool` は `questionary` で入力を集め、service を呼ぶだけの薄い層へ寄せる。

完了条件:

- GUI から project init と member add に必要な入力 model が揃っている。
- CLI の `guildbotics config init` と `guildbotics config add` の生成結果が既存互換である。
- i18n 文言を Python に新規ハードコードしない。

確認:

```bash
uv run --no-sync ruff format --check guildbotics
uv run --no-sync ruff check guildbotics
uv run --no-sync mypy guildbotics
uv run --no-sync python -m pytest tests/guildbotics/cli/simple tests/guildbotics/cli/test_start_command.py
```

### Session 3: 非対話 verify API を実用化する

状態: 完了。

目的:

- GUI で設定状態を安全に診断できる `POST /verify` を作る。
- 既存 `config verify` のような外部サービス変更や対話 prompt は行わない。

主な作業:

- `verify` を read-only check に限定する。
- `.env` key presence、config file presence、active member、LLM provider 設定、CLI agent executable presence、GitHub credential presence を確認する。
- 外部 API を叩く check は timeout 付きの optional deep check として分ける。
- response は `ok`、`checks[]`、`warnings[]`、`errors[]` に分ける。

完了条件:

- GUI が「何が不足しているか」を field 単位で表示できる。
- verify API は project config や `.env` を変更しない。
- 既存 `config verify` は現状の対話的挙動を維持する。

確認:

```bash
uv run --no-sync python -m pytest tests/guildbotics/app_api tests/guildbotics/utils/test_fileio.py
```

### Session 4: command run の streaming/progress を実装する

状態: 完了。

目的:

- 長時間 command 実行中に GUI が logs/progress を表示できるようにする。

主な作業:

- `POST /commands/run` は request id を発行し、完了結果を返す。
- `WS /events` に `command.started`、`command.log`、`command.finished`、`command.failed` を流す。
- Python logging handler から request id を紐付けられる範囲を整理する。
- frontend API client に event subscription helper を追加する。

完了条件:

- GUI は command 実行中に spinner だけでなく実ログを表示できる。
- command 失敗時に traceback 全文を通常 UI に出さず、詳細表示用 payload として扱える。
- 複数 command の同時実行方針を決める。v1 では同時 1 件に制限してよい。

確認:

```bash
uv run --no-sync python -m pytest tests/guildbotics/app_api tests/guildbotics/cli/test_run_command.py
npm run build
```

### Session 5: scheduler / event listener lifecycle を整理する

状態: 完了。

目的:

- GUI から scheduler と event listener の起動・停止・状態確認を安全に行う。

主な作業:

- `AppRuntime` の scheduler thread 管理を service 化する。
- event listener runtime を API 管理対象に追加する。
- `only=scheduler|events` の意味を CLI と揃える。
- running / starting / stopping / stopped / failed の状態 model を追加する。
- pidfile を使う CLI daemon と、GUI sidecar 内 runtime の責務差を docs に明記する。

完了条件:

- GUI sidecar 内 runtime は CLI `guildbotics start` の pidfile と競合しない。
- start 二重押し、stop 二重押し、起動失敗、終了待ち timeout の挙動がテストされている。

確認:

```bash
uv run --no-sync python -m pytest tests/guildbotics/app_api tests/guildbotics/drivers/test_event_listener_runner.py tests/guildbotics/cli/test_start_command.py
```

### Session 6: Desktop UI を初期セットアップ中心に作る

状態: 完了。

目的:

- 初回ユーザーが CLI なしで設定完了まで進める GUI を作る。

主な作業:

- Setup UI は `docs/mockups/setup/README.md` の方針に従い、Wizard 専用画面ではなく「単一設定画面 + 初回ガイド層」として作る。
- Mantine v7、React Router、`@mantine/form`、zod、i18next / react-i18next を導入する。
- GUI 表示言語と `project.yml` の `language` は別設定として扱う。GUI 表示言語は app preference、`project.yml` の `language` は command / role / LLM 出力に効くエージェント既定言語とする。
- 新規 project 作成時のみ、エージェント既定言語の初期値を GUI 表示言語から決める。既存 config がある場合は `project.yml` の language を優先する。
- language、workspace directory、config dir、`.env` 方針、LLM provider、CLI agent を入力できるようにする。
- GitHub project/repository/repo access は任意入力だが、初期設定では GitHub を使うか使わないかの選択を必須にする。GitHub を使わない場合も `project.yml` は生成できる。
- GitHub 未設定でも `project.yml` を生成できるように `SimpleProjectSetupService` と App API write endpoint を更新する。
- GUI で選んだ workspace directory を backend sidecar の cwd として扱う設計にする。v1 は workspace 変更時に backend を再起動する。
- 常設フィールドはユーザーが保存状態を信頼できるよう、validation 成功後に自動保存し、保存済み / 保存中 / 失敗の状態を表示する。
- secrets は masked input にし、既存 `.env` 値は直接表示しない。
- Setup の不足 / 完了状態はフォーム validation、CLI agent 検出、保存処理の成否、保存後の config/team 再読み込みから判定する。
- advanced intelligence 設定（機能ごとの brain mapping、model definitions、CLI agent script editing）は Session 6 では skeleton / deferred note に留める。
- `docs/mockups/setup/README.md` のコンポーネント構成を実装単位の目安にする。最低限、`SetupSectionNav`、`SetupStatusBanner`、`ProjectSection`、`IntelligenceSection`、`SecretsSection`、`MembersSection`、`GitHubIntegrationSection`、`AutosaveIndicator`、`SecretRow`、`InfoCallout`、`FolderPicker` を分ける。
- App API write endpoint は `SimpleProjectSetupService` / `SimplePersonSetupService` を呼ぶ薄い層にし、CLI と GUI の生成ロジックを分岐させない。
- 正となる入力 model / validation は `ProjectSetupInput`、`PersonSetupInput`、および `SimpleSetupTool` の既存条件分岐とする。frontend の zod schema は Pydantic model と対称に保つ。
- `.env` の生値は API response にも UI にも返さない。masked summary、設定済み状態、置き換え操作だけを扱う。
- CLI agent は同梱せず、検出・状態表示・設定支援だけを行う。軽量診断としての `/verify` は Overview の補助機能に限定し、Setup 完了条件には使わない。

完了条件:

- fresh workspace で GUI から project init 相当が完了する。
- GitHub を使わない選択を明示すれば初期設定は完了扱いになり、生成された `project.yml` を既存 loader が読める。
- workspace directory が明示され、`.env` と project-local `.guildbotics/config` の基点が UI 上で分かる。
- CLI `guildbotics config init` で作った既存 config を GUI が認識する。
- mobile viewport 対応は不要だが、最小 window size で text overflow がない。

確認:

```bash
npm run build
uv run --no-sync python -m pytest tests/guildbotics/cli/simple tests/guildbotics/app_api
```

### Session 7: Desktop UI を運用画面として整える

状態: 完了。

目的:

- 設定後の日常操作を GUI で行えるようにする。

主な作業:

- overview、member list、command runner、scheduler control、logs/events viewer を整理する。
- member の追加/編集（最低 1 active member の作成）を setup 導線に組み込み、初期設定完了条件と整合させる。
- `config add` 相当の GUI（member 一覧 + 追加/編集フォーム + GitHub 認証 + Slack + 稼働切替）を実装し、`POST /config/members` を client から配線する。
- member 追加に必要な GitHub username / Apps URL 解決 API（timeout 付き）を追加し、フォームから利用する。
- active member、roles、routine commands、scheduler status を見やすく表示する。
- command 実行フォームは person、cwd、args、message を扱う。
- GitHub 連携未設定時は `ticket_driven_workflow` / scheduler の起動導線を guard し、GitHub 設定が必要な理由と設定先を表示する。
- GitHub 連携設定済み時だけ `ticket_driven_workflow` を通常導線に出す。
- destructive / long-running 操作には明確な pending / disabled state を入れる。
- project 更新 API は導入済み。Session 7 では member/intelligences 導線の追加に合わせ、更新 API の回帰テストを強化する。

Session 7 完了メモ（2026-05-30）:

- 既存 project の非破壊更新導線（`GET/PUT /config/project`）を setup に接続済み。
- member 管理は追加/編集/削除、GitHub identity resolve、GitHub 認証、Slack 設定、active 切替まで GUI から操作可能。
- scheduler routine は GitHub 必須フラグ付きで取得でき、GitHub 未設定時の起動ガードを backend/frontend 両方で有効化済み。
- Overview/Commands で runtime events/logs を可視化し、日常運用の最短導線を整備済み。

完了条件:

- GUI skeleton ではなく、通常利用の最短導線が成立している。
- GitHub 未設定 workspace で ticket-driven workflow を誤起動できない。
- API unavailable、invalid token、backend startup failure の表示がある。

確認:

```bash
npm run build
```

### Session 8: advanced intelligence 設定 UI を完成させる

状態: 完了。

目的:

- `docs/mockups/setup/README.md` で定義した intelligences の詳細編集を GUI で完了させる。
- 初期設定だけでなく、設定後の通常運用フェーズでの編集導線（既存値の表示・変更・保存）まで含めて Setup UI を完成状態にする。

主な作業:

- `brain_mapping.yml` の機能単位割り当てを表示・編集する。
- `model_mapping.yml` と `models/*` のモデル定義、モデル ID を表示・編集する。
- `cli_agent_mapping.yml` と `cli_agents/*` の CLI agent 定義、検出状態、呼び出し script を表示・編集する。
- member 単位の `team/members/<person_id>/intelligences/` override を表示・編集する。
- CLI agent script editing は上級者向けとして明示し、verify の CLI executable detection と連動させる。
- `LLM・CLIエージェント` セクションの「詳細設定（準備中）」表記を解消し、モックアップにある詳細編集 UI（機能割当・モデル定義・CLI 定義）が実際に操作できる状態にする。
- 保存挙動は mockup 方針に合わせ、常設設定はオートセーブ、member 編集は明示保存の一貫した挙動にする（初回ガイドの `次へ` は保存操作にしない）。
- `models/*` の `rate_limit` は v1 では編集 UI の対象外とする（現状コードで実効利用されるのは `max_requests_per_minute` のみで、`max_requests_per_day` は未使用のため）。

Session 8 完了メモ（2026-05-31）:

- `GET/PUT /config/intelligences` を追加し、チーム既定と member override を同じ API model で扱うようにした。
- member override がない場合はチーム既定を継承し、GUI から `チーム既定を使う` に戻すと member 配下の `intelligences/` override を削除する。
- 詳細設定 UI は `model_mapping.yml`、モデル ID / model class、`cli_agent_mapping.yml`、`brain_mapping.yml`、CLI agent env/script を編集できる。
- チーム既定の詳細設定はオートセーブ、member 個別 override は member 編集フォームの `変更を保存` に統合した。
- 初期設定前はチーム既定の詳細設定を表示せず、初期設定作成後にだけ表示する。
- `rate_limit` は v1 の編集対象から外した。
- Setup サイドバーは `プロジェクト → LLM・CLIエージェント → GitHub → メンバー` の順にし、必須設定 / 任意連携の見出し分離は廃止した。
- member の `person_type` は GitHub 連携時だけ意味を持つため Basic タブから GitHub タブへ移動し、GitHub と連携しない member を追加できるようにした。
- `POST /verify` は Setup 完了導線から外し、Overview の補助的な軽量診断として扱う。Setup ではフォーム validation、CLI agent 検出、保存処理の成否、保存後の config/team 再読み込みで初期設定の成否を判断する。
- 初期設定中に追加した member は `project.yml` 作成前でも UI 上の draft 一覧に保持し、編集・削除できるようにした。
- `初期設定を作成` 成功後は workspace を backend cwd として反映し、config/team を再取得して設定済み画面へ切り替える。選択 workspace は `localStorage` に保存し、desktop app 再起動時の sidecar cwd として復元する。

受け入れテスト観点:

- fresh workspace で初期表示すると、Setup には入力進捗のみが表示され、軽量診断は表示されない。
- Project / LLM・CLI / GitHub / Member を入力し、member を追加すると、初期設定前でも追加済み member が一覧に表示され、編集・削除できる。
- `初期設定を作成` 成功後、成功メッセージが表示され、Setup が設定済み画面へ切り替わる。
- `初期設定を作成` 失敗時は初期設定画面のまま、保存エラーが表示される。
- desktop app を終了・再起動しても、最後に選択した workspace の `.guildbotics/config` と `.env` を認識し、未設定画面へ戻らない。
- Overview の軽量診断における LLM API key は存在確認のみであり、不正キーの live 疎通確認は行わない。live validation を入れる場合は provider 別の明示的な接続テストとして別途実装する。

完了条件:

- チーム既定と member override の両方で、LLM / CLI / brain mapping の実効設定が UI から確認できる。
- advanced intelligence 設定を変更しても既存 CLI command execution と config file resolution が壊れない。
- 既存設定を読み込んだ編集で破壊的上書きが起きず、初期設定後の再編集（既存 project/member の変更）が GUI だけで完結する。
- Setup 画面内に未実装プレースホルダ（準備中表示）が残っていない。

確認:

```bash
npm run build
uv run --no-sync python -m pytest tests/guildbotics/app_api tests/guildbotics/utils/test_fileio.py
```

### Session 8.1: GUI 向け設定検証 UX を再設計する

状態: 完了。

目的:

- CLI `guildbotics config verify` が担っている「設定全体が実際に使えるか」の検証を、GUI でどう提供するか再設計する。
- 軽量診断 `/verify` と、read-only のシナリオ型 live diagnostics を混同しない情報設計にする。
- v1 では個別接続テスト UI を増やさず、`設定を検証` という 1 つの総合診断から、Config / Members / LLM / CLI agent / GitHub / Slack / Git の項目別結果を返す。
- どの診断も接続先システムのデータを更新しない。GitHub への push / issue 作成 / comment / project item 更新、Slack への message 投稿 / reaction 追加は禁止する。

主な作業:

- 現行 CLI `config verify` の実処理を棚卸しする。
  - LLM API への実リクエスト。
  - GitHub Projects / Issues / repository への実接続。
  - Git / checkout / workspace 操作。
  - CLI agent 実行。
  - custom fields 作成、status mapping など外部サービス変更・対話を含む処理。
- GUI で提供する検証を以下の層に分解する。
  - **入力時 validation**: フォーム制約、CLI agent 検出、保存処理の成否。Setup 完了条件に使う。
  - **軽量診断**: `/verify` 相当の read-only presence check。Overview の補助機能として扱う。
  - **read-only シナリオ診断**: ユーザーが明示的に押した時だけ外部 API / 外部サービスへ接続する総合診断。Setup ではなく Overview に置く。
- v1 の App API endpoint は `POST /diagnostics/scenario` とする。個別 endpoint は v1 では作らない。
- read-only シナリオ診断の検査範囲を以下にする。
  - Config: config / `.env` / active member の読み込み。
  - LLM: active member のうち代表 1 名で最小 prompt を実行し、既定 LLM provider / API key / model 設定の実効性を確認する。
  - CLI agent: 既定 CLI agent の mapping と executable 検出に加え、`CliAgentBrain` 経由で最小の read-only prompt を実行する。
  - GitHub: GitHub 連携有効時、active member ごとに Project status 取得と repository default branch 取得を行う。push / issue 作成 / comment / project item 更新 / custom field 作成は行わない。
  - Slack: Slack channel 設定がある active member ごとに bot identity、channel 解決、`conversations.history` の read-only 取得を確認する。message 投稿 / reaction 追加は行わない。
  - Git: v1 のシナリオ診断では remote repository metadata 取得までに留め、local checkout / branch 作成は行わない。
- 診断結果は section、person_id、target、code、status、message、context を持つ構造にし、エラー時に「どの設定が原因か」が UI で追えるようにする。
- Overview の診断ボタンは `/verify` ではなく `POST /diagnostics/scenario` を呼ぶ。軽量 `/verify` は API として残すが通常 UI の主導線にはしない。
- `初期設定を作成` の必須条件にはしない。ユーザーが明示的に押した時だけ外部 API / 外部サービスを呼ぶ。
- API key / token / private key の生値を response / log / event stream に出さない設計を確認する。
- テストは SDK / GitHub client / CLI agent 実行を mock し、通常 CI では実ネットワークに依存しない。
- Session 8.1 で実装する範囲、後続 Session に分ける範囲、実装しない範囲を docs に追記する。

完了条件:

- CLI `config verify` と GUI 検証 UX の責務差分が docs に明記されている。
- 入力時 validation、軽量診断、read-only シナリオ診断の境界が docs に明記されている。
- 個別接続テスト UI は v1 では作らない方針が docs に明記されている。
- read-only シナリオ診断の GitHub / Slack 禁止操作が docs に明記されている。
- `POST /diagnostics/scenario` が追加され、Overview から実行できる。
- どの検証も `初期設定を作成` の必須条件にはしない方針が明記されている。
- secret 生値を response / log / event stream に出さない方針が明記されている。

Session 8.1 完了メモ（2026-05-31）:

- `POST /diagnostics/scenario` を追加し、Overview の `設定を検証` から実行できるようにした。
- `POST /diagnostics/scenario?person_id=<id>` に対応し、メンバー編集画面の `検証` タブから対象メンバーだけを検証できるようにした。
- 既存 `/verify` は軽量 presence check として残し、通常 UI の主導線は read-only シナリオ診断へ切り替えた。
- シナリオ診断は Config / Members / LLM / CLI agent / GitHub / Slack / Git の section 別 check を返す。
- Overview は全体診断、メンバー編集画面はメンバー単位診断の入口とする。メンバー追加中は保存済み設定がないため、保存後に検証できる表示にする。
- GitHub 診断は Project status 取得と repository metadata 取得までに限定し、push / issue 作成 / comment / project item 更新 / custom field 作成は行わない。
- Slack 診断は bot identity、channel 解決、`conversations.history` の読み取りまでに限定し、message 投稿 / reaction 追加は行わない。
- LLM 診断は `talk_as()` 経由で最小 prompt を実行し、通常の `brain_mapping.yml` では `AgnoAgentDefaultBrain` が使われることを確認する。
- CLI agent 診断は executable 検出だけでなく、`CliAgentBrain` 経由で一時 directory 上の最小 read-only prompt を実行する。

確認:

```bash
npm run build
uv run --no-sync python -m pytest tests/guildbotics/app_api
uv run --no-sync ruff check guildbotics/app_api tests/guildbotics/app_api
uv run --no-sync mypy guildbotics
```

### Session 8.2: Overview 運用画面を仮実装から完成状態へ寄せる

状態: 完成。

目的:

- 現在の Overview は config / scheduler / events / logs の最小導線を持つが、サービス実行、状態確認、診断が同一画面に混在しており、運用画面としては仮実装状態である。
- セットアップ完了後にユーザーが最初に見る画面として、常駐サービスの開始/停止と稼働状態を扱う「サービス実行」を成立させる。診断・ログ・prompt trace は「診断」へ分離し、目的別に辿れる構成にする。

現状:

- `/overview` route は存在し、`GET /config/status`、`GET /team`、`GET /config/project`、`GET /scheduler/status`、`GET /scheduler/routines`、`POST /scheduler/start`、`POST /scheduler/stop`、`POST /verify`、`WS /events`、`WS /logs` に接続済み。
- サイドメニューは「サービス実行」「コマンド実行」「診断」「設定」に分ける。`/overview` は互換用に `/service` へ redirect する。
- ただし `scheduler.data` を raw JSON の `<pre>` で表示している。
- Events / Logs は最新行を並べるだけで、空状態、接続状態、フィルタ、request id との対応、エラー時表示が不足している。
- 日本語環境でも `Overview`、`Runtime Control`、`Configuration`、`Ready`、`Missing`、`Found`、`Active members`、`Scheduler`、`Running`、`Stopped`、`Start`、`Stop` など英語表記が残っている。
- `/verify` は軽量診断として置かれているが、Session 8.1 の検証 UX 再設計結果に合わせて配置・名称・説明を見直す必要がある。

主な作業:

- Overview の i18n を完成させ、日本語環境で内部用語・英語ラベルが残らないようにする。最終的な画面名は「概要」ではなく「サービス実行」とする。
- 「サービス実行」は、常駐サービスとしての scheduler / event listener の開始・停止と状態把握に絞る。
- 「コマンド実行」は、CLI `guildbotics run` 相当の one-off command 実行に絞る。
- 「診断」は、準備状態、設定検証、runtime event/log stream、出力済み prompt trace の確認に絞る。
- 診断画面はタブで「準備状態」「プロンプトトレース」「実行ストリーム」に分ける。実行ストリーム内の `events/logs` 切り替えは二段タブにせず segmented control とする。
- raw JSON 表示を廃止し、scheduler / event listener の状態をユーザー向けの状態カードとして表示する。
- CLI の `guildbotics start` は `--only scheduler|events`、`--max-consecutive-errors`、routine command 引数を持つが、Overview では CLI 用語をそのまま出さず、通常導線を「自動運用を開始」へ寄せる。
- runtime はユーザー向けに以下の 2 つの稼働単位として表示する。
  - **自動巡回**: `TaskScheduler`。巡回処理 (GitHub チケット対応等) を実行する。メンバーごとのルーチンワークフロー / 定期コマンド設定は Session 8.4 で扱うため、Session 8.2 時点の説明文は詳細に踏み込まない。
  - **チャット対応**: `EventListenerRunner`。Slack などのチャットイベントを受けて `workflows/chat_conversation_workflow` を実行し、必要な応答を投稿する。
- 自動巡回カードには、状態、起動時刻、停止時刻、巡回ワークフロー、対象 active member、GitHub 必須フラグ、エラーを表示する。
- チャット対応カードには、状態、起動時刻、停止時刻、応答ワークフロー、対象 subscription、受信/処理/スキップ/失敗回数、エラーを表示する。
- 通常操作は「自動運用を開始」と「停止」を主導線にする。起動対象は三択の segmented control ではなく、「自動巡回」「チャット対応」それぞれの ON/OFF スイッチとして表示し、両方 OFF の場合は開始できないことを明示する。
- ルーチンコマンド、巡回間隔（分）、連続失敗で停止する回数は自動巡回専用の設定として、自動巡回の説明・状態と同じグループ内に配置する。巡回間隔の既定値は 10 分とし、分単位で変更できるようにする。入力項目と同じ値を状態表示として重複表示しない。
- `GET /scheduler/status` は `state/running/started_at/stopped_at/error` だけでは運用画面に不足するため、GUI 向けに開始時の routine commands、routine interval minutes、max consecutive errors、scheduler active worker count、event listener の counters を返すよう拡張する。
- config / `.env` / GitHub / active member / runtime の状態を、次に必要な操作が分かる形で整理する。サービス実行画面では起動可否に関わる警告だけを出し、詳細な準備状態と検証結果は診断画面に置く。
- scheduler start / stop の pending、失敗、二重押し、GitHub 必須 routine guard を見直す。
- 停止 timeout は「停止要求は送ったが、sidecar 内 worker thread の終了確認が待機時間内に完了しなかった状態」として扱う。通常の 5 秒 polling で `stopped` に戻ることが多いため、初期表示では警告 alert にせず `停止中` として表示し、「終了処理に少し時間がかかっています」程度の補足に留める。後続の status refresh で thread 終了が確認できたら `stopped` に戻す。
- GitHub 必須 routine が選択され、GitHub 未設定の場合は開始ボタンを無効化し、「チケット駆動ワークフローには GitHub 連携が必要です」と設定導線を表示する。
- 診断画面に Events / Logs viewer を配置し、空状態、接続状態、エラー状態、フィルタ、request id / command との紐付け表示を追加する。
- Events / Logs は raw payload を常時表示せず、時刻、種別、対象 request id、command、ユーザー向け要約を一覧表示し、詳細は必要時だけ開ける形にする。
- Slack チャット応答の会話スタイル調査用に、prompt trace を opt-in で出力できるようにする。`GUILDBOTICS_PROMPT_TRACE=1` で有効化し、既定では storage の `run/prompt_trace.jsonl`、`GUILDBOTICS_PROMPT_TRACE_PATH` 指定時はその path に JSONL を出力する。
- prompt trace には、chat reply 入力（`agent_profile`、`thread_messages`、`reply_intent` など）、LLM request/response、CLI agent request/response を記録する。prompt や Slack 発言を含むため既定は OFF とする。
- prompt trace の ON/OFF と出力 path は、これから実行する処理の実行前設定として「サービス実行」と「コマンド実行」に配置する。ON/OFF と出力 path は `.env` に保存し、sidecar プロセスの環境変数にも即時反映する。
- prompt trace の出力 path と表示対象 path は別物として扱う。出力 path はこれから追記する JSONL の保存先であり、表示対象 path は過去に保存済みの JSONL を読み込むための入力である。`診断` の `プロンプトトレース` タブでは表示対象 path を指定でき、出力設定を変えずに任意の記録済み trace file を読み込めるようにする。どちらもファイルパス入力欄そのものを主操作とし、Enter / focus out で適用する。ファイル選択と既定値復帰は、入力欄に紐づく補助アイコンとして配置する。
- prompt trace の最新 event は raw JSON ではなく、event 種別、person、brain / command / cwd などのメタ情報、prompt/input、response/output に分けて表示する。デバッグ用途のため prompt 本文は表示するが、機密情報を含む可能性を UI 上で明示する。
- 軽量診断 `/verify` の扱いを Session 8.1 の決定に合わせ、診断内で「何を検証し、何を検証しないか」を UI 上で誤解なく表現する。
- Setup 未完了時は、サービス実行画面で操作できない理由と Setup への導線を明示する。
- Playwright または component test で、設定済み / 未設定 / GitHub 未設定 / scheduler 起動失敗の主要状態を確認する。

完了条件:

- サービス実行が「仮のステータス表示」ではなく、設定後に日常的に使える常駐サービス実行画面として成立している。
- 診断が準備状態、runtime stream、prompt trace を扱う診断専用画面として成立している。
- 日本語環境で主要 UI 文言が日本語化されている。
- raw JSON や未整形の内部 payload が通常 UI に露出していない。
- Setup 未完了、GitHub 未設定、runtime 停止中、runtime 起動中、runtime 起動失敗の状態が判別でき、次の操作が明確である。

確認:

```bash
npm run build
```

### Session 8.3: Commands 実行画面を仮実装から完成状態へ寄せる

状態: 完了。

目的:

- 現在の Commands は `POST /commands/run` と event/log stream に接続済みだが、one-off command 実行 UI としては仮実装状態である。
- 「サービス実行」は常駐サービスの開始/停止、「コマンド実行」は `guildbotics run` 相当の一回限りの手動実行、という役割分担を明確にする。
- セットアップ済みユーザーが、メンバー、コマンド、入力、必要な調査ログ出力を指定し、実行結果と関連ログを同じ文脈で確認できる状態にする。

現状:

- `/commands` route は存在し、active member 選択、command 文字列、message 入力、`POST /commands/run`、`WS /events`、`WS /logs` に接続済み。
- prompt trace 出力設定は「コマンド実行」に配置済みで、実行前に ON/OFF と出力 path を変更できる。
- ただし command は自由入力のみで、既存 command の候補提示、workflow / routine の説明、引数入力支援がない。
- CLI `guildbotics run` は `--person`、`--cwd`、`custom_command`、追加 args、stdin message を扱えるが、GUI では `cwd` と追加 args の扱いが整理されていない。
- 実行結果は `<pre>` に request id と output を出すだけで、実行中 / 成功 / 失敗 / 再実行 / 関連ログ / 詳細表示の整理が不足している。
- `ticket_driven_workflow` だけを GitHub 必須として判定しており、他 command の要件判定や実行前 validation は不足している。
- command event / log stream は表示されているが、現在の実行 request id との関係が弱く、診断画面の runtime stream と役割が重複して見える。
- 日本語環境の主要文言は改善済みだが、コマンド実行画面としての情報設計は未整理である。

主な作業:

- Commands の i18n を確認し、日本語環境で主要 UI 文言が日本語化されている状態を保つ。Session 8.2 の判断に合わせ、タイトル上の decorative eyebrow のような意味の薄い補助ラベルは追加しない。
- 画面名は「コマンド実行」とし、サービス常駐操作ではなく一回限りの手動実行であることが UI 構造から分かるようにする。
- 画面上部は、実行フォームと直近の実行状態が視線移動少なく確認できる配置にする。カードを過度に入れ子にせず、フォーム、調査ログ出力、結果を明確なグループとして並べる。
- command 入力を自由入力だけでなく、利用可能な command / workflow / routine 候補を選べる UI にする。自由入力は上級者向けに残す。
- command 候補取得用の backend API を追加する。候補には、表示名、実行 command、説明、引数の有無、GitHub / Slack / CLI agent / LLM などの必要条件、推奨される入力形式を含める。
- command 候補 API は、既存の command 解決順と同じ設定解決を使う。特定 command 名の hard-code ではなく、既存 command metadata または backend 判定に寄せる。
- GUI の command 候補はユーザーが編集できる member/project/home commands に絞り、`guildbotics/templates/commands` の部品的な組み込み command は一覧から除外する。初期設定作成またはプロジェクト設定保存時に `config/commands` が空なら、custom command guide ベースのサンプル command を配置する。
- member 選択、message、command、追加 args、cwd の関係を CLI の `guildbotics run` と整合させる。GUI では `cwd` は詳細設定として扱い、通常は現在の workspace 既定値を使う。
- message 入力は「標準入力相当」であることをユーザーが理解できる名称にする。ただし内部語を前面に出さず、コマンドへ渡す入力文として扱う。
- 追加 args は command 候補に応じて structured form で入力できるようにし、対応できない command では raw args 入力へ fallback する。
- prompt trace 出力設定は「この実行で発生する LLM / CLI 呼び出しの記録設定」として、実行フォームの近くに置く。診断画面の「表示するトレースファイル」と混同しないよう、出力先 path のみを扱う。
- 実行中、成功、失敗の状態表示を整理する。v1 でキャンセルできない場合はキャンセル UI を出さず、実行中は二重実行を防ぐ。直近履歴や「再実行を準備」のような曖昧な補助導線は出さず、現在の実行結果に集中する。
- 実行結果は raw `<pre>` ではなく、概要、出力、関連イベント、関連ログ、エラー詳細を分けて表示する。長文 output はスクロール可能な等幅領域で表示し、ページ全体の横幅を広げない。
- command event / log stream は request id で現在の実行と紐付け、現在の実行に関係する行だけを標準表示にする。全体 runtime stream は診断画面へ任せる。
- 失敗時は、通常表示では復旧に必要な要約を出し、traceback や内部 payload は詳細表示に閉じる。
- Setup 未完了、active member なし、CLI agent 未検出、LLM key 未設定、GitHub / Slack 未設定など、実行前に分かる問題を必要条件に応じて明示する。
- サービス実行と同様に、テキストやボタンの位置が内容量や折り返しで不自然にずれないよう、固定形式の要素には stable dimensions と上詰めレイアウトを使う。
- command 実行の component / integration test を追加する。

完了条件:

- コマンド実行画面から主要 command / workflow / routine を選択・実行し、進捗と結果を GUI 上で理解できる。
- 自由入力 command も残り、候補にない command を CLI と同じ発想で実行できる。
- 現在の実行に関連する event / log / output が request id でまとまり、診断画面の全体 stream と役割が重複して見えない。
- prompt trace 出力を ON にして実行した場合、診断画面のプロンプトトレースでその記録を追える。
- 失敗時に traceback や内部 payload を通常 UI に露出せず、詳細表示として扱える。
- 日本語環境で主要 UI 文言が日本語化され、内部用語だけでは意味が分からないラベルが残っていない。
- `guildbotics run` の主要ユースケースが GUI から破綻なく実行できる。

確認:

```bash
npm run build
uv run --no-sync python -m pytest tests/guildbotics/app_api tests/guildbotics/cli/test_run_command.py
```

### Session 8.4: メンバー別巡回設定を GUI から編集できるようにする

状態: 完了。

目的:

- 自動巡回の実体である `TaskScheduler` は、active member ごとに worker を立て、各メンバーの設定に基づいて処理を実行する。
- しかし現在の GUI では、起動時の共通ルーチンワークフローと連続失敗上限だけを設定でき、メンバーごとの「ルーチンワークフロー」と「定期コマンド」を編集できない。
- サービス実行画面の説明文で内部設定名を出さずに済むよう、メンバーごとの巡回設定を GUI 上で確認・編集できる導線を追加する。

現状:

- `Person` model には `routine_commands` と `task_schedules` がある。
- `TaskScheduler` はメンバーに `routine_commands` があればそれを使い、なければ起動時に渡された default routine commands を使う。
- `TaskScheduler` は各メンバーの `task_schedules` を cron schedule として展開し、該当時刻に scheduled command として実行する。
- 現在のサービス実行画面では、共通既定値としてのルーチンワークフロー、巡回間隔（分）、連続失敗上限を設定できるが、メンバー別 `routine_commands` / `task_schedules` の編集 UI はない。
- サービス実行画面の主操作は右上の `実行` / `停止` 排他ボタンに整理済みであり、自動巡回カード内には共通既定値の設定だけを置いている。
- Commands 画面では command 候補 API、説明付き候補表示、必要条件バッジ、引数入力支援、スクリプト path 表示/コピー/オープンが実装済みである。
- command 候補一覧は `guildbotics/templates/commands` の部品的な組み込み command をそのまま表示せず、workspace / home commands を中心に扱う。初期 project commands が空の場合は sample commands を配置する方針である。
- 診断基盤の本格的な相関 ID / trace UI 再設計は Session 8.4 の範囲外とし、`docs/runtime_diagnostics_todo.ja.md` で別 TODO として扱う。
- そのため、「自動巡回」の説明に `task_schedules` などの内部名を出すと、GUI 利用者には意味が分からない。

主な作業:

- メンバーごとの設定画面に「巡回設定」セクションを追加する。
- 「ルーチンワークフロー」は、メンバー個別の上書き設定として表示する。未設定の場合はサービス実行画面で選ぶ共通既定値を使うことを明示する。
- 「定期コマンド」は、cron schedule と実行コマンドの組として追加・編集・削除できるようにする。
- 定期コマンドの入力 UI は、内部名 `task_schedules` を前面に出さず、「いつ」「どのコマンドを実行するか」が分かる表現にする。
- cron 表記は自由入力だけにせず、毎時 / 毎日 / 毎週などの基本パターンを選べる UI と、詳細 cron 入力を併用する。
- コマンド入力は、Commands 画面と同じ command 候補 API・validation 方針を再利用する。候補表示はファイル名だけでなく説明文を含め、必要条件バッジも同じ意味で表示する。
- ルーチンワークフロー / 定期コマンドの command 選択では、Commands 画面と同じく workspace / home の command 候補を優先し、部品的な package template command を直接選ばせない。
- 選択した command について、Commands 画面と同じくスクリプト path の表示、コピー、デスクトップ実行時のオープン導線を用意する。ただしフォームの主操作を圧迫しないよう、リンク表示はコマンド入力直下の補助情報に留める。
- 定期コマンドの引数入力は、Commands 画面の structured argument 入力を再利用する。候補 metadata で引数定義が取れない場合は raw args 入力へ fallback する。
- メンバー設定保存時に `routine_commands` / `task_schedules` を既存 YAML へ反映する backend API を追加または既存 API を拡張する。
- 保存 API は既存 `Person` model と YAML 構造を正とし、GUI 専用の別 schema を作らない。
- 保存後は team / command options / scheduler status を再取得し、サービス実行画面に戻った時に共通既定値とメンバー別上書きの関係が破綻しないようにする。
- 設定後に、サービス実行画面の自動巡回カードから「メンバー別巡回設定」へ辿れる導線を追加する。
- Session 8.4 完了後、自動巡回の説明文を再検討し、「共通既定値」「メンバー別上書き」「定期実行」の関係が GUI 上で自然に理解できる文言へ更新する。サービス実行画面では細かい内部設定名を出さず、詳細編集への導線を示す。
- 診断画面の runtime stream / prompt trace は Session 8.4 では大きく拡張しない。巡回設定の保存・実行で必要な最低限の event/log 表示に留め、相関 ID に基づく診断再設計は runtime diagnostics TODO へ送る。

完了条件:

- GUI からメンバーごとのルーチンワークフローを設定・解除できる。
- GUI からメンバーごとの定期コマンドを追加・編集・削除できる。
- ルーチンワークフロー / 定期コマンドの command 選択が Commands 画面の候補表示・説明・必要条件・引数入力方針と一貫している。
- サービス実行画面だけを見ても、自動巡回で何が実行されるか、詳細設定をどこで変更するかが分かる。
- 内部設定名 `routine_commands` / `task_schedules` を知らないユーザーでも操作できる。
- 保存後の YAML が既存 `Person` model と `TaskScheduler` にそのまま読み込まれる。
- 診断基盤の相関 ID / 統合タイムライン再設計は Session 8.4 の完了条件に含めず、`docs/runtime_diagnostics_todo.ja.md` に追跡されている。

実装結果:

- メンバー編集フォームに「巡回」タブを追加し、個別巡回ワークフローの設定・解除と、定期実行コマンドの追加・編集・削除を同じ保存ボタンで反映できるようにした。
- `GET/PUT /config/members/{person_id}` の model と `SimplePersonSetupService` を拡張し、`routine_commands` / `task_schedules` を既存 `person.yml` 構造のまま読み書きするようにした。
- 定期実行 UI は毎時 / 毎日 / 毎週 / 詳細 cron の入力を持ち、command 候補 API の説明・必要条件・script path 表示・copy/open 導線と structured args / raw args fallback を再利用するようにした。
- サービス実行画面の自動巡回カードに、共通既定値とメンバー別上書きの関係を示す説明と、メンバー別巡回設定への導線を追加した。

確認:

```bash
npm run build
uv run --no-sync python -m pytest tests/guildbotics/cli/simple/test_setup_service.py -q
uv run --no-sync python -m pytest tests/guildbotics/app_api/test_api.py -q
uv run --no-sync python -m pytest tests/guildbotics/entities/test_team.py -q
uv run --no-sync ruff check guildbotics
uv run --no-sync mypy guildbotics
uv run --no-sync pylint guildbotics
```

### Session 9: macOS arm64 packaging を実際に通す

状態: 完了。

目的:

- Mac Apple Silicon 用の desktop artifact を CI で生成できるようにする。

主な作業:

- `.github/workflows/desktop-macos.yml` の PyInstaller sidecar build を実行可能にする。
- `desktop/src-tauri/binaries/guildbotics-app-api-aarch64-apple-darwin` の配置規則を確認する。
- Tauri sidecar 起動 permission を実機で検証する。
- signing / notarization secrets の名前を決め、workflow に optional step として追加する。
- WeasyPrint native dependency を sidecar に含める方針を検証する。

完了条件:

- GitHub Actions の manual run で unsigned DMG artifact が生成される。
- secrets 設定済み環境では signed + notarized DMG が生成される。
- fresh Mac で app 起動、backend health、config status 表示まで確認できる。

Session 9 完了メモ（2026-06-03）:

- PyInstaller の sidecar build を再現可能にするため、`desktop/sidecar/guildbotics-app-api.spec` を追加した。`guildbotics` package は config 経由で brain / command を動的解決する（`load_class`）ため、静的 import graph に依存せず submodule と data file をまとめて収集する。`.gitignore` の `*.spec` 除外に対して `!desktop/sidecar/*.spec` の例外を追加し、spec を version control に含めた。
- `weasyprint` は spec の `excludes` で sidecar に含めない方針とした。`ToPdfCommand` は実行時 lazy import で、native lib 不在時は `CommandError` を返すため、GTK / Pango / Cairo を持たない CI でも sidecar が build できる。packaged GUI の PDF 変換は v1 既知の制約とし、native dependency を入れた CLI を fallback とする。
- `.github/workflows/desktop-macos.yml` を更新し、(1) spec ベースの sidecar build、(2) build 直後の sidecar health smoke test、(3) signing / notarization の optional step、(4) `tauri build` を実行する。secrets 未設定でも unsigned DMG が生成される。
- signing / notarization 用 secrets 名を確定した: `APPLE_CERTIFICATE`（base64 .p12）、`APPLE_CERTIFICATE_PASSWORD`、`APPLE_KEYCHAIN_PASSWORD`、`APPLE_SIGNING_IDENTITY`、`APPLE_ID`、`APPLE_PASSWORD`（app-specific password）、`APPLE_TEAM_ID`。`APPLE_CERTIFICATE` が無ければ signing step を skip、`APPLE_CERTIFICATE` と `APPLE_ID` が揃った時だけ notarization 用 env を渡す。Tauri は env が空なら unsigned build を生成する。
- onefile sidecar は初回起動時に temp dir へ自己展開するため cold start に約 10 秒かかる。`desktop/src/api/backend.ts` の health 待ち deadline を 15 秒から 45 秒へ広げ、fresh Mac の初回起動を backend 起動失敗扱いにしないようにした。
- ローカル実機検証（macOS Apple Silicon, 2026-06-03）:
  - `desktop/sidecar/guildbotics-app-api.spec` から PyInstaller onefile（約 212MB）を build 成功。
  - build した sidecar を直接起動し、`GET /health` が `{"status":"ok"}`、`GET /config/status` が cwd / config path を正しく返すことを確認（cwd を workspace として扱う設計が packaged binary でも機能）。
  - `npm run tauri build -- --target aarch64-apple-darwin` で unsigned DMG（`GuildBotics_0.1.0_aarch64.dmg`, 約 215MB）を生成。DMG 内の `GuildBotics.app/Contents/MacOS/` に desktop 本体と sidecar `guildbotics-app-api` が同梱され、ad-hoc 署名であることを確認。
  - 注意: ローカルの `cargo` は Homebrew 版 1.83 が PATH 優先になっていると `edition2024` 要求の依存解決に失敗する。rustup の stable toolchain（1.85 以降）を使う必要がある。CI runner (macos-14) の既定 toolchain は要件を満たす。
- インストール後の起動検証で 2 件の packaging bug を修正した:
  - **アプリアイコン**: `tauri.conf.json` の `bundle` に `icon` 指定が無く、Tauri 組み込みのデフォルトアイコンが使われていた。`icons/*.png` / `icon.icns`（GuildBotics アイコン）を `bundle.icon` に明示した。
  - **sidecar 起動の ACL**: 当初 frontend が `Command.sidecar(...).spawn()` で sidecar を起動しており、capability が `shell:allow-execute` のみで `Command plugin:shell|spawn not allowed by ACL` になっていた。後述の lifecycle 再設計で sidecar 起動を Rust 側へ移したため、frontend からの shell spawn 自体を廃止し、capability は `shell:allow-open` のみに戻した（Rust 内からの sidecar spawn は ACL の対象外）。
- インストール後の再起動検証で sidecar lifecycle のバグを修正した（`ウインドウを閉じて再起動すると invalid_session_token になる`）:
  - **原因**: sidecar 起動が frontend にあり、(1) macOS ではウィンドウを閉じてもアプリが Dock に残り、JS spawn した sidecar がアプリ終了後も orphan 化する、(2) window 再表示のたびに新しいトークンで固定ポート 8765 に別 sidecar を起動しようとし、orphan とポート競合 → 古い sidecar が古いトークンで応答 → `invalid_session_token`。
  - **修正**: sidecar lifecycle を Rust（`desktop/src-tauri/src/lib.rs`）へ移管。アプリプロセスごとに 1 度だけ起動し、`RunEvent::Exit` で kill する。ポートは固定 8765 ではなく起動時に空きポートを動的取得し、orphan 衝突を回避する。frontend（`desktop/src/api/backend.ts`）は `backend_info` command で port / token を取得して既存バックエンドを再利用し、window 再表示で二重起動しない。workspace 切替は sidecar 再起動ではなく実行時 `POST /workspace` に統一した。
  - **PyInstaller onefile の orphan 対策**: onefile は bootloader が実 worker を子プロセスとして起動するため、bootloader だけを kill しても worker が port を握ったまま残る。sidecar に親 PID 監視 watchdog（`GUILDBOTICS_APP_API_PARENT_PID`、`guildbotics/app_api/server.py`）を追加し、Rust が渡したアプリ PID が消えたら worker 自身も終了するようにした。CLI 実行時は env 未設定のため watchdog は無効。
  - **実機検証**: 起動 → ウィンドウを閉じる → 再起動でバックエンド再利用（listen port 1 つ、`invalid_session_token` 解消）、通常終了で orphan ゼロ・ポート解放、アプリ SIGKILL（クラッシュ相当）でも watchdog が約 3 秒で sidecar を回収することを確認した。
- 起動 UX を 2 点改善した:
  - **起動中の白画面解消**: backend health 待ちの間（onefile cold start で約 10 秒）にこれまで白画面だったのを、`desktop/src/main.tsx` の `Bootstrap` でローダー + 起動メッセージを表示し、失敗時は理由と再試行ボタンを出すようにした。
  - **未設定時の初期画面**: 初期表示ルートを設定状態で分岐し、project config 未作成なら `サービス実行` ではなく `設定` 画面（`/setup`）を開くようにした（`desktop/src/App.tsx` の `IndexRedirect`）。fresh workspace で `config/status` の `primary/home_project_file_exists` がともに false のとき `/setup` に遷移することを実機確認した。

確認:

```bash
cd desktop
npm run tauri build -- --target aarch64-apple-darwin
```

### Session 10: CI と fallback 保証を整える

目的:

- GUI 追加後も CLI サポート環境を壊さないことを CI で保証する。

主な作業:

- 既存 CI に app API tests を含める。
- desktop build check を optional または別 workflow として維持する。
- Linux CI では GUI artifact を作らず、Python CLI と API importability を確認する。
- WeasyPrint native dependency がない環境で、PDF 以外の command registry が壊れない regression test を追加する。

完了条件:

- 通常 CI は Python package / CLI / Local API を検証する。
- desktop packaging は macOS workflow に分離されている。
- GUI 非対応環境でも CLI fallback が正式に守られる。

確認:

```bash
uv sync --extra test --extra dev
uv run --no-sync ruff format --check guildbotics
uv run --no-sync ruff check guildbotics
uv run --no-sync mypy guildbotics
uv run --no-sync python -m pytest tests/ --cov=guildbotics --cov-report=xml
```
