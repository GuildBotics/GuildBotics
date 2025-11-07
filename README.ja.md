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
- [3. 動作環境](#3-動作環境)
  - [対応統合](#対応統合)
- [4. インストール](#4-インストール)
- [5. 基本的な使い方](#5-基本的な使い方)
  - [5.1. 初期セットアップ](#51-初期セットアップ)
  - [5.2. メンバーの追加](#52-メンバーの追加)
  - [5.3. コマンド実行](#53-コマンド実行)
    - [カスタムコマンドを実行](#カスタムコマンドを実行)
    - [スケジューラを起動](#スケジューラを起動)
- [6. GitHub統合の使用例](#6-github統合の使用例)
  - [6.1. 事前準備](#61-事前準備)
    - [6.1.1. Git環境](#611-git環境)
    - [6.1.2. GitHub プロジェクトの作成](#612-github-プロジェクトの作成)
    - [6.1.3. AIエージェント用GitHubアカウントの準備](#613-aiエージェント用githubアカウントの準備)
      - [マシンアカウントを利用する場合](#マシンアカウントを利用する場合)
      - [GitHub Appを利用する場合](#github-appを利用する場合)
      - [代理エージェント (AIエージェント用に自分自身のアカウント) を利用する場合](#代理エージェント-aiエージェント用に自分自身のアカウント-を利用する場合)
    - [6.1.4. LLM API](#614-llm-api)
    - [6.1.5. CLI エージェント（オプション）](#615-cli-エージェントオプション)
  - [6.2. GitHub統合のセットアップ](#62-github統合のセットアップ)
  - [6.3. チケット駆動ワークフローの実行](#63-チケット駆動ワークフローの実行)
    - [6.3.1. 起動](#631-起動)
    - [6.3.2. AIエージェントへの作業指示](#632-aiエージェントへの作業指示)
    - [6.3.3. AIエージェントとの対話](#633-aiエージェントとの対話)
  - [6.4. できること](#64-できること)
- [7. リファレンス](#7-リファレンス)
  - [7.1. アカウント関連環境変数](#71-アカウント関連環境変数)
  - [7.2. 設定ファイル](#72-設定ファイル)
  - [7.3. カスタムコマンド](#73-カスタムコマンド)
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

# 3. 動作環境
- OS: Linux（Ubuntu 24.04 で動作確認）/ macOS（Sequoia で動作確認）
- ランタイム: `uv`（必要な Python を uv が自動で取得・管理します）

## 対応統合
- **LLMプロバイダー**: Google Gemini、OpenAI、Anthropic Claude
- **CLIエージェント**: Gemini CLI、OpenAI Codex CLI、Claude Code
- **GitHub統合**: Projects (v2)、Issues、Pull Requests

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

### カスタムコマンドを実行

```bash
guildbotics run <command_name> [args...]
```

例:
```bash
echo "Hello" | guildbotics run translate English Japanese
```

カスタムコマンドの作成方法は[カスタムコマンド](#73-カスタムコマンド)と[カスタムコマンド開発ガイド](docs/custom_command_guide.ja.md)を参照してください。

### スケジューラを起動

```bash
guildbotics start [routine_commands...]
```

ルーチンコマンドとスケジュールタスクを実行するタスクスケジューラを起動します。コマンドを指定しない場合、デフォルトの`workflows/ticket_driven_workflow`を実行します。

停止するには:
```bash
guildbotics stop
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

### 6.1.4. LLM API
以下のいずれかを選択してください:
- Google Gemini API: [Google AI Studio](https://aistudio.google.com/app/apikey) からAPIキーを取得
- OpenAI API: [OpenAI Platform](https://platform.openai.com/api-keys) からAPIキーを取得
- Anthropic Claude API: [Anthropic Console](https://console.anthropic.com/settings/keys) からAPIキーを取得

### 6.1.5. CLI エージェント（オプション）
以下のCLI エージェントのいずれかをインストールして、起動して認証を行ってください。
- [Gemini CLI](https://github.com/google-gemini/gemini-cli/)
- [OpenAI Codex CLI](https://github.com/openai/codex/)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (Claude Pro または Max サブスクリプションが必要)


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
guildbotics start [default_routine_commands...]
```

- `default_routine_commands` は、定常的に実行するコマンドのリストです。指定しない場合は、 `workflows/ticket_driven_workflow` が既定値として利用されます。

これにより、タスクスケジューラが起動し、AIエージェントがタスクを実行できるようになります。

実行中のスケジューラを停止するには:

```bash
guildbotics stop
```

即座に強制停止する場合:

```bash
guildbotics kill
```

これは `guildbotics stop --force --timeout 0` と同じです。

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

**Brain/CLIエージェント設定**:
- `intelligences/cli_agent_mapping.yml`: デフォルトCLIエージェント選択
- `intelligences/cli_agents/*.yml`: CLIエージェントスクリプト
- `team/members/<person_id>/intelligences/`: person毎の上書き

## 7.3. カスタムコマンド

`~/.guildbotics/config/commands/` （またはプロジェクトローカルのコマンドディレクトリ）にカスタムコマンドを作成:

- **Markdownファイル** (`.md`): フロントマター付きLLMプロンプト
- **Pythonファイル** (`.py`): コンテキスト注入付きカスタムロジック
- **Shellスクリプト** (`.sh`): Shellコマンド
- **YAMLファイル** (`.yml`): ワークフロー合成

詳細な作成方法は[カスタムコマンド開発ガイド](docs/custom_command_guide.ja.md)を参照してください。

**簡単な例**:
```markdown
<!-- translate.md -->
以下のテキストが ${1} の場合は ${2} に、${2} の場合は ${1} に翻訳してください:
```

使い方:
```bash
echo "Hello" | guildbotics run translate English Japanese
```

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
