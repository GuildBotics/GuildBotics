# Runtime Diagnostics TODO

## 目的

GuildBotics の GUI 診断機能を、単なるイベント/ログ表示ではなく、トラブルシューティングに使える実行追跡基盤へ発展させる。

トラブルシューティングで必要なのは、「過去に誰が、いつ、何を実行し、それに関連してどの処理・ログ・LLM/CLI 呼び出し・エラーが発生したか」を一連の足跡として辿れることである。

現在の GUI は `イベント`、`ログ`、`プロンプトトレース` を別々に表示しているが、これらを同じ実行単位で結び付ける相関 ID と UI が不足している。

## 現状の整理

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
- CLI Agent request/response
- チャット応答入力

ただし、LLM/CLI Agent の trace payload には `person_id`、`brain`、`model`、`prompt` などは含まれるが、コマンド実行の `request_id` やサービス実行のサイクル ID といった共通相関 ID は保証されていない。

## 現在の問題

### 1. 相関 ID が不足している

現在の `request_id` は主にコマンド実行にだけ存在する。

サービス実行には以下を識別する ID がない。

- サービス起動操作
- 自動巡回の各サイクル
- チャット受信イベント
- チャット応答ワークフロー
- サービス実行内で発生した LLM/CLI Agent 呼び出し

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

## 目指す状態

### 相関 ID モデル

全ての実行に共通の相関 ID を付ける。

候補:

- `trace_id`: 調査対象となる一連の実行全体を表す ID
- `span_id`: その中の個別処理を表す ID
- `parent_id`: 親処理を表す ID

少なくとも初期実装では、以下のような対応を目指す。

| 実行単位 | 必要な ID |
| --- | --- |
| コマンド実行 | `trace_id` または既存 `request_id` の昇格 |
| サービス起動 | `service_run_id` |
| 自動巡回 1 サイクル | `cycle_id` |
| チャット受信 1 件 | `message_event_id` |
| ワークフロー実行 | `workflow_run_id` |
| LLM/CLI Agent 呼び出し | `span_id` + 親 ID |

既存 `request_id` は廃止せず、GUI/API 上の互換性を保ちながら `trace_id` へ統合または alias 化する。

### 構造化イベント/ログ

イベント、ログ、プロンプトトレースを同じ相関 ID で検索・集約できるようにする。

各レコードには最低限以下を持たせる。

- `timestamp`
- `kind` (`event` / `log` / `prompt_trace`)
- `trace_id`
- `span_id`
- `parent_id`
- `source` (`command` / `scheduler` / `chat` / `llm` / `cli_agent` / `diagnostics` など)
- `level` または `type`
- `message`
- `person_id`
- `command`
- `workflow`
- `payload`

### UI

診断画面は「種類別に眺める」だけでなく、「実行単位を起点に辿る」構造へ寄せる。

想定 UI:

1. 実行一覧
   - コマンド実行
   - 自動巡回セッション
   - 自動巡回サイクル
   - チャット受信イベント
   - 診断実行
2. 実行詳細
   - 関連イベント、ログ、プロンプトトレースを時系列で統合表示
   - `ERROR` / `WARN` / `LLM` / `CLI Agent` / `イベント` / `ログ` でフィルタ
   - 各行から payload や prompt/response の詳細を開ける
3. 横断検索
   - `trace_id`
   - `request_id`
   - `person_id`
   - command/workflow
   - エラー文字列

## 実装 TODO

### Phase 1: 現状 UI の誤解を減らす

- [ ] 実行ストリームの `イベント` と `ログ` の説明を画面内またはヘルプに追加する。
- [ ] `ログ` タブの `REQUEST` 列を削除するか、request ID が存在する場合のみ表示する。
- [ ] `イベント` タブの `command.log` が「コマンド実行中のログ」であることを分かりやすくする。
- [ ] `コマンド実行` 画面の実行結果と診断画面のイベントを相互に辿れる導線を検討する。
- [ ] `コマンド実行` 画面 (`desktop/src/App.tsx` の `CommandsPage`) の `activeRequestId` が、手動実行と無関係な `command.started`（スケジューラ等）でも書き換わる問題を整理する。
  - 影響: 表示中の実行が別実行に奪われる、`onError` のエラーが別 request に誤紐付けされる（PR #180 のレビュー指摘）。
  - 現状はサーバ側失敗時に websocket レコードへマージして重複を防ぐため `activeRequestId` に依存しており、フロント単独では「手動実行の request か」を判別できない。
  - 根本解決は Phase 2 の相関 ID 導入（手動実行を識別できる `trace_id` / クライアント相関トークン、`onError` での実 ID 取得）に依存する。Phase 1 では問題の所在を明確化し、手動実行スコープに限定する暫定 UI 対応の要否を判断する。

### Phase 2: 相関 ID の導入

- [ ] `guildbotics/app_api/events.py` の event/log record に `trace_id`、`span_id`、`parent_id` を追加する。
- [ ] `guildbotics/app_api/runtime.py` のコマンド実行で、既存 `request_id` と `trace_id` の関係を定義する。
- [ ] `guildbotics/app_api/lifecycle.py` のサービス起動に `service_run_id` を付与する。
- [ ] `guildbotics/drivers/task_scheduler.py` の巡回サイクルに `cycle_id` を付与する。
- [ ] `guildbotics/drivers/event_listener_runner.py` のチャット受信イベントに `message_event_id` または `event_id` を付与する。
- [ ] context propagation の方法を決める。
  - 候補: `Context` に diagnostics context を持たせる。
  - 候補: `contextvars` を使って logger / prompt trace から現在の相関 ID を参照する。
- [ ] 通常 logger 出力に現在の相関 ID を自動付与する。

### Phase 3: プロンプトトレースとの統合

- [ ] `guildbotics/utils/prompt_trace.py` の payload に相関 ID を含める。
- [ ] `AgnoAgent` / `CliAgent` の request/response trace に `trace_id`、`span_id`、`parent_id` を渡す。
- [ ] request と response の対応関係を安定して表現する。
  - 現状は timestamp と順序に依存しやすい。
  - `call_id` の導入を検討する。
- [ ] チャット応答入力、LLM、CLI Agent、command event を同じ実行詳細で表示できるようにする。

### Phase 4: 永続化

- [ ] イベント/ログ/プロンプトトレースを同じ保存基盤で扱うか、既存 JSONL を拡張するかを決める。
- [ ] アプリ再起動後も一定期間の診断情報を参照できるようにする。
- [ ] 保存期間、最大サイズ、ローテーション方針を決める。
- [ ] 機密情報の扱いを決める。
  - prompt/response には secrets や個人情報が含まれる可能性がある。
  - GUI 上の表示/エクスポート/削除機能が必要。

### Phase 5: 診断 UI の再設計

- [ ] 診断画面に「実行一覧」を追加する。
- [ ] 実行一覧から選択した `trace_id` の統合タイムラインを表示する。
- [ ] イベント/ログ/プロンプトトレースを 1 つのタイムラインへ混在表示する。
- [ ] 種別フィルタ、重要度フィルタ、検索を追加する。
- [ ] 各行の詳細 drawer で payload、prompt、response、error、metadata を表示する。
- [ ] コマンド実行画面の request id から診断詳細へ遷移できるようにする。
- [ ] サービス実行画面の自動巡回/チャット対応カードから、該当 service run の診断詳細へ遷移できるようにする。

## 未確定課題

### 相関 ID の粒度

`trace_id` をどの粒度で切るかが未確定である。

候補:

- コマンド実行 1 回 = 1 trace
- 自動巡回サービス起動 1 回 = 1 trace、各サイクル = span
- 自動巡回サイクル 1 回 = 1 trace
- チャット受信 1 件 = 1 trace

トラブルシューティングの入口として何を一覧表示したいかに合わせて決める必要がある。

### `request_id` と `trace_id` の関係

既存 GUI/API では `request_id` がコマンド実行 ID として使われている。これを `trace_id` に置き換えるか、`request_id` を互換 alias として残すかを決める必要がある。

### 通常ログの扱い

通常ログを全て trace に紐づけるべきか、紐づかない global log を許容するかを決める必要がある。

紐づかないログを残す場合、UI では `Global` や `Unscoped` として扱う必要がある。

### プロンプトトレースの機密性

プロンプトトレースは調査価値が高い一方、機密情報を含みやすい。保存・表示・削除・エクスポートのポリシーを決める必要がある。

### 外部連携イベントの ID

Slack、GitHub など外部イベントの ID をどのように保持し、trace と結び付けるかを整理する必要がある。

## 短期判断

現在の GUI タスクでは、診断基盤の全面再設計までは行わない。

ただし、今後の UI 修正では以下を前提にする。

- `イベント` は実行単位の状態変化を中心に扱う。
- `ログ` は通常ログであり、現状では request 単位の追跡には向かない。
- `プロンプトトレース` は LLM/CLI Agent 呼び出しの詳細であり、現状ではイベント/ログとの相関が不完全である。
- `REQUEST` 列など、相関追跡ができるように見えるが実際にはできない UI 表現は避ける。

