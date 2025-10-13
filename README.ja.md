<h1>GuildBotics</h1>

[English](https://github.com/GuildBotics/GuildBotics/blob/main/README.md) • [日本語](https://github.com/GuildBotics/GuildBotics/blob/main/README.ja.md)

AIエージェントとのやり取りをタスクボードを通じて行うためのツールです。

---

## 重要な注意（免責事項）

- 本ソフトウェアはアルファ版です。今後、破壊的な非互換を伴う変更が行われる可能性が非常に高く、動作不具合も頻繁に発生することが想定されるため、実運用環境での利用は推奨しません。
- 本ソフトウェアの動作不具合やそれによって生じた損害について、作者および配布者は一切の責任を負いません。特に、AIエージェントの誤動作や暴走により、利用中のシステムや外部サービスに対する致命的な破壊、データ損失、秘密データ漏洩が発生する可能性があります。使用は自己責任で行い、隔離されたテスト環境で検証してください。

---

- [1. できること](#1-できること)
- [2. 動作環境](#2-動作環境)
- [3. 対応サービス / ソフトウェア](#3-対応サービス--ソフトウェア)
- [4. 事前準備](#4-事前準備)
  - [4.1. Git環境](#41-git環境)
  - [4.2. GitHub プロジェクトの作成](#42-github-プロジェクトの作成)
  - [4.3. AIエージェント用GitHubアカウントの準備](#43-aiエージェント用githubアカウントの準備)
    - [4.3.1. マシンアカウントを利用する場合](#431-マシンアカウントを利用する場合)
    - [4.3.2. GitHub Appを利用する場合](#432-github-appを利用する場合)
    - [4.3.3. 代理エージェント (AIエージェント用に自分自身のアカウント) を利用する場合](#433-代理エージェント-aiエージェント用に自分自身のアカウント-を利用する場合)
  - [4.4. Gemini APIもしくはOpenAI API](#44-gemini-apiもしくはopenai-api)
  - [4.5. CLI エージェント](#45-cli-エージェント)
- [5. GuildBotics のインストールとセットアップ](#5-guildbotics-のインストールとセットアップ)
  - [5.1. 初期セットアップ](#51-初期セットアップ)
  - [5.2. メンバーの追加](#52-メンバーの追加)
  - [5.3. 設定確認・カスタムフィールド追加・ステータス設定](#53-設定確認カスタムフィールド追加ステータス設定)
    - [5.3.1. カスタムフィールドの追加](#531-カスタムフィールドの追加)
    - [5.3.2. ステータス設定](#532-ステータス設定)
- [6. 実行](#6-実行)
  - [6.1. 起動](#61-起動)
  - [6.2. AIエージェントへの作業指示の出し方](#62-aiエージェントへの作業指示の出し方)
  - [6.3. AIエージェントとの対話](#63-aiエージェントとの対話)
- [7. リファレンス](#7-リファレンス)
  - [7.1. アカウント関連環境変数設定](#71-アカウント関連環境変数設定)
    - [7.1.1. LLM API の環境変数](#711-llm-api-の環境変数)
    - [7.1.2. GitHub へのアクセス設定](#712-github-へのアクセス設定)
  - [7.2. プロジェクト設定（`team/project.yml`）](#72-プロジェクト設定teamprojectyml)
  - [7.3. メンバー設定（`team/members/<person_id>/person.yml`）](#73-メンバー設定teammembersperson_idpersonyml)
  - [7.4. CLI エージェントの選択](#74-cli-エージェントの選択)
  - [7.5. CLI エージェント呼び出しスクリプトの変更](#75-cli-エージェント呼び出しスクリプトの変更)
  - [7.6. AIエージェント毎のCLIエージェント設定](#76-aiエージェント毎のcliエージェント設定)
  - [7.7. カスタムコマンド実行](#77-カスタムコマンド実行)
- [8. トラブルシューティング](#8-トラブルシューティング)
  - [8.1. エラーログ](#81-エラーログ)
  - [8.2. デバッグ情報の取得](#82-デバッグ情報の取得)
- [9. Contributing](#9-contributing)

---

# 1. できること
- タスクボードでのAIエージェントへのタスク依頼
  - タスクボード上のチケットでAIエージェントをアサインして **Ready** 列にチケットを移動すれば、AIエージェントがそのタスクを実行します。
- AIエージェントの実行結果をタスクボード上で確認
  - AIエージェントがタスクを完了すると、チケットが **In Review** 列に移動し、実施結果がチケットのコメントとして書き込まれます。
- AIエージェントによるPull Requestの作成
  - AIエージェントはタスクを完了すると Pull Requestを作成します。
- チケット作成
  - AIエージェントに対してチケット作成の指示を出せば、AIエージェントが自動でタスクボード上にチケットを作成します。
- 振り返り
  - タスク実施済みチケットをタスクボード上の **Retrospective** 列に移動させ、振り返りの実施依頼をコメントに書き込めば、AIエージェントが作成したPull Requestのレビュワーとのやりとりに関して分析及び課題抽出を行い、改善チケットを作成します。

# 2. 動作環境
- OS: Linux（Ubuntu 24.04 で動作確認）/ macOS（Sequoia で動作確認）
- ランタイム: **uv**（必要な Python を uv が自動で取得・管理します）

# 3. 対応サービス / ソフトウェア
現在のバージョンでは以下に対応しています。

- タスクボード
  - GitHub Projects (Project v2)
- コードホスティングサービス
  - GitHub
- CLI エージェント
  - Google Gemini CLI
  - OpenAI Codex CLI
- LLM API
  - Google Gemini 2.5 Flash
  - OpenAI GPT-5 Mini

# 4. 事前準備
## 4.1. Git環境
- リポジトリへの Git アクセス方式を設定してください:
  - HTTPS: GCM (Git Credential Manager) をインストールし、サインイン
  - または SSH: SSH 鍵を設定し、`known_hosts` を登録

## 4.2. GitHub プロジェクトの作成
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

## 4.3. AIエージェント用GitHubアカウントの準備
AIエージェントがGitHubにアクセスするためのアカウントを用意します。以下のいずれかの方法が利用可能です。

- **マシンアカウント** (マシンユーザー)
  - 「AIエージェントとタスクボードやPull Requestを通じて対話しながら進める」という雰囲気が味わえるという意味でおすすめの方法ですが、[GitHub の利用規約上](https://docs.github.com/ja/site-policy/github-terms/github-terms-of-service#3-account-requirements)、無料で作成できるマシンアカウントは、1ユーザーにつき1つだけとなっていますのでご注意ください。
- **GitHub App**
  - アカウント作成数に制限がないというメリットはありますが、**個人**アカウントの GitHub Project へのアクセスはできません。また、GitHub サイト上ではボットであることが明記されるため、少し雰囲気が削がれます。
- **代理エージェント** (自分自身のアカウントをAIエージェント用に利用する)
  - 最も簡単な利用方法です。ただし、この方法の場合、AIエージェントと対話しながら進めるというよりは自問自答しているという見た目になります。

### 4.3.1. マシンアカウントを利用する場合
マシンアカウント作成後に以下の作業を行ってください。

1. 作成したマシンアカウントをProjectおよびリポジトリに Collaborator として追加してください。
2. Classic PAT 発行
  - **Classic** PAT (Personal Access Token) を発行してください。
  - PATのスコープは、`repo` と `project` の2つを選択してください。

### 4.3.2. GitHub Appを利用する場合
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

### 4.3.3. 代理エージェント (AIエージェント用に自分自身のアカウント) を利用する場合
自分自身のアカウントをAIエージェント用に利用する場合、**Classic** PAT を発行してください。
PATのスコープは、`repo` と `project` の2つを選択してください。

## 4.4. Gemini APIもしくはOpenAI API
Gemini API キーもしくは OpenAI API キーを取得してください。

## 4.5. CLI エージェント
[Gemini CLI](https://github.com/google-gemini/gemini-cli/) もしくは [OpenAI Codex CLI](https://github.com/openai/codex/) のいずれかをインストールして、起動して認証を行ってください。


# 5. GuildBotics のインストールとセットアップ
以下の方法でインストールできます。

```bash
uv tool install guildbotics
```

## 5.1. 初期セットアップ

以下のコマンドで初期セットアップを行ってください。

```bash
guildbotics init
```

`guildbotics init` では、以下の内容をターミナル上で対話的に選択・入力した後、設定ファイルを生成します。

- 言語の選択
  - 英語または日本語を選択
- 設定ディレクトリの選択
  - ホームディレクトリ配下または現在のディレクトリの配下のいずれかを選択
- 環境ファイルの作成
  - `.env` ファイルの作成、追加、上書きの選択
- LLM APIの選択
  - Gemini API または OpenAI API を選択
- CLI エージェントの選択
  - Gemini CLI または OpenAI Codex CLI を選択
- リポジトリアクセス方法の選択
  - Git 操作用に HTTPS か SSH を選択
- GitHub プロジェクトおよびリポジトリ URL の入力
  - GitHub Projects の URL とリポジトリの URL を入力

以下の設定ファイルが作成/更新されます:

- カレントディレクトリ
  - `.env` ファイルに環境変数を追加
- ホームディレクトリもしくはカレントディレクトリ配下の `.guildbotics/config/`
  - プロジェクト定義ファイル: `team/project.yml`
  - CLIエージェントマッピングファイル: `intelligences/cli_agent_mapping.yml`
  - CLIエージェントスクリプト定義ファイル (以下のいずれか):
    - `intelligences/cli_agents/codex-cli.yml`
    - `intelligences/cli_agents/gemini-cli.yml`


## 5.2. メンバーの追加

以下のコマンドでメンバーを追加できます。

```bash
guildbotics add
```

`guildbotics add` では、プロジェクトメンバー（AIエージェントもしくは人間）の情報を対話的に入力することで、設定ファイルを生成します。

- メンバータイプの選択
  - 人間、マシンアカウント、GitHub App、代理エージェント（自分自身のアカウント利用）のいずれかを選択
    - 「人間」は、AIエージェントに対してチームメンバー情報を提供するための設定です。AIエージェントとしては動作しません
- GitHub ユーザー名の入力 (人間、マシンアカウント、GitHub App、代理エージェントの場合)
  - 代理エージェントの場合は自分自身のGitHubユーザー名を入力する
- GitHub App の URL を入力 (GitHub App の場合)
- GuildBotics 内でのメンバーID (person_id) の入力 (全タイプ共通)
  - 英数字小文字のみ。デフォルトはGitHubユーザー名
- ユーザー名の入力 (全タイプ共通)
  - メンバーの名前（フルネーム）を入力
- ロールの選択 (全タイプ共通)
  - プロダクトオーナー、プロジェクトマネージャー、アーキテクトなどのロールを選択。複数選択可能。
- AIエージェントの会話スタイル選択 (マシンアカウント、GitHub App、代理エージェントの場合)
  - フレンドリー、プロフェッショナル、マシンのいずれかを選択
- 環境変数の設定
  - GitHub App の場合: インストールID、App ID、プライベートキーパスを入力
  - マシンアカウントの場合: PATを入力

以下の2種類の設定ファイルが作成/更新されます:

- カレントディレクトリ
  - `.env` ファイルに環境変数を追加。PATやGitHub Appのシークレット情報を保存。
- ホームディレクトリもしくはカレントディレクトリ配下の `.guildbotics/config/`
  - メンバー定義ファイル: `team/members/<person_id>/person.yml`

追加したいAIエージェントが複数存在する場合は、同様の手順でそれぞれのメンバーを追加してください。

**メモ:**
**person_id** は、GuildBotics 内でメンバーを識別するためのIDです。環境変数名やディレクトリ名などに利用されるため、英数字小文字のみを利用してください。"-", "_" 以外の記号や空白の利用はできません。


## 5.3. 設定確認・カスタムフィールド追加・ステータス設定

以下のコマンドで設定が正常に行われているかどうかの確認を兼ねて、次の処理を実行します。

- GitHub Projects に対するGuildBotics用カスタムフィールドの追加
- GitHub Projects のステータス紐付け設定

```bash
guildbotics verify
```

### 5.3.1. カスタムフィールドの追加
以下のカスタムフィールド（いずれも選択肢形式）が GitHub Projects に追加されます。

- `Mode`: AIエージェントの動作モードを指定するためのフィールド
  - `comment`: チケットの指示に対してコメントで応答します。
  - `edit`: チケットの指示に基づいてファイルを編集し、Pull Requestを作成します。
  - `ticket`: チケットの作成を行います。
- `Role`: チケットに記述されたタスクを実行する際の役割を指定するためのフィールド
- `Agent`: タスクを実行するAIエージェントを指定するためのフィールド

### 5.3.2. ステータス設定
GitHub Projects 上のステータスとGuildBoticsが扱うステータスとの紐付け設定を行います。

GuildBoticsが扱うステータスは、以下の6つです。
  - New (新規)
    - ユーザーが `ticket` モードでAIエージェントに対してチケット作成依頼した際に、AIエージェントが設定するステータスです。
  - Ready (着手可能)
    - ユーザーがAIエージェントにタスクを依頼する際に設定するステータスです。
  - In Progress (進行中)
    - AIエージェントがタスクに着手した際に設定するステータスです。
  - In Review (レビュー中)
    - AIエージェントがタスクを完了した際に設定するステータスです。
  - Retrospective (振り返り)
    - ユーザーがAIエージェントに対して振り返りの実施を依頼する際に設定するステータスです。
  - Done (完了)
    - ユーザーがタスク完了を判断した際に設定するステータスです。

ステータスの紐付け設定は、プロジェクト定義ファイル `.guildbotics/config/team/project.yml` に保存されます。


# 6. 実行
## 6.1. 起動
以下のコマンドで起動します。

```bash
guildbotics start
```

これにより、タスクスケジューラが起動し、AIエージェントがタスクを実行できるようになります。

実行中のスケジューラを停止するには、次のコマンドを実行します。

```bash
guildbotics stop [--timeout <seconds>] [--force]
```

- SIGTERM を送信し、`--timeout` 秒（デフォルト: 30）まで終了を待機します。
- タイムアウトまでに終了しない場合、`--force` を指定すると SIGKILL を送信します。
- スケジューラが動作していない場合はその旨を表示し、古い pid ファイルがあればクリーンアップします。

すぐに強制停止したい場合は以下を使用できます。

```bash
guildbotics kill
```

これは `guildbotics stop --force --timeout 0` と同等です。

## 6.2. AIエージェントへの作業指示の出し方

AIエージェントにタスクを依頼するには、GitHub Projects 上のチケットを以下のように操作します。

1. チケットを作成し、対象のGitリポジトリを指定してIssueとして保存する
2. チケットに対して、AIエージェントに依頼したい内容を記述する
   - これがエージェントに対するプロンプトになるため、できるだけ具体的に記述してください
3. チケットの `Agent` フィールドでタスクを実行するAIエージェントを指定する
4. チケットの `Mode` フィールドを設定する
   - `comment`: チケットに対するコメントでの応答を依頼する場合
   - `edit`: ファイル編集とPull Request作成を依頼する場合
   - `ticket`: チケット作成を依頼する場合
5. チケットの `Role` フィールドにより、チケットに記述されたタスクを実行する際の役割を指定する (省略可)
6. チケットのステータスを `Ready` に変更する

**メモ:**
AIエージェントは、ホームディレクトリ内の `.guildbotics/data/workspaces/<person_id>` に指定したGitリポジトリをクローンして作業を行います。

## 6.3. AIエージェントとの対話
- AIエージェントは、作業に際して質問がある場合、チケットにコメントとして質問を書き込みます。ユーザーは回答をチケットのコメントとして記述してください。AIエージェントは定期的にチケットのコメントをチェックし、質問に対する回答が記入されている場合はそれを踏まえた対応を行います。
- AIエージェントはタスクを完了すると、チケットのステータスを `In Review` に変更し、実施結果や作成したPull RequestのURLをコメントとして書き込みます。
- `edit`モードの場合、AIエージェントは同時にPull Requestを作成します。レビュー結果はPull Requestのコメントとして記入してください。AIエージェントは `In Review` ステータスのチケットが存在する場合、Pull Requestのコメントの存在チェックを行い、レビューコメントが存在する場合はそれを踏まえた対応を行います。


# 7. リファレンス
## 7.1. アカウント関連環境変数設定

GuildBotics は以下の環境変数を利用します。

- `GOOGLE_API_KEY`: Gemini API を利用する際に必須。
- `{PERSON_ID}_GITHUB_ACCESS_TOKEN`: マシンアカウントのパーソナルアクセストークン  
- `{PERSON_ID}_GITHUB_APP_ID`: GitHub App ID  
- `{PERSON_ID}_GITHUB_INSTALLATION_ID`: GitHub AppのインストールID  
- `{PERSON_ID}_GITHUB_PRIVATE_KEY_PATH`: GitHub Appのプライベートキーのパス  

`.env` がある場合は自動で読み込まれます。

### 7.1.1. LLM API の環境変数

Gemini API を利用する際の API キーは `GOOGLE_API_KEY` に設定します。

```bash
export GOOGLE_API_KEY=your_google_api_key
```

OpenAI API を利用する際の API キーは `OPENAI_API_KEY` に設定します。

```bash
export OPENAI_API_KEY=your_openai_api_key
```

### 7.1.2. GitHub へのアクセス設定

Person ごとのシークレットは `${PERSON_ID_UPPER}_${KEY_UPPER}` として参照されます（例: `person_id: yuki`）。

- マシンユーザーおよび代理エージェントの場合:

  ```bash
  export YUKI_GITHUB_ACCESS_TOKEN=ghp_xxx
  ```

- GitHub App の場合:

  ```bash
  export YUKI_GITHUB_APP_ID=123456
  export YUKI_GITHUB_INSTALLATION_ID=987654321
  export YUKI_GITHUB_PRIVATE_KEY_PATH=/absolute/path/to/your-app-private-key.pem
  ```

## 7.2. プロジェクト設定（`team/project.yml`）
- `team/project.yml`:
  - `language`: プロジェクトで使用する言語 (例: `ja`, `en`)
  - `repositories.name`: リポジトリ名
  - `services.ticket_manager.name`: `GitHub` (変更不可)
  - `services.ticket_manager.owner`: GitHub のユーザー/組織名
  - `services.ticket_manager.project_id`: GitHub Projects (v2) のプロジェクトID
  - `services.ticket_manager.url`: 上記プロジェクトのURL
  - `services.code_hosting_service.repo_base_url`: クローン時に利用するベースURL
    - 例: `https://github.com` (HTTPS) または `ssh://git@github.com` (SSH)

## 7.3. メンバー設定（`team/members/<person_id>/person.yml`）
- `team/members/<person_id>/person.yml`:
  - `person_id`: メンバーID (英数字小文字のみ。記号や空白の利用は不可)
  - `name`: メンバー名（フルネーム）
  - `is_active`: AIエージェントとして活動可能にするかどうか (true/false)
  - `person_type`: メンバー種別（human/machine_user/github_apps/proxy_agent など）
  - `account_info.github_username`: GitHub ユーザー名
  - `account_info.git_user`: Git ユーザー名
  - `account_info.git_email`: Git メールアドレス
  - `profile`: ロールごとのプロフィール設定。キーにロールID（例: professional, programmer, product_owner など）を置き、必要に応じて summary/description を記述します。空のマップ（例: product_owner:）でもそのロールが有効化され、定義済みロール（roles/default.*.yml 等）とマージされます。
  - `speaking_style`: 会話スタイルの説明文
  - `relationships`: 他メンバーとの関係性の説明文



## 7.4. CLI エージェントの選択

`intelligences/cli_agent_mapping.yml` を変更することで、CLIエージェントを切り替えることが可能です。

Codex CLI を使用する場合:

```yaml
default: codex-cli.yml
```

Gemini CLI を使用する場合:

```yaml
default: gemini-cli.yml
```

## 7.5. CLI エージェント呼び出しスクリプトの変更
`intelligences/cli_agents` ディレクトリ内の YAML ファイルを変更することで、CLI エージェントの呼び出しスクリプトをカスタマイズできます。

## 7.6. AIエージェント毎のCLIエージェント設定
デフォルトでは、すべてのAIエージェントが同じCLIエージェントを利用しますが、`team/members/<person_id>/intelligences` ディレクトリの配下に `cli_agent_mapping.yml` や `cli_agents/*.yml` があれば、そちらのファイルが優先して利用されます。

これにより、メンバー (AIエージェント) ごとに利用するCLIエージェントを変更することが可能です。


## 7.7. カスタムコマンド実行
チケットの本文やコメント内に、`//` で始まる1行を記述することで、カスタムコマンド（カスタムプロンプト）を実行できます。エージェント実行前に、その行は対応するプロンプトの本文に置換されます。

> より詳細な作成・運用方法は [docs/custom_command_guide.ja.md](docs/custom_command_guide.ja.md) を参照してください。

- 記述例: `//<name> [args...]`
  - 例: `//code-review target=src/app.py level=deep`
  - コマンド名の末尾にコロンを付けても可: `//code-review: target=...`

プロンプトファイルの探索順（最初に見つかったものを使用）:
- メンバー単位: `.guildbotics/config/team/members/<person_id>/prompts/<name>.<lang>.md` → `<name>.en.md` → `<name>.md`
- プロジェクト全体: `.guildbotics/config/prompts/<name>.<lang>.md` → `<name>.en.md` → `<name>.md`
- フォールバック: `.guildbotics/config/intelligences/<name>.md`

プロンプトファイル形式は「YAML フロントマター付き Markdown」です。本文が展開に利用されます。

```markdown
---
template_engine: jinja2  # または "default"
---
# {{ target }} のレビュー計画

優先度: {{ level | default('normal') }}
手順:
- {{ target }} に対して静的解析を実施
- ホットスポットと追加すべきテストの要約
```

引数の渡し方とテンプレート:
- キー指定: `//review target="src/app.py" level=deep`
  - 引用付き値をサポート（空白を保持）
- 位置引数も利用可能:
  - `template_engine: jinja2` の場合: `{{ arg1 }}`, `{{ arg2 }}` ...
  - `template_engine: default` の場合: `{{1}}`, `{1}`, `${1}`, `$1` など（名前付きキーも可: `{{target}}`, `{target}`, `${target}`, `$target`）

利用のコツ:
- コメントを書いた場合は一番新しい（一番最後の）コメントに `//<name> ...` を記述してください。
- 再利用したいプロンプトは `.guildbotics/config/prompts/` に、メンバー別の上書きは `.guildbotics/config/team/members/<person_id>/prompts/` に配置します。


# 8. トラブルシューティング
## 8.1. エラーログ
タスクの実行中に想定外のエラーが発生した場合、チケットに「タスクの実行中にエラーが発生しました。詳細については、エラーログを確認してください。」というコメントが追加されます。このときのエラーログは `~/.guildbotics/data/error.log` に出力されています。

## 8.2. デバッグ情報の取得
以下の環境変数設定により、デバッグ情報を取得可能です。

- `AGNO_DEBUG`: `agno` エンジンの追加デバッグ出力（`true`/`false`）。
- `LOG_LEVEL`: ログレベル（`debug` / `info` / `warning` / `error`）。
- `LOG_OUTPUT_DIR`: ログ出力ディレクトリ（例: `./tmp/logs`）。この指定により、コンソールに出力しているログを指定したディレクトリ配下のファイルにも出力します。


# 9. Contributing
Pull Request (PR) を歓迎します。新しい機能の追加、バグ修正、ドキュメントの改善など、どんな貢献でも大歓迎です。

コーディングスタイル、テスト、ドキュメント、セキュリティガイドラインについては [CONTRIBUTING.ja.md](https://github.com/GuildBotics/GuildBotics/blob/main/CONTRIBUTING.ja.md) をご参照ください。
