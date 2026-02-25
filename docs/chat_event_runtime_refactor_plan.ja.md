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

4. 段階的移行を前提にする
- 既存 `TaskScheduler` / workflow を壊さずに、並行運用可能な構成で移行する

## 4. 目標アーキテクチャ（概要）

同レイヤ（Drivers 層）に、時刻駆動とイベント駆動の実行基盤を並列配置する。

- `TaskScheduler`: cron / polling 専用
- `EventRuntime`（新規）: 常駐接続・イベント処理専用

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
- 常駐イベント処理を `TaskScheduler` から分離した `EventRuntime` として実装する
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
- Slack 以外のチャットイベント入力（Webhook / queue / 他サービス）を追加しやすいこと
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
- 常駐接続の所有・寿命管理は `EventRuntime` 側で持つ

## 5.4 接続共有の要件（Slack Socket Mode）

1. 接続共有キー
- 少なくとも以下の組み合わせで接続共有可否を判定する
- `service=slack`
- `workspace/app identity`（実装上は token 由来 identity または app token）
- `base_url`

2. 接続共有の原則
- 同一共有キーなら 1 接続 + 複数購読者（Person）を基本とする

3. Person 別状態の維持
- 接続共有でも `ConversationStateStore` は Person 別データを保持する
- 既読・処理済み・返信抑止状態は共有しない

4. 配送ポリシー
- 同一イベントを複数 Person へ配送可能（broadcast）とする
- 将来的に router による単一配送（exclusive routing）を差し替え可能にする

## 6. 推奨コンポーネント構成（案）

## 6.1 Drivers 層

- `guildbotics/drivers/task_scheduler.py`（既存）
- `guildbotics/drivers/event_runtime.py`（新規）
- `guildbotics/drivers/event_supervisor.py`（任意、複数 listener 管理）

## 6.2 Runtime / Port 層（抽象）

- `guildbotics/runtime/event_listener.py`（抽象）
- `guildbotics/runtime/event_dispatcher.py`（抽象 or 小さな実装）
- `guildbotics/runtime/event_subscription.py`（購読定義モデル）

## 6.3 Integrations 層（実装）

- `guildbotics/integrations/slack/slack_socket_listener.py`（Socket接続実装）
- `guildbotics/integrations/slack/...`（Slack API / 正規化モデル）

## 6.4 状態管理

- `guildbotics/integrations/chat_state_store.py`（既存抽象を継続活用）
- `guildbotics/integrations/file_chat_state_store.py`（既存実装を継続活用）

## 7. 実装ステップ（段階的移行）

## Phase 0: 設計の明文化（この段階）

1. 本ドキュメントを起点に責務境界を合意する
2. `TaskScheduler` は時刻駆動専用、Socket は `EventRuntime` へ分離する方針を決める
3. 接続共有単位（Slack App 単位）の方針を決める

## Phase 1: 抽象の整理（依存逆転の修正）

1. `ChatEventSource` 相当の抽象を `drivers` 以外へ移設する
- 例: `runtime/event_listener.py`

2. `IntegrationFactory` / `Context` が `drivers` 型を参照しないよう修正する
- `runtime` は抽象契約だけを参照する

3. 既存コードの参照先を置換する
- workflow / tests / integrations の import を段階的に更新する

完了条件:
- `runtime/*` が `guildbotics/drivers/*` のイベント型を import しない

## Phase 2: EventRuntime の骨格追加（TaskScheduler とは別）

1. `EventRuntime` を新規追加する
- listener の起動/停止
- main loop
- 例外隔離
- shutdown 処理

2. listener 抽象インターフェースを定義する
- `start() / stop()`
- `poll_events()` または `recv()` 系（内部実装は常駐でも可）

3. イベント -> workflow 実行の共通入口を実装する
- `run_command()` を再利用
- Person ごとに `Context` を生成して実行

完了条件:
- `TaskScheduler` を変更せず、独立に EventRuntime が起動できる

## Phase 3: Slack Socket Mode listener を EventRuntime 配下へ移す

1. 現在の Socket reader thread 実装を listener として移植する
2. ACK / 再接続 / バックオフ / キュー投入責務を listener に閉じる
3. workflow 側の `hasattr(...)/TypeError fallback` の互換コードを削減する

完了条件:
- Slack Socket 処理が `TaskScheduler` / `Context` の特例処理に依存しない

## Phase 4: 接続共有の導入（App単位）

1. 接続共有レジストリを導入する
- キー: Slack App / base_url / 接続設定
- 値: 共有 listener / connection session

2. Person 購読を listener に登録する形へ変更する
- `Person` は接続所有者ではなく購読者になる

3. Person 別状態ストアを維持したまま配送する
- 同一イベントを複数 Person に配送可能にする
- 重複処理は Person 別 state で抑止する

完了条件:
- 同一共有キーの複数 Person で Socket 接続数が 1 に集約される

## Phase 5: 運用性改善（監視・CLI・テスト）

1. ログ/メトリクス追加
- 接続状態
- 再接続回数
- 受信/配送/処理失敗件数

2. CLI 起動方式の整理（任意）
- `guildbotics start` に event runtime 起動を統合するか
- `guildbotics start-events` のように分離するかを決定

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

3. 回帰テスト
- `TaskScheduler` の既存 cron / routine 挙動が変わらないこと
- 既存チケット駆動 workflow が影響を受けないこと

## 9. 受け入れ条件（Definition of Done）

1. `TaskScheduler` はチャット Socket 常駐処理を直接扱わない
2. `runtime` が `drivers` のイベント型に依存しない
3. 同一 Slack App の複数 Person で接続共有が成立する
4. Person 別既読・処理済み状態が維持される
5. 停止時に接続/スレッド/クライアントが正常解放される
6. 主要経路のテストが追加されている

## 10. 非ゴール（本リファクタでは扱わない）

1. 分散環境での厳密な排他制御
2. 複数プロセス間での接続共有
3. 高度な優先度ルーティング
4. 全チャットサービス共通の完全抽象化

## 11. 参考実装上の注意

1. `Context.pipe` / `shared_state` の互換性を壊さない
2. workflow 実行入口は `run_command()` を再利用し、新ランタイムでも実行モデルを増やしすぎない
3. 段階移行中の互換コードは短命にし、削除条件（Phase完了条件）を明示する

