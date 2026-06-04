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
- scheduler enabled で selected routine が `routine_commands` に入る。
- events only の場合に routine が送られない。
- both disabled の場合に start が disabled になる。
- config missing の場合に start が disabled になり setup link が出る。
- runtime starting / stopping / running / failed の表示。
- stop timeout pending の warning 表示。
- GitHub required routine で GitHub disabled の場合に start が blocked される。
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

- routine override off では shared default を使う説明を表示する。
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

- Service Runtime: config/team/scheduler/routines/project API を mock し、start/stop payload を検証する。
- Setup: config/project/member/intelligence API を mock し、初回 setup から member 追加までを検証する。
- Commands: command options / run / websocket event を mock し、history 更新まで検証する。

### P1: Browser-level smoke test

不足しているテスト:

- `npm run dev` で `/service`, `/commands`, `/diagnostics`, `/setup` が表示できる。
- mock API mode または dev token mode で backend 起動失敗画面と retry を確認する。
- 主要 viewport で sidebar / workspace が重ならない。

### P2: Tauri packaging smoke test

不足しているテスト:

- sidecar から `backend_info` を取得し、frontend が health check に到達する。
- workspace restore が packaged app で機能する。
- Tauri dialog / shell plugin を使う file picker / open path の smoke test。

## Python backend / CLI: Unit Test

### P0: `guildbotics/app_api/models.py`

不足しているテスト:

- `MemberTaskSchedule` が five-field cron だけを許可する。
- blank schedule を除外する。
- `SchedulerStartRequest.only` が `scheduler` / `events` 以外を拒否する。
- `SchedulerStartRequest.max_consecutive_errors` / `routine_interval_minutes` が 1 以上である。
- request/response model が Path を JSON へ安全に serialize する。
- `MemberConfigUpdateRequest` / `ProjectConfigUpdateRequest` の optional secret fields の空文字扱い。

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
  - default routines fallback。
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

### P1: `guildbotics/app_api/cli_agents.py`

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
  - `GET /scheduler/routines`。
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

### P2: Desktop + backend smoke

不足しているテスト:

- frontend dev server + Local API を起動し、browser test で `/service` の runtime status を表示する。
- setup page で temp workspace を入力して initial setup を作成する happy path。
- commands page で sample command を実行し output が history に出る。
- diagnostics page で verify result を表示する。

### P2: macOS Tauri smoke

不足しているテスト:

- Tauri app が sidecar を起動し `backend_info` を返す。
- packaged sidecar の first health response。
- app close 時に sidecar が終了する。
- file picker / open path の plugin integration。

## CI 上の今後の検討

- Python coverage threshold を module ごとに設定する。
- Desktop coverage を導入する場合は、最初は threshold を低めに置き、P0 領域追加に合わせて段階的に上げる。
- E2E は通常 push CI では最小 smoke のみにし、macOS/Tauri packaging smoke は workflow_dispatch または release workflow に寄せる。
- external service が必要なテストは default CI では skip し、契約 test / stub test を default に置く。

