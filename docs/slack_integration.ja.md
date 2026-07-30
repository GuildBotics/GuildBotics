# Slack 連携ガイド

メンバーが Slack チャネルを監視し、本人として返信・リアクションするチャットワークフローの設定ガイドです。
概要は [README の「Slack で応答させる」](../README.ja.md#slack-で応答させる)を参照してください。

- [全体像](#全体像)
- [Slack App の設定手順](#slack-app-の設定手順)
- [`person.yml` の設定](#personyml-の設定)
- [定期投稿](#定期投稿)
- [手動での投稿・リアクション](#手動での投稿リアクション)
- [チャット処理の内部動作](#チャット処理の内部動作)

## 全体像

メンバーは `person.yml` の `message_channels` に設定したチャネルを監視します。メッセージを受信すると、設定済み AI CLI ツールが以下のいずれの対応にするかを判断します。

- 返信する
- リアクションを付ける
- 何もしない（no-op として記録）
- 質問を返す
- 作業できない理由を報告する（blocked）

Slack への投稿・返信・リアクションは、すべてメンバー専用 CLI（`guildbotics member chat ...`）経由で実行・記録されます。

チャットイベントを受信するには `guildbotics start` の実行が必要です。定期投稿は受信とは別経路で、[定期投稿](#定期投稿)のとおり `task_schedules` + `workflows/chat_post_command` を使います。

## Slack App の設定手順

AI エージェントとして振る舞う Slack App（送信 + 受信）を Slack 上で作成します。

1. https://api.slack.com/apps で Slack App を作成する
2. 必要な権限（scope）を付与する
   - Slack App 管理画面の `OAuth & Permissions` -> `Scopes` で追加する
   - 最低限必要（利用する会話種別に応じて追加）
     - `chat:write`（`chat.postMessage` 用）
     - `reactions:write`（`reactions.add` 用）
     - `channels:history`（public channel の `conversations.history` 用）
     - `groups:history`（private channel の `conversations.history` 用）
     - `im:history`（DM を扱う場合）
     - `mpim:history`（グループDMを扱う場合）
   - `channel_name` で設定したい場合は、名前解決（`conversations.list`）用に以下も追加
     - `channels:read`（public channel）
     - `groups:read`（private channel）
   - Slack からメンバーのアバターをインポートしたい場合（セットアップ画面）は以下も追加
     - `users:read`（`users.info` 用）
   - 参考URL（Slack公式）
     - `conversations.history`: `https://api.slack.com/methods/conversations.history`
     - `conversations.list`: `https://api.slack.com/methods/conversations.list`
     - `chat.postMessage`: `https://api.slack.com/methods/chat.postMessage`
     - `reactions.add`: `https://api.slack.com/methods/reactions.add`
     - `users.info`: `https://api.slack.com/methods/users.info`
3. App を Workspace にインストールする（scope変更後は再インストールが必要な場合あり）
4. Bot Token（`xoxb-...`）を環境変数 `{PERSON_ID}_SLACK_BOT_TOKEN` に設定する
   - 例: `alice` 用なら `ALICE_SLACK_BOT_TOKEN`
5. Socket Mode 用の設定を行う
   - `Socket Mode` で `Enable Socket Mode` をONにする
   - `Event Subscriptions` を有効化し、bot events を追加する
     - channel を扱う場合: `message.channels`, `message.groups`
     - DM を扱う場合: `message.im`, `message.mpim`
   - `Basic Information` で App-Level Token（`xapp-...`）を発行し、環境変数 `{PERSON_ID}_SLACK_APP_TOKEN` に設定する
     - 例: `alice` 用なら `ALICE_SLACK_APP_TOKEN`
6. Bot を対象チャネルへ招待する
7. `person.yml` の `message_channels` で対象チャネルを設定する

### 複数のエージェントを追加する場合

2人め以降も同様の設定を行えば追加できますが、Socket Mode の設定をスキップして最初のAIエージェントで設定した通信経路を共有することもできます。

受信接続を共有する場合は、既存メンバーと同じ App-Level Token を二人目の `{PERSON_ID}_SLACK_APP_TOKEN` に設定します。

- 例: `alice` と `bob` が同じ受信接続を共有する場合
  - `ALICE_SLACK_APP_TOKEN=<alice_xapp_token>`
  - `BOB_SLACK_APP_TOKEN=<alice_xapp_token>`

別の受信経路にしたい場合（例: 別 Workspace、別 Slack App で分離したい場合）は、追加の Slack App を作成して Socket Mode / Event Subscriptions / App-Level Token を別途設定します。

## `person.yml` の設定

チャット受信チャネル（`message_channels`）と定期投稿（`task_schedules`）は `team/members/<person_id>/person.yml` に設定します。

```yaml
# team/members/alice/person.yml
person_id: alice
name: Alice
is_active: true

message_channels:
  - service: slack
    name: dev-chat
    chat:
      enabled: true
      participation: strict
      startup_backfill_minutes: 60
      backfill_interval_seconds: 300

task_schedules:
  - command: 'workflows/chat_post_command service=slack channel_id=C0123456789 command="examples/reports/ai_news_digest query=\"OpenAI OR Anthropic OR Gemini\" language=ja country=JP limit=10 max_age_hours=24"'
    schedules:
      - "0 9 * * 1-5"
```

ポイント:

- 監視対象チャネルは `message_channels` で定義し、`chat.enabled: true` のものが対象になります
- `chat.participation` で Slack thread への参加条件を制御できます。`strict`（既定）は明示メンションと一度呼ばれた thread の follow-up、`social` は雑談チャネル向けに未メンションの自然参加も許可、`muted` は明示メンションのみを処理します
- 起動時に Slack history から直近の channel message と既知 thread reply を取り込みます（backfill）。`startup_backfill_minutes` の既定値は `60`、`backfill_interval_seconds` の既定値は `300` で、`0` にすると起動後の定期 history 確認を無効化できます
- `person.yml` の `character` には、興味・嗜好・会話参加方針などを定義できます。チャット判断と返信生成は AI CLI ツール経由でこのプロフィールを参照します

## 定期投稿

定期投稿は `task_schedules` + `workflows/chat_post_command` で行います（投稿本文は GuildBotics カスタムコマンドの出力）。

本文を生成するコマンドの例（AIニュースダイジェスト）:

```bash
guildbotics run examples/reports/ai_news_digest query="OpenAI OR Anthropic OR Gemini" language=ja country=JP limit=10 max_age_hours=24
```

`examples/reports/ai_news_digest` は組み込みのサンプルコマンドで、前段で Google News RSS からニュース候補を取得し、後段で LLM が Slack 向けの日本語ダイジェスト文面に整形します。

投稿までを一度に実行する例（手動）:

```bash
guildbotics run workflows/chat_post_command service=slack channel_name=dev-chat command='examples/reports/ai_news_digest query="OpenAI OR Anthropic OR Gemini" language=ja country=JP limit=10 max_age_hours=24'
```

## 手動での投稿・リアクション

メンバーとしての返信・リアクションは CLI から直接実行することもできます。

```bash
guildbotics member chat reply --person alice --service slack --channel-id C0123456789 --thread-ts 1777554000.000000 --content-stdin <<'EOF'
`$HOME`、backtick (`command`)、`$(command)` をそのまま含む返信本文
EOF
guildbotics member chat reaction add --person alice --service slack --channel-id C0123456789 --message-ts 1777554000.000000 --reaction ack
```

## チャット処理の内部動作

- チャットイベントの受信は `guildbotics start` で起動されるイベントリスナーランナーが担当し、処理は各メンバーのメンバーワーカー内のイベントキューソースが直列に実行します
- `guildbotics start --only scheduler` で巡回/定期実行だけを起動している場合、チャットイベントは受信されません。`--only events` の場合、巡回/定期実行は無効になりますが、メンバーワーカーはキュー済みチャットイベントを処理します
- AI CLI ツールによるチャット処理では、`functions/handle_chat_event` がメンバーごとの作業ディレクトリを `cwd` にして実行されます。既定では `<workspace>/.guildbotics/data/workspaces/<person_id>/` です。この配下にある複製済みリポジトリを参照できます
- 返信・リアクション・no-op・完了の証跡は `guildbotics member chat reply|post|reaction add|noop|complete` 経由で記録されます。ワークフローはこの実行証跡を検証し、ツールの自然言語の標準出力だけでは Slack に投稿した証拠として扱いません
