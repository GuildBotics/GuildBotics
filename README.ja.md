<h1>GuildBotics</h1>

[English](https://github.com/GuildBotics/GuildBotics/blob/main/README.md) • [日本語](https://github.com/GuildBotics/GuildBotics/blob/main/README.ja.md)

GuildBotics は、AI エージェントを「チームメンバー」として開発チームに参加させるツールです。各メンバーは自分の GitHub / Slack アカウントを持ち、GitHub Projects のチケットを拾って調査・実装・プルリクエスト作成まで進め、レビューコメントや Slack のメンションに応答します。実際の調査・実装・判断は Claude Code や Codex などの AI CLI ツールが行い、メンバーとしての外部操作（コミット、PR、コメント、Slack 投稿、記憶の保存）はすべて専用 CLI（`guildbotics member`）を通して実行・記録されます。

メンバーとの働き方は 2 つあります。

- **一緒に作業する** — Claude Code や Codex のセッションで guildbotics スキルを使ってメンバーを呼び出し、いま開いているリポジトリでペアプログラミングします。作業の進め方をその場で教えられます。
- **任せる** — スケジューラがメンバーを定期起動し、チケットの取得から PR 作成までを自律的に進めます。フィードバックは PR レビューやチケットコメント、Slack の返信で返します（詳細は [6. GitHub統合](#6-github統合)）。

どちらのモードでも同じメンバー（同じアカウント、同じ記憶）が働きます。一緒に作業しながら教えたことは記憶として残り、任せたときの作業にも引き継がれます。

設定と監視は Desktop アプリ（GUI）で行い、実行は `guildbotics` CLI で行います（GUI のないサーバでも実行できます）。メンバーの振る舞いは、Markdown プロンプト / Python / Shell / YAML のカスタムコマンドとスケジュール定義でカスタマイズできます。

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
  - [3.4. AI CLIツール](#34-ai-cliツール)
- [4. インストール](#4-インストール)
- [5. 基本的な使い方](#5-基本的な使い方)
  - [5.1. 初期セットアップ](#51-初期セットアップ)
  - [5.2. その他の設定](#52-その他の設定)
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
- [6. GitHub統合](#6-github統合)
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
  - [7.2. シークレットの保存](#72-シークレットの保存)
  - [7.3. 設定ファイル](#73-設定ファイル)
- [8. トラブルシューティング](#8-トラブルシューティング)

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
- **Brain抽象化**: LLMプロバイダーの切り替え、またはAI CLIツール（Codex、Claude Code、Antigravityなど、ローカルCLIから起動するAI実行ツール）への委譲

## 組み込み機能

- **GitHub統合**（デフォルト）: GitHub Projects/Issuesによるチケット管理、PR作成、コードホスティング
- **Slack統合**: 設定したチャンネルをメンバーが監視し、本人として返信・リアクションするチャットワークフロー
- **メンバー記憶**: メンバーがセッションを越えて参照・維持する個人/チームの記憶
- **対話メンバーセッション**: guildbotics スキルにより、AI CLIツールが現在のリポジトリでメンバーとして作業
- **国際化**: 多言語対応（英語/日本語）
- **カスタムコマンド**: person/role毎に再利用可能なコマンドテンプレートを定義

# 2. クイックスタート

GuildBotics の設定は **GuildBotics デスクトップアプリ（GUI）** で行い、実行は **`guildbotics` CLI** で行います。
デスクトップアプリには CLI が同梱されており、初回起動時に AI CLIツールから参照しやすい場所へ管理用 CLI とスキルを配置します。
セットアップ結果はプレーンな設定ファイル（`.env` と `.guildbotics/config/...`）に書き出されます。
API キーやアカウントトークンなどのシークレットは、利用可能な場合は OS のキーチェーンに保存されます
（[7.2. シークレットの保存](#72-シークレットの保存) 参照）。一度設定すれば、設定ファイルをコピーし、
シークレットを `guildbotics secrets export` / `import` で移行するだけで、GUI のない環境（ヘッドレスサーバー等）でも CLI を実行できます。

```bash
# 1. デスクトップアプリで設定する
#    GuildBotics デスクトップアプリを起動し、プロジェクトとメンバーのセットアップを完了します。
#    選択したワークスペースに .env と .guildbotics/config/... が書き出されます。
#    インストール方法は desktop/README.md を参照してください。

# 2. デスクトップアプリが配置した管理用 CLI で実行する
#    ~/.local/bin が PATH にあれば "guildbotics" shim も使えます。
#    安定した絶対パスは常に以下です。
~/.guildbotics/bin/guildbotics workspace status

# カスタムコマンドを実行
echo "Hello" | ~/.guildbotics/bin/guildbotics run translate English Japanese

# またはメンバーワーカーとイベントリスナーを起動
~/.guildbotics/bin/guildbotics start
```

詳細は[基本的な使い方](#5-基本的な使い方)を、またはチケット駆動ワークフローのセットアップは[GitHub統合](#6-github統合)を参照してください。

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

## 3.4. AI CLIツール

以下のAI CLIツールのいずれかを事前にインストールして一度起動し、認証を行ってください。

- [Antigravity CLI](https://github.com/google-antigravity/antigravity-cli)
- [OpenAI Codex CLI](https://github.com/openai/codex/)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (Claude Pro または Max サブスクリプションが必要)
- [GitHub Copilot CLI](https://docs.github.com/ja/copilot/concepts/agents/about-copilot-cli)

GuildBoticsでCodexまたはClaude Codeを利用する場合は、セッションを引き継ぎ、前回の
続きから作業を再開できます。認証方法、Slackスレッドやチケットとセッションの対応付け、
実行権限の設定、セッションのリセット方法、問題が起きたときの対処については、
[Codex・Claude Codeのセッション連携](docs/native_agent_runtime.ja.md)をご覧ください。

# 4. インストール

セットアップは **GuildBotics デスクトップアプリ** で行い、コマンド実行は **`guildbotics` CLI** で行います。

**デスクトップアプリ（セットアップ + 管理用 CLI）:** GuildBotics デスクトップアプリをビルド／インストールします。
ビルド・インストール手順は [desktop/README.md](desktop/README.md) を参照してください（macOS Apple Silicon と Linux x86_64 に対応）。
初回起動時に以下が配置されます。

- `~/.guildbotics/bin/guildbotics`: AI CLIツール / スキルが使う管理用 GuildBotics CLI
- `~/.local/bin/guildbotics`: 未作成、または既存の管理用 shim の場合だけ更新される小さな shim
- 検出済みの Codex / Claude Code / Antigravity CLI / GitHub Copilot CLI のユーザースキル用ディレクトリ配下の
  GuildBotics スキル。ユーザーが作成・編集したスキルは上書きしません。

**単体 CLI（ヘッドレス / 非デスクトップ環境）:** デスクトップアプリを使わない環境、または別管理の CLI を明示的に使いたい場合だけ `uv tool install guildbotics` を使います。

# 5. 基本的な使い方

## 5.1. 初期セットアップ

プロジェクトのセットアップは **GuildBotics デスクトップアプリ** で行います。
アプリを起動すると **プロジェクト** 設定が開くので、以下を設定します:

- 言語選択（英語/日本語）
- ワークスペースフォルダの選択
- プロジェクトの説明文
- GitHub連携を行うかどうか

GuildBotics では、プロジェクトの作業場所として選ぶフォルダを **ワークスペース** と呼びます。
ワークスペースには、以下のような GuildBotics の設定ファイルが書き出されます。

- `.env`: 環境変数設定（ログレベル等の非シークレット設定）
- `.guildbotics/config/secrets.yml`: OS キーチェーンに保存したシークレットのキー名一覧（値は含まない）
- `.guildbotics/config/team/project.yml`: プロジェクト定義
- `.guildbotics/config/intelligences/`: BrainとAI CLIツール設定

これらはすべてプレーンテキストの設定ファイルです。OS キーチェーンを利用している場合、
API キーやアカウントトークンの値はキーチェーン側に保存され、上記ファイルにはキー名だけが記録されます
（[7.2. シークレットの保存](#72-シークレットの保存) 参照）。
非シークレットの設定はすべて上記のファイルに保存されるため、GUI のない環境（サーバー等）へワークスペースフォルダをコピーし、
シークレットを `guildbotics secrets export` / `import` か環境変数で引き渡せば、`guildbotics` CLI だけで運用できます。

なお使用中のワークスペースは `~/.guildbotics/data/active-workspace.json` に記録されます。
Desktop と Local API は runtime service の構築前に、この backend 管理の状態を復元します。
状態が未設定、不正、または保存先ディレクトリが存在しない場合は、プロセスの起動 cwd を使用します。
CLI からワークスペースを確認・変更するには以下を使います。

```bash
guildbotics workspace current
guildbotics workspace use /path/to/workspace
```

GuildBotics が保存するローカルデータは、大きく 2 種類あります。

- 使用中のワークスペース情報や CLI スケジューラーの PID など、PC 全体で共有する管理情報は
  `$HOME/.guildbotics/data` に保存されます。
- メンバーごとの作業ディレクトリ、タスクやチャット実行の証跡、診断ログ、プロンプトの記録、
  チャット状態など、ワークスペースごとの実行データは、既定で `<workspace>/.guildbotics/data`
  に保存されます。この保存場所を変更したい場合は、.env に `GUILDBOTICS_DATA_DIR` を設定します。

## 5.2. その他の設定

プロジェクトの設定が終わったら、以下の初期設定を行う必要があります

- **LLM・AI CLIツール:** デフォルトのLLM、AI CLIツールの設定とLLM API Key設定
- **メンバー:** チームメンバーの追加と設定
- **GitHub:** GitHub連携の設定 (GitHubを利用する場合のみ)

## 5.3. コマンド実行

### 5.3.1. コマンドの種類と配置方法

GuildBoticsでは、複数の種類のコマンドを実行できます。コマンドはプロジェクトの設定ファイル用ディレクトリに配置します。

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

コマンドは以下の場所から解決されます（優先順位順）:

1. **メンバー毎のコマンド**: `.guildbotics/config/team/members/<person_id>/commands/`
   - 特定のメンバー専用のコマンド

2. **プロジェクトローカルコマンド**: `.guildbotics/config/commands/`
   - プロジェクト全体で共有するコマンド

3. **組み込みコマンド**: パッケージ内の `guildbotics/templates/` に配置（フォールバック）
   - 例: `workflows/ticket_driven_workflow`

設定ディレクトリは既定でワークスペースの `.guildbotics/config` であり、環境変数
`GUILDBOTICS_CONFIG_DIR` で変更できます。

**簡単な例** (`.guildbotics/config/commands/translate.md`):

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

**メンバーワーカーでの自動実行**:

```bash
guildbotics start
```

既定では、以下の2つを起動します。

- メンバーワーカー（ルーチンコマンド / スケジュールタスク / キュー済みイベント）
- イベントリスナーランナー（Slack Socket Mode などのイベント駆動受信）

各メンバーワーカーは、そのメンバーの `person.yml` に設定された `routine_commands` を実行します。

起動対象を片方に限定することもできます:

```bash
guildbotics start --only scheduler
guildbotics start --only events
```

実行中のサービスを停止するには:

```bash
guildbotics stop
```

スケジューラは新しい作業の受付を止め、実行中の作業の完了を待ってから終了します。
`guildbotics stop` をもう一度実行すると実行中の作業をキャンセルし、`guildbotics kill` は
即座に強制停止します。`--timeout` や `--force` などのオプションは
[CLI リファレンス](docs/cli_reference.md#guildbotics-stop)を参照してください。

## 5.4. スケジュール機能

GuildBoticsでは、チームメンバー毎に `person.yml` 設定ファイルを通じてスケジュールタスクを設定できます。スケジューラは2種類のコマンド実行方式をサポートしています。

### 5.4.1. ルーチンコマンド

**ルーチンコマンド** (`routine_commands`) は、ラウンドロビン方式で継続的に実行されるコマンドです。

**特徴**:

- スケジューラがアクティブな間、毎分実行
- 複数のコマンドを指定した場合、順番に1つずつ実行
- 初期セットアップは新規エージェントメンバーに `workflows/ticket_driven_workflow` を設定する。`routine_commands` が無いメンバーはルーチンコマンドを実行しない

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
      - "0 2 * * *" # 毎日午前2:00
      - "30 14 * * 5" # 毎週金曜日14:30
  - command: workflows/backup
    schedules:
      - "0 0 1 * *" # 毎月1日の午前0時
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
  - "0 9 * * *" # 毎日午前9:00
  - "*/15 * * * *" # 15分毎
  - "0 */2 * * *" # 2時間毎
  - "0 0 * * 0" # 毎週日曜日の午前0時
  - "30 8 1,15 * *" # 毎月1日と15日の午前8:30
  - "0 22 * * 1-5" # 平日の午後10:00
```

**ランダム化構文（ジッタ）**:

GuildBoticsは標準cron記法を拡張し、ランダム化をサポートしています:

- `?`: デフォルト範囲内のランダムな値
- `?(min-max)`: 指定範囲内のランダムな値

**例**:

```yaml
schedules:
  - "? 9 * * *" # 毎日午前9:00-9:59のランダムな分
  - "?(0-30) 14 * * *" # 毎日14:00-14:30のランダムな分
  - "0 ?(9-17) * * 1-5" # 平日の9-17時のランダムな時刻（00分）
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
- 検索用の実行サマリーは `<workspace>/.guildbotics/data/run/diagnostics.jsonl`、
  実行ごとの全文記録は `run/sessions/` に保存

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
      - "0 9 * * 1-5" # 平日午前9:00
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
      - "0 0 * * 0" # 日曜日午前0時
  - command: workflows/dependency_update_check
    schedules:
      - "?(0-59) 10 1 * *" # 毎月1日の午前10時台のランダムな分
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
      - "0 2 * * 1-5" # 平日午前2:00
      - "0 0 * * 0,6" # 週末午前0:00

  # バックアップを毎日の午前3時と月初の午前0時に実行
  - command: workflows/backup
    schedules:
      - "0 3 * * *" # 毎日午前3:00
      - "0 0 1 * *" # 毎月1日の午前0時（月次バックアップ）
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
      - "?(0-59) 9 * * 1-5" # 平日の9:00-9:59のランダムな分

  # 日中の時間帯にランダムにモニタリングを実行
  - command: workflows/health_check
    schedules:
      - "0 ?(9-17) * * *" # 毎日9-17時のランダムな時刻（00分）
```

## 5.6. Slack チャットワークフロー

Slackチャットワークフローでは、`person.yml` の `message_channels` に設定したチャネルを監視し、受信イベントを設定済み AI CLIツールへ委譲します。AI CLIツールは返信、リアクション、no-op、質問、blocked のいずれにするかを判断します。Slack への投稿・返信・リアクションは公開メンバー機能である `guildbotics member chat ...` 経由でのみ実行されます。

定期投稿は別経路です。定期投稿には従来どおり `task_schedules` + `workflows/chat_post_command` を使います。

チャットイベントの受信は `guildbotics start` で起動されるイベントリスナーランナーが担当し、処理は各メンバーのメンバーワーカー内のイベントキューソースが直列に実行します。`--only scheduler` で巡回/定期実行だけを起動している場合、チャットイベントは受信されません。`--only events` の場合、巡回/定期実行は無効になりますが、メンバーワーカーはキュー済みチャットイベントを処理します。

AI CLIツールによるチャット処理では、`functions/handle_chat_event` がメンバーごとの作業ディレクトリを `cwd` にして実行されます。既定では `<workspace>/.guildbotics/data/workspaces/<person_id>/` です。この配下にある複製済みリポジトリを参照できます。ワークフローは `guildbotics member chat complete` が記録した実行証跡を検証し、ツールの自然言語の標準出力だけでは Slack に投稿した証拠として扱いません。
`person.yml` の `character` には、興味・嗜好・会話参加方針などを定義できます。チャット判断と返信生成は AI CLIツール経由でこのプロフィールを参照します。

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
      participation: strict
      startup_backfill_minutes: 60
      backfill_interval_seconds: 300

task_schedules:
  - command: 'workflows/chat_post_command service=slack channel_id=C0123456789 command="examples/reports/ai_news_digest query=\"OpenAI OR Anthropic OR Gemini\" language=ja country=JP limit=10 max_age_hours=24"'
    schedules:
      - "0 9 * * 1-5"
```

ポイント:

- 監視対象チャネルは `person.yml` の `message_channels` で定義し、`chat.enabled: true` のものが対象
- `chat.participation` で Slack thread への参加条件を制御できます。`strict`（既定）は明示メンションと一度呼ばれた thread の follow-up、`social` は雑談チャネル向けに未メンションの自然参加も許可、`muted` は明示メンションのみを処理します。
- 起動時に Slack history から直近の channel message と既知 thread reply を backfill します。`startup_backfill_minutes` の既定値は `60`、`backfill_interval_seconds` の既定値は `300` で、`0` にすると起動後の定期 history 確認を無効化できます。
- incoming reply / reaction / no-op / completion evidence は `guildbotics member chat reply|post|reaction add|noop|complete` 経由で記録される
- 定期投稿は `task_schedules` + `workflows/chat_post_command` を使う（投稿本文は GuildBotics カスタムコマンドの出力）
- 例: `examples/reports/ai_news_digest` は前段でニュースRSSを取得し、後段でLLMがSlack向けに要約整形するサンプル

interactive member chat の例:

```bash
guildbotics member chat reply --person alice --service slack --channel-id C0123456789 --thread-ts 1777554000.000000 --content-stdin <<'EOF'
`$HOME`、backtick (`command`)、`$(command)` をそのまま含む返信本文
EOF
guildbotics member chat reaction add --person alice --service slack --channel-id C0123456789 --message-ts 1777554000.000000 --reaction ack
```

定期投稿コマンドの具体例（AIニュースダイジェスト）:

```bash
guildbotics run examples/reports/ai_news_digest query="OpenAI OR Anthropic OR Gemini" language=ja country=JP limit=10 max_age_hours=24
```

投稿専用コマンドの例（手動）:

```bash
guildbotics run workflows/chat_post_command service=slack channel_name=dev-chat command='examples/reports/ai_news_digest query="OpenAI OR Anthropic OR Gemini" language=ja country=JP limit=10 max_age_hours=24'
```

このコマンドは、前段で Google News RSS からニュース候補を取得し、LLM で Slack 向けの日本語ダイジェスト文面に整形します。

# 6. GitHub統合

このセクションでは、デフォルトの `ticket_driven_workflow` を使用して、GitHub Projects および Issues と統合し、チケットベースのAIエージェント協働を行う方法を説明します。

**注**: GitHub統合は任意です。GitHub統合なしでも、Slackチャットワークフローやスケジュール実行によるコマンド自動化は利用できます。

## 6.1. 事前準備

### 6.1.1. Git環境

- チケット駆動の作業は `guildbotics member ...` CLI 経由で行います。ワークフローは GitHub Project
  から対象を選び、AI CLIツールをメンバーごとの作業ディレクトリで起動します。既定では
  `<workspace>/.guildbotics/data/workspaces/<person_id>` です。ツールが完了記録を残したことを
  検証し、リポジトリの複製、push、PR 作成、コメント、返信はツール自身が
  `guildbotics member` 経由で実行します。
- 各 AI メンバーの GitHub 認証情報を GuildBotics に設定してください。GitHub/git 書き込みは
  ローカルの `gh auth` ユーザーではなく、割り当てられたメンバーのマシンユーザートークンまたは
  GitHub App インストールを使って行われます。認証情報が必要なメンバーコマンドは、
  使用中のワークスペースの OS キーチェーンと `.env`、`GUILDBOTICS_ENV_FILE`、
  またはカレントディレクトリの `.env` からこれらを読み込みます。
- AI CLIツールを対話的に使う場合は、ワークスペース選択後に GuildBotics デスクトップアプリを
  少なくとも一度起動してください。アプリが GuildBotics スキルと管理用 CLI
  (`~/.guildbotics/bin/guildbotics`) を配置します。さらに `gh`、直接のトークン/API 書き込み、`git push` は拒否または承認必須にすることを推奨します。
  これは利用者自身の GitHub アカウントへフォールバックすることを避けるための防止策であり、
  トークン流出を完全に技術的に封じ込めるサンドボックスではありません。
- Codex を AI CLIツールとして使う場合は、Codex CLI の認証とネットワーク到達性を確認してください:
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
**Diagnostics / Verify** ビューから設定を検証します。各アクティブメンバーの GitHub・LLM・AI CLIツール設定が
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
`services.ticket_manager.lane_map` キーでマッピングを指定してください（[設定ファイル](#73-設定ファイル)を参照）。
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
AIエージェントは `guildbotics member git prepare` でメンバーごとの作業ディレクトリにリポジトリを準備し、そこで作業します。既定では `<workspace>/.guildbotics/data/workspaces/<person_id>` 配下です。

### 6.3.3. AIエージェントとの対話

- AIエージェントは作業中に質問がある場合、チケットコメントで質問を投稿します。チケットコメントで回答してください。エージェントは定期的にチケットコメントをチェックし、回答が得られれば作業を進めます。
- AIエージェントがタスクを完了すると、`guildbotics member ...` 経由で comment / PR URL / review reply / reaction を残し、最後に `guildbotics member task complete` を記録します。
- チケットから作成された PR にはレビュー結果を PR 上で書き込んでください。GuildBotics は未対応 review thread を確認し、担当エージェントへ再委譲します。

## 6.4. できること

チケット駆動ワークフローでは以下が可能です:

- **タスクボードでのAIエージェントへのタスク依頼**
  - タスクボード上のチケットでAIエージェントをアサインして着手可能レーンにチケットを移動すれば、AIエージェントがそのタスクを実行します
- **AIエージェントの実行結果をタスクボード上で確認**
  - AIエージェントがタスクを完了すると、メンバー機能を通じてコメント、PR、レビュー返信、リアクションのいずれかの痕跡を残します
- **AIエージェントによるPull Requestの作成**
  - コード変更が必要な場合、エージェントがメンバー用作業ディレクトリのブランチを公開し、`guildbotics member github pr create` で Pull Request を作成または再利用します
- **チケット作成**
  - AIエージェントに follow-up ticket 作成を指示した場合、`guildbotics member github issue create` で repository の実 issue を作成します

# 7. リファレンス

CLI コマンドとオプションの完全な一覧は、ソースコードから生成される
[CLI リファレンス（英語）](docs/cli_reference.md)を参照してください。

## 7.1. アカウント関連環境変数

**LLM APIキー**:

- `GOOGLE_API_KEY`: Google Gemini API
- `OPENAI_API_KEY`: OpenAI API
- `ANTHROPIC_API_KEY`: Anthropic Claude API

**Slack アクセス**:

- `{PERSON_ID}_SLACK_BOT_TOKEN`: person毎の Slack Bot Token
- `{PERSON_ID}_SLACK_APP_TOKEN`: person毎の Slack App-Level Token

**GitHub アクセス**（person毎、形式: `{PERSON_ID}_...`）:

- `{PERSON_ID}_GITHUB_ACCESS_TOKEN`: マシンアカウント/代理エージェント用PAT
- `{PERSON_ID}_GITHUB_APP_ID`, `{PERSON_ID}_GITHUB_INSTALLATION_ID`, `{PERSON_ID}_GITHUB_PRIVATE_KEY_PATH`: GitHub App用

カレントディレクトリに `.env` ファイルが存在する場合、自動的に読み込まれます。OS キーチェーンに
保存されたシークレット（[7.2. シークレットの保存](#72-シークレットの保存) 参照）も同様に自動で読み込まれます。
`guildbotics member` コマンドは、まず `--workspace <dir>` を優先します。指定が無い場合は、
コマンドがすでに設定済みワークスペース内で実行されている場合を除き、デスクトップアプリまたは
`guildbotics workspace use` が記録した使用中のワークスペースを使います。
選択されたワークスペースから `GUILDBOTICS_CONFIG_DIR` を `<workspace>/.guildbotics/config` に設定し、
`<workspace>/.env` が存在する場合は `GUILDBOTICS_ENV_FILE` も設定します。

ワークスペースの確認・変更には以下を使います。

```bash
guildbotics workspace status
guildbotics workspace current
guildbotics workspace use /path/to/workspace
guildbotics member --workspace /path/to/workspace context --person <person_id> --check-credentials
```

デスクトップアプリを使わずにサーバーなどで運用する場合は、絶対パスの `.env` を指す
`GUILDBOTICS_ENV_FILE`、またはカレントディレクトリの `.env` がフォールバックになります。
`guildbotics start` とデスクトップ実行環境は、ワークスペースの `.env` を読み込んだときに
`GUILDBOTICS_ENV_FILE` も自動設定します。
ワークスペースの `.env` に `GUILDBOTICS_DATA_DIR` を設定すると、ワークスペースごとの実行データの
保存先を変更できます。起動時点の環境変数に `GUILDBOTICS_DATA_DIR` があり、ワークスペースの `.env`
に同じ設定が無い場合は、その起動中のプロセスで共有する実行データの保存先として使われます。

## 7.2. シークレットの保存

GuildBotics は、シークレット（LLM API キーおよび上記のアカウントトークン類）を可能な限り
プレーンテキストファイルの外に保存します。

- **OS キーチェーン（新規ワークスペースの既定）:** 利用可能なキーチェーン（macOS キーチェーン、
  Windows 資格情報マネージャー、Linux Secret Service（GNOME Keyring 等））がある場合、
  セットアップはシークレットの値をキーチェーンに保存します。ワークスペース側には、保存済みキー名の
  一覧だけを記録した非シークレットのインデックスファイル `.guildbotics/config/secrets.yml` を置きます。
- **`.env` バックエンド:** インデックスファイルの無いワークスペース（キーチェーンが使えない
  マシンで作成した場合など）は、ワークスペースの `.env` を使います。ヘッドレスサーバーや CI では
  この方式をサポートします。GuildBotics が書き出す `.env` は所有者のみ読み書き可能（`0600`）の
  パーミッションになります。
- **優先順位:** 実環境変数 > OS キーチェーン > `.env`。サーバー運用では、バックエンドに関係なく
  環境変数での注入が常に最優先されます。
- **GitHub App 秘密鍵:** キーチェーン利用ワークスペースでのメンバー設定保存は、
  `*_GITHUB_PRIVATE_KEY_PATH` が指す PEM ファイルの中身をキーチェーンへ
  コピーし、パスのエントリも `.env` から取り除きます。キーチェーンの内容がファイルを置き換える
  ため、残る作業は平文の `.pem` ファイルを手動で削除することだけです。他のシークレットと
  異なり、鍵の中身は環境変数には一切公開されず、GitHub App トークン発行の瞬間にだけキーチェーン
  から読み出されるため、AI CLI ツールの子プロセスから見えることはありません。
- **`GUILDBOTICS_SECRETS_BACKEND`:** `keyring` または `env-file` を指定すると、そのプロセスに限り
  バックエンドを強制できます（CI やスクリプト環境向け）。

シークレットの管理には `guildbotics secrets` CLI を使います（サブコマンドとオプションの
一覧は [CLI リファレンス](docs/cli_reference.md#guildbotics-secrets)を参照）。

```bash
guildbotics secrets status                        # 使用中のバックエンドとキーチェーンの利用可否
guildbotics secrets export --file secrets.env     # 引越用にシークレットを書き出し
guildbotics secrets import secrets.env            # 移行先マシンで読み込み
```

シークレットはワークスペースごとに保存されます（キーチェーンのエントリは `secrets.yml` の
`store_id` で名前空間が分かれます）。`guildbotics member` と同様に、対象ワークスペースは
サブコマンドの前の `--workspace` で指定できます。省略時はカレントディレクトリのワークスペース、
無ければ選択中の active workspace が使われます。対象がどこに解決されたかは
`guildbotics secrets status` の `workspace:` 行で常に確認できます。

```bash
guildbotics secrets --workspace /path/to/workspace status
guildbotics secrets --workspace /path/to/workspace migrate
```

ワークスペースを別のマシンへ移す場合は、ワークスペースフォルダをコピーした上で、移行元で
`guildbotics secrets export --file ...`、移行先で `guildbotics secrets import ...` を実行します
（エクスポートファイルは使用後に削除してください）。キーチェーンのエントリ自体がマシンの外に
出ることはありません。キーチェーンの無いサーバーでは、`.env` に保存するか環境変数で渡してください。

## 7.3. 設定ファイル

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

**Brain/AI CLIツール設定**:

- `intelligences/cli_agent_mapping.yml`: デフォルトの AI CLIツール選択
- `intelligences/native_agent_policy.yml`: Codexのファイルアクセス範囲（`workspace`または
  `host`）。新規workspaceのsetup時に作成され、Desktopの **LLM・AI CLIツール → 詳細設定**
  または画面を利用できない環境でのファイル直接編集により設定します。ネットワークアクセスと
  確認を求めない実行方式はGuildBoticsのCodex連携内で固定します
- `intelligences/cli_agents/*.yml`: Codex・Claude Code以外のAI CLIツールをスクリプト経由で
  実行するための設定
- `team/members/<person_id>/intelligences/`: Codexの実行権限を含むメンバーごとの任意の上書き。
  既定ではチーム設定を継承します

設定可能な値とセキュリティ上の注意事項は、
[Codex・Claude Codeのセッション連携](docs/native_agent_runtime.ja.md#設定)をご覧ください。

# 8. トラブルシューティング

**診断ログ**: 検索用の実行サマリーは
`<workspace>/.guildbotics/data/run/diagnostics.jsonl` に記録され、イベント・ログ・span・
入出力の全文は実行ごとの JSONL として `run/sessions/` に保存されます。Desktop の診断画面では、
実行履歴と最新の Global / system session の両方を確認できます。

**デバッグ出力**: 詳細なログを取得するための環境変数:

- `LOG_LEVEL`: `debug` / `info` / `warning` / `error`
- `AGNO_DEBUG`: Agnoエンジンの追加デバッグ出力 (`true`/`false`)
- `GUILDBOTICS_TRANSCRIPT_DETAIL`: `standard`（既定）または `full`。`standard` は大量の
  thinking/delta イベントを省き、AI CLIツールの stderr は末尾 8 KiB のみ保持
- `GUILDBOTICS_TRANSCRIPT_RETENTION_DAYS`: session JSONL の保持日数（既定: `30`）
