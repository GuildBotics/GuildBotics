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
    - [ランダム化を活用した例](#ランダム化を活用した例)
  - [5.6. Slack チャットワークフロー](#56-slack-チャットワークフロー)
    - [5.6.1. 事前設定（Slack 側）](#561-事前設定slack-側)
      - [基本的な設定](#基本的な設定)
      - [複数のエージェントを追加する場合](#複数のエージェントを追加する場合)
    - [5.6.2. `person.yml` の設定例](#562-personyml-の設定例)
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

GuildBotics の設定は **GuildBotics デスクトップアプリ（GUI）** で行い、実行は **`guildbotics` CLI** で行います。
セットアップ結果はすべてプレーンな設定ファイル（`.env` と `.guildbotics/config/...`）に書き出されるため、
一度設定すれば、それらのファイルをコピーするだけで GUI のない環境（ヘッドレスサーバー等）でも CLI を実行できます。

```bash
# 1. GUI で設定する
#    GuildBotics デスクトップアプリを起動し、プロジェクトとメンバーのセットアップを完了します。
#    選択したワークスペースに .env と .guildbotics/config/... が書き出されます。
#    インストール方法は desktop/README.md を参照してください。

# 2. CLI で実行する
uv tool install guildbotics

# カスタムコマンドを実行（.env / .guildbotics があるワークスペースで実行）
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

セットアップは **GuildBotics デスクトップアプリ** で行い、コマンド実行は **`guildbotics` CLI** で行います。

**デスクトップアプリ（セットアップ）:** GuildBotics デスクトップアプリをビルド／インストールします。
ビルド・インストール手順は [desktop/README.md](desktop/README.md) を参照してください（現状 macOS Apple Silicon 対応）。

**CLI（実行）:**

```bash
uv tool install guildbotics
```

# 5. 基本的な使い方

## 5.1. 初期セットアップ

プロジェクトのセットアップは **GuildBotics デスクトップアプリ** で行います。
アプリを起動してワークスペースディレクトリを選び、プロジェクトセットアップのフォームを入力します。GUI は以下を行います:

- 言語選択（英語/日本語）
- 設定ディレクトリの場所を選択（ワークスペースまたはホーム）
- LLM API 設定とデフォルト CLI エージェントの設定
- 基本的なプロジェクト構造のセットアップ

書き出されるファイル:
- `.env`: 環境変数
- `.guildbotics/config/team/project.yml`: プロジェクト定義
- `.guildbotics/config/intelligences/`: BrainとCLIエージェント設定

> これらはすべてプレーンテキストの設定ファイルです。設定はすべて設定ファイルに落ちるため、
> GUI のない環境（サーバー等）へファイルを移し、`guildbotics` CLI だけで運用できます。

## 5.2. メンバーの追加

AIエージェントまたは人間のチームメンバーは、デスクトップアプリの **Members** セクションから追加します。
各メンバーについて以下を入力します:

- メンバータイプ（human、AI agent等）
- 表示名とperson_id
- 役割（例: programmer、architect、product_owner）
- Speaking style（AIエージェントの場合）
- GitHub / Slack の認証情報（`.env` に保存されます）

書き出されるファイル:
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

既定では、以下の2つを起動します。

- タスクスケジューラ（ルーチンコマンド / スケジュールタスク）
- イベントリスナーランナー（Slack Socket Mode などのイベント駆動受信）

コマンドを指定しない場合、スケジューラ側のデフォルトルーチンとして `workflows/ticket_driven_workflow` を実行します。

起動対象を片方に限定することもできます:

```bash
guildbotics start --only scheduler
guildbotics start --only events
```

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
- 定期巡回型の処理

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

## 5.6. Slack チャットワークフロー

Slackチャットワークフローでは、`person.yml` の `message_channels` に設定したチャネルを監視し、メンションやスレッド継続に反応して返信します。また、定期投稿用のコマンド出力をチャネルへ投稿できます。

チャット受信は `guildbotics start` で起動されるイベントリスナーランナーが担当するため、`--only scheduler` オプションでスケジューラのみを起動している場合は受信できません。

CLI ベースのチャット返信では、返信生成は agent ごとの workspace root である `~/.guildbotics/data/workspaces/<person_id>/` を `cwd` にして実行されます。この配下にある cloned repository を参照できます。また、GuildBotics は `~/.guildbotics/data/memory/<person_id>/` に agent ごとの personal memory リポジトリも保持します。
既定の personal memory backend は Cognee で、person ごとに `guildbotics:person:<person_id>` dataset を使います。`GUILDBOTICS_MEMORY_BACKEND=file` を設定すると fallback / test / migration 用の file backend を使い、`memory_index.yml` と `topics/<topic_id>/memory.md` の topic 別メモリーへ保存します。`GUILDBOTICS_MEMORY_BACKEND=fake` は deterministic test 用です。GuildBotics は返信生成前に関連メモリーを取得して正規化済み `memory_context` を prompt 経由で渡し、返信投稿後に独立したメモリー更新ステップを実行します。`GUILDBOTICS_MEMORY_TRACE=1` を設定すると recall / remember の正規化済み trace event を JSONL に追記します。`GUILDBOTICS_MEMORY_TRACE_PATH` で出力先を指定でき、未指定時は `~/.guildbotics/data/run/memory_trace.jsonl` を使います。
`person.yml` の `character` には、興味・嗜好・会話参加方針などを定義できます。チャット返信判断と返信生成はこの profile を参照し、明示メンションがない会話でも、その agent ならではの観点を足せる場合に参加できます。

### 5.6.1. 事前設定（Slack 側）

#### 基本的な設定

AIエージェントとして振る舞う Slack App（送信 + 受信）を Slack 上で作成します。

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
   - 参考URL（Slack公式）
     - `conversations.history`: `https://api.slack.com/methods/conversations.history`
     - `conversations.list`: `https://api.slack.com/methods/conversations.list`
     - `chat.postMessage`: `https://api.slack.com/methods/chat.postMessage`
     - `reactions.add`: `https://api.slack.com/methods/reactions.add`
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

#### 複数のエージェントを追加する場合

2人め以降も同様の設定を行えば追加できますが、Socket Mode の設定をスキップして最初のAIエージェントで設定した通信経路を共有することもできます。

受信接続を共有する場合は、既存 Person と同じ App-Level Token を二人目の `{PERSON_ID}_SLACK_APP_TOKEN` に設定します。

- 例: `alice` と `bob` が同じ受信接続を共有する場合
  - `ALICE_SLACK_APP_TOKEN=<alice_xapp_token>`
  - `BOB_SLACK_APP_TOKEN=<alice_xapp_token>`

別の受信経路にしたい場合（例: 別 Workspace、別 Slack App で分離したい場合）は、追加の Slack App を作成して Socket Mode / Event Subscriptions / App-Level Token を別途設定します。


### 5.6.2. `person.yml` の設定例

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

task_schedules:
  - command: 'workflows/chat_post_command service=slack channel_id=C0123456789 command="examples/reports/ai_news_digest query=\"OpenAI OR Anthropic OR Gemini\" language=ja country=JP limit=10 max_age_hours=24"'
    schedules:
      - "0 9 * * 1-5"
```

ポイント:

- 監視対象チャネルは `person.yml` の `message_channels` で定義し、`chat.enabled: true` のものが対象
- 定期投稿は `task_schedules` + `workflows/chat_post_command` を使う（投稿本文は GuildBotics カスタムコマンドの出力）
- 例: `examples/reports/ai_news_digest` は前段でニュースRSSを取得し、後段でLLMがSlack向けに要約整形するサンプル


定期投稿コマンドの具体例（AIニュースダイジェスト）:

```bash
guildbotics run examples/reports/ai_news_digest query="OpenAI OR Anthropic OR Gemini" language=ja country=JP limit=10 max_age_hours=24
```

投稿専用コマンドの例（手動）:

```bash
guildbotics run workflows/chat_post_command service=slack channel_name=dev-chat command='examples/reports/ai_news_digest query="OpenAI OR Anthropic OR Gemini" language=ja country=JP limit=10 max_age_hours=24'
```

このコマンドは、前段で Google News RSS からニュース候補を取得し、LLM で Slack 向けの日本語ダイジェスト文面に整形します。

# 6. GitHub統合の使用例

このセクションでは、デフォルトの `ticket_driven_workflow` を使用して、GitHub Projects および Issues と統合し、チケットベースのAIエージェント協働を行う方法を説明します。

**注**: これは一つの使用例です。GuildBoticsはGitHub統合なしでも、任意のスケジュール化された自動化タスクに使用できます。

## 6.1. 事前準備
### 6.1.1. Git環境
- リポジトリへの Git アクセス方式を設定してください:
  - HTTPS: GCM (Git Credential Manager) をインストールし、サインイン
  - または SSH: SSH 鍵を設定し、`known_hosts` を登録
- 各 AI メンバーの GitHub 認証情報を GuildBotics に設定してください。チケット駆動ワークフローの
  書き込み（branch push、PR 作成、issue comment、review reply）は、ローカルの `gh auth`
  ユーザーではなく、割り当てられたメンバーの machine user token または GitHub App installation
  を使って行われます。
- Codex CLI を CLI エージェントとして使う場合は、Codex CLI の認証とネットワーク到達性を確認してください:
  ```bash
  codex doctor
  ```

### 6.1.2. GitHub プロジェクトの作成
GitHub Projects (v2) のプロジェクトを作成し、以下の列（ステータス）をあらかじめ追加しておきます。
  - Todo (着手可能)
  - In Progress (進行中)
  - Done (完了)

**メモ:**
- 既存プロジェクトの場合、後述する設定により、すでに存在するステータスと上記のレーンとの紐付けを行うことができます。

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

[基本的な使い方](#5-基本的な使い方)の手順を完了した後、デスクトップアプリの
**Diagnostics / Verify** ビューから設定を検証します。各アクティブメンバーの GitHub・LLM・CLI エージェント設定が
実際に利用可能かをチェックします。

**カスタムフィールド** は、GuildBotics が GitHub Project を最初に操作したときに自動で作成されるため、
明示的なセットアップ手順は不要です。GuildBotics は、GitHub assignee だけでは表現しきれない場合のために
タスクを実行するAIエージェントを選ぶ `Agent` フィールドを管理します。

**レーンマッピング**: GuildBotics は GitHub Projects のステータスを軽量な workflow lane として使います。
既定では `Todo` を着手可能、`In Progress` を作業中、`Done` を完了として扱います。着手可能レーンと完了レーンは
作業対象の境界も兼ねます。ボード上でこの 2 つの**間**に位置するステータス（例: `In Review`）は自動的に作業中
レーンとして扱われ、着手可能レーンより**前**のステータス（例: `Backlog`）や完了レーン**以降**のステータス
（例: `Icebox`）は無視されます。したがって、途中レーンや保留レーンはボード列の並び順だけで追加でき、`lane_map`
を変更する必要はありません。
着手可能 / 作業中 / 完了レーンに独自のステータス名を使っている場合は、`team/project.yml` の
`services.ticket_manager.lane_map` キーでマッピングを指定してください（[設定ファイル](#72-設定ファイル)を参照）。
デスクトップのセットアップアプリの GitHub セクションでも設定できます。Project の status options を取得できる場合は
その候補から選べ、取得できない場合は手入力にフォールバックします。標準的な `Todo` / `In Progress` / `Done` の
ボードなら既定値で動作するため、ここでのレーン設定は不要です。

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
3. 対象のAIエージェントを assign するか、必要に応じて `Agent` フィールドで選択
4. チケットを着手可能レーンへ移動

**メモ:**
AIエージェントは `~/.guildbotics/data/workspaces/<person_id>` 配下に指定されたGitリポジトリをクローンして作業を行います。

### 6.3.3. AIエージェントとの対話
- AIエージェントは作業中に質問がある場合、チケットコメントで質問を投稿します。チケットコメントで回答してください。エージェントは定期的にチケットコメントをチェックし、回答が得られれば作業を進めます。
- AIエージェントがタスクを完了すると、実施結果と作成したPull Request のURLをコメントで投稿します。
- チケットから作成された PR にはレビュー結果を PR 上で書き込んでください。GuildBotics は未対応 review thread を確認し、担当エージェントへ再委譲します。

## 6.4. できること

チケット駆動ワークフローでは以下が可能です:

- **タスクボードでのAIエージェントへのタスク依頼**
  - タスクボード上のチケットでAIエージェントをアサインして着手可能レーンにチケットを移動すれば、AIエージェントがそのタスクを実行します
- **AIエージェントの実行結果をタスクボード上で確認**
  - AIエージェントがタスクを完了すると、実施結果がチケットのコメントとして書き込まれます
- **AIエージェントによるPull Requestの作成**
  - コード変更が必要な場合、GuildBotics はエージェントの workspace 変更から Pull Requestを作成します
- **チケット作成**
  - AIエージェントに対してチケット作成の指示を出せば、GuildBotics がエージェントの構造化結果からタスクボード上にチケットを作成します

# 7. リファレンス

## 7.1. アカウント関連環境変数

**LLM APIキー**:
- `GOOGLE_API_KEY`: Google Gemini API
- `OPENAI_API_KEY`: OpenAI API
- `ANTHROPIC_API_KEY`: Anthropic Claude API

Cognee memory もこれらのキーを再利用します。`LLM_API_KEY` が未設定の場合、GuildBotics は `OPENAI_API_KEY` を Cognee の OpenAI LLM / embedding 設定へ、または `GOOGLE_API_KEY` を Cognee の Gemini LLM / embedding 設定へ補完します。`OPENAI_API_KEY` と `GOOGLE_API_KEY` の両方が設定されている場合、Cognee memory は既定で `OPENAI_API_KEY` を優先します。この優先順を変えたい場合は、Cognee の `LLM_*` / `EMBEDDING_*` 変数を明示的に設定してください。`ANTHROPIC_API_KEY` は Cognee の LLM には使えますが、Cognee では別途 embedding provider / key の明示設定が必要です。

**Slack アクセス**:
- `{PERSON_ID}_SLACK_BOT_TOKEN`: person毎の Slack Bot Token
- `{PERSON_ID}_SLACK_APP_TOKEN`: person毎の Slack App-Level Token

**GitHub アクセス**（person毎、形式: `{PERSON_ID}_...`）:
- `{PERSON_ID}_GITHUB_ACCESS_TOKEN`: マシンアカウント/代理エージェント用PAT
- `{PERSON_ID}_GITHUB_APP_ID`, `{PERSON_ID}_GITHUB_INSTALLATION_ID`, `{PERSON_ID}_GITHUB_PRIVATE_KEY_PATH`: GitHub App用

カレントディレクトリに `.env` ファイルが存在する場合、自動的に読み込まれます。

## 7.2. 設定ファイル

**プロジェクト設定** (`team/project.yml`):
- `name`: プロジェクト名
- `description`: エージェント文脈として使う短いプロジェクト説明
- `language`: プロジェクト言語
- `repositories`: リポジトリ定義
- `services.ticket_manager`: GitHub Projects設定
- `services.ticket_manager.lane_map`: ready / working / done lane を GitHub Project のステータス名に対応付けます。Project が独自のステータス名を使う場合に設定します。
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
- `~/.guildbotics/data/memory/<person_id>/`: チャット返信用の person 別ローカル記憶リポジトリ

# 8. トラブルシューティング

**エラーログ**: エラーが発生した場合は `~/.guildbotics/data/error.log` で詳細を確認してください。

**デバッグ出力**: 詳細なログを取得するための環境変数:
- `LOG_LEVEL`: `debug` / `info` / `warning` / `error`
- `LOG_OUTPUT_DIR`: ログファイルを書き込むディレクトリ (例: `./tmp/logs`)
- `AGNO_DEBUG`: Agnoエンジンの追加デバッグ出力 (`true`/`false`)

# 9. Contributing

コントリビューションを歓迎します！[CONTRIBUTING.ja.md](CONTRIBUTING.ja.md)を参照してください:
- コーディングスタイルと規約
- CI と揃えたローカルの lint / 型チェック / テスト手順
- テストガイドライン
- ドキュメント標準
- セキュリティベストプラクティス
