# Slack 連携ガイド

メンバーが Slack チャネルを監視し、そこで受けた依頼を本人として処理するチャットワークフローの設定ガイドです。
概要は [README の「Slack で作業を依頼する」](../README.ja.md#slack-で作業を依頼する)を参照してください。

- [全体像](#全体像)
- [Slack App の設定手順](#slack-app-の設定手順)
- [Slack App を手動で作成する場合](#slack-app-を手動で作成する場合)
- [複数のエージェントを追加する場合](#複数のエージェントを追加する場合)
- [デスクトップアプリでの設定](#デスクトップアプリでの設定)
- [定期投稿](#定期投稿)
- [`person.yml` リファレンス](#personyml-リファレンス)
- [手動での投稿・リアクション](#手動での投稿リアクション)
- [チャット処理の内部動作](#チャット処理の内部動作)

## 全体像

メンバーは、設定で指定したチャネルを監視します。メッセージを受信すると、設定済み AI CLI ツールが thread の文脈とメンバー自身の role / 参加条件を読み、必要な作業を行ったうえで Slack への応答形式を選びます。

**依頼された作業の実行**: メッセージが GitHub などの操作を明示的に指示している場合（例:「この PR のレビューコメントに対応して」）、メンバーはその作業を実行します。メンバーの作業ディレクトリにはリポジトリの checkout がないため、対象リポジトリをメッセージと thread 文脈から特定し、`guildbotics member git prepare` で作業ブランチを準備してから作業します。対象が曖昧な場合は thread で質問し、`asking` として完了します。

**Slack への応答形式**: 作業の有無にかかわらず、最後に以下のいずれかで thread へ対応します。

- 返信する
- リアクションを付ける
- 何もしない（no-op として記録）
- 質問を返す（`asking`）
- 作業できない理由を報告する（`blocked`）

返信するかどうかは、そのメンバーの role の観点で新しい価値を足せるかで判断します。既に同じ観点が出ている、単なる同意で足りる、role 外で確信が低い場合は、リアクションのみか no-op を優先します。自分の role 外の観点が必要な場合は、該当 role を持つ別メンバーにメンションで引き継ぎます。

Slack への投稿・返信・リアクションと完了記録は、すべてメンバー専用 CLI（`guildbotics member chat ...`）経由で実行・記録されます。

チャットイベントを受信するには、デスクトップアプリの **サービス実行** 画面で **イベント起動** を含めてサービスを開始します（CLI の場合は `guildbotics start`）。定期投稿は受信とは別経路で、[定期投稿](#定期投稿)のとおり定期実行コマンドとして設定します。

## Slack App の設定手順

AI エージェントとして振る舞う Slack App（送信 + 受信）を Slack 上で作成します。デスクトップアプリからの半自動登録が既定の手順で、必要な scope・Socket Mode・イベント購読は GuildBotics が生成する App Manifest で設定済みになります。

**設定 → メンバー → Slack** タブで **新規に App を登録** を選び、以下を行います。

1. **App 名** を入力し（既定はメンバー ID）、**Slack で App を作成** を押す
   - 外部ブラウザで、manifest を適用した Slack のアプリ作成画面が開きます
   - Slack にサインインしていない場合は、サインイン後にもう一度ボタンを押してください（サインインの遷移で manifest が失われます）
2. ブラウザでワークスペースを選び、**Create** をクリックする
   - manifest で Socket Mode を要求しているため、App の作成・ワークスペースへのインストール・App-Level Token の発行まで Slack 側で完了します
3. **OAuth & Permissions** を開き、**Reinstall to Workspace** を実行して承認する
   - **この手順は必須です。** Slack が作成直後に発行する Bot トークンには manifest で指定した scope が付与されず、そのままでは `missing_scope` で動作しません（新規作成した App で再現を確認済み）
4. 同じ画面の **Bot User OAuth Token**（`xoxb-...`）をコピーする
5. **Basic Information** → **App-Level Tokens** でトークン（`xapp-...`）をコピーする
   - 作成直後の **Your app credentials** に表示される App token と同じものです（App トークン側は scope の問題がないため、そちらをコピーしても構いません）
6. 2 つのトークンをデスクトップアプリの入力欄へ貼り付け、**App を検証** を押す
   - bot の表示名・bot user ID・ワークスペース名に加えて、**Bot の権限** が OK になれば成功です
   - **Bot の権限** が失敗する場合は、手順 3 の再インストール後にトークンをコピーし直せていません
   - `xoxb-` と `xapp-` を取り違えている場合は、その旨がエラーとして表示されます
7. Bot を対象チャネルへ招待する（チャンネルで `/invite @<bot 名>` を実行）
   - 招待していないと、**設定を検証** で `not_in_channel` として報告されます
8. チャネルと参加条件を [デスクトップアプリでの設定](#デスクトップアプリでの設定)のとおり登録する

Slack の OAuth リダイレクトは `http://localhost` を許可せず、トークンを取得するコールバックを Local API で受けられないため、この 2 つのトークンのコピーだけは手作業で残ります。

## Slack App を手動で作成する場合

デスクトップアプリを使わない場合（サーバー運用など）は、以下の手順で同じ構成を手動で作成します。

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
4. Bot Token（`xoxb-...`）を控えておく
5. Socket Mode 用の設定を行う
   - `Socket Mode` で `Enable Socket Mode` をONにする
   - `Event Subscriptions` を有効化し、bot events を追加する
     - channel を扱う場合: `message.channels`, `message.groups`
     - DM を扱う場合: `message.im`, `message.mpim`
   - `Basic Information` で App-Level Token（`xapp-...`）を発行し、控えておく
6. Bot を対象チャネルへ招待する
7. 控えた 2 つのトークンと対象チャネルを、[デスクトップアプリでの設定](#デスクトップアプリでの設定)のとおり登録する

## 複数のエージェントを追加する場合

2人め以降も同様の設定を行えば追加できますが、Socket Mode の設定をスキップして最初のAIエージェントで設定した通信経路を共有することもできます。

受信接続を共有する場合は、二人目の **Slack App トークン** に、既存メンバーと同じ App-Level Token を設定します（環境変数で渡す場合は `{PERSON_ID}_SLACK_APP_TOKEN`。例: `ALICE_SLACK_APP_TOKEN` と `BOB_SLACK_APP_TOKEN` に同じ値）。

別の受信経路にしたい場合（例: 別 Workspace、別 Slack App で分離したい場合）は、追加の Slack App を作成して Socket Mode / Event Subscriptions / App-Level Token を別途設定します。

## デスクトップアプリでの設定

**設定 → メンバー → Slack** タブで、メンバーごとに設定します。

- **Slack Bot トークン**（`xoxb-...`）と **Slack App トークン**（`xapp-...`）: Slack App の設定手順で控えたトークンを入力します。保存済みのメンバーでは、空欄のままにすると保存済みのトークンが維持されます
- **App を検証**: 保存したときに有効になる構成を Slack に問い合わせます。トークンの有効性、Bot に付与された権限、設定したチャンネルを Bot が読み取れるか（＝招待済みか）をまとめて確認します。空欄のトークンは保存済みのものが検証され、結果には「保存済みの Bot トークン」のようにどちらを検証したかが表示されます
- **Slack User ID**: 人間メンバーの場合に指定します（`U` で始まる member ID）
- **チャンネル**: 監視するチャンネル名または ID を追加します（例: `general`、`C0123456789`）
- **会話への参加条件**: チャンネルごとに以下から選びます
  - **積極的に参加**（`social`）: 雑談チャネル向け。メンションされていない会話にも自然に参加します
  - **必要なときだけ参加**（`strict`、既定）: 業務チャネル向け。メンションされたとき、または一度呼ばれた thread の続きだけ対応します
  - **メンションのみに対応**（`muted`）: 通知チャネル向け。明示的にメンションされたときだけ対応します

会話の内容や参加判断に影響するメンバー像（会話上の立ち位置、性格・ふるまい、得意・関心領域、会話に参加する場面、参加を控える場面、参加時の貢献スタイル、会話スタイル）は **基本** タブで設定します。

定期投稿は **巡回** タブの **定期実行コマンド** として設定します（→ [定期投稿](#定期投稿)）。

トークンは OS キーチェーンまたは `.env` に保存され、それ以外の設定は `team/members/<person_id>/person.yml` に保存されます。保存形式と、GUI では設定できない項目は [`person.yml` リファレンス](#personyml-リファレンス)を参照してください。

## 定期投稿

決まった時刻の投稿は、`workflows/chat_post_command` を定期実行することで行います（投稿本文は GuildBotics カスタムコマンドの出力）。

**設定 → メンバー → 巡回** タブで **定期実行を追加** を押し、コマンド欄を **直接入力** に切り替えて、以下のようなコマンドラインを指定します。実行時刻は 毎時 / 毎日 / 毎週 のプリセットか **詳細 cron** で指定します。

```text
workflows/chat_post_command service=slack channel_name=dev-chat command='examples/reports/ai_news_digest query="OpenAI OR Anthropic OR Gemini" language=ja country=JP limit=10 max_age_hours=24'
```

本文を生成するコマンドの例（AIニュースダイジェスト）:

```bash
guildbotics run examples/reports/ai_news_digest query="OpenAI OR Anthropic OR Gemini" language=ja country=JP limit=10 max_age_hours=24
```

`examples/reports/ai_news_digest` は組み込みのサンプルコマンドで、前段で Google News RSS からニュース候補を取得し、後段で LLM が Slack 向けの日本語ダイジェスト文面に整形します。

投稿までを一度に実行する例（手動）:

```bash
guildbotics run workflows/chat_post_command service=slack channel_name=dev-chat command='examples/reports/ai_news_digest query="OpenAI OR Anthropic OR Gemini" language=ja country=JP limit=10 max_age_hours=24'
```

## `person.yml` リファレンス

デスクトップアプリで保存した Slack 設定は、`team/members/<person_id>/person.yml` に次の形で書き出されます。GUI を使わないサーバーでは、このファイルを直接編集します。

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
- `chat.participation` は GUI の **会話への参加条件** に対応します。`strict`（既定）は明示メンションと一度呼ばれた thread の follow-up、`social` は雑談チャネル向けに未メンションの自然参加も許可、`muted` は明示メンションのみを処理します
- `startup_backfill_minutes` と `backfill_interval_seconds` は GUI からは設定できません。起動時に Slack history から直近の channel message と既知 thread reply を取り込み（backfill）、既定値はそれぞれ `60` と `300` です。`backfill_interval_seconds` を `0` にすると、起動後の定期 history 確認を無効化できます
- `character` には、興味・嗜好・会話参加方針などを定義できます（GUI の **基本** タブに対応）。チャット判断と返信生成は AI CLI ツール経由でこのプロフィールを参照します

Bot Token と App-Level Token は `person.yml` には保存されません。OS キーチェーン、`.env`、または環境変数 `{PERSON_ID}_SLACK_BOT_TOKEN` / `{PERSON_ID}_SLACK_APP_TOKEN` で渡します（例: `alice` なら `ALICE_SLACK_BOT_TOKEN`）。

## 手動での投稿・リアクション

メンバーとしての返信・リアクションは CLI から直接実行することもできます。

```bash
guildbotics member chat reply --person alice --service slack --channel-id C0123456789 --thread-ts 1777554000.000000 --content-stdin <<'EOF'
`$HOME`、backtick (`command`)、`$(command)` をそのまま含む返信本文
EOF
guildbotics member chat reaction add --person alice --service slack --channel-id C0123456789 --message-ts 1777554000.000000 --reaction ack
```

## チャット処理の内部動作

- チャットイベントの受信はイベントリスナーランナーが担当し、処理は各メンバーのメンバーワーカー内のイベントキューソースが直列に実行します。どちらもサービスの起動（GUI の **サービス実行 → 実行**、または `guildbotics start`）で起動します
- **イベント起動** を含めずに起動した場合（CLI では `guildbotics start --only scheduler`）、チャットイベントは受信されません。逆に **巡回実行コマンド** と **定期実行コマンド** を外した場合（CLI では `--only events`）でも、メンバーワーカーはキュー済みチャットイベントを処理します
- AI CLI ツールによるチャット処理では、`functions/handle_chat_event` がメンバーごとの作業ディレクトリを `cwd` にして実行されます。既定では `<workspace>/.guildbotics/data/workspaces/<person_id>/` です。この配下にある複製済みリポジトリを参照できます
- 返信・リアクション・no-op・完了の証跡は `guildbotics member chat reply|post|reaction add|noop|complete` 経由で記録されます。ワークフローはこの実行証跡を検証し、ツールの自然言語の標準出力だけでは Slack に投稿した証拠として扱いません
