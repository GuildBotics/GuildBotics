<h1>GuildBotics</h1>

[English](https://github.com/GuildBotics/GuildBotics/blob/main/README.md) • [日本語](https://github.com/GuildBotics/GuildBotics/blob/main/README.ja.md)

マルチエージェント対応タスク自動化・コマンド実行フレームワーク

GuildBotics でできること:
- 異なる役割と個性を持つ複数のAIエージェントを管理
- コマンド（Markdownプロンプト、Python/Shellスクリプト、YAMLワークフロー）のスケジュール実行
- プラガブルなアダプター経由での外部サービス統合
- 複数のLLMプロバイダー対応（Google Gemini、OpenAI、Anthropic Claude）

**使用例**: デフォルトワークフローではGitHub Projectsと統合し、チケット駆動型のAIエージェント協働を実現します（詳細は[GitHub統合の使用例](#6-github統合の使用例)参照）。

---

## 重要な注意（免責事項）

- 本ソフトウェアはアルファ版です。今後、破壊的な非互換を伴う変更が行われる可能性が非常に高く、動作不具合も頻繁に発生することが想定されるため、実運用環境での利用は推奨しません。
- 本ソフトウェアの動作不具合やそれによって生じた損害について、作者および配布者は一切の責任を負いません。特に、AIエージェントの誤動作や暴走により、利用中のシステムや外部サービスに対する致命的な破壊、データ損失、秘密データ漏洩が発生する可能性があります。使用は自己責任で行い、隔離されたテスト環境で検証してください。

---

- [1. 主要機能](#1-主要機能)
  - [コアフレームワーク](#コアフレームワーク)
  - [組み込み機能](#組み込み機能)
- [2. クイックスタート](#2-クイックスタート)
- [3. 動作環境と事前準備](#3-動作環境と事前準備)
  - [3.1. 動作環境](#31-動作環境)
  - [3.2. 必須ソフトウェア](#32-必須ソフトウェア)
  - [3.3. LLM API](#33-llm-api)
  - [3.4. CLI エージェント](#34-cli-エージェント)
- [4. インストール](#4-インストール)
- [5. 基本的な使い方](#5-基本的な使い方)
  - [5.1. 初期セットアップ](#51-初期セットアップ)
  - [5.2. メンバーの追加](#52-メンバーの追加)
  - [5.3. コマンド実行](#53-コマンド実行)
    - [5.3.1. コマンドの種類と配置方法](#531-コマンドの種類と配置方法)
    - [5.3.2. コマンドの実行方法](#532-コマンドの実行方法)
  - [5.4. スケジュール機能](#54-スケジュール機能)
    - [5.4.1. ルーチンコマンド](#541-ルーチンコマンド)
    - [5.4.2. スケジュールタスク](#542-スケジュールタスク)
    - [5.4.3. Cron表記の詳細](#543-cron表記の詳細)
    - [5.4.4. スケジューラの内部動作](#544-スケジューラの内部動作)
  - [5.5. スケジュール設定の例](#55-スケジュール設定の例)
    - [マルチエージェント・スケジュールワークフローの例](#マルチエージェントスケジュールワークフローの例)
    - [複数スケジュールパターンの例](#複数スケジュールパターンの例)
  - [5.6. Slack チャットワークフロー](#56-slack-チャットワークフロー)
    - [5.6.1. 事前設定（Slack 側）](#561-事前設定slack-側)
    - [5.6.2. `person.yml` の設定例](#562-personyml-の設定例)
    - [5.6.3. 起動例（routine / 定期投稿）](#563-起動例routine--定期投稿)
    - [ランダム化を活用した例](#ランダム化を活用した例)
- [6. GitHub統合の使用例](#6-github統合の使用例)
  - [6.1. 事前準備](#61-事前準備)
    - [6.1.1. Git環境](#611-git環境)
    - [6.1.2. GitHub プロジェクトの作成](#612-github-プロジェクトの作成)
    - [6.1.3. AIエージェント用GitHubアカウントの準備](#613-aiエージェント用githubアカウントの準備)
      - [マシンアカウントを利用する場合](#マシンアカウントを利用する場合)
      - [GitHub Appを利用する場合](#github-appを利用する場合)
      - [代理エージェント (AIエージェント用に自分自身のアカウント) を利用する場合](#代理エージェント-aiエージェント用に自分自身のアカウント-を利用する場合)
  - [6.2. GitHub統合のセットアップ](#62-github統合のセットアップ)
  - [6.3. チケット駆動ワークフローの実行](#63-チケット駆動ワークフローの実行)
    - [6.3.1. 起動](#631-起動)
    - [6.3.2. AIエージェントへの作業指示](#632-aiエージェントへの作業指示)
    - [6.3.3. AIエージェントとの対話](#633-aiエージェントとの対話)
  - [6.4. できること](#64-できること)
- [7. リファレンス](#7-リファレンス)
  - [7.1. アカウント関連環境変数](#71-アカウント関連環境変数)
  - [7.2. 設定ファイル](#72-設定ファイル)
- [8. トラブルシューティング](#8-トラブルシューティング)
- [9. Contributing](#9-contributing)

---

# 1. 主要機能

## コアフレームワーク
- **マルチエージェント管理**: 異なる役割、個性、能力を持つ複数のAIエージェント（person）を定義
- **柔軟なスケジューリング**: Cronベースのスケジュールコマンドと、person毎のルーチンコマンド
- **コマンド実行フレームワーク**:
  - Markdownコマンド（構造化出力を伴うLLMプロンプト）
  - Pythonスクリプト（コンテキスト注入付き）
  - Shellスクリプト
  - YAMLワークフロー（コマンド合成）
- **Brain抽象化**: LLMプロバイダーの切り替え、またはCLIエージェント（Gemini CLI、Codex CLI、Claude Code）への委譲
- **拡張可能な統合**: 外部サービス向けプラガブルアダプター

## 組み込み機能
- **GitHub統合**（デフォルト）: GitHub Projects/Issuesによるチケット管理、PR作成、コードホスティング
- **国際化**: 多言語対応（英語/日本語）
- **カスタムコマンド**: person/role毎に再利用可能なコマンドテンプレートを定義

# 2. クイックスタート

```bash
# インストール
uv tool install guildbotics

# 設定の初期化
guildbotics config init

# AIエージェント（メンバー）の追加
guildbotics config add

# カスタムコマンドを実行
echo "Hello" | guildbotics run translate English Japanese

# またはスケジューラ起動（デフォルトワークフロー: ticket_driven_workflow を実行）
guildbotics start
```

詳細は[基本的な使い方](#5-基本的な使い方)を、またはチケット駆動ワークフローのセットアップは[GitHub統合の使用例](#6-github統合の使用例)を参照してください。

# 3. 動作環境と事前準備
## 3.1. 動作環境
以下の環境で動作します。
- OS: Linux（Ubuntu 24.04 で動作確認）/ macOS（Sequoia で動作確認）

## 3.2. 必須ソフトウェア
以下のソフトウェアを事前にインストールしてください。
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

## 3.3. LLM API
以下の API キーのいずれかを事前に取得してください。
- Google Gemini API: [Google AI Studio](https://aistudio.google.com/app/apikey)
- OpenAI API: [OpenAI Platform](https://platform.openai.com/api-keys)
- Anthropic Claude API: [Anthropic Console](https://console.anthropic.com/settings/keys)

## 3.4. CLI エージェント
以下のCLI エージェントのいずれかを事前にインストールして一度起動し、認証を行ってください。
- [Gemini CLI](https://github.com/google-gemini/gemini-cli/)
- [OpenAI Codex CLI](https://github.com/openai/codex/)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (Claude Pro または Max サブスクリプションが必要)
- [GitHub Copilot CLI](https://docs.github.com/ja/copilot/concepts/agents/about-copilot-cli)


# 4. インストール

```bash
uv tool install guildbotics
```

# 5. 基本的な使い方

## 5.1. 初期セットアップ

対話的に設定を初期化します:

```bash
guildbotics config init
```

このコマンドは以下を行います:
- 言語選択（英語/日本語）
- 設定ディレクトリの場所を選択
- LLM API設定
- 基本的なプロジェクト構造のセットアップ

作成されるファイル:
- `.env`: 環境変数
- `.guildbotics/config/team/project.yml`: プロジェクト定義
- `.guildbotics/config/intelligences/`: BrainとCLIエージェント設定

## 5.2. メンバーの追加

AIエージェントまたは人間のチームメンバーを追加:

```bash
guildbotics config add
```

このコマンドで以下を入力します:
- メンバータイプ（human、AI agent等）
- 表示名とperson_id
- 役割（例: programmer、architect、product_owner）
- Speaking style（AIエージェントの場合）

作成されるファイル:
- `.guildbotics/config/team/members/<person_id>/person.yml`
- 環境変数（`.env`に認証情報）

チームメンバー毎に繰り返します。

## 5.3. コマンド実行

### 5.3.1. コマンドの種類と配置方法

GuildBoticsでは、複数の種類のコマンドを実行できます。コマンドはプロジェクトの設定ディレクトリに配置します。

**コマンドの種類**:

1. **Markdownコマンド** (`.md`): LLMプロンプトとして実行
   - フロントマターでモデルや出力形式を指定可能
   - テキスト処理、翻訳、要約などに最適

2. **Pythonスクリプト** (`.py`): コンテキスト注入付きで実行
   - プロジェクト情報やチームメンバー情報にアクセス可能
   - 複雑な処理やAPI連携に最適

3. **Shellスクリプト** (`.sh`): シェルコマンドとして実行
   - システムコマンドやツール呼び出しに最適

4. **YAMLワークフロー** (`.yml`): 複数のコマンドを組み合わせて実行
   - コマンドの合成や条件分岐に最適

**コマンドの配置場所**:

コマンドは以下のいずれかのディレクトリに配置します（優先順位順）:

1. **ビルトインワークフロー**: パッケージ内の `guildbotics/templates/` に配置
   - 例: `workflows/ticket_driven_workflow`

2. **メンバー毎のコマンド**: `.guildbotics/config/team/members/<person_id>/commands/`
   - 特定のメンバー専用のコマンド

3. **プロジェクトローカルコマンド**: `.guildbotics/config/commands/`
   - プロジェクト全体で共有するコマンド

4. **グローバルコマンド**: `~/.guildbotics/config/commands/`
   - 全プロジェクトで共有するコマンド

**簡単な例** (`~/.guildbotics/config/commands/translate.md`):
```markdown
以下のテキストが ${1} の場合は ${2} に、${2} の場合は ${1} に翻訳してください:
```

詳細な作成方法は[カスタムコマンド開発ガイド](docs/custom_command_guide.ja.md)を参照してください。

### 5.3.2. コマンドの実行方法

**手動実行**:

```bash
guildbotics run <command_name> [args...]
```

例:
```bash
echo "Hello" | guildbotics run translate 英語 日本語
```

**スケジューラでの自動実行**:

```bash
guildbotics start [routine_commands...]
```

ルーチンコマンドとスケジュールタスクを実行するタスクスケジューラを起動します。コマンドを指定しない場合、デフォルトの`workflows/ticket_driven_workflow`を実行します。

スケジューラを停止するには:
```bash
guildbotics stop [--timeout <seconds>] [--force]
```

- SIGTERM を送信し、最大 `--timeout` 秒（デフォルト: 30）待機します。
- タイムアウト内に終了せず、`--force` が指定されている場合、SIGKILL を送信します。

即座に強制停止する場合:

```bash
guildbotics kill
```

これは `guildbotics stop --force --timeout 0` と同じです。

## 5.4. スケジュール機能

GuildBoticsでは、チームメンバー毎に `person.yml` 設定ファイルを通じてスケジュールタスクを設定できます。スケジューラは2種類のコマンド実行方式をサポートしています。

### 5.4.1. ルーチンコマンド

**ルーチンコマンド** (`routine_commands`) は、ラウンドロビン方式で継続的に実行されるコマンドです。

**特徴**:
- スケジューラがアクティブな間、毎分実行
- 複数のコマンドを指定した場合、順番に1つずつ実行
- `person.yml` で指定しない場合、`guildbotics start` に渡されたデフォルトコマンドを使用 (引数指定がない場合は、 `workflows/ticket_driven_workflow` を使用)

**設定例**:
```yaml
person_id: alice
name: Alice
is_active: true

# デフォルトのルーチンコマンドを上書き（オプション）
routine_commands:
  - workflows/ticket_driven_workflow
  - workflows/custom_workflow
```

**典型的な用途**:
- タスクボードの定期チェック（例: `workflows/ticket_driven_workflow`）
- 継続的な監視タスク
- イベント駆動型の処理

### 5.4.2. スケジュールタスク

**スケジュールタスク** (`task_schedules`) は、cron表記で定義された特定の時刻に実行されるコマンドです。

**特徴**:
- 毎分チェックされ、現在時刻がスケジュールに一致した時に実行
- 1つのコマンドに複数のスケジュールパターンを設定可能
- ランダム化構文（ジッタ）をサポート

**設定例**:
```yaml
person_id: alice
name: Alice
is_active: true

# 特定の時刻に実行するコマンドをスケジュール
task_schedules:
  - command: workflows/cleanup
    schedules:
      - "0 2 * * *"        # 毎日午前2:00
      - "30 14 * * 5"      # 毎週金曜日14:30
  - command: workflows/backup
    schedules:
      - "0 0 1 * *"        # 毎月1日の午前0時
```

**典型的な用途**:
- 定期的なクリーンアップ処理
- バックアップやレポート生成
- 定時実行が必要なタスク

### 5.4.3. Cron表記の詳細

GuildBoticsは標準的な5フィールドのcron表記を使用します:

```
* * * * *
│ │ │ │ │
│ │ │ │ └─── 曜日 (0-6, 日曜日=0)
│ │ │ └───── 月 (1-12)
│ │ └─────── 日 (1-31)
│ └───────── 時 (0-23)
└─────────── 分 (0-59)
```

**よく使う例**:
```yaml
schedules:
  - "0 9 * * *"          # 毎日午前9:00
  - "*/15 * * * *"       # 15分毎
  - "0 */2 * * *"        # 2時間毎
  - "0 0 * * 0"          # 毎週日曜日の午前0時
  - "30 8 1,15 * *"      # 毎月1日と15日の午前8:30
  - "0 22 * * 1-5"       # 平日の午後10:00
```

**ランダム化構文（ジッタ）**:

GuildBoticsは標準cron記法を拡張し、ランダム化をサポートしています:

- `?`: デフォルト範囲内のランダムな値
- `?(min-max)`: 指定範囲内のランダムな値

**例**:
```yaml
schedules:
  - "? 9 * * *"          # 毎日午前9:00-9:59のランダムな分
  - "?(0-30) 14 * * *"   # 毎日14:00-14:30のランダムな分
  - "0 ?(9-17) * * 1-5"  # 平日の9-17時のランダムな時刻（00分）
```

**ランダム化の用途**:
- 複数エージェント間での同時実行を回避
- 人間らしい不規則なタイミングをシミュレート
- 時間枠全体での負荷分散

### 5.4.4. スケジューラの内部動作

スケジューラの動作（`guildbotics/drivers/task_scheduler.py` および `guildbotics/entities/task.py` より）:

**アーキテクチャ**:
1. **person毎のワーカースレッド**: アクティブな各チームメンバーに専用のワーカースレッドが割り当てられます
2. **分単位のチェックサイクル**: 毎分、各ワーカースレッドは以下を行います:
   - 現在のpersonの全 `task_schedules` をチェック
   - スケジュールが現在時刻に一致するコマンドを実行
   - ラウンドロビン順で1つの `routine_command` を実行

**ランダム化の処理**:
1. 初期化時に、ランダム化されたスケジュールの次回実行時刻を計算
2. `?` フィールドについては、境界内でランダムな値をサンプリング
3. 各実行境界に達した後、再サンプリング

**エラーハンドリング**:
- 連続したコマンド失敗（デフォルト: 3回）でワーカースレッドを停止
- エラーログは `~/.guildbotics/data/error.log` に記録

## 5.5. スケジュール設定の例

このセクションでは、実際のスケジュール設定の例を紹介します。

### マルチエージェント・スケジュールワークフローの例

**シナリオ**: 異なるスケジュールを持つ2つのエージェント

**エージェント1** (`.guildbotics/config/team/members/agent1/person.yml`):
```yaml
person_id: agent1
name: Agent One
is_active: true

# チケット駆動ワークフローを定期実行
routine_commands:
  - workflows/ticket_driven_workflow

# 平日午前9時に朝会レポートを生成
task_schedules:
  - command: workflows/morning_standup
    schedules:
      - "0 9 * * 1-5"     # 平日午前9:00
```

**エージェント2** (`.guildbotics/config/team/members/agent2/person.yml`):
```yaml
person_id: agent2
name: Agent Two
is_active: true

# コードレビューチェックを定期実行
routine_commands:
  - workflows/code_review_check

# 週次・月次のメンテナンスタスク
task_schedules:
  - command: workflows/cleanup_old_branches
    schedules:
      - "0 0 * * 0"       # 日曜日午前0時
  - command: workflows/dependency_update_check
    schedules:
      - "?(0-59) 10 1 * *"  # 毎月1日の午前10時台のランダムな分
```

**両方のエージェントを起動**:
```bash
guildbotics start
```

両エージェントは並行して動作し、それぞれがルーチンコマンドを継続的に実行し、スケジュールタスクを指定された時刻に実行します。

### 複数スケジュールパターンの例

1つのコマンドに複数のスケジュールを設定する例:

```yaml
person_id: maintenance_bot
name: Maintenance Bot
is_active: true

task_schedules:
  # クリーンアップを平日の午前2時と週末の午前0時に実行
  - command: workflows/cleanup
    schedules:
      - "0 2 * * 1-5"     # 平日午前2:00
      - "0 0 * * 0,6"     # 週末午前0:00

  # バックアップを毎日の午前3時と月初の午前0時に実行
  - command: workflows/backup
    schedules:
      - "0 3 * * *"       # 毎日午前3:00
      - "0 0 1 * *"       # 毎月1日の午前0時（月次バックアップ）
```

## 5.6. Slack チャットワークフロー

Slackチャットワークフローでは、`person.yml` の `message_channels` に設定したチャネルを監視し、メンションやスレッド継続に反応して返信します。また、定期投稿用のコマンド出力をチャネルへ投稿できます。

受信方式は `socket_mode`（Slack Socket Mode, イベント駆動）を使用します。

補足:

- `polling`（`conversations.history` 巡回）は Slack channel スレッド継続会話を安定して扱えないため、チャット会話ワークフローでは採用していません

詳細設計は `docs/chat_workflow_design.ja.md` を参照してください。

### 5.6.1. 事前設定（Slack 側）

最低限必要な設定:

1. Slack App を作成する
2. Bot Token（`xoxb-...`）を発行する
   - 各エージェントごとに `{PERSON_ID}_SLACK_BOT_TOKEN` を設定する（必須）
   - 例: `alice` 用なら `ALICE_SLACK_BOT_TOKEN`
3. 必要な権限（scope）を付与する
   - Slack App 管理画面の `OAuth & Permissions` -> `Bot Token Scopes` で追加する
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
   - 参考URL（Slack公式）
     - App一覧 / App作成: `https://api.slack.com/apps`
     - `conversations.history`: `https://api.slack.com/methods/conversations.history`
     - `conversations.list`: `https://api.slack.com/methods/conversations.list`
     - `chat.postMessage`: `https://api.slack.com/methods/chat.postMessage`
     - `reactions.add`: `https://api.slack.com/methods/reactions.add`
4. App を Workspace にインストール（scope変更後は再インストールが必要な場合あり）
5. Bot を対象チャネルへ招待する（private channel は特に注意）
6. 設定に必要なIDを控える
   - `channel_id`（`C...` / `G...`）
   - `channel_name` だけでも設定可能（実行時に解決される）が、名前変更の影響を受けにくい `channel_id` の指定を推奨

Socket Mode（本実装の受信方式）に必要な追加設定:

1. Slack App の `Socket Mode` を有効化する
2. App-Level Token（`xapp-...`）を発行し、`{PERSON_ID}_SLACK_APP_TOKEN` に設定する
   - 例: `alice` 用なら `ALICE_SLACK_APP_TOKEN`
3. `Event Subscriptions` を有効化し、bot events を追加する
   - channel を扱う場合: `message.channels`, `message.groups`
   - DM を扱う場合: `message.im`, `message.mpim`
4. `person.yml` の `message_channels` で対象チャネルを設定する（`chat.event_source` は未指定でも `socket_mode`）
5. `guildbotics` 実行環境に `websockets` パッケージがインストールされていることを確認する

補足:

- 複数のSlack botを使い分ける場合、`{PERSON_ID}_SLACK_BOT_TOKEN` を設定すると各エージェントが別botとして投稿できます
- `socket_mode` の WebSocket 接続は、`guildbotics start` 直後ではなく、`workflows/chat_conversation_workflow` が最初に実行されたタイミングで開始されます
- `socket_mode` の接続はエージェント（person）ごとに1本です。同じエージェントが複数チャネルを監視する場合は、その1接続を共有します
- この実装では `conversations.replies` を使わず、ポーリングで観測したメッセージをローカル state に蓄積してスレッド文脈を構成します
- そのため、state を削除した直後や初回起動直後は、過去スレッド全件を遡って読めない場合があります（起動後に観測した範囲から文脈が蓄積されます）
- 初回起動時のウォームアップ運用:
  - スレッド文脈を使った自然な応答を期待する場合、運用開始直後は「新規メンションへの応答中心」で使い始め、数分〜数十分（チャネル流量次第）観測させてから継続会話の精度を評価してください
  - 既存の長いスレッドに対して即時に高精度な文脈応答が必要な場合は、まず人間が再度メンションして最新情報を与える運用にすると安定します

### 5.6.2. `person.yml` の設定例

チャット受信チャネル（`message_channels`）と定期投稿（`task_schedules`）は `team/members/<person_id>/person.yml` に設定します。

```yaml
# team/members/alice/person.yml
person_id: alice
name: Alice
is_active: true

# チャット監視を定期巡回したい場合（routine）
routine_commands:
  - workflows/chat_conversation_workflow

message_channels:
  - service: slack
    name: dev-chat
    chat:
      enabled: true
      channel_id: C0123456789   # 未指定時は channel_name として name を使って解決
      event_source: socket_mode # 既定: socket_mode（省略可）

task_schedules:
  - command: 'workflows/chat_post_command service=slack channel_id=C0123456789 command="reports/ai_news_digest query=\"OpenAI OR Anthropic OR Gemini\" language=ja country=JP limit=10 max_age_hours=24"'
    schedules:
      - "0 9 * * 1-5"
```

ポイント:

- 監視対象チャネルは `person.yml` の `message_channels` で定義し、`chat.enabled: true` のものが対象
- `chat.event_source`: `socket_mode`（チャネル単位、省略時も `socket_mode`）
- `channel_id` 優先。未指定時は `name`（または `chat.channel_name`）を `channel_name` として解決
- 定期投稿は `task_schedules` + `workflows/chat_post_command` を使う（投稿本文は GuildBotics カスタムコマンドの出力）
- 定期投稿の文面は固定文字列ではなく、コマンドの出力で作る
- 例: `reports/ai_news_digest` は前段でニュースRSSを取得し、後段でLLMがSlack向けに要約整形するサンプル

### 5.6.3. 起動例（routine / 定期投稿）

`chat_conversation_workflow` を routine で巡回しつつ、`task_schedules` + `workflows/chat_post_command` で定期投稿を行う例:

```bash
guildbotics start workflows/chat_conversation_workflow
```

または `person.yml` の `routine_commands` に設定しておき、通常どおり起動:

```bash
guildbotics start
```

現在の挙動（概要）:

- 自分宛てメンションに返信
- メンション起点のスレッド継続に返信（条件付き）
- bot-to-bot 自動返信は無効
- 定期投稿は `task_schedules` で `workflows/chat_post_command` を実行して投稿

定期投稿コマンドの具体例（AIニュースダイジェスト）:

```bash
guildbotics run reports/ai_news_digest query="OpenAI OR Anthropic OR Gemini" language=ja country=JP limit=10 max_age_hours=24
```

投稿専用コマンドの例（手動）:

```bash
guildbotics run workflows/chat_post_command service=slack channel_name=dev-chat command='reports/ai_news_digest query="OpenAI OR Anthropic OR Gemini" language=ja country=JP limit=10 max_age_hours=24'
```

このコマンドは、前段で Google News RSS からニュース候補を取得し、LLM で Slack 向けの日本語ダイジェスト文面に整形します。

### ランダム化を活用した例

複数エージェント間での競合を避けるためのランダム化設定:

```yaml
person_id: agent_alpha
name: Agent Alpha
is_active: true

task_schedules:
  # 午前9時台のランダムな時刻にチェックを実行
  - command: workflows/morning_check
    schedules:
      - "?(0-59) 9 * * 1-5"  # 平日の9:00-9:59のランダムな分

  # 日中の時間帯にランダムにモニタリングを実行
  - command: workflows/health_check
    schedules:
      - "0 ?(9-17) * * *"    # 毎日9-17時のランダムな時刻（00分）
```

# 6. GitHub統合の使用例

このセクションでは、デフォルトの `ticket_driven_workflow` を使用して、GitHub Projects および Issues と統合し、チケットベースのAIエージェント協働を行う方法を説明します。

**注**: これは一つの使用例です。GuildBoticsはGitHub統合なしでも、任意のスケジュール化された自動化タスクに使用できます。

## 6.1. 事前準備
### 6.1.1. Git環境
- リポジトリへの Git アクセス方式を設定してください:
  - HTTPS: GCM (Git Credential Manager) をインストールし、サインイン
  - または SSH: SSH 鍵を設定し、`known_hosts` を登録

### 6.1.2. GitHub プロジェクトの作成
GitHub Projects (v2) のプロジェクトを作成し、以下の列（ステータス）をあらかじめ追加しておきます。
  - New (新規)
  - Ready (着手可能)
  - In Progress (進行中)
  - In Review (レビュー中)
  - Retrospective (振り返り)
  - Done (完了)

**メモ:**
- 既存プロジェクトの場合、後述する設定により、すでに存在するステータスと上記のステータスとの紐付けを行うことができます。
- 振り返りを行わない場合は、Retrospective 列は不要です。

### 6.1.3. AIエージェント用GitHubアカウントの準備
AIエージェントがGitHubにアクセスするためのアカウントを用意します。以下のいずれかの方法が利用可能です。

- **マシンアカウント** (マシンユーザー)
  - 「AIエージェントとタスクボードやPull Requestを通じて対話しながら進める」という雰囲気が味わえるという意味でおすすめの方法ですが、[GitHub の利用規約上](https://docs.github.com/ja/site-policy/github-terms/github-terms-of-service#3-account-requirements)、無料で作成できるマシンアカウントは、1ユーザーにつき1つだけとなっていますのでご注意ください。
- **GitHub App**
  - アカウント作成数に制限がないというメリットはありますが、**個人**アカウントの GitHub Project へのアクセスはできません。また、GitHub サイト上ではボットであることが明記されるため、少し雰囲気が削がれます。
- **代理エージェント** (自分自身のアカウントをAIエージェント用に利用する)
  - 最も簡単な利用方法です。ただし、この方法の場合、AIエージェントと対話しながら進めるというよりは自問自答しているという見た目になります。

#### マシンアカウントを利用する場合
マシンアカウント作成後に以下の作業を行ってください。

1. 作成したマシンアカウントをProjectおよびリポジトリに Collaborator として追加してください。
2. Classic PAT 発行
  - **Classic** PAT (Personal Access Token) を発行してください。
  - PATのスコープは、`repo` と `project` の2つを選択してください。

#### GitHub Appを利用する場合
GitHub App作成の際には、以下のPermission設定を行ってください。

- **Repository permissions**
    - **Contents** : Read & Write
    - **Issues** : Read & Write
    - **Projects** : Read & Write
    - **Pull requests** : Read & Write
- **Organization permissions**
    - **Projects** : Read & Write

GitHub App作成後に以下の作業を行ってください。

1. GitHub App設定ページで「Generate a private key」により `.pem` ファイルをダウンロードして、保存してください。
2. 「Install App」からリポジトリ/組織にインストールを行い、**インストールID**を取得してください。インストール後に表示された画面のURLの末尾の数字 (`.../settings/installations/<インストールID>`) がインストールIDです。設定時に利用するため、メモしておいてください。

#### 代理エージェント (AIエージェント用に自分自身のアカウント) を利用する場合
自分自身のアカウントをAIエージェント用に利用する場合、**Classic** PAT を発行してください。
PATのスコープは、`repo` と `project` の2つを選択してください。

## 6.2. GitHub統合のセットアップ

[基本的な使い方](#5-基本的な使い方)の手順を完了した後、GitHub固有の設定を行います:

```bash
guildbotics config verify
```

このコマンドは以下を行います:
- GitHub Projectsにカスタムフィールドを追加:
  - `Mode`: 動作モード（comment/edit/ticket）
  - `Role`: タスク実行時の役割
  - `Agent`: タスクを実行するAIエージェント
- GitHub Projectsのステータスマッピング（New/Ready/In Progress/In Review/Retrospective/Done）

## 6.3. チケット駆動ワークフローの実行

### 6.3.1. 起動
以下のコマンドで起動します:

```bash
guildbotics start
```

### 6.3.2. AIエージェントへの作業指示

AIエージェントにタスクを依頼するには、GitHub Projectsのチケットを以下のように操作します:

1. チケットを作成し、対象のGitリポジトリを選択してIssueとして保存
2. チケットにAIエージェントへの指示を記述
   - この内容がエージェントへのプロンプトとなるため、できるだけ具体的に記述
3. `Agent` フィールドでタスクを実行するAIエージェントを選択
4. `Mode` フィールドを設定
   - `comment`: エージェントにチケットコメントで返信してもらう
   - `edit`: エージェントにファイル編集とPull Request作成を依頼
   - `ticket`: エージェントにチケット作成を依頼
5. 必要に応じて `Role` フィールドでタスク実行時の役割を指定
6. チケットのステータスを `Ready` に変更

**メモ:**
AIエージェントは `~/.guildbotics/data/workspaces/<person_id>` 配下に指定されたGitリポジトリをクローンして作業を行います。

### 6.3.3. AIエージェントとの対話
- AIエージェントは作業中に質問がある場合、チケットコメントで質問を投稿します。チケットコメントで回答してください。エージェントは定期的にチケットコメントをチェックし、回答が得られれば作業を進めます。
- AIエージェントがタスクを完了すると、チケットのステータスを `In Review` に変更し、実施結果と作成したPull Request のURLをコメントで投稿します。
- `edit` モードでは、AIエージェントがPull Requestを作成します。PRのコメントでレビュー結果を書き込んでください。`In Review` 状態のチケットがある場合、エージェントはPRのコメントをチェックして対応します。

## 6.4. できること

チケット駆動ワークフローでは以下が可能です:

- **タスクボードでのAIエージェントへのタスク依頼**
  - タスクボード上のチケットでAIエージェントをアサインして **Ready** 列にチケットを移動すれば、AIエージェントがそのタスクを実行します
- **AIエージェントの実行結果をタスクボード上で確認**
  - AIエージェントがタスクを完了すると、チケットが **In Review** 列に移動し、実施結果がチケットのコメントとして書き込まれます
- **AIエージェントによるPull Requestの作成**
  - AIエージェントはタスクを完了すると Pull Requestを作成します
- **チケット作成**
  - AIエージェントに対してチケット作成の指示を出せば、AIエージェントが自動でタスクボード上にチケットを作成します
- **振り返り**
  - タスク実施済みチケットをタスクボード上の **Retrospective** 列に移動させ、振り返りの実施依頼をコメントに書き込めば、AIエージェントが作成したPull Requestのレビュワーとのやりとりに関して分析及び課題抽出を行い、改善チケットを作成します

# 7. リファレンス

## 7.1. アカウント関連環境変数

**LLM APIキー**:
- `GOOGLE_API_KEY`: Google Gemini API
- `OPENAI_API_KEY`: OpenAI API
- `ANTHROPIC_API_KEY`: Anthropic Claude API

**Slack アクセス**:
- `{PERSON_ID}_SLACK_BOT_TOKEN`: person毎の Slack Bot Token（必須）
- `{PERSON_ID}_SLACK_APP_TOKEN`: person毎の Slack App-Level Token（`person.yml` の `message_channels[].chat.event_source=socket_mode` を使う場合に必須）

**GitHub アクセス**（person毎、形式: `{PERSON_ID}_...`）:
- `{PERSON_ID}_GITHUB_ACCESS_TOKEN`: マシンアカウント/代理エージェント用PAT
- `{PERSON_ID}_GITHUB_APP_ID`, `{PERSON_ID}_GITHUB_INSTALLATION_ID`, `{PERSON_ID}_GITHUB_PRIVATE_KEY_PATH`: GitHub App用

カレントディレクトリに `.env` ファイルが存在する場合、自動的に読み込まれます。

## 7.2. 設定ファイル

**プロジェクト設定** (`team/project.yml`):
- `language`: プロジェクト言語
- `repositories`: リポジトリ定義
- `services.ticket_manager`: GitHub Projects設定
- `services.code_hosting_service`: GitHubリポジトリ設定

**メンバー設定** (`team/members/<person_id>/person.yml`):
- `person_id`: 一意な識別子（英数字小文字、`-`、`_` のみ）
- `name`: 表示名
- `is_active`: AIエージェントとして動作するかどうか
- `roles`: 役割の割り当て
- `routine_commands`: デフォルトルーチンコマンドの上書き
- `task_schedules`: Cronベースのスケジュールコマンド
- `task_schedules[].command`: 定期投稿は `workflows/chat_post_command ...` を指定して実現可能
- `message_channels`: 監視対象チャネル設定（`chat.enabled`, `chat.event_source=socket_mode`, `channel_id`/`name`）

**Brain/CLIエージェント設定**:
- `intelligences/cli_agent_mapping.yml`: デフォルトCLIエージェント選択
- `intelligences/cli_agents/*.yml`: CLIエージェントスクリプト
- `team/members/<person_id>/intelligences/`: person毎の上書き

# 8. トラブルシューティング

**エラーログ**: エラーが発生した場合は `~/.guildbotics/data/error.log` で詳細を確認してください。

**デバッグ出力**: 詳細なログを取得するための環境変数:
- `LOG_LEVEL`: `debug` / `info` / `warning` / `error`
- `LOG_OUTPUT_DIR`: ログファイルを書き込むディレクトリ (例: `./tmp/logs`)
- `AGNO_DEBUG`: Agnoエンジンの追加デバッグ出力 (`true`/`false`)

# 9. Contributing

コントリビューションを歓迎します！[CONTRIBUTING.md](CONTRIBUTING.md)を参照してください:
- コーディングスタイルと規約
- テストガイドライン
- ドキュメント標準
- セキュリティベストプラクティス
