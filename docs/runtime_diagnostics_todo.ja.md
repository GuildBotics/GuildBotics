# Runtime Diagnostics TODO

## 目的

GuildBotics の GUI 診断機能を、単なるイベント/ログ表示ではなく、トラブルシューティングに使える実行追跡基盤へ発展させる。

トラブルシューティングで必要なのは、「過去に誰が、いつ、何を実行し、それに関連してどの処理・ログ・LLM/CLI 呼び出し・エラーが発生したか」を一連の足跡として辿れることである。

現在の GUI は `イベント`、`ログ`、`プロンプトトレース` を別々に表示しているが、これらを同じ実行単位で結び付ける相関 ID と UI が不足している。

## 実装状況（実装完了 / 手動動作確認待ち）

決定事項に沿って Step 1〜5 を実装済み。`request_id` は廃止し `trace_id` に一本化した。主要な実装の所在は次のとおり。

- 相関コア: `guildbotics/observability/__init__.py`（`trace_scope` / `span_scope` / `correlation_fields` / `set_attributes`）
- イベント/ログ統一スキーマ＋store 連携: `guildbotics/app_api/events.py`
- 永続化＋クエリ: `guildbotics/observability/diagnostics_store.py`、API は `GET /diagnostics/traces`・`GET /diagnostics/traces/{id}`・`GET /diagnostics/global`
- trace 発番: 手動コマンド `guildbotics/app_api/runtime.py`、サービス起動 `guildbotics/app_api/lifecycle.py`（`service_run_id`）、巡回 `guildbotics/drivers/task_scheduler.py`、イベントリスナー受信 `guildbotics/drivers/event_listener_runner.py`
- プロンプトトレース相関: `guildbotics/utils/prompt_trace.py`、`agno_agent.py` / `cli_agent.py`（`span_scope` + `call_id`）
- 診断 UI「実行履歴」タブ: `desktop/src/App.tsx`（`TraceExplorer`）、`desktop/src/api/client.ts`、`desktop/src/i18n.ts`

### 後続の整理（経路の単純化）

- **ログ経路を1本化**: 暫定の `command.log` イベント（`CommandEventLogHandler`）を廃止。ログは `EventBusLogHandler` 経由の `kind=log`（`trace_id` 付き）のみとし、コマンド実行画面は `/logs` を `trace_id` で絞ってインライン表示する（`buildCommandTimeline`）。
- **`storage/error.log` を削除**: 構造化 ERROR ログ＋`command.failed` イベントで代替済み（`drivers/utils.py` の `write_error_log` を撤去）。
- **`ticket_driven_workflow` のアドホックなエラーログを撤去**: `_write_task_error_log`（ローカルファイルへ二重書き）と、そのローカルパスをチケットコメントへ貼る挙動を廃止。失敗時はトレースバックを再送出で `command runner` がログ（trace 内 ERROR ログ）に残し、チケットコメントは安全なペルソナ口調メッセージのみにした。
- **チケット相関 + 検索 UX**: ticket workflow で trace に `github.url`/`github.number`/`github.repo`/`github.kind` を `set_attributes` で付与。診断 `/diagnostics/traces` に**構造化属性の完全一致フィルタ**（`attr_key`/`attr_value`、汎用 `q` とは別経路）を追加。UI は統合検索欄で `#42`/`owner/repo#42`/URL を exact 照合し、通常検索語は `q` として併用できる。失敗 trace のチケットチップ→クリックで横断絞り込み＋アクティブフィルタのピル、で「番号→trace」動線を実装。
- **LLM / AI CLIツールフィルタをログにも拡張**: `span_scope` の名前をレコードに乗せ（`correlation_fields().span`）、ブレインの span を自身のログ出力も含む範囲へ拡張。フィルタは「prompt_trace の `llm.*`/`cli_agent.*`」に加え「`span` が一致する `kind=log`」も対象（AI CLIツールのロガー出力は `LOG_LEVEL=DEBUG` で出力される）。
- **Runtime debug 切り替え**: `GET/PUT /runtime/debug` と UI 共通部品を追加し、サービス実行画面・コマンド実行画面から `LOG_LEVEL` と `AGNO_DEBUG` を同時に ON/OFF できるようにした。`.env` と現在プロセスの `os.environ`、`guildbotics` / `agno` logger level を同期する。
- **Global / システム ビュー**: 旧「実行ストリーム」タブを撤去し、サービスイベント（`scheduler.*`/`events.*`）と非trace ログを `GET /diagnostics/global` ＋「実行履歴」先頭の固定エントリ（`source=すべて` 時のみ表示）へ統合。
- **trace 単位の手動削除を撤去**: `DELETE /diagnostics/traces/{id}` と「実行履歴」の削除ボタン／確認 UI を廃止。ストアは既にメモリ件数上限（`deque(maxlen)`）＋ファイルサイズ上限ローテーションで自動的に枯れるため、個別削除は不要。加えて、手動削除を残すと別ファイルの prompt-trace レコードが消し残り trace が prompt-only として再出現する不整合が生じる（削除自体を無くすことで根治）。保持は retention に一本化。

以降の「背景」2 節は実装前の状況・課題であり、本実装で解消済み（経緯の記録として残す）。「実装方針」の各 Step に完了状況を反映し、未了分は「残作業」にまとめた。

## 背景：実装前の現状整理（解消済み）

### イベント

`guildbotics/app_api/events.py` の `publish_event()` により `WS /events` へ流れる。

現在イベントに出る主な情報:

- コマンド実行
  - `command.started`
  - `command.log`
  - `command.finished`
  - `command.failed`
- サービス実行
  - `scheduler.starting/running/stopping/stopped/failed`
  - `events.starting/running/stopping/stopped/failed`

コマンド実行では `guildbotics/app_api/runtime.py` が `request_id` を生成し、開始/終了/失敗イベントに付与している。コマンド実行中の logger 出力も `CommandEventLogHandler` により `command.log` イベントとして `request_id` 付きで流れる。

一方、自動巡回やチャット対応などのサービス実行イベントには、サービス起動単位、巡回サイクル単位、チャット受信単位を表す共通 ID がない。

### ログ

`guildbotics/app_api/events.py` の `publish_log()` により `WS /logs` へ流れる。

GUI 起動時に `EventBusLogHandler` が `guildbotics` logger に追加され、通常ログをログストリームへ流している。ただし、この経路では通常 `request_id` が付かない。

このため、現在のログ一覧は「アプリ全体・サービス内部・診断処理などで発生した通常ログ」を見る場所であり、特定の実行単位に紐づけて辿る用途には弱い。

### プロンプトトレース

`guildbotics/utils/prompt_trace.py` が JSONL に記録し、GUI はそれを読み込んで表示している。

現在記録される主な情報:

- LLM request/response
- AI CLIツール request/response
- チャット応答入力

ただし、LLM/AI CLIツールの trace payload には `person_id`、`brain`、`model`、`prompt` などは含まれるが、コマンド実行の `request_id` やサービス実行のサイクル ID といった共通相関 ID は保証されていない。

## 背景：実装前の課題（本実装で解消済み）

以下は実装前に存在した課題であり、いずれも本実装で対応済み（解消の所在は「実装状況」「実装方針」を参照）。

### 1. 相関 ID が不足している

現在の `request_id` は主にコマンド実行にだけ存在する。

サービス実行には以下を識別する ID がない。

- サービス起動操作
- 自動巡回の各サイクル
- チャット受信イベント
- チャット応答ワークフロー
- サービス実行内で発生した LLM/AI CLIツール呼び出し

このため、障害発生時に「この巡回サイクルで何が起きたか」「この Slack メッセージに対してどの LLM 呼び出しが発生したか」を安定して辿れない。

### 2. イベント、ログ、プロンプトトレースが分断されている

現在は画面上もデータ上も以下が別管理である。

- `WS /events`
- `WS /logs`
- prompt trace JSONL

そのため、調査者は時刻や文面を手掛かりに手動で突き合わせる必要がある。

### 3. ログが構造化されていない

通常ログは `level`、`message`、`timestamp`、任意の `request_id` だけで構成されている。

トラブルシューティングに必要な以下の情報が安定して含まれない。

- 実行種別
- 対象メンバー
- コマンド名
- ワークフロー名
- サービス対象 (`scheduler` / `events`)
- Slack/GitHub など外部イベントの識別子
- 親子関係

### 4. UI が実行単位中心になっていない

現在の診断 UI は「イベント」「ログ」「プロンプトトレース」を横断的に眺める構造であり、特定の実行を選んで関連情報を集約表示する構造ではない。

トラブルシューティング目的では、まず調査対象となる実行単位を選び、その中の時系列を追える UI が望ましい。

## 決定事項（確定）

設計方針は以下で確定済み。旧「未確定課題」はすべて本セクションで解決する。

### 相関 ID モデル

- `trace_id`: 調査単位となる「1 回の有界な処理」を表す。trace 根は必ず**閉じる単位**に揃える（無期限に開き続ける単位を根にしない）。
- `span_id`: trace 内の個別処理（workflow / LLM / AI CLIツール呼び出しなど）を表す。
- `parent_id`: span の親を表す。

`request_id` は**廃止する**（互換 alias も残さない）。コマンド実行・巡回・チャット・診断のすべてを `trace_id` に一本化し、最もシンプルな姿にする。

#### trace 根（`trace_id` を発番する単位）

| 実行単位 | `source` | 主な属性 |
| --- | --- | --- |
| 手動コマンド実行 1 回 | `manual` | `person_id` |
| 巡回 routine / scheduled コマンド 1 回 | `routine` / `scheduled` | `person_id`, `service_run_id` |
| イベントリスナー受信イベント 1 件 | `event_listener` | `person_id`, `event.provider`, `slack.channel`, `slack.thread_ts`, `slack.ts` |
| 診断実行 1 回 | `diagnostics` | `person_id` |

補足:

- 自動巡回はメンバーごとに独立スレッド・独立ループで動く（`TaskScheduler` が thread name = `person_id` で起動）。したがって trace はメンバー単位で分かれ、`person_id` は属性、サービス起動 1 回ぶんは `service_run_id` でまとめる。
- サービス起動全体やチャットスレッド全体は「閉じない単位」なので trace 根にしない。これらの連続性は属性＋横断検索で担保する。
- trace 内の workflow / LLM / AI CLIツール呼び出しは span とし、`parent_id` で親子関係を持つ。

### グルーピング属性と横断検索キー

連続性・集約は `trace_id` ではなく**属性**で表現し、横断検索（UI）のキーにする。

| 属性 | 意味 | 横断検索でできること |
| --- | --- | --- |
| `service_run_id` | サービス起動 1 回 | 1 起動セッション内の全 trace を集約（巡回セッション → サイクルのドリルダウン） |
| `person_id` | 対象メンバー | メンバー単位で実行を絞り込む |
| `event.provider` | イベント提供元（Slack / GitHub など） | イベントリスナー起動 trace を提供元別に集約 |
| `slack.thread_ts` | チャットスレッド | スレッド単位で会話の全メッセージ trace を集約 |
| `slack.channel` / `slack.ts` | チャンネル / メッセージ | 特定メッセージを特定する |
| `github.repo` | リポジトリ | リポジトリ単位で集約する |
| `github.issue` | チケット | サイクル・日付をまたいで同一チケットに触れた全 trace を集約 |
| `github.pull_request` | PR | 同一 PR に触れた全 trace を集約 |

横断検索は最低限 `trace_id` / `person_id` / `source` / command / workflow / 上記外部属性 / エラー文字列をキーにする。

### 通常ログの扱い

unscoped（trace に紐づかない）ログを許容する。

- ログ出力時、contextvar に現在の `trace_id` / `span_id` があれば自動付与する。
- 無い場合（アプリ起動・診断・バックグラウンド等）は `Global` / `Unscoped` として扱う。
- UI では Global ログを分離し、trace タイムラインを汚さない。全ログの trace 紐付けは強制しない。

### プロンプトトレースの機密性

- 保存先は既存 storage（`.guildbotics`、gitignore 済）に限定し、ローカルのみ・外部送信しない。
- 取得は現状どおり `GUILDBOTICS_PROMPT_TRACE` の opt-in を維持する。
- 永続化レコードでは prompt / response 本体を `call_id` 参照の重量ペイロードとして骨格（event / log）から分離保存する。機密ペイロードの抑制はストアの自動ローテーション／件数上限（retention）に委ね、trace 単位の手動削除 API/UI は提供しない（一般的な観測ツールでも個別削除は稀で、自動上限管理で十分なため）。
- 初期は自動マスキングを入れない（ローカル開発ツール前提）。GUI に閲覧 / エクスポートを用意し、保持期間・サイズ上限で露出を抑える。

## 目指す状態（構造化レコード）

イベント、ログ、プロンプトトレースを同じ相関 ID と統一スキーマで検索・集約できるようにする。

各レコードには最低限以下を持たせる。

- `timestamp`
- `kind`（`event` / `log` / `prompt_trace`）
- `trace_id`
- `span_id`
- `parent_id`
- `source`（`manual` / `routine` / `scheduled` / `event_listener` / `diagnostics` / `llm` / `cli_agent` など）
- `level` または `type`
- `message`
- `person_id`
- `command`
- `workflow`
- 属性（`service_run_id`, `slack.*`, `github.*` など）
- `payload`

prompt_trace の本体（prompt / response）は `call_id` で参照し、別ペイロードとして分離保存する。

### UI

診断画面は「種類別に眺める」だけでなく、「実行単位を起点に辿る」構造へ寄せる。

想定 UI:

1. 実行一覧
   - 手動コマンド実行
   - 自動巡回セッション（`service_run_id`）→ サイクル trace のドリルダウン
   - チャット受信イベント
   - 診断実行
2. 実行詳細
   - 関連イベント、ログ、プロンプトトレースを時系列で統合表示
   - `ERROR` / `WARN` / `LLM` / `AI CLIツール` / `イベント` / `ログ` でフィルタ
   - 各行から payload や prompt / response の詳細を開ける
3. 横断検索
   - `trace_id`
   - `person_id`
   - `source`
   - command / workflow
   - `github.repo` / `github.issue` / `github.pull_request`
   - `slack.thread_ts`
   - エラー文字列
4. `Global` / `Unscoped` ログの分離表示

## 実装方針（Phase 1〜5 一括実行）

本基盤は段階リリースではなく一括実装する前提のため、旧 Phase 1（暫定 UI 対処）は**実施しない**。旧 Phase 1 の UI 項目は新 UI（Step 5）へ吸収し、`activeRequestId`（現 `activeTraceId`）誤紐付けは Step 2 の `trace_id` 導入で根本解決した。

実装は「スキーマ → 計装 → 永続化 → UI」の順で行った（粒度・属性・機密などの方針は上記「決定事項」で確定済み）。各 Step の完了状況を反映する（`[x]` 完了 / `[ ]` 未了は「残作業」参照）。

### Step 1: 統一スキーマ確定

- [x] 「目指す状態（構造化レコード）」のスキーマ（`kind` / `trace_id` / `span_id` / `parent_id` / `source` / 属性 / `call_id` / `payload`）を `guildbotics/observability` と `events.py` で定義。
- [x] event / log / prompt_trace がこのスキーマ（相関フィールド）を共有することを保証。

### Step 2: 相関 ID の発番と伝搬

- [x] `request_id` を廃止し、`trace_id` / `span_id` / `parent_id` へ置き換え（`events.py`, `runtime.py`, desktop GUI）。
- [x] `run_command`（手動 / routine / scheduled）の各実行で `trace_id` を発番し、`source` を付与。
- [x] `lifecycle.py` のサービス起動に `service_run_id` を付与。
- [x] `task_scheduler.py` の各メンバー巡回実行を trace 根にし、`person_id` / `service_run_id` を属性化。
- [x] `event_listener_runner.py` の受信イベントを trace 根にし、`event.provider` / `slack.*` 属性を付与。
- [x] `contextvars` で現在の `trace_id` / `span_id` を保持し、logger 出力・prompt trace から自動参照。
- [x] 通常 logger 出力に現在の相関 ID を自動付与（無ければ `trace_id=None` の Unscoped レコード）。

### Step 3: プロンプトトレース統合

- [x] `prompt_trace.py` の payload に `trace_id` / `span_id` / `parent_id` / `call_id` / `source` を含める。
- [x] `AgnoAgent` / `CliAgent` の request / response を span として記録し、`call_id` で request↔response を対応付け。
- [x] チャット応答入力、LLM、AI CLIツール、command event を同じ trace 詳細に統合表示できるデータ化（query 時に `trace_id` でマージ）。

### Step 4: 永続化

- [x] event / log を統一スキーマで永続化（`diagnostics_store.py`、`run/diagnostics.jsonl`）。
- [x] prompt / response は別ファイル（`prompt_trace.jsonl`）に保持し、`trace_id` / `call_id` で query 時にマージ。独立に無効化・パス切替・閲覧が可能（既存 prompt-trace 設定を踏襲）。
- [x] 最大ファイルサイズ上限＋ローテーション、メモリ件数上限を実装（※時間ベースの保持期間ポリシーは未実装）。
- [x] 再起動後も参照できるクエリ API（`GET /diagnostics/traces`・`/traces/{id}`・`/logs`）。
- [ ] 機密ペイロードのエクスポート専用 API（閲覧＝詳細 drawer・prompt-trace 無効化は実装済み。エクスポートは未提供。trace 単位の手動削除はストアの自動ローテーション／件数上限で代替し、API/UI は提供しない方針へ変更）。

### Step 5: 診断 UI 再設計

- [x] 実行一覧（手動コマンド / 巡回（`service_run_id`）/ チャット受信 / 診断）を「実行履歴」タブに追加。
- [x] 選択した `trace_id` の統合タイムライン（event / log / prompt_trace 混在）を表示。
- [x] 種別フィルタ＋横断検索（`source` / `q`（command・workflow・`github.*`・`slack.thread_ts`・エラー文字列を横断）/ `person_id`）。
- [x] 各行の詳細 drawer で payload / prompt / response / error / metadata（属性・相関 ID）を表示。
- [x] `Global` / `Unscoped` 表示を「実行履歴」タブに統合（先頭固定の「Global / システム」エントリ）。サービスライフサイクルイベント（`scheduler.*` / `events.*`）と非trace ログを `GET /diagnostics/global` で表示。
- [x] タイムラインを降順（新しい→古い）表示にし、ポーリング更新時にスクロール不要に。
- [x] 旧「実行ストリーム」タブを撤去（固有価値だったサービスイベント／Global ログを上記 Global ビューへ吸収）。
- [x] サービス実行画面・コマンド実行画面から runtime debug（`LOG_LEVEL=DEBUG` + `AGNO_DEBUG=true`）を切り替える UI を追加。
- [ ] コマンド実行画面・サービス実行画面から該当 trace の診断詳細へ遷移する導線（未実装）。

## 残作業（手動動作確認後に対応）

- コマンド実行画面・サービス実行画面 → 該当 trace の「実行履歴」詳細への遷移導線。
- 機密ペイロードのエクスポート機能、時間ベースの保持期間ポリシー。
- 巡回セッション（`service_run_id`）→ サイクル trace のドリルダウン UI（現状は `service_run_id` 属性での横断検索で代替）。
- GUI での手動動作確認 → 問題なければコミット。
