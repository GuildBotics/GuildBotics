# テスト不足リスト

このドキュメントは、テストピラミッドの考え方に基づき、現時点で不足しているテストを整理する。対象は Python backend / CLI / command runtime と `desktop/` frontend の両方とする。

基本方針:

- Unit test を最も厚くする。純粋関数、入力変換、validator、状態遷移、エラー変換を高速に検証する。
- Component / service integration test は、ユーザー操作や API endpoint の境界で主要 workflow を検証する。
- E2E test は少数に絞り、backend と frontend の接続、起動、主要 happy path / critical failure path だけを検証する。
- LLM や外部 SaaS への実通信は通常テストに入れない。既存の抽象化や stub を使い、契約・payload・エラー処理を検証する。
- snapshot だけに依存しない。ユーザーが観測する状態、生成される request、永続化される file/env、publish される event を assertion する。

## 現在のテスト状況

Python 側:

- `tests/guildbotics/...` には entities、utils、drivers、commands、templates/workflows、integrations、CLI、app_api のテストがある。
- `guildbotics/app_api` は `tests/guildbotics/app_api/test_api.py` と `test_verify.py` があるが、endpoint と runtime service の全体に対しては薄い。
- command runner / scheduler / setup service は一部テスト済みだが、desktop API 経由の config 更新、runtime lifecycle、event streaming、prompt trace 設定などは不足している。

Desktop 側:

- `desktop/src/i18n.test.ts` と `desktop/src/App.test.tsx` がある。
- unit test は `i18n` と trace grouping のみ。
- component test は Service Runtime 画面の初期描画のみ。
- `SetupPage.tsx`、`CommandsPage`、`DiagnosticsPage`、`api/client.ts`、`api/backend.ts` は実質未テスト。

## 優先度の定義

- P0: regression すると GUI / CLI の主要機能が壊れる。次に実装するべき。
- P1: 頻繁に変更される、または分岐が多い。早期に追加するべき。
- P2: 重要だが変更頻度や影響範囲が限定的。P0/P1 の後で追加する。

## セッション実施計画（TODO）

このリストは、上記の不足テストを「1 セッション = 1 コンテキストウィンドウで完了できる単位」に分割したものである。分割方針:

- 対象ソースの近さ（共有コンテキスト）でまとめる。`App.tsx`(約2150行) / `SetupPage.tsx`(約4400行) / `runtime.py`(約1060行) は巨大なため複数セッションに分ける。
- Python track と Desktop track は独立しており、任意の順で（並行でも）進められる。
- Phase 0 (P0) → Phase 1 (P1) → Phase 2 (P2) の順を推奨。各 track 内は番号順だと依存（基盤 → 応用）が自然。
- 各セッションの完了条件は「該当テストの追加 + 関連品質チェックが green」。
  - Python: `uv run --no-sync ruff check guildbotics` / `mypy guildbotics` / `pylint guildbotics` / 該当 `pytest`
  - Desktop: `cd desktop && npm run format:check && npm run lint && npm run typecheck && npm run duplicates && npm run test`

進め方: 各セッション開始時にこのファイルを読み、担当セッションのチェックを完了時に `[x]` へ更新する。

### Phase 0 — P0（主要機能の regression 防止）

Python track:

- [x] **S1 — app_api models + events** ✅ `test_models.py`(24) + `test_events.py`(16) = 40 cases, green
  - 対象: `guildbotics/app_api/models.py`, `events.py`
  - 追加: `tests/guildbotics/app_api/test_models.py`, `test_events.py`
  - 範囲: 「Python backend / CLI: Unit Test」の `models.py`, `events.py` 節
- [x] **S2 — app_api lifecycle** ✅ `test_lifecycle.py` 20 cases, green
  - 対象: `guildbotics/app_api/lifecycle.py`
  - 追加: `tests/guildbotics/app_api/test_lifecycle.py`
  - 範囲: 「Unit Test」の `lifecycle.py` 節（status transition / only 選択 / stop 順序 / timeout / metadata）
- [x] **S3 — app_api runtime（設定・workspace 系）** ✅ `test_runtime_config.py` 19 cases, green
  - 対象: `guildbotics/app_api/runtime.py`
  - 追加: `tests/guildbotics/app_api/test_runtime_config.py`
  - 範囲: `runtime.py` 節のうち `get_config_status` / `set_workspace` / `get_team_summary` / prompt trace / `detect_cli_agents`
- [x] **S4 — app_api runtime（command 系）** ✅ `test_runtime_commands.py` 25 cases, green
  - 対象: `guildbotics/app_api/runtime.py`
  - 追加: `tests/guildbotics/app_api/test_runtime_commands.py`
  - 範囲: `runtime.py` 節のうち `get_command_options` / `run_command` / `start_scheduler`
- [x] **S5 — app_api intelligences** ✅ `test_intelligences.py` 16 cases, green
  - 対象: `guildbotics/app_api/intelligences.py`
  - 追加: `tests/guildbotics/app_api/test_intelligences.py`
  - 範囲: 「Unit Test」の `intelligences.py` 節
- [x] **S6 — api.py endpoint integration** ✅ `test_api.py` を 118 tests へ拡張（auth matrix / 500 / not-found / validation / conflict / POST /verify 等の不足ブランチを追加）, green
  - 対象: `guildbotics/app_api/api.py`（`TestClient`）
  - 追加/更新: `tests/guildbotics/app_api/test_api.py` を拡張
  - 範囲: 「API Integration Test」の `api.py` 節（auth / config / members / intelligences / scheduler / prompt trace / commands / diagnostics endpoint）
- [x] **S7 — websocket integration** ✅ 新規 `test_api_ws.py` 9 cases（success / invalid-token 1008 close / history replay / live delivery / disconnect cleanup, events+logs）, green
  - 対象: `guildbotics/app_api/api.py` の `/events` `/logs`
  - 追加: `tests/guildbotics/app_api/test_api_ws.py`
  - 範囲: 「API Integration Test」の websocket 節

Desktop track:

- [x] **S8 — api/client.ts + api/backend.ts** ✅ `client.test.ts`(24) + `backend.test.ts`(10) = 34 cases, green
  - 対象: `desktop/src/api/client.ts`, `backend.ts`
  - 追加: `desktop/src/api/client.test.ts`, `backend.test.ts`
  - 範囲: 「Desktop frontend: Unit Test」の `client.ts`, `backend.ts` 節
- [x] **S9 — SetupPage.tsx 純粋関数** ✅ 純粋関数を `export` 化し `SetupPage.test.tsx` に 95 cases 追加, green
  - 対象: `desktop/src/setup/SetupPage.tsx`（pure functions）
  - 追加/更新: `desktop/src/setup/SetupPage.test.tsx` を拡張
  - 範囲: 「Unit Test」の `SetupPage.tsx の純粋関数` 節（schema / parseGitHub / request 変換 / member errors / character / intelligence payload / patrol・schedule helpers）
- [x] **S10 — App.tsx 純粋関数（+ trace.ts / i18n.ts 拡充）** ✅ App 47 + trace 13 + i18n 8 = 68 cases 追加, green
  - 対象: `desktop/src/App.tsx`（pure functions）, `trace.ts`, `i18n.ts`
  - 追加/更新: `desktop/src/App.test.tsx`, `trace.test.ts`(新規 or 既存), `i18n.test.ts` を拡張
  - 範囲: 「Unit Test」の `App.tsx の純粋関数` 節 + `trace.ts`(P1) + `i18n.ts`(P1) 節
- [x] **S11 — Bootstrap + App routing + Service Runtime（component）** ✅ `Bootstrap.test.tsx`(5) + `ServiceRuntime.test.tsx`(17) = 22 cases, green
  - 対象: `desktop/src/Bootstrap.tsx`, `App.tsx`(routing / `ServiceRuntimeSection`)
  - 追加: `desktop/src/Bootstrap.test.tsx` ほか component test
  - 範囲: 「Component Test」の `Bootstrap` / `App routing` / `Service Runtime 画面` 節
- [x] **S12 — Commands + Setup（component）** ✅ `Commands.test.tsx`(11) + `SetupPage.test.tsx` の SetupPage component 群, 全 203 desktop tests green
  - 対象: `App.tsx`(`CommandsPage`), `setup/SetupPage.tsx`(`ProjectSection` ほか Setup 全体)
  - 追加: component test
  - 範囲: 「Component Test」の `Commands 画面` / `Setup 画面` 節
- [x] **S13 — Members editor + Patrol settings（component）** ✅ `SetupPage.test.tsx` に Members 12 + Patrol 8 = 20 cases 追加, desktop 223 tests green
  - 対象: `setup/SetupPage.tsx`(`MembersSection`, `PatrolSettingsEditor`)
  - 追加: component test
  - 範囲: 「Component Test」の `Members editor` / `Patrol settings` 節
- [x] **S14 — API client + component integration（mock server, P0）** ✅ `integration/ApiIntegration.test.tsx` 7 cases（実 client.ts を fetch/WS 境界モックで検証）, desktop 230 tests green
  - 対象: Service Runtime / Setup / Commands と mock API の結合
  - 追加: integration test（mock server）
  - 範囲: 「Integration / E2E」の `API client + component integration` 節

### Phase 1 — P1（変更頻度・分岐が多い領域）

Python track:

- [x] **S15 — verify / diagnostics + cli_agents** ✅ `test_verify.py`(25) + `test_cli_agents.py`(25) + 新規 `test_diagnostics.py`(22) = 72 tests, green
  - 対象: `guildbotics/app_api/verify.py`, `diagnostics.py`, `cli_agents.py`
  - 追加/更新: `tests/guildbotics/app_api/test_verify.py` 拡張 + `test_diagnostics.py`, `test_cli_agents.py`
  - 範囲: 「Unit Test」の `verify.py / diagnostics.py`, `cli_agents.py` 節
- [x] **S16 — command runner / command specs 拡充** ✅ `test_command_chain.py` 22 cases, green
  - 対象: `guildbotics/drivers/command_runner.py`, `guildbotics/commands/*`
  - 追加/更新: `tests/guildbotics/...` の該当テスト
  - 範囲: 「Unit Test」の `command runner / command specs` 節（pipe/shared_state 順序、child failure、localized fallback 等）
- [x] **S17 — setup service / config write 拡充** ✅ `test_setup_service_config_write.py` 16 cases, green
  - 対象: `guildbotics/editions/simple/setup_service.py`
  - 追加/更新: 該当テスト
  - 範囲: 「Unit Test」の `setup service / config write` 節
- [x] **S18 — real temp project integration + Local API sidecar smoke** ✅ `test_api_integration.py`(4) + `test_sidecar_smoke.py`(4) = 8 cases, green
  - 対象: `api.py` end-to-end（temp workspace） + `python -m guildbotics.app_api` 起動
  - 追加: integration / smoke test
  - 範囲: 「API Integration Test」の `real temporary project integration` 節 + 「E2E / packaging」の `Local API sidecar smoke` 節

Desktop track:

- [x] **S19 — Intelligence editor + Diagnostics（component）** ✅ `SetupPage.test.tsx` に Intelligence 9 + 新規 `Diagnostics.test.tsx` 12 = 21 cases, desktop 251 tests green
  - 対象: `setup/SetupPage.tsx`(`IntelligenceEditor`), `App.tsx`(`DiagnosticsPage`)
  - 追加: component test
  - 範囲: 「Component Test」の `Intelligence editor` / `Diagnostics 画面` 節
- [x] **S20 — Playwright E2E 基盤 + 初回 setup journey** ✅ `playwright.config.ts` + `e2e/start-stack.mjs`(実backend+vite harness) + `e2e/setup.spec.ts`(journey①, chromium green, `project.yml` 実書き込み検証), vitest 251 維持
  - 対象: 実ブラウザ E2E 基盤（Playwright）+ journey① 初回 setup happy path
  - 追加: `desktop/playwright.config.ts`, `desktop/e2e/` 雛形（`webServer` で Local API を temp workspace + token 付き起動 → `/health` 待ち → vite frontend、browser-preview token mode で接続）, 専用 CI ジョブ骨子, journey①（作成後に backend が `project.yml` を実書き込みしたことまで確認）
  - 範囲: 「Integration / E2E」§`Playwright E2E（lean-but-real journeys）` の harness + journey①
- [x] **S21 — Service Runtime + Commands journeys** ✅ `e2e/service.spec.ts`(journey③ start→running→stop + scheduler GitHub guard) + `e2e/commands.spec.ts`(journey④ 実 `/commands/run`+`/events` ストリーム→history Success), harness を setup/configured 2スタックに分離, e2e 5 specs green
  - 対象: 実 backend + 実 websocket 相手の runtime / commands フロー
  - 追加: journey③ scheduler start(scheduler/events/both)→running→stop, journey④ command 実行→`/events` ストリーム→history 表示
  - 範囲: 同節 journey③④
- [x] **S22 — member追加 + diagnostics + 起動失敗 journeys** ✅ `e2e/members.spec.ts`(journey② 追加→`person.yml` 実永続) + `e2e/diagnostics.spec.ts`(journey⑤ 実 scenario 実行→結果描画) + `e2e/failure.spec.ts`(journey⑥ backend down→Bootstrap error→control server で復帰→retry, 決定論的), e2e 全6 journey green×2
  - 対象: member 反映 / diagnostics 描画 / backend ダウン時の回復
  - 追加: journey② member 追加→team 反映, journey⑤ verify・scenario 実行→結果描画, journey⑥ backend down→Bootstrap error→retry
  - 範囲: 同節 journey②⑤⑥

### Phase 2 — P2（影響範囲が限定的・packaging 系）

- [ ] **S23 — Tauri ネイティブ最小ティア（tauri-driver, workflow_dispatch 想定）** ⏸ 保留（実 OS + Tauri runtime が必要）
  - 対象: Tauri sidecar `backend_info` / workspace restore / file picker（dialog/shell plugin）/ packaged sidecar の first health / app close 時の sidecar 終了
  - 追加: tauri-driver/WebDriver smoke（通常 push CI ではなく workflow_dispatch / release workflow 側）
  - 範囲: 「Integration / E2E」§`Tauri ネイティブ smoke（tauri-driver）` + 「E2E / packaging」§`macOS Tauri smoke`

E2E 方針（lean-but-real）: ブラウザ E2E は「振る舞いパターン総当たり」ではなく、実 `client.ts ↔ FastAPI ↔ EventBus(ws)` を貫く **critical journey** に絞る。分岐網羅は S9〜S19 のコンポーネント/統合層に委ね、ピラミッドを維持する。E2E は push CI から隔離し専用ジョブで回す。

合計 23 セッション（Phase 0: 14 / Phase 1: 8 / Phase 2: 1）。1 セッションが大きすぎると感じた場合は、対象節の小見出し単位でさらに分割してよい。

## Desktop frontend: Unit Test

### P0: `desktop/src/api/client.ts`

不足しているテスト:

- `configureApi()` が token と base URL を正しく保持する。
- 全 request が `X-GuildBotics-Session-Token` と JSON body を正しく送る。
- GET 系 endpoint が query parameter を正しく encode する。
  - `getPromptTrace(limit, path)`
  - `runScenarioDiagnostics(personId)`
  - `getIntelligenceConfig(personId)`
  - `getCommandOptions(person)`
  - `getMemberConfig(personId)`
- non-2xx response で `ApiRequestError` に `code`, `message`, `context` が入る。
- backend が JSON ではない error response を返した場合の fallback。
- validation error payload の context が壊れず保持される。
- `subscribeEvents()` / `subscribeLogs()` が websocket URL、token、status transition、message parse、close cleanup を正しく処理する。
- `websocketBase()` が `http://` を `ws://`、`https://` を `wss://` に変換する。

### P0: `desktop/src/api/backend.ts`

不足しているテスト:

- `VITE_GUILDBOTICS_API_TOKEN` がある browser preview mode で、Tauri invoke を呼ばず `configureApi()` と health check を実行する。
- Tauri runtime で `backend_info` を呼び、返された port/token を使う。
- Tauri でも static token でもない場合に明確な error を返す。
- `waitForHealth()` が一時的な fetch 失敗から retry して成功する。
- `waitForHealth()` が deadline 超過時に最後の error を含めて失敗する。
- `restartBackend(workspace)` が localStorage と backend workspace を更新する。
- `restoreWorkspace()` が workspace 復元失敗時に localStorage を掃除し、起動自体を壊さない。
- `setWorkspace()` 失敗時に `guildbotics.workspace` が削除される。

### P0: `desktop/src/setup/SetupPage.tsx` の純粋関数

不足しているテスト:

- `createProjectSchema()`
  - workspace 未入力。
  - description 未入力。
  - GitHub decision 未選択。
  - GitHub disabled では GitHub URL validation を要求しない。
  - GitHub enabled では project URL / repository URL を検証する。
  - repository owner と project owner の mismatch。
- `parseGitHub()`
  - organization project URL。
  - user project URL。
  - repository URL。
  - invalid URL。
  - owner / repository / project_id 抽出。
- `initialProjectValues()`
  - config 未作成時の初期値。
  - workspace config / home config / custom config。
  - 既存 project config と hidden API key placeholder の扱い。
- `toProjectSetupRequest()` / `toProjectUpdateRequest()`
  - env file option。
  - GitHub disabled 時に GitHub fields を送らない。
  - repo access が https / ssh で変換される。
  - 空の API key は既存 secret を消さない。
- `getMemberFieldErrors()`
  - member id / display name / roles / speaking style / character fields。
  - human は GitHub auth 不要。
  - machine_user / proxy_agent は access token を要求。
  - github_apps は installation id / app id / private key path を要求。
  - Slack channel / bot token / app token validation。
- `getMemberResolveErrorMessage()`
  - API error code ごとの表示。
  - unknown error fallback。
- `buildCharacterPayload()` / `parseCharacterFields()`
  - list field の trim / empty line removal。
  - extra character fields の保持。
  - malformed character object。
- `toIntelligenceUpdatePayload()`
  - team default update。
  - member override update。
  - inherit team defaults。
  - savePersonId による person_id 差し替え。
- patrol / schedule helpers
  - `createScheduledCommandDraft()`
  - `buildTaskSchedules()`
  - `buildScheduledCommandExpression()`
  - `parseCommandExpression()`
  - `draftToCron()`
  - `parseCron()`
  - `isValidCron()`
  - `splitCommandLine()` / `quoteCommandArg()`
  - catalog command と custom command の round trip。

### P0: `desktop/src/App.tsx` の純粋関数

不足しているテスト:

- Commands:
  - `buildCommandArgs()` が positional / keyword / default / raw args を正しく組み立てる。
  - `splitCommandLine()` が quote / escape / 空白を扱う。
  - `upsertCommandRecord()` が既存 request を更新し、新規 request を先頭に追加する。
  - `commandFailureDetail()` が payload の code / message / error を優先順どおり表示する。
  - `formatCommandEvent()` が command event 種別ごとの文言を返す。
- Diagnostics / runtime:
  - `matchesFeedFilter()` と `matchesLogFilter()`。
  - `formatRuntimeEvent()`。
  - `eventTypeLabel()` / `eventBadgeColor()` / `logBadgeColor()`。
  - `isStopTimeoutPending()`。
- Prompt trace:
  - `decodeTraceText()`。
  - `traceGroupMetadata()` / `traceFieldRows()`。
  - `traceBrainLabel()`。
  - request/response/single event の表示 fallback。
- File helpers:
  - `localFileHref()` が absolute / relative / Windows path を扱う。
  - Tauri runtime でない場合に `openLocalFile()` / `selectTraceFile()` が no-op になる。

### P1: `desktop/src/trace.ts`

既存テストはあるが不足しているテスト:

- request が response より前にある場合の扱い。
- 同じ person / brain / model で複数 request/response が混在する場合。
- model / cli_agent field が欠落した場合。
- `llm`, `cli_agent`, `chat`, unknown event の kind 分類。
- parse error や single event が request/response pair と混ざる場合。

### P1: `desktop/src/i18n.ts`

既存テストはあるが不足しているテスト:

- localStorage が invalid 値の場合に navigator language へ fallback する。
- navigator language も unsupported の場合に `en` へ fallback する。
- `setAppLanguage()` が localStorage と i18next state の両方を更新する。
- `resources` に `en` / `ja` のキー欠落がないこと。

## Desktop frontend: Component Test

### P0: `Bootstrap`

不足しているテスト:

- 起動中 loading 表示。
- `startBackend()` 成功時に `App` を表示する。
- `startBackend()` 失敗時に error alert と retry button を表示する。
- retry で loading に戻り、再成功できる。
- unmount 後の async completion で state update しない。

### P0: `App` routing / layout

不足しているテスト:

- config 未作成時に `/setup` へ redirect する。
- config 作成済み時に `/service` へ redirect する。
- sidebar nav が route と active state を反映する。
- language select が `setAppLanguage()` を呼ぶ。
- `/overview` が `/service` に redirect する。

### P0: Service Runtime 画面

不足しているテスト:

- scheduler / events target toggle が `startScheduler()` の `only` payload に反映される。
- scheduler enabled でも Service Runtime から `routine_commands` を送らない。
- events only の場合に routine が送られない。
- both disabled の場合に start が disabled になる。
- config missing の場合に start が disabled になり setup link が出る。
- runtime starting / stopping / running / failed の表示。
- stop timeout pending の warning 表示。
- interval / max consecutive errors の入力値が payload に反映される。
- start / stop mutation error の alert。

### P0: Commands 画面

不足しているテスト:

- active member がない場合の blocked 表示。
- catalog command 選択時の argument form rendering。
- positional / keyword argument 入力から `runCommand()` payload を作る。
- raw args mode / custom command mode。
- selected member が `person` に入る。
- cwd advanced input が payload に入る。
- command requirements が unsatisfied の場合に run disabled。
- `command.started` / `command.finished` / `command.failed` event で history が更新される。
- command logs が request id で紐付く。
- script path copy / open button の browser/Tauri 分岐。

### P0: Setup 画面

不足しているテスト:

- 初回 setup の required progress。
- project section の workspace / language / config location / description。
- LLM provider / CLI agent 選択と API key availability。
- GitHub disabled / enabled の section completion。
- initial setup 作成時に `initConfig()` と `restartBackend()` が呼ばれる。
- 既存 project では autosave が `updateProjectConfig()` を呼ぶ。
- autosave validation error では API を呼ばない。
- section back/next navigation。
- URL query `section=members&tab=patrol` の初期表示。

### P0: Members editor

不足しているテスト:

- member 0 件時に add form を表示する。
- add member happy path。
- edit member happy path。
- delete member confirmation。
- draft member mode と persisted member mode の差分。
- GitHub identity resolve。
- person type 切替で必要な credential field が変わる。
- human member は GitHub auth 不要。
- machine_user / proxy_agent / github_apps の validation。
- Slack token / channel validation。
- character preset apply / clear。
- roles loading error。
- member diagnostics tab。

### P0: Patrol settings

不足しているテスト:

- routine override off では、その member は巡回実行しない説明を表示する。
- routine override on で routine 未選択なら validation error。
- scheduled command 追加 / 削除。
- catalog command の args 編集。
- custom command の raw args 編集。
- hourly / daily / weekly / custom cron の切替。
- invalid cron で保存不可。
- `task_schedules` payload への変換。

### P1: Intelligence editor

不足しているテスト:

- team default の model / cli / brain mapping 編集。
- member override の default model / CLI agent 編集。
- inherit team defaults の切替。
- env JSON validation。
- external save mode で parent に save callback を登録する。
- auto save mode で debounce して保存する。
- save error 表示。
- CLI agent detection badge。

### P1: Diagnostics 画面

不足しているテスト:

- readiness diagnostics の実行。
- scenario diagnostics の warning / error / ok rendering。
- prompt trace read path apply / reset / pick。
- prompt trace output settings toggle / path update。
- runtime stream の event/log tab。
- event/log filtering。
- websocket status badge。
- trace details drawer。

## Desktop frontend: Integration / E2E

### P0: API client + component integration with mock server

不足しているテスト:

- Service Runtime: config/team/scheduler/project API を mock し、start/stop payload を検証する。
- Setup: config/project/member/intelligence API を mock し、初回 setup から member 追加までを検証する。
- Commands: command options / run / websocket event を mock し、history 更新まで検証する。

### P1: Playwright E2E（lean-but-real journeys）

方針: Vitest + jsdom のコンポーネント/統合テスト（S9〜S19, S14）が分岐網羅を担うのに対し、ここでは **実ブラウザ engine + 実 Local API バックエンド**でしか検証できない領域に絞る。具体的には実 `client.ts ↔ FastAPI ↔ EventBus(websocket)` のワイヤ契約、実 setup がバックエンドで実ファイルを書く一気通貫、実 DOM のレイアウト/フォーカス/タイミング。振る舞いパターンの総当たりはしない（コンポーネント層に委譲しピラミッドを維持）。

実行基盤（harness）:

- `desktop/playwright.config.ts` の `webServer` で Python Local API を temp workspace + `--token` 付きで起動し、`/health` 待ちの後に Vite frontend を起動する。
- frontend は `VITE_GUILDBOTICS_API_TOKEN`（browser-preview mode, Tauri 非依存）で実 backend に接続する。
- 各テストは隔離した temp workspace を使い、終了時に backend/前面プロセスを確実に停止する。
- 通常 push CI からは隔離し、専用ジョブ（workflow_dispatch / nightly / label gate）で実行する。

カバーする critical journey（各 journey に必要最小の動作バリエーションのみ）:

1. 初回 setup happy path: 空 temp workspace → project 入力 → 作成 → `/service` へ遷移。**backend が `project.yml` を実書き込み**したことを確認。
2. member 追加: setup の members から追加 → `/team`（または members 一覧）に反映される。
3. Service Runtime: scheduler / events / both で start → 実 websocket で running 状態が反映 → stop で stopped。
4. Commands: sample command 実行 → 実 `/commands/run` + `/events` 経由で output が history にストリーム表示される。
5. Diagnostics: verify / scenario diagnostics を実行 → 結果（ok/warning/error）が描画される。
6. critical failure: backend ダウン状態 → Bootstrap の error 表示 → backend 復帰後の retry で起動できる。

### P2: Tauri ネイティブ smoke（tauri-driver）

不足しているテスト（packaged app / ネイティブ bridge 固有。実 OS + Tauri runtime が必要で workflow_dispatch / release workflow 側に隔離）:

- sidecar から `backend_info` を取得し、frontend が health check に到達する。
- workspace restore が packaged app で機能する。
- Tauri dialog / shell plugin を使う file picker / open path の smoke test。
- packaged sidecar の first health response、app close 時に sidecar が終了する。

## Python backend / CLI: Unit Test

### P0: `guildbotics/app_api/models.py`

不足しているテスト:

- `PersonTaskScheduleInput`（`guildbotics/editions/simple/setup_service.py`）が five-field cron だけを許可する。
- blank schedule を除外する。
- `SchedulerStartRequest.only` が `scheduler` / `events` 以外を拒否する。
- `SchedulerStartRequest.max_consecutive_errors` / `routine_interval_minutes` が 1 以上である。
- request/response model が Path を JSON へ安全に serialize する。
- `ProjectConfigUpdateRequest` / `ProjectUpdateInput` の optional secret fields の空文字扱い（`""` と `None` を区別して既存 secret を保持する）。

> 注: 旧版にあった `MemberTaskSchedule` / `MemberConfigUpdateRequest` は実装に存在しない。上記の実モデルで読み替える（S1 で確認済み）。

### P0: `guildbotics/app_api/events.py`

不足しているテスト:

- EventBus が history_limit を守る。
- subscribe 時に history を replay する。
- close 後の subscriber に publish されない。
- event と log の history が分離される。
- background thread から publish した item が async subscriber に届く。
- `EventBusLogHandler` / `CommandEventLogHandler` が expected payload を publish する。

### P0: `guildbotics/app_api/lifecycle.py`

不足しているテスト:

- scheduler start が `starting` -> `running` event を publish する。
- events start が `starting` -> `running` event を publish する。
- start 済み target に再 start しても二重 thread を作らない。
- `only=scheduler` / `only=events` / both の選択。
- stop が events を先に止め scheduler を後で止める。
- stop timeout 時に `failed` + running true になる。
- context_factory が例外を投げた時に failed になる。
- scheduler thread 内例外が failed event と status に反映される。
- thread が自然終了した時の status refresh。
- metadata (`routine_commands`, `worker_count`, `cycle_count`, etc.) が更新される。

### P0: `guildbotics/app_api/runtime.py`

不足しているテスト:

- `get_config_status()`
  - workspace config がある場合。
  - home config fallback。
  - custom `GUILDBOTICS_CONFIG_DIR`。
  - config missing。
- `set_workspace()`
  - 存在しない path。
  - file path。
  - scheduler stop を呼ぶ。
  - `.env` を load する。
  - `cwd` を変更し config status を更新する。
- `get_team_summary()`
  - project language/name。
  - member roles sorted。
  - inactive member の扱い。
- `get_command_options()`
  - workspace / home / template / member-specific precedence。
  - localized file precedence (`.<lang>` -> `.en` -> base)。
  - Python command の signature から arguments を抽出する。
  - YAML / Markdown frontmatter arguments。
  - `requirements` の GitHub / Slack / CLI / LLM 判定。
  - invalid command metadata を無視して落ちない。
  - person not found error。
- `run_command()`
  - command.started / command.finished event。
  - command.failed event for person selection / person not found / command error / unexpected error。
  - command log handler が request id 付き event を publish する。
  - concurrent run が 409 `command_already_running` になる。
  - failure 後に reservation が解放される。
  - request.cwd が command execution に渡る。
- `start_scheduler()`
  - GitHub disabled で GitHub required routine を拒否する。
  - custom routine requirement 判定。
  - routine 未指定時に default routines fallback を行わない。
- prompt trace
  - `get_prompt_trace_status()` が limit / read_path を反映する。
  - `update_prompt_trace()` が `.env` と `os.environ` を更新する。
  - trace path 空文字で env key を削除する。
  - `.env` 既存 key / comment / order の扱い。
- `detect_cli_agents()`
  - template mapping 読み込み失敗 fallback。
  - script から executable を解決する。
  - PATH にある/ない場合。

### P0: `guildbotics/app_api/intelligences.py`

不足しているテスト:

- team config がない場合に template fallback する。
- member override がない場合に inherited true になる。
- member override がある場合に inherited false になる。
- model mapping から重複 model file を一度だけ読む。
- malformed YAML / missing file fallback。
- team update が model / CLI agent / brain mapping files を書く。
- member override update が member scoped files だけを書く。
- `inherit_team_defaults` が member scoped intelligences を削除する。
- env object が dict でない場合の fallback。
- CLI agent detected path。
- runtime cache clear が呼ばれる。

### P1: `guildbotics/app_api/verify.py` / `diagnostics.py`

不足しているテスト:

- config file missing。
- env file missing。
- no active members。
- multiple active members。
- LLM provider ごとの API key。
- CLI mapping missing / CLI definition missing / executable missing / executable found。
- GitHub disabled / enabled。
- GitHub Apps credential set / missing。
- machine_user / proxy_agent token set / missing。
- Slack credential set / missing。
- git repository / branch / clean dirty state。
- scenario diagnostics の person_id 指定。
- context construction failure。

### P1: `guildbotics/intelligences/cli_agents.py`

不足しているテスト:

- `resolve_cli_executable()` が plain command / env prefix / quoted command / shell option を扱う。
- `load_cli_agent_script()` が missing file / malformed YAML / template fallback を扱う。
- CLI agent mapping と executable name の対応。

### P1: command runner / command specs

既存テストはあるが不足しているテスト:

- `Context.pipe` と `shared_state` の更新順序を複数 command chain で検証する。
- child command failure 時の parent command の扱い。
- YAML command の `commands:` が空 / invalid / nested の場合。
- Markdown command の frontmatter options。
- Python command の async / sync main。
- Shell command の cwd / env / stderr / non-zero exit。
- inline `print`, `to_html`, `to_pdf` の chain 内動作。
- person-specific command と common command の fallback。

### P1: setup service / config write

既存テストはあるが不足しているテスト:

- project init で workspace/home/custom config location ごとの file set。
- `.env` skip / append / overwrite。
- secret の空文字更新で既存 secret を消さない。
- GitHub disabled -> enabled -> disabled の差分。
- repo access https / ssh。
- member add/update/delete で env secret key が追加/更新/削除される。
- member id rename 時に directory / env key / mapping が移動する。
- task_schedules / routine_commands の YAML round trip。
- character / relationships / speaking_style の round trip。

## Python backend / CLI: API Integration Test

### P0: `guildbotics/app_api/api.py`

既存 `test_api.py` に加えて不足している endpoint test:

- auth:
  - 全 HTTP endpoint が token 必須。
  - invalid token が 401。
  - request validation error が unified `ApiError` になる。
  - unexpected error が 500 `internal_error` になる。
- config:
  - `POST /workspace` の success / not found / not directory。
  - `GET /config/roles?language=en|ja`。
  - invalid language が 422。
  - `POST /config/init` success / setup service error。
  - `GET /config/project` not found / success。
  - `PUT /config/project` success / setup service error。
- members:
  - `POST /config/members` が既存 config_dir/env_file を runtime status から補正する。
  - `GET /config/members/{person_id}` not found / success。
  - `PUT /config/members/{person_id}` person id mismatch が 400。
  - `DELETE /config/members/{person_id}`。
  - `POST /config/members/resolve` GitHub Apps URL / username / service error。
- intelligences:
  - `GET /config/intelligences` team / member。
  - project not found。
  - `PUT /config/intelligences` success / setup service error。
- scheduler:
  - `GET /commands/routine-options`（routine 候補一覧と既定コマンド）。
  - `POST /scheduler/start` only scheduler / only events / both。
  - GitHub required routine rejection。
  - `POST /scheduler/stop`。
- prompt trace:
  - `GET /prompt-trace` limit min/max validation。
  - `GET /prompt-trace?path=...`。
  - `PUT /prompt-trace`。
- commands:
  - `GET /commands/options?person=...` success / person not found。
  - `POST /commands/run` success / command error / conflict。
- diagnostics:
  - `POST /verify`。
  - `POST /diagnostics/scenario?person_id=...`。
  - `GET /intelligences/cli-agents/detection`。

### P0: websocket integration

不足しているテスト:

- `/events?token=...` success。
- `/events` invalid token closes with policy violation。
- `/logs?token=...` success。
- history replay。
- published event/log is delivered to connected client。
- disconnect closes subscription。

### P1: real temporary project integration

不足しているテスト:

- temp workspace で `POST /config/init` -> `GET /config/project` -> `POST /config/members` -> `GET /team`。
- temp workspace で `GET /commands/options` が sample command を ensure し、localized command を返す。
- temp workspace で `PUT /config/intelligences` 後に files が実際に更新される。
- prompt trace `.env` 更新後に `GET /prompt-trace` が enabled/path を返す。

## E2E / packaging

### P1: Local API sidecar smoke

不足しているテスト:

- `python -m guildbotics.app_api --host 127.0.0.1 --port <free> --token <token>` を起動し `/health` を確認する。
- invalid token の 401。
- `/config/status` が working directory を返す。
- process shutdown が clean。

### P1: Desktop + backend journey（→「Playwright E2E」へ統合）

frontend dev server + 実 Local API を起動して browser でユーザーフローを検証する内容（`/service` runtime status、初回 setup happy path、command 実行→history、verify 結果描画）は、上の「Desktop frontend: Integration / E2E」§「Playwright E2E（lean-but-real journeys）」の journey ①③④⑤ に統合した。本節は重複のためそちらを正とする。

### P2: macOS Tauri smoke（→「Tauri ネイティブ smoke」へ統合）

Tauri app の sidecar 起動・`backend_info`・packaged sidecar の first health・app close 時の sidecar 終了・file picker / open path plugin integration は、上の §「Tauri ネイティブ smoke（tauri-driver）」に統合した。実 OS + Tauri runtime が必要なため workflow_dispatch / release workflow に隔離する。

## CI 上の今後の検討

- Python coverage threshold を module ごとに設定する。
- Desktop coverage を導入する場合は、最初は threshold を低めに置き、P0 領域追加に合わせて段階的に上げる。
- Playwright E2E（lean-but-real journeys）は push CI の lint/test ジョブから隔離し、専用ジョブ（workflow_dispatch / nightly / label gate）で実 backend + frontend を起動して回す。Tauri ネイティブ smoke は workflow_dispatch / release workflow に寄せる。
- external service が必要なテストは default CI では skip し、契約 test / stub test を default に置く。
