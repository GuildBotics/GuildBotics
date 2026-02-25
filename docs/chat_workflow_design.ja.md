# チャット会話ワークフロー設計メモ（Slack MVP / 将来拡張前提）

## 1. 目的

本ドキュメントは、GuildBotics に新たに追加する「チャット会話ワークフロー」の実装資料です。

対象はまず Slack のみとしつつ、将来的に他チャットシステム（Discord, Teams など）やイベント駆動起動へ拡張しやすい構成を前提に設計します。

## 2. 前提（現行実装ベース）

現行の GuildBotics は以下の構成で動作します。

- CLI 入口: `guildbotics/cli/__init__.py`
- 定期実行: `guildbotics/drivers/task_scheduler.py`
- ワークフロー実行基盤: `guildbotics/drivers/command_runner.py`
- コマンド種別: `guildbotics/commands/*` (`.md`, `.py`, `.sh`, `.yml/.yaml`, inline command)
- 実行コンテキスト: `guildbotics/runtime/context.py`
- チケット中心ワークフロー: `guildbotics/templates/commands/workflows/ticket_driven_workflow.py`

重要な既存特性:

- ワークフローは Python コマンドとして実装可能（`*.py` の `main()`）
- `Context.pipe` / `Context.shared_state` によるコマンド間連携がある
- Integration は `runtime/integration_factory.py` 経由で差し替え可能な構造
- スケジューラはメンバーごとに定期的に routine command を実行する方式

## 3. ゴール / 非ゴール

### 3.1 ゴール（MVP）

- Slack の特定チャネルを監視する
- 「自分が反応すべきメッセージ」に反応する
- 返信は可能ならスレッド返信で行う
- 必要に応じて返信の代わりにリアクションを返す
- 返信/リアクション判断時に、そのスレッド全体を読める
- 同一チャネル/スレッドに複数エージェントが参加できる
- cron ベースでチャネルに定期投稿できる（別ワークフロー）

### 3.2 非ゴール（初期段階ではやらない）

- すべての Slack 機能（ファイルアップロード、Block Kit 完全対応）
- イベント Webhook 方式の本実装（抽象は用意する）
- 他チャットサービス実装（抽象は用意する）
- 高度な分散ロック / 複数プロセス完全同期

## 4. 用語定義（本設計内）

- `ChatService`: チャットサービス依存部（Slack API など）を隠蔽する抽象
- `ChatEventSource`: 新着イベント供給方式（ポーリング / Webhook）を隠蔽する抽象
- `ShouldReactPolicy`: 「反応すべきか」を判定する抽象
- `ConversationStateStore`: カーソル・既読位置・処理済みイベントを保存する抽象
- `Conversation`: チャネル/スレッド単位の会話文脈（新規モデル候補）

## 5. MVP 挙動仕様（確定案）

### 5.1 監視対象

- 各エージェント（`Person`）ごとに、参加対象チャネルを設定する
- 同一チャネルに複数エージェントが参加可能

### 5.2 「反応すべきメッセージ」の初期定義（MVP）

以下のいずれかに該当するメッセージを対象とする。

1. 自分宛てメンションを含むメッセージ
2. 自分が既に参加したスレッドで、自分が反応すべき追加入力

MVPでの簡易ルール:

- 自分自身の投稿には反応しない
- 他エージェント宛てメンションのみの場合は反応しない
- 複数エージェント宛てメンションは、各エージェントが反応候補になり得る（競合回避ルールは後述）
- bot 投稿への bot 自動返信（bot-to-bot 会話）は MVP では原則無効（Phase 2 以降で解禁）

### 5.3 返信方法

- 元メッセージがスレッド外なら、スレッド返信として開始する（Slack が許せば thread reply）
- 元メッセージがスレッド内なら、そのスレッドに返信する

### 5.4 リアクション応答

返信の代わりにリアクションを返せる。

MVPで最低限用意する用途:

- 既読/了解（例: `eyes`, `white_check_mark`）
- 担当外または他エージェント対応中の通知（例: `hand`, `hourglass_flowing_sand`）
- 重複処理抑止の軽いアック

最終判断は LLM に委ねてもよいが、MVP ではまずルールベース優先とする（安全で再現性が高い）。

MVP ではリアクションの意味を固定する（実装・運用・テストを簡単にするため）。

推奨デフォルト（MVP 確定）:

- `ack` -> `eyes`
- `in_progress` -> `hourglass_flowing_sand`
- `not_my_turn` -> `hand`
- `done_without_reply`（任意） -> `white_check_mark`

実装上は `ShouldReactPolicy` が直接絵文字を返してもよいが、将来の差し替え性を考えると `reason -> reaction` の変換層（例: `ReactionResolver`）を分ける設計が望ましい。

### 5.5 スレッド履歴の利用

- 返信/リアクション判断前に対象スレッドの履歴を取得する
- 取得した履歴を正規化して LLM 入力へ渡す
- 効率化のためキャッシュを利用する（後述）

### 5.6 複数エージェント会話

- 同一チャネル / 同一スレッドに複数 `Person` が参加可能
- bot 同士のメッセージも会話履歴として読み取り対象に含める
- 無限応答ループ防止ルールを導入する（後述）

## 6. ワークフロー構成（推奨）

責務を分けて 2 本に分離する。

### 6.1 `workflows/chat_conversation_workflow`

役割:

- チャネル監視
- 反応対象判定
- スレッド履歴取得
- 応答（返信 / リアクション）

性質:

- 主に受動（incoming message に反応）
- スケジューラ routine command として実行可能

### 6.2 `workflows/chat_post_command`（投稿専用コマンド）

役割:

- 指定した GuildBotics コマンドを実行し、出力をチャネルへ投稿
- `task_schedules` と組み合わせて定期投稿を実現

性質:

- 能動（手動/定期どちらでも利用可能）
- 既存の `TaskScheduler` / `CommandSchedule` と親和性が高い

## 7. 実装アーキテクチャ案（拡張性優先）

## 7.1 新規 Integration 抽象（必須）

`TicketManager` と同様に、チャット専用抽象を追加する。

候補:

- `guildbotics/integrations/chat_service.py`（抽象）
- `guildbotics/integrations/slack/slack_chat_service.py`（Slack 実装）

最小インターフェース案（MVP）:

```python
from abc import ABC, abstractmethod
from typing import Iterable


class ChatService(ABC):
    @abstractmethod
    async def get_bot_identity(self) -> "ChatIdentity":
        ...

    @abstractmethod
    async def list_channel_events(
        self,
        channel_id: str,
        *,
        cursor: str | None = None,
        oldest_ts: str | None = None,
        limit: int = 100,
    ) -> "ChatEventPage":
        ...

    @abstractmethod
    async def post_message(
        self,
        channel_id: str,
        text: str,
        *,
        thread_ts: str | None = None,
    ) -> "ChatPostResult":
        ...

    @abstractmethod
    async def add_reaction(
        self, channel_id: str, message_ts: str, reaction: str
    ) -> None:
        ...
```

補足:

- `cursor` と `oldest_ts` の両対応にしておくと Slack 以外にも流用しやすい
- 戻り値は Slack 生 JSON ではなく、共通モデルへ正規化する

## 7.2 起動方式抽象（先に切る）

MVP はポーリング実装でよいが、差し替え点は最初から用意する。

候補:

- `guildbotics/drivers/chat_event_source.py`（抽象）
- `guildbotics/drivers/chat_event_source.py`（`ChatEventSource` 抽象）
- `guildbotics/integrations/slack/slack_socket_mode_chat_event_source.py`（`SocketModeChatEventSource`）
- 将来: `guildbotics/integrations/slack/slack_webhook_chat_event_source.py` など

インターフェース案:

```python
class ChatEventSource(ABC):
    @abstractmethod
    async def fetch_events(
        self, channel_id: str, state: "ChannelCursorState"
    ) -> tuple[list["ChatEvent"], "ChannelCursorState"]:
        ...
```

この抽象により、ワークフロー側は「イベントを受け取って処理する」ことだけに集中できる。

## 7.3 反応判定抽象（ShouldReactPolicy）

`ticket_driven_workflow` 内の `_should_react_to_ticket()` に相当する役割をチャット用に分離する。

候補:

- `guildbotics/templates/commands/workflows/chat/policies/should_react.py`

責務:

- 自分宛てメンション判定
- スレッド継続判定
- 自己投稿除外
- 他エージェント宛てのみメッセージ除外
- 競合回避（軽量版）
- ループ防止

MVP はルールベース実装、将来的に LLM 補助判定を追加可能にする。

## 7.4 会話状態ストア（効率化・重複防止）

候補:

- `guildbotics/integrations/chat_state_store.py`（抽象）
- `guildbotics/integrations/file_chat_state_store.py`（MVP）

保存対象:

- チャネルごとの取得カーソル（`cursor`, `oldest_ts`）
- 処理済みイベント ID / message ts
- スレッドごとの最終既読 ts
- スレッド参加エージェント情報
- スレッド連続自動返信回数（ループ防止）
- スレッド履歴キャッシュ metadata（最新 reply ts / 件数）

保存先（MVP 推奨）:

- `get_storage_path()` 配下（`~/.guildbotics/data/...`）
- 例: `~/.guildbotics/data/chat_state/slack/<person_id>/...`

## 7.5 会話モデル（新規追加推奨）

現行 `Task` はチケット中心であり、チャット会話を直接表現すると意味論が崩れやすい。

新規モデルを推奨:

- `guildbotics/entities/chat.py`

候補モデル:

- `ChatIdentity`
- `ChatMessage`
- `ChatThread`
- `ChatEvent`
- `ChatChannelSubscription`
- `ChatScheduledPost`

既存 `entities/message.py` は LLM 入力整形に流用しやすいため、変換レイヤーで活かす。

## 8. ワークフロー詳細仕様（MVP）

## 8.1 `chat_conversation_workflow` 処理フロー

1. `ChatService` を取得
2. 対象チャネル一覧（この `Person` が参加するチャネル）を取得
3. 各チャネルについて `ChatEventSource.fetch_events()` で差分取得
4. 各イベントに対して `ShouldReactPolicy` で反応要否判定
5. 反応対象ならスレッド履歴を取得（キャッシュ活用）
6. 応答アクションを決定（返信 / リアクション / 何もしない）
7. Slack に送信
8. 状態ストア更新（処理済みイベント、スレッド参加、カーソル）

## 8.2 応答アクション（MVP）

MVP の推奨順序:

1. ルールベースで即応可否を判定
2. 必要なら LLM で返信文生成
3. 投稿 or リアクション

ルールベースで処理しやすいケース例:

- 明らかな ACK のみ必要: リアクション
- 他エージェント担当と判定: リアクション + 返信なし
- 自分宛て質問: 返信

## 8.3 返信文生成

現行の `intelligences.functions` を流用する前提で、スレッド履歴を `Message` 配列へ正規化して渡す。

候補実装:

- `talk_as` / `reply_as` 系を利用
- 将来的に `chat_mode` 相当の専用モードを追加

MVP では新モードを増やしすぎず、まずは単一の `chat_mode` 的実装が妥当。

## 9. 複数エージェント対応のルール（MVP）

## 9.1 同一チャネル参加

- 複数 `Person` が同一チャネルを監視してよい
- ただし、同一プロセス内での競合抑止を行う（状態ストア or in-memory lock）

## 9.2 競合回避（MVP ルール）

以下の優先順を推奨:

1. 明示メンションされたエージェントのみ反応
2. 複数メンション時は全員反応可（MVP 確定）
3. スレッド継続時は直近で返信したエージェントを優先
4. 他エージェントが直近数秒内に返信済みなら自分はリアクションのみ

## 9.3 無限会話ループ防止（MVP）

最低限の安全策:

- bot 投稿に対する bot 自動返信は MVP では原則無効
- スレッドごとの連続自動返信回数に上限を設ける
- 一定時間内の同一内容返信を抑止

## 9.4 明示メンション規約（Phase 2 以降の拡張方針）

MVP では bot-to-bot 自動返信を無効とするため、この節は将来の拡張方針（Phase 2 以降）として扱う。

bot-to-bot 会話を解禁する場合は、ループ防止のため bot が他 bot をメンションする基準を先に固定する。

### 9.4.1 基本方針

- bot は通常、他 bot をメンションしない
- 例外として、明確な委譲・質問・引き継ぎ時のみ明示メンションを使う
- 明示メンションされた bot のみが「bot 投稿への自動返信」候補になる

### 9.4.2 メンション目的（Phase 2 分類案）

- `ask`: 特定エージェントへの質問（返信期待あり）
- `delegate`: 特定エージェントへの依頼（返信期待あり）
- `handoff`: 担当引き継ぎ（返信期待あり）
- `notify`: 通知のみ（返信期待なし、リアクション可）
- `broadcast`: 全体共有（返信期待なし）

Phase 2 ルール案:

- 自動返信を許可するのは `ask` / `delegate` / `handoff`
- `notify` / `broadcast` は返信不要扱い（リアクションのみ可）

### 9.4.3 Slack 上の判定基準

- 判定対象は Slack の正式メンション（ユーザーIDメンション）を正とする
- 表示名ベースの `@alice` テキストは補助情報として扱ってよいが、MVP の反応判定の主条件にはしない

理由:

- 表示名変更や同名ユーザーで誤判定しやすいため
- 他サービス実装時も「プラットフォーム固有の mention entity を正」とする方が抽象化しやすいため

### 9.4.4 bot がメンションを付与する条件（Phase 2）

- 他エージェントの専門性・担当が必要
- 自分では回答確度が低い
- 担当引き継ぎを明示したい

以下では原則メンションしない:

- 単なる進捗報告
- 独り言・要約
- 返信不要の共有

### 9.4.5 連続メンション制限（ループ抑止・Phase 2）

- 同一スレッドで同じ相手への連続メンションにクールダウンを設ける
- 同一スレッド内の bot-to-bot 自動返信回数に上限を設ける
- 直近で相手が応答済みの場合は、再メンションではなくリアクションで済ませる選択肢を優先する

### 9.4.6 実装上の扱い（推奨・Phase 2）

文字列だけで判定せず、内部的には「メンション意図」として正規化して扱う。

例（概念）:

- `mentions=[bob]`
- `mention_intent="ask"`
- `response_expected=True`

`ShouldReactPolicy` は上記の正規化結果を使って反応可否を判定する。

## 9.5 `ShouldReactPolicy` 判定表（MVP 実装仕様）

`ShouldReactPolicy` は、1件のイベントに対して少なくとも以下を返す。

- `decision`: `reply` / `react_only` / `ignore`
- `reason`: 判定理由（ログ・デバッグ用）
- `reaction`: リアクション名（`react_only` の場合）
- `response_expected`: 返信期待の有無（主にスレッド継続判定で利用）

### 9.5.1 判定入力（正規化済み）

最低限必要な入力:

- `event`
- `channel_id`
- `thread_ts`（スレッド外なら `event.ts` を thread root とみなして扱える形に正規化）
- `author_id`
- `text`
- `mentions`（Slack 正式メンションから抽出した user id 一覧）
- `is_bot_message`
- `is_thread_reply`
- `is_from_self`（この `Person` に紐づく bot 自身の投稿）
- `thread_context`
- `participants`（スレッド参加者）
- `last_bot_replier_id`
- `last_message_author_id`
- `bot_auto_turn_count`
- `recent_bot_replies`（一定時間内の bot 応答履歴）
- `state`
- `already_processed`
- `thread_claimed_by_other`（任意: 競合抑止用）
- `response_expected`

### 9.5.2 判定順序（上から優先）

先に「無条件で落とす条件」を評価し、その後に返信/リアクションを決める。

1. 無効/対象外イベント除外
2. 重複処理除外
3. 自己投稿除外
4. ループ防止（bot 投稿制限）
5. 明示メンション判定
6. スレッド継続判定
7. 競合回避
8. 最終アクション決定

### 9.5.3 判定表（MVP）

| No | 条件 | 判定 | 備考 |
|---|---|---|---|
| 1 | イベント種別がメッセージでない | `ignore` | Slack の message 以外は対象外 |
| 2 | 編集イベント / 削除イベント（MVPで未対応） | `ignore` | 後で対応可能 |
| 3 | 監視対象チャネル外 | `ignore` | 設定で弾く |
| 4 | `already_processed == True` | `ignore` | 重複防止 |
| 5 | `is_from_self == True` | `ignore` | 自己投稿には反応しない |
| 6 | `is_bot_message == True` | `ignore` | MVP では bot-to-bot 自動返信を無効 |
| 7 | `bot_auto_turn_count` が上限超過 | `react_only` | 例: `hand` / `no_entry_sign` |
| 8 | 短時間に同一スレッドで bot 応答が過密 | `react_only` | 例: `hourglass_flowing_sand` |
| 9 | 自分への明示メンションあり | `reply` | MVP は意図分類せず返信 |
| 10 | 他エージェントへの明示メンションのみ | `ignore` | 担当外 |
| 11 | 複数エージェント宛て明示メンションで自分含む | `reply` | MVP では全員反応可（競合回避へ進む） |
| 12 | 明示メンションなし かつ スレッド継続条件を満たす かつ `response_expected == True` | `reply` | 追加入力への継続応答 |
| 13 | 明示メンションなし かつ スレッド継続条件を満たす かつ `response_expected == False` | `ignore` | MVP ではノイズ削減を優先 |
| 14 | `thread_claimed_by_other == True` かつ自分への明示メンションなし | `react_only` | 例: `eyes` で傍観 |
| 15 | 上記に該当しない | `ignore` | デフォルト |

### 9.5.4 スレッド継続条件（MVP）

「明示メンションなしでも返信してよい」条件は以下をすべて満たす場合とする。

- 対象イベントが、自分が参加したスレッド内の新規メッセージである
- 自分自身の投稿ではない
- 自分が thread participant として登録済み
- 直近の bot 応答者が自分
- 直近メッセージが他エージェントへの明示メンションのみではない
- 対象イベントが bot 投稿ではない（MVP）
- ループ防止制約（自動ターン上限 / クールダウン）を満たす

補足:

- 「thread participant」は、返信またはリアクションを行った時点で state store に記録する
- MVP では意味理解ベース判定を避け、状態 + メンション + 直近発話者で決める
- 「参加済みなら常に継続返信可」にはしない（誤反応を避けるため、`直近の bot 応答者が自分` を必須条件にする）

### 9.5.5 競合回避ルール（MVP の具体化）

`decision == reply` の候補でも、送信前に以下をチェックして `react_only` に降格してよい。

降格条件例:

- 同一スレッドで他エージェントが直近 `N` 秒以内に返信済み
- スレッドクレームが他エージェントにある
- 同一イベントを他エージェントが先に処理済みとして state store に記録した

降格時のリアクション候補:

- `eyes`（確認中/見ている）
- `white_check_mark`（了解）
- `hand`（自分は対応しない）

### 9.5.6 実装用の擬似コード（MVP）

```python
def should_react(input: PolicyInput) -> PolicyDecision:
    if not input.event.is_message:
        return ignore("not_message")
    if input.event.is_edit_or_delete:
        return ignore("unsupported_message_subtype")
    if not input.event.is_in_subscribed_channel:
        return ignore("channel_not_subscribed")
    if input.state.already_processed:
        return ignore("already_processed")
    if input.event.is_from_self:
        return ignore("self_message")

    mentions_me = input.self_user_id in input.event.mentions
    mentions_non_self_users = any(uid != input.self_user_id for uid in input.event.mentions)

    if input.event.is_bot_message:
        return ignore("bot_message_ignored_in_mvp")

    if input.thread_context.bot_auto_turn_count >= input.policy.max_bot_auto_turns:
        return react_only("bot_loop_limit", "hand")

    if input.thread_context.too_many_recent_bot_replies:
        return react_only("bot_reply_cooldown", "hourglass_flowing_sand")

    if mentions_me:
        return reply("explicit_mention")

    if mentions_non_self_users and not mentions_me:
        return ignore("mentioned_other_agent_only")

    if is_thread_followup_for_me(input):
        if not input.response_expected:
            return ignore("thread_followup_no_response_expected")
        return reply("thread_followup")

    return ignore("no_trigger")
```

### 9.5.7 ログ出力（推奨）

運用時の調整を容易にするため、`decision` と `reason` を必ずログに残す。

例:

- `decision=reply reason=explicit_mention`
- `decision=ignore reason=bot_message_ignored_in_mvp`
- `decision=react_only reason=bot_reply_cooldown reaction=hourglass_flowing_sand`

### 9.5.8 Phase 2 拡張: `mention_intent` 導入

MVP では `mention_intent` を使わない。

Phase 2 以降で bot-to-bot 会話を導入する際に、以下を追加する。

- `ShouldMentionPolicy`（送信側ポリシー）を先に導入する（推奨）
- 明示タグ方式を優先する（例: `[ask]`, `[delegate]`, `[handoff]`）
- LLM による意図推定は後段の拡張として扱う
- `mention_intent`（`ask` / `delegate` / `handoff` / `notify` / `broadcast` / `unknown`）
- 必要に応じて LLM 補助による意図推定

## 9.6 `ShouldReactPolicy` 実装インターフェース案（MVP）

MVP 実装を迷いなく進めるため、`ShouldReactPolicy` の入力/出力型を先に固定する。

実装配置候補:

- `guildbotics/templates/commands/workflows/chat/policies/should_react.py`
- `guildbotics/templates/commands/workflows/chat/policies/models.py`

### 9.6.1 型定義案（概念）

```python
from dataclasses import dataclass, field
from typing import Literal


DecisionKind = Literal["reply", "react_only", "ignore"]


@dataclass(slots=True)
class PolicyLimits:
    max_bot_auto_turns: int = 2
    bot_reply_cooldown_seconds: int = 10


@dataclass(slots=True)
class PolicyEvent:
    event_id: str
    channel_id: str
    message_ts: str
    thread_ts: str
    author_id: str | None
    text: str
    mentions: list[str]
    is_message: bool = True
    is_edit_or_delete: bool = False
    is_bot_message: bool = False
    is_from_self: bool = False
    is_in_subscribed_channel: bool = True


@dataclass(slots=True)
class ThreadContext:
    participants: set[str] = field(default_factory=set)  # person_id set
    last_bot_replier_id: str | None = None              # person_id
    last_message_author_id: str | None = None           # platform user id / bot user id
    bot_auto_turn_count: int = 0
    too_many_recent_bot_replies: bool = False
    thread_claimed_by_other: bool = False


@dataclass(slots=True)
class ProcessingState:
    already_processed: bool = False
    response_expected: bool = True


@dataclass(slots=True)
class PolicyInput:
    self_person_id: str
    self_user_id: str                     # Slack user id (bot user)
    event: PolicyEvent
    thread_context: ThreadContext
    state: ProcessingState
    limits: PolicyLimits = field(default_factory=PolicyLimits)


@dataclass(slots=True)
class PolicyDecision:
    decision: DecisionKind
    reason: str
    reaction: str | None = None
    response_expected: bool | None = None
```

### 9.6.2 補助関数 / メソッド（推奨）

- `mentions_me(input) -> bool`
- `mentions_other_agents(input) -> bool`
- `is_thread_followup_for_me(input) -> bool`
- `reply(reason)`
- `react_only(reason, reaction)`
- `ignore(reason)`

設計意図:

- 判定ロジック本体を短く保つ
- ユニットテストで条件差分を作りやすくする

### 9.6.3 `response_expected` の MVP 解釈（暫定）

MVP では意味理解ベースではなく、状態ベースで扱う。

推奨:

- 初回の自分宛てメンションに対して返信した後は、同一スレッドで `response_expected=True`
- スレッドが収束したと判断したら（例: 自分が完了応答した後）、state store 側で `False` にできるようにする
- `False` の場合、MVP ではメンションなし継続投稿に対して `ignore`

## 9.7 `ShouldReactPolicy` テストケース一覧（MVP）

テストはテーブル駆動を推奨する。`reason` まで検証すると回帰を検出しやすい。

配置例:

- `tests/guildbotics/templates/commands/workflows/chat/test_should_react_policy.py`

### 9.7.1 最低限のテスト観点

- 対象外イベント除外
- 重複除外
- 自己投稿除外
- bot 投稿除外（MVP）
- 明示メンション返信
- 他エージェント宛て無視
- 複数メンション許可
- スレッド継続返信
- スレッド継続 `ignore`（`response_expected=False`）
- 競合回避による `react_only`
- ループ抑止による `react_only`

### 9.7.2 テストケース表（MVP）

| ID | ケース | 主な入力差分 | 期待 `decision` | 期待 `reason` | 備考 |
|---|---|---|---|---|---|
| SRP-001 | 非メッセージイベント | `is_message=False` | `ignore` | `not_message` | |
| SRP-002 | 編集/削除イベント | `is_edit_or_delete=True` | `ignore` | `unsupported_message_subtype` | |
| SRP-003 | 監視対象外チャネル | `is_in_subscribed_channel=False` | `ignore` | `channel_not_subscribed` | |
| SRP-004 | 重複イベント | `already_processed=True` | `ignore` | `already_processed` | |
| SRP-005 | 自己投稿 | `is_from_self=True` | `ignore` | `self_message` | |
| SRP-006 | bot 投稿（MVP） | `is_bot_message=True` | `ignore` | `bot_message_ignored_in_mvp` | bot-to-bot 無効 |
| SRP-007 | 自動ターン上限超過 | `bot_auto_turn_count=max` | `react_only` | `bot_loop_limit` | `reaction=hand` |
| SRP-008 | bot 応答過密 | `too_many_recent_bot_replies=True` | `react_only` | `bot_reply_cooldown` | `reaction=hourglass_flowing_sand` |
| SRP-009 | 自分宛て明示メンション | `mentions=[self]` | `reply` | `explicit_mention` | MVP主経路 |
| SRP-010 | 他エージェント宛てのみ | `mentions=[other_agent]` | `ignore` | `mentioned_other_agent_only` | |
| SRP-011 | 複数メンション（自分含む） | `mentions=[self, other_agent]` | `reply` | `explicit_mention` | MVPでは全員返信可 |
| SRP-012 | スレッド継続返信 | `participants` に自分, `last_bot_replier_id=self_person_id`, `response_expected=True` | `reply` | `thread_followup` | メンションなし |
| SRP-013 | スレッド継続だが返信不要 | 上記 + `response_expected=False` | `ignore` | `thread_followup_no_response_expected` | ノイズ削減 |
| SRP-014 | スレッド継続条件不足（未参加） | `participants` に自分なし | `ignore` | `no_trigger` | |
| SRP-015 | スレッド継続条件不足（直近bot返信者が他人） | `last_bot_replier_id=other_person` | `ignore` | `no_trigger` | |
| SRP-016 | 他エージェントへ thread claim 済み | `thread_claimed_by_other=True`, メンションなし | `react_only` | 実装定義 | `eyes` 想定 |

### 9.7.3 競合回避のテスト設計メモ

`thread_claimed_by_other` の扱いは、policy 単体で判定するか、ワークフロー送信直前で降格するかで実装箇所が変わる。

MVP 推奨:

- `ShouldReactPolicy` は `react_only(reason="thread_claimed_by_other", reaction="eyes")` を返してよい
- もしくは policy では `reply` を返し、送信直前の競合チェック層で降格する

どちらでもよいが、MVP はテストしやすさを優先して policy 側で扱う方がシンプル。

## 10. 会話履歴取得・キャッシュ戦略（効率重視）

要件「スレッド全体を読んだ上で応答」と「通信回数削減」を両立するための方針。

## 10.1 チャネル差分取得

- `channel_id` ごとに `oldest_ts` または `cursor` を保存
- ワークフロー実行時は差分のみ取得

## 10.2 スレッド履歴取得

- 反応候補イベントに対してのみ取得
- `thread_ts` 単位でキャッシュ metadata を保存
- 例: `last_message_ts`, `message_count`, `fetched_at`

更新判定:

- `latest_reply_ts` が変化していないなら再取得をスキップ可能
- 変化している場合のみフル再取得（MVP）

将来の改善:

- 差分取得 API が使えるなら増分マージ
- 要約キャッシュ（長スレッド対策）

## 10.3 正規化キャッシュ

LLM 入力用の正規化済み `Message` 配列を保存して再利用すると、再整形コストを削減できる。

ただし MVP では複雑化を避けるため、まずは生メッセージキャッシュ + 都度変換でもよい。

## 11. 定期投稿設計（`task_schedules` + `workflows/chat_post_command`）

## 11.1 用途

- cron で指定された時刻に、指定コマンドの出力をチャネルへ投稿
- 定型メッセージ / テンプレート / LLM生成のいずれも対応可能にする

## 11.2 MVP 仕様

- 各 `Person` の `task_schedules` に定期投稿を定義できる
- 投稿先チャネル、cron、投稿生成に使うコマンドを `workflows/chat_post_command` の引数で指定可能
- thread ではなく通常投稿を基本とする（必要なら `thread_ts` 指定対応）

投稿文生成方式（MVP 確定）:

- GuildBotics のカスタムコマンドを実行し、その出力テキストを投稿する
- 固定文 / テンプレート / LLM 利用は、投稿用コマンド側で表現する

例:

- `reports/morning_summary`
- `chat/post_daily_topic`

## 11.3 実行方式

- `Person.task_schedules` に `workflows/chat_post_command ...` を設定する
- cron 判定は既存の `TaskScheduler` / `ScheduledCommand` に任せる

推奨:

- 専用の `scheduled_posts` 設定を増やさず、既存スケジュール機構を利用する

## 12. 設定ファイル案（現行実装）

現行実装では、責務分離のため以下のように配置する。

- `person.yml.message_channels`: チャネル定義 + チャネル単位の chat 監視設定（`subscriptions`, `event_source` 相当）
- `person.yml.task_schedules`: 定期投稿（`workflows/chat_post_command`）

### 12.1 `person.yml` の例（現行実装）

```yaml
person_id: alice
name: Alice
is_active: true

task_schedules:
  - command: 'workflows/chat_post_command service=slack channel_name=dev-chat command="reports/ai_news_digest query=\"OpenAI OR Anthropic OR Gemini\" language=ja country=JP limit=10 max_age_hours=24"'
    schedules:
      - "0 9 * * 1-5"

message_channels:
  - name: dev-chat
    service: slack
    chat:
      enabled: true
      channel_id: C0123456789
      # channel_id 未指定時は name（または chat.channel_name）で解決
      event_source: socket_mode   # 既定: socket_mode（省略可）
```

### 12.2 `.env` / 環境変数（Slack MVP）

例:

- `{PERSON_ID}_SLACK_BOT_TOKEN`（person毎、必須）
- `{PERSON_ID}_SLACK_APP_TOKEN`（person毎、`person.yml.message_channels[].chat.event_source=socket_mode` を使う場合）

将来的には service 別 prefix を整理する（例: `GUILDBOTICS_SLACK_BOT_TOKEN`）。

### 12.3 Slack 側の事前設定（現行実装）

現行実装の Slack 会話ワークフローは `socket_mode` を前提とする。

事前設定（MVP）:

1. Slack App を作成する
- 対象 Workspace にインストール可能な App を作成する

2. Bot Token を発行する
- `{PERSON_ID}_SLACK_BOT_TOKEN` に設定する（MVPでは必須）
- Bot Token は通常 `xoxb-...`

3. 必要な Bot 権限（scope）を付与する
- 少なくとも以下の API が使える権限が必要
  - メッセージ取得（`conversations.history`）
  - メッセージ投稿（`chat.postMessage`）
  - リアクション追加（`reactions.add`）
- 対象が public/private channel かによって必要な scope が変わるため、運用チャネル種別に合わせて付与する

4. App を Workspace にインストール（または再インストール）する
- scope 変更後は再インストールが必要な場合がある

5. Bot を対象チャネルへ参加させる
- bot が参加していないチャネルは、履歴取得や投稿が失敗する可能性がある
- private channel は特に招待漏れに注意する

6. 設定に必要な ID を控える
- `channel_id`（例: `C...` / `G...`）

7. 設定ファイルを更新する
- `person.yml.message_channels[]` の `chat.enabled`, `chat.event_source=socket_mode`, `channel_id`（または `name`）
- `person.yml.task_schedules`（定期投稿に `workflows/chat_post_command` を使う場合）

運用上の注意:

- `socket_mode` は反応速度が高いが、`SLACK_APP_TOKEN` と Slack App の追加設定が必要

## 13. 既存コードへの組み込みポイント

## 13.1 `IntegrationFactory`

追加候補:

- `create_chat_service(self, logger, person, team) -> ChatService`

既存 `Context` にも accessor を追加候補:

- `Context.get_chat_service()`

## 13.2 新ワークフロー配置

候補:

- `guildbotics/templates/commands/workflows/chat_conversation_workflow.py`
- `guildbotics/templates/commands/workflows/chat_post_command.py`

モード分割する場合:

- `guildbotics/templates/commands/workflows/chat_modes/reply_mode.py`
- `guildbotics/templates/commands/workflows/chat_modes/reaction_mode.py`

MVP ではワークフロー1本 + policy/helper 分割で十分。

## 13.3 スケジューラ統合

初期段階では、`guildbotics start` の routine command に `workflows/chat_conversation_workflow` を追加して定期巡回させる方式が最小コスト。

将来的にイベント駆動へ移行する場合も、ワークフロー本体は再利用し、`ChatEventSource` 実装だけ差し替える。

## 14. テスト戦略（MVP）

優先度順:

1. `ShouldReactPolicy` のユニットテスト
2. イベント重複防止 / カーソル更新の状態ストアテスト
3. スレッド履歴キャッシュ更新判定のテスト
4. ワークフロー本体の統合テスト（Fake `ChatService`）
5. Slack 実装の API 変換テスト（HTTP レイヤーのモック）

既存テスト構成にならう配置例:

- `tests/guildbotics/templates/commands/workflows/test_chat_conversation_workflow.py`
- `tests/guildbotics/integrations/test_chat_service_models.py`
- `tests/guildbotics/integrations/slack/test_slack_chat_service.py`

## 15. 実装フェーズ案（推奨）

### Phase 1（MVP基盤）

- `ChatService` 抽象 + Slack 実装（最小 API）
- `ConversationStateStore`（ファイル実装）
- `chat_conversation_workflow`（メンション反応 + スレッド返信）
- `ShouldReactPolicy`（ルールベース）

### Phase 2（運用性）

- リアクション応答の改善
- 複数エージェント競合回避の強化
- スレッド履歴キャッシュ最適化

### Phase 3（機能拡張）

- 定期投稿テンプレート/投稿モードの拡張（`chat_post_command` を利用）
- Webhook イベントソース
- 他サービス実装（Discord 等）

## 15.1 実装の進め方（推奨）

今回の実装はボリュームが大きいため、以下の方針で進めると安全。

基本方針（採用推奨）:

1. 依存が少ないモジュールから順番に作成する
2. モジュール間の結合時のトラブルを避けるため、できるだけ単体テストを実装する

加えて推奨する補助方針:

3. 外部API境界（`ChatService`, `ConversationStateStore`）の抽象を先に固定する
4. `ShouldReactPolicy` はテスト先行で実装する
5. 早い段階で「最小の縦切り（Fake 実装で1件返信）」を通す

## 15.2 実装順序（MVP 推奨）

以下の順番で実装すると、依存とデバッグコストを抑えやすい。

1. `entities/chat.py`（共通モデル）
- `ChatMessage`, `ChatEvent`, `ChatEventPage`, `ChatPostResult` などの最小モデル

2. `ShouldReactPolicy` とその型定義
- `PolicyInput`, `PolicyDecision`, `PolicyEvent`, `ThreadContext`, `ProcessingState`

3. `ShouldReactPolicy` 単体テスト（テーブル駆動）
- `SRP-001` 〜 `SRP-016` をベースに実装

4. `ConversationStateStore` 抽象 + ファイル実装（MVP）
- カーソル、処理済みイベント、thread participant、`response_expected`

5. `ConversationStateStore` 単体テスト
- 重複防止、更新、復元、競合しない基本動作

6. `ChatService` 抽象 + Fake 実装（テスト用）
- ワークフローテスト用に外部API依存を外す

7. `chat_conversation_workflow`（Fake `ChatService` で統合テスト）
- 「メンション1件に返信する」最小経路を通す
- 次にスレッド継続、競合回避、リアクション降格を追加

8. Slack `ChatService` 実装
- APIレスポンスの正規化
- message subtype の最低限対応

9. Slack 実装テスト
- HTTP モックでの変換テスト中心

10. `IntegrationFactory` / `Context` 組み込み
- `create_chat_service(...)`
- `Context.get_chat_service()`

11. `workflows/chat_post_command`
- `person.yml.task_schedules[].command` と組み合わせて投稿

12. 設定読込・バリデーション整理
- `person.yml.message_channels[]` 読込ヘルパー
- エラー時のログ/スキップ方針

## 15.3 最小の縦切り（早期に通すゴール）

実装中盤までに、以下が動く状態を作る。

- 1つの `Person` が 1つの Slack チャネル（Fake）を監視
- 自分宛てメンションを検知
- スレッド履歴を取得
- `ShouldReactPolicy` が `reply` を返す
- ワークフローがスレッド返信を1件投稿
- state store に処理済みイベントと thread participant を記録

この縦切りが通ると、その後の拡張（競合回避、キャッシュ、リアクション最適化）が追加しやすい。

## 15.4 PR 分割の推奨（レビューしやすさ重視）

PR1: Policy / Models / Tests

- `ShouldReactPolicy`
- policy 入出力モデル
- テーブル駆動テスト

PR2: State Store / Tests

- `ConversationStateStore` 抽象
- ファイル実装
- 単体テスト

PR3: Chat Workflow (Fake integration) / Tests

- `chat_conversation_workflow`
- Fake `ChatService`
- 統合テスト

PR4: Slack Integration + Wiring

- Slack `ChatService`
- `IntegrationFactory` / `Context` 組み込み
- 設定読込の接続

PR5: Scheduled Post Workflow

- `workflows/chat_post_command`
- コマンド実行結果の投稿
- 関連テスト

## 15.5 実装時の注意点（先に固定）

- `ShouldReactPolicy` の `reason` は最初からログ出力する（デバッグ容易化）
- `thread_claimed_by_other` を policy で扱うか、送信直前で扱うかを実装開始時に決める
- Slack の message subtype は想定漏れが出やすいため、MVPで未対応の subtype は明示的に `ignore` する
- `person.yml.message_channels[]` の設定読込はワークフロー本体に埋め込まず、ヘルパー関数/モジュールに分ける

## 16. 実装前に最終確認したい項目（未確定事項）

MVP 実装に必要な主要方針は概ね確定済み。

残りは主に Phase 2（bot-to-bot 会話）で詰める項目:

1. bot-to-bot 会話の解禁条件（Phase 2）
- 送信側 `ShouldMentionPolicy` の詳細ルール
- 明示タグ（`[ask]`, `[notify]` など）の構文・運用ルール
- LLM に意図推定を任せる範囲をどこまで許可するか

## 17. まとめ

本要件は、現行の GuildBotics の「ワークフロー + Integration + Scheduler」構造と相性が良い。

実装の成功要因は次の 3 点:

- 早い段階で `ChatService` / `ChatEventSource` / `ConversationStateStore` の抽象を切る
- MVP の「反応すべきメッセージ」をルールベースで明確化する
- スレッド履歴取得と重複防止の状態管理を先に入れる

この方針なら、まず Slack + ポーリングで開始しつつ、後からイベント駆動や他サービス対応へ拡張しやすい。
