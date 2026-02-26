# チャットイベント実行基盤の再設計要件と実装ステップ（Slack Socket Mode 対応）

## 1. 目的

本ドキュメントは、現行のチャット会話機能（Slack Socket Mode を含む）で生じているアーキテクチャ上の歪みを整理し、より自然で保守しやすい構成へ移行するための要件定義と実装ステップをまとめたものです。

対象は以下です。

- `TaskScheduler`（定期実行）
- Slack Socket Mode による常時接続
- チャットイベントの取得・配送・状態管理
- `Person` 単位のワークフロー実行

## 2. 背景と現状の課題（実装ベース）

今回の実装は機能としては成立している一方、以下の歪みがある。

1. 駆動方式の混在
- `TaskScheduler` は cron / polling 前提の時刻駆動
- Slack Socket Mode は常駐接続のイベント駆動
- 周期実行器の内側に常駐スレッドを持ち込み、責務が混在している

2. 依存方向の逆転
- `runtime` の抽象や `Context` が `drivers` 配下の `ChatEventSource` 型に依存している
- 上位調停層（drivers）の都合が下位の抽象契約へ漏れている

3. ライフサイクル責務の侵入
- `Context` が Socket/Chat 接続資源のキャッシュと `aclose()` を持つ
- 本来の実行文脈の責務を超えて、接続資源の寿命管理まで抱えている

4. 接続単位の過分割
- Slack Socket 接続が `Person` 単位で張られやすい構造になっている
- 同一 Slack App / 同一 Workspace / 同一チャネルでも接続とスレッドが増える

5. エラー監視の分裂
- scheduler 側の連続エラー監視と socket reader 側の接続異常が別系統
- 障害の検知・停止・再起動ルールが一貫しにくい

## 3. 再設計の基本方針

1. 駆動方式を第一級概念として分離する
- 時刻駆動（cron / polling）とイベント駆動（socket / webhook）を別 runtime として扱う

2. 抽象は `runtime` / `integrations` 側に置き、`drivers` はそれを利用する
- `drivers` 配下の型を `runtime` の契約として参照しない

3. 接続経路と処理主体を分離する
- Socket 接続は Slack App 単位（または接続設定単位）で共有可能にする
- `Person` 単位では状態管理とワークフロー実行を行う

4. 実装ステップは「作業分割」のためにのみ段階化する
- 本機能は未リリースであるため、後方互換のための段階移行は原則不要
- 段階化は、実装・テスト・レビューをしやすくするための分割としてのみ採用する

5. CLI は統合、実行基盤は分離する
- `TaskScheduler` と `EventListenerRunner` はモジュールとして分離する
- 起動入口は原則 `guildbotics start` に統合し、オプションで起動対象を絞れるようにする

## 4. 目標アーキテクチャ（概要）

同レイヤ（Drivers 層）に、時刻駆動とイベント駆動の実行基盤を並列配置する。

- `TaskScheduler`: cron / polling 専用
- `EventListenerRunner`（新規）: 常駐接続・イベント処理専用

イベント駆動系の責務分担は以下を基本とする。

1. 接続層（共有）
- Slack Socket 接続の維持
- ACK
- 再接続 / バックオフ

2. 配送層（共有）
- 受信イベントの正規化
- チャネル / サービス / 条件による購読者（Person）への配送

3. 状態層（Person別）
- 既読管理
- 処理済みイベント管理
- 会話状態 / 重複防止

4. 実行層（Person別）
- `run_command()` を使った workflow 実行

## 5. 実装要件定義

## 5.1 機能要件

1. 実行基盤の分離
- 常駐イベント処理を `TaskScheduler` から分離した `EventListenerRunner` として実装する
- `TaskScheduler` は cron / routine 実行に専念する

2. プラガブルなイベントリスナー
- Slack Socket Mode を listener 実装として差し替え可能にする
- 将来の Webhook / 他チャットサービス listener を追加可能にする

3. 接続共有（重要）
- 同一 Slack App / Workspace / 接続設定であれば Socket 接続を共有できること
- `Person` ごとに別接続を強制しない
- 必要時のみ `Person` 単位接続（別 token / 別 app）を許容する

4. Person 別状態管理
- 処理済みイベント、既読位置、会話状態は `Person` ごとに分離する
- 同一イベントを複数 Person が購読する場合でも、それぞれ独立した処理状態を持てること

5. 既存 workflow 実行基盤の再利用
- `run_command()` / `CommandRunner` / `Context.pipe` / `Context.shared_state` を継続利用する
- イベント駆動でも既存のコマンド実行モデルを再利用する

6. 明確な停止・再起動制御
- listener ごとの起動/停止/再起動が可能
- runtime 停止時に接続・スレッド・キューが確実に解放される

## 5.2 非機能要件

1. 保守性
- 責務境界が明確で、`TaskScheduler` に Socket 実装詳細が漏れないこと
- `Context` に常駐接続資源の寿命管理を過度に持ち込まないこと

2. 拡張性
- 複数 listener の同時運用が可能であること

3. 可観測性
- listener 単位で接続状態、再接続回数、イベント受信数、配送数、失敗数を記録できること
- 障害箇所（接続/配送/workflow実行）をログで切り分けられること

4. 信頼性
- 接続障害時にバックオフ付き再接続ができること
- 1つの listener 障害が他 listener や scheduler 全体を巻き込まないこと

## 5.3 アーキテクチャ要件（依存関係）

1. 依存方向
- `runtime` は `drivers` 配下の具体型に依存しない
- `drivers` は `runtime` / `integrations` の抽象を利用する

2. 抽象の配置
- イベント受信・配送の抽象契約は `runtime` 側（または `integrations` 側のポート）に置く
- Slack Socket Mode 実装は `integrations/slack/` か `drivers/event_listeners/` に置く（どちらかを一貫採用）

3. `Context` の責務制限
- `Context` は workflow 実行文脈に集中する
- 常駐接続の所有・寿命管理は `EventListenerRunner` 側で持つ

4. イベント抽象の最終形
- 既存 `ChatEventSource` が持つ責務（受信・処理済み管理・周期確定）は分解する
- 最終的に、受信接続とイベント回収は `runtime/event_listener.py` の `EventListener` 抽象へ統一する
- `mark_processed` / `finalize_cycle` 相当の責務は `ConversationStateStore` と runner 側の処理制御へ移す
- `ChatEventSource` 抽象は恒久的に共存させない（移行途中の一時的存在は許容）

## 5.4 接続共有の要件（Slack Socket Mode）

`接続共有キー` は、受信Socket接続を再利用できるかどうかを判定するための実装上の識別子である。

主な用途:
- `EventListenerRunner` 内の接続レジストリで、既存 listener / 接続を再利用できるか判定する
- 複数 `Person` の購読設定を、共有可能な受信接続単位へグルーピングする

このキーは workflow の処理状態キーではない。既読/処理済み/返信抑止などの会話状態は、従来どおり `Person` 別に管理する。

1. 接続共有キー
- 接続共有キーは「設定値ベース」とする（起動前にグルーピング可能なため）
- 少なくとも `service=slack` と接続先（`base_url`）は考慮対象に含める
- `Person` は接続共有キーに含めない方針を基本とする

キー構成（推奨）:
- `service=slack`
- `event_source=socket_mode`
- `SLACK_APP_TOKEN`（接続共有判定の入力値。接続共有キー内部ではハッシュ化して保持し、ログ出力はマスクまたは短縮表示する）
- `base_url`

2. 接続共有の原則
- 同一共有キーなら 1 接続 + 複数購読者（Person）を基本とする

3. 受信と送信の責務分離（確定）
- 受信（Socket接続）は共有を基本とする
- 送信（投稿/リアクション）は `Person` 別クライアントを許容する
- 受信共有・送信個別の非対称構成を許可する

4. Person 別状態の維持
- 接続共有でも `ConversationStateStore` は Person 別データを保持する
- 既読・処理済み・返信抑止状態は共有しない

5. 配送ポリシー
- 同一イベントを複数 Person へ配送可能な `broadcast` を採用する

## 5.5 設定項目の分離方針（受信接続共有・送信クライアント個別）

受信は Slack Socket 接続を共有し、送信（投稿/リアクション）は `Person` ごとのクライアントを許容する構成を前提に、設定は「共有接続設定」と「Person別設定」に分離する。

1. 共有接続設定（Socket受信用）
- 対象: Slack App 単位の受信接続（Socket Mode）
- 目的: 接続共有判定、接続生成、再接続管理

必要な項目:
- `service`（`slack`）
- `event_source`（`socket_mode`）
- `SLACK_APP_TOKEN`（`xapp-...`）
- `base_url`（省略時は `https://slack.com/api`）

補足:
- チャネル購読一覧は共有接続設定に含めない（Person別の購読設定として扱う）

2. Person別設定（送信・購読・状態管理用）
- 対象: `Person` ごとの投稿主体、購読対象、応答方針
- 目的: workflow 実行時の振る舞いと state 分離

必要な項目:
- `SLACK_BOT_TOKEN`（`xoxb-...`、投稿/リアクション用）
- 購読チャネル設定（`channel_id` または `channel_name`）

追加項目（運用方針に応じて）:
- 応答ポリシー（mention時のみ、thread継続可否など）
- reaction 方針
- 監視条件（service/event_source の上書き）

3. 責務分離（確定方針）
- 受信（Socket接続）は共有接続設定を使って App 単位で管理する
- 送信（投稿/リアクション）は Person 別設定を使って実行主体ごとに管理する
- 既読/処理済み/返信抑止などの会話状態は Person 別で管理する

## 5.6 EventListenerRunner から workflow へのイベント受け渡し

イベントデータは `Context.shared_state` 経由で workflow に渡す。

方針:
- `EventListenerRunner` が受信イベントを構造化データとして `Context.shared_state` に設定してから `run_command()` を呼ぶ
- workflow はイベント取得（`fetch_events()` / `drain_events()`）を行わず、`shared_state` から入力イベントを読む
- イベント受信・接続共有・配送の責務は workflow へ持ち込まない

補足:
- `Context.pipe` は文字列の受け渡し用途であり、イベント本体の受け渡しには使わない
- `shared_state` の予約キー名（例: `incoming_event`）は実装時に固定し、workflow と runner で共通化する

## 5.7 スコープ（polling の扱い）

今回の実装対象は Slack Socket Mode を `EventListenerRunner` 配下へ再配置することに限定する。

方針:
- `PollingChatEventSource`（Web API polling）は今回 `EventListenerRunner` へ移さない
- polling は当面、`TaskScheduler` + 定期コマンド経由のまま継続する
- ただし、後から polling を listener 化できるように抽象配置・命名は阻害しない

## 6. 推奨コンポーネント構成（案）

## 6.1 Drivers 層

- `guildbotics/drivers/task_scheduler.py`（既存）
- `guildbotics/drivers/event_listener_runner.py`（新規）
- `guildbotics/drivers/event_supervisor.py`（初期スコープ外）

## 6.2 Runtime / Port 層（抽象）

- `guildbotics/runtime/event_listener.py`（抽象）
- `guildbotics/runtime/event_dispatcher.py`（抽象 or 小さな実装）
- `guildbotics/runtime/event_subscription.py`（購読定義モデル）

補足（確定方針）:
- イベント抽象（listener / subscription / dispatcher の契約）は `runtime` に置く
- `runtime` は `drivers` 配下のイベント型に依存しない

## 6.3 Integrations 層（実装）

- `guildbotics/integrations/slack/slack_socket_listener.py`（Socket接続実装）
- `guildbotics/integrations/slack/...`（Slack API / 正規化モデル）

## 6.4 状態管理

- `guildbotics/integrations/chat_state_store.py`（既存抽象を継続活用）
- `guildbotics/integrations/file_chat_state_store.py`（既存実装を継続活用）

## 7. 実装ステップ（作業分割のための段階化）

本章の Phase は「互換性維持のための移行計画」ではなく、実装を進めやすくするための作業分割である。

未リリース機能であるため、以下を原則とする。

- 互換コードは原則追加しない
- 現行チャット実装との並行運用は必須要件にしない
- 置換した方が簡潔なら、既存実装を直接置き換える
- `EventSupervisor` は初期スコープ外とし、`EventListenerRunner` 単体で成立させる

## 7.1 実装進捗（2026-02-25 時点）

この節は、設計ドキュメントを実装状況と同期させるための進捗メモである。

完了済み:
- `runtime` から `drivers` のイベント型依存を除去
- `EventListenerRunner` を追加し、`guildbotics start` から起動可能にした
- `guildbotics start --only scheduler|events` を実装した（未指定時は両方起動）
- Slack Socket の受信処理を `SlackSocketEventListener`（`EventListener` 実装）へ分離した
- `EventListenerRunner` が `Context.shared_state` 経由で workflow へイベントを渡す構成にした
- `chat_conversation_workflow` を `shared_state` 入力専用に整理した
- 接続共有キー（`SLACK_APP_TOKEN` ハッシュ + `base_url`）による listener 再利用を実装した
- `broadcast` 配送と Person 別 processed 判定/記録を `EventListenerRunner` 側で実装した
- listener 障害時の再接続テスト（`SlackSocketEventListener`）を追加した
- shutdown 時の listener 資源解放テスト（`EventListenerRunner._aclose_sources()`）を追加した
- `SocketModeChatEventSource` の互換テストを Socket プロトコル詳細からラッパー責務中心へ整理した

意図的に残している互換レイヤー:
- `SocketModeChatEventSource`
  - 旧 `ChatEventSource` 契約の利用箇所およびテスト互換のために残す
  - 内部実装は `SlackSocketEventListener` を利用する互換ラッパーとする
  - 現時点の直接参照箇所（棚卸し結果）:
    - `tests/guildbotics/drivers/test_chat_event_source.py`
    - `guildbotics/integrations/slack/slack_socket_mode_chat_event_source.py`（定義元）
  - 削除条件:
    - 本番コードが Socket Mode を `ChatEventSource` 経由で利用しない
    - 互換テストが `SlackSocketEventListener` / `EventListenerRunner` 側へ移った
    - Socket Mode の polling 互換経路が不要になった

未完（次段階の整理対象）:
- `ChatEventSource` 系の命名/配置整理（polling-oriented / compatibility 層としての位置づけをより明確化）
- `EventListenerRunner` の可観測性強化（メトリクス、接続状態詳細）
- `EventSupervisor`（初期スコープ外）

## Phase 0: 設計の明文化（この段階）

1. 本ドキュメントを起点に責務境界を合意する
2. `TaskScheduler` は時刻駆動専用、Socket は `EventListenerRunner` へ分離する方針を決める
3. 接続共有単位（Slack App 単位）の方針を決める

## Phase 1: 抽象の整理（依存逆転の修正）

1. `ChatEventSource` の責務を分解し、受信部分を `EventListener` 抽象へ置き換える
- 例: `runtime/event_listener.py`
- `ChatEventSource` をそのまま移設するのではなく、最終形の責務分離に合わせて整理する

2. `IntegrationFactory` / `Context` が `drivers` 型を参照しないよう修正する
- `runtime` は抽象契約だけを参照する

3. 既存コードの参照先を置換する
- workflow / tests / integrations の import を一括で更新してよい
- 互換 import や暫定 alias は、実装上必要な場合のみ導入する

完了条件:
- `runtime/*` が `guildbotics/drivers/*` のイベント型を import しない

実装状況:
- 完了

## Phase 2: EventListenerRunner の骨格追加（TaskScheduler とは別）

1. `EventListenerRunner` を新規追加する
- listener の起動/停止
- main loop
- 例外隔離
- shutdown 処理
- dispatcher（`broadcast` 配送）を内包する
- 公開インターフェースは `start() / stop() / join(timeout=None)` とする

2. listener 抽象インターフェースを定義する
- `start() / stop() / drain_events()`
- listener 内部実装は常駐スレッド + 内部キューを許容する

3. イベント -> workflow 実行の共通入口を実装する
- `run_command()` を再利用
- Person ごとに `Context` を生成して実行
- 入力イベントは `Context.shared_state` の予約キー経由で workflow に渡す

完了条件:
- `TaskScheduler` とは独立した常駐イベント runner が起動できる
- `EventSupervisor` なしで Slack listener を運用できる

実装状況:
- 完了（`guildbotics start --only events` で起動可能）

## Phase 3: Slack Socket Mode listener を EventListenerRunner 配下へ再配置する

1. 現在の Socket reader thread 実装を listener として移植する
2. ACK / 再接続 / バックオフ / キュー投入責務を listener に閉じる
3. workflow 側の `hasattr(...)/TypeError fallback` のような互換コードを削除する（不要なら導入しない）
4. polling 系実装（`PollingChatEventSource`）は本 Phase の対象外とする

完了条件:
- Slack Socket 処理が `TaskScheduler` / `Context` の特例処理に依存しない

実装状況:
- 概ね完了（Socket 受信は `SlackSocketEventListener` へ分離済み）
- `SocketModeChatEventSource` は互換ラッパーとして残存

## Phase 4: 接続共有の導入（App単位）

1. 接続共有レジストリを導入する
- 接続共有レジストリの所有者は `EventListenerRunner` とする（初期実装）
- キー: `service`, `event_source`, `SLACK_APP_TOKEN` ハッシュ, `base_url`
- 値: 共有 listener / connection session

2. Person 購読を listener に登録する形へ変更する
- `Person` は接続所有者ではなく購読者になる

3. Person 別状態ストアを維持したまま配送する
- 同一イベントを複数 Person に配送可能にする
- 重複処理は Person 別 state で抑止する

4. 送信クライアントの扱いを分離する
- 受信接続共有の有無に関わらず、投稿/リアクション用クライアントは `Person` 別を許容する

完了条件:
- 同一共有キーの複数 Person で Socket 接続数が 1 に集約される

実装状況:
- 完了（接続共有キー: `service`, `event_source`, `SLACK_APP_TOKEN` ハッシュ, `base_url`）
- 単体テストで同一キー再利用 / `base_url` 差分分離を確認済み

## Phase 5: 運用性改善（監視・CLI・テスト）

1. ログ/メトリクス追加
- 接続状態
- 再接続回数
- 受信/配送/処理失敗件数

2. CLI 起動方式の整理（任意）
- `guildbotics start` に scheduler と event listener runner の起動を統合する
- `--only scheduler` / `--only events` オプションで起動対象を絞れるようにする
- オプション未指定時は両方起動する
- 初期化に失敗した場合は全体を失敗として終了する（初期実装）
- 必要に応じて内部的な統合ランナー（軽量 supervisor）を導入する

実装状況:
- 一部完了（`guildbotics start` 統合起動 + `--only scheduler|events` 実装済み）
- 関連テスト追加済み（起動分岐、既定起動、シグナル時の停止呼び出し順）
- 軽量 supervisor の明示的な切り出しは未実施

3. テスト強化
- 接続共有
- Person 別状態保持
- listener 障害時の再接続
- shutdown 時の資源解放

## 8. テスト要件

1. 単体テスト
- 接続共有キー判定
- dispatcher の配送ロジック（broadcast）
- Person 別 state の独立性
- retry / backoff の基本動作

2. 結合テスト
- 1接続・複数Person購読
- 同一イベントを複数 workflow が処理できること
- runtime 停止時に listener thread / socket が解放されること
- `EventListenerRunner.dispatch_incoming_event()` が `Context.shared_state["incoming_event"]` 経由で実 workflow を実行できること

3. 回帰テスト
- `TaskScheduler` の既存 cron / routine 挙動が変わらないこと（チャット機能の再設計が既存機能へ波及しないこと）
- 既存チケット駆動 workflow が影響を受けないこと

## 8.1 実装タスクリスト（着手順）

以下は、実装者がそのまま着手できる粒度に分解した推奨タスク順である。

1. イベント抽象の移設（依存逆転の修正）
- 変更対象:
  - `guildbotics/drivers/chat_event_source.py`（抽象と共通モデルを移設/分割）
  - `guildbotics/runtime/`（新規: `event_listener.py`, `event_subscription.py` など）
  - `guildbotics/runtime/integration_factory.py`
  - `guildbotics/runtime/context.py`
  - `guildbotics/integrations/slack/slack_socket_mode_chat_event_source.py`（import 更新）
  - `guildbotics/templates/commands/workflows/chat_conversation_workflow.py`（import 更新）
  - 関連 tests
- 目的:
  - `runtime` が `drivers` 型に依存しない状態を先に作る

2. `EventListenerRunner` の最小骨格を追加
- 変更対象:
  - 新規 `guildbotics/drivers/event_listener_runner.py`
  - 必要なら `guildbotics/drivers/__init__.py` 等
- 実装内容:
  - `start() / stop() / join(timeout=None)`
  - 内部スレッド（またはループ）管理
  - listener 一覧保持
  - `broadcast` 配送の最小実装

3. listener 抽象の最小契約を追加
- 変更対象:
  - `guildbotics/runtime/event_listener.py`（新規）
- 実装内容:
  - `start()`
  - `stop()`
  - `drain_events()`
- テスト観点:
- Fake listener を作れる形になっていること
- `ChatEventSource` の責務分解後も workflow 入力契約（`shared_state`）が明確であること

4. Slack Socket listener を runner 契約へ合わせる
- 変更対象:
  - `guildbotics/integrations/slack/slack_socket_mode_chat_event_source.py`
  - 必要に応じてファイル名/クラス名の整理（listener 名へ寄せる）
- 実装内容:
- ACK / 再接続 / バックオフ / 内部キュー投入を listener 内に閉じる
- `drain_events()` を提供する
- `EventListenerRunner` へのイベント受け渡しを前提に、workflow から受信処理を切り離す

5. 接続共有レジストリを `EventListenerRunner` 内に実装
- 変更対象:
  - `guildbotics/drivers/event_listener_runner.py`
  - 必要なら `guildbotics/runtime/event_subscription.py`
- 実装内容:
  - `ConnectionKey`（`SLACK_APP_TOKEN` ハッシュ含む）
  - `dict[ConnectionKey, listener]`
  - `get_or_create` 相当の内部処理
  - `Person` を購読者として登録する処理
- テスト観点:
  - 同一キーで listener 再利用
  - 異なるキーで listener 分離

6. イベント -> workflow 実行経路の接続
- 変更対象:
  - `guildbotics/drivers/event_listener_runner.py`
  - `guildbotics/drivers/command_runner.py`（必要最小限の連携）
  - `guildbotics/templates/commands/workflows/chat_conversation_workflow.py`（runner 前提の調整）
- 実装内容:
- 受信イベントを `Person` ごとに `broadcast`
- `Context` 生成 -> `shared_state` に入力イベント設定 -> `run_command()` 実行
- 重複返信抑止は `Person` 別 state + policy ベースで扱う

7. CLI 統合（`guildbotics start`）
- 変更対象:
  - `guildbotics/cli/__init__.py`
  - 必要に応じて `main.py` / setup tool 周辺
- 実装内容:
  - `guildbotics start` で scheduler + event listener runner 起動（既定）
  - `--only scheduler|events`
  - 停止シグナルの停止伝播
  - 初期化失敗時は全体失敗

8. 互換コードの削除と整理
- 変更対象:
  - `guildbotics/templates/commands/workflows/chat_conversation_workflow.py`
  - `guildbotics/runtime/context.py`
- 実装内容:
  - `hasattr(...)/TypeError` fallback の削除
  - `Context` から listener 所有・寿命管理責務を削除

9. テスト追加・更新
- 変更対象:
  - `tests/guildbotics/runtime/...`
  - `tests/guildbotics/drivers/...`
  - `tests/guildbotics/integrations/slack/...`
  - `tests/guildbotics/templates/commands/workflows/...`
- 最低限の確認:
  - 接続共有キー判定（トークンハッシュ）
  - 1接続・複数Person購読
  - shutdown 時の資源解放
  - `guildbotics start --only events` / `--only scheduler` の起動経路

## 9. 受け入れ条件（Definition of Done）

1. `TaskScheduler` はチャット Socket 常駐処理を直接扱わない
2. `runtime` が `drivers` のイベント型に依存しない
3. 同一 Slack App の複数 Person で接続共有が成立する
4. Person 別既読・処理済み状態が維持される
5. 停止時に接続/スレッド/クライアントが正常解放される
6. 主要経路のテストが追加されている
7. `guildbotics start` から scheduler / events を起動でき、`--only` で片方だけ起動できる

## 10. 非ゴール（本リファクタでは扱わない）

1. 分散環境での厳密な排他制御
2. 複数プロセス間での接続共有
3. 高度な優先度ルーティング
4. 全チャットサービス共通の完全抽象化

## 11. 参考実装上の注意

1. `Context.pipe` / `shared_state` の互換性を壊さない
2. workflow 実行入口は `run_command()` を再利用し、新ランタイムでも実行モデルを増やしすぎない
3. 互換コードは原則追加しない。追加した場合のみ短命化し、削除条件を明示する
