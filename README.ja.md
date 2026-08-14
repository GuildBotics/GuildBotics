<h1>GuildBotics</h1>

[English](https://github.com/GuildBotics/GuildBotics/blob/main/README.md) • [日本語](https://github.com/GuildBotics/GuildBotics/blob/main/README.ja.md)

GuildBotics は、Claude Code や Codex などの AI CLI ツールを、開発チームで継続的に働く「チームメンバー」として運用するためのツールです。各メンバーには固有の名前、GitHub / Slack のアカウント、役割、記憶を設定できます。実際の調査・実装・判断は AI CLI ツールが行い、メンバーとしての外部操作（コミット、PR 作成、コメント、Slack 投稿、記憶の保存）はすべて専用 CLI（`guildbotics member`）を通して実行・記録されます。

同じメンバーと 2 つの方法で仕事ができます。

- **一緒に作業する** — Claude Code や Codex のセッションにメンバーを呼び出し、いま開いているリポジトリでペアプログラミングします（→ [メンバーと一緒に作業する](#メンバーと一緒に作業する)）
- **任せる** — GitHub Projects のチケットや Slack のメンションで依頼すると、メンバーが調査・実装・PR 作成・返信まで自律的に進めます（→ [GitHub チケットを任せる](#github-チケットを任せる) / [Slack で作業を依頼する](#slack-で作業を依頼する)）

どちらの働き方でも同じメンバー（同じ名義、同じ記憶）が働きます。一緒に作業しながら教えたことは記憶として残り、任せたときの作業にも引き継がれます。

設定・実行・監視は GuildBotics デスクトップアプリ（GUI）で行います。設定はワークスペース内のプレーンテキストファイルに保存されるため、そのまま GUI の無いサーバーへ移して `guildbotics` CLI だけで運用することもできます（→ [サーバーで運用する](#サーバーで運用する)）。

---

## 重要な注意（免責事項）

- 本ソフトウェアはアルファ版です。今後、破壊的な非互換を伴う変更が行われる可能性が非常に高く、動作不具合も頻繁に発生することが想定されるため、実運用環境での利用は推奨しません。
- 本ソフトウェアの動作不具合やそれによって生じた損害について、作者および配布者は一切の責任を負いません。特に、AIエージェントの誤動作や暴走により、利用中のシステムや外部サービスに対する致命的な破壊、データ損失、秘密データ漏洩が発生する可能性があります。使用は自己責任で行い、隔離されたテスト環境で検証してください。

---

- [何ができるか](#何ができるか)
- [はじめる](#はじめる)
- [メンバーと一緒に作業する](#メンバーと一緒に作業する)
- [GitHub チケットを任せる](#github-チケットを任せる)
- [Slack で作業を依頼する](#slack-で作業を依頼する)
- [決まった作業を自動で実行する](#決まった作業を自動で実行する)
- [独自のコマンドを作る](#独自のコマンドを作る)
- [運用リファレンス](#運用リファレンス)
- [トラブルシューティング](#トラブルシューティング)

---

## 何ができるか

- **複数メンバーの定義**: 異なる役割、個性、記憶を持つ複数の AI メンバーを定義できます（設定ファイル上の識別子は `person`）
- **GitHub 統合**: GitHub Projects / Issues によるチケット管理と、メンバーによる PR 作成・コメント・レビュー対応
- **Slack 統合**: 設定したチャネルをメンバーが監視し、そこで受けた依頼を本人として処理・応答
- **メンバー記憶**: メンバーがセッションを越えて参照・維持する個人/チームの記憶
- **対話メンバーセッション**: guildbotics スキルにより、AI CLI ツールが現在のリポジトリでメンバーとして作業
- **スケジュール実行**: メンバーごとの巡回実行コマンドと、cron ベースの定期実行コマンド
- **カスタムコマンド**: Markdown プロンプト / Python / Shell / YAML で独自の作業を定義し、メンバーや役割ごとに再利用
- **LLM / AI CLI ツールの切り替え**: LLM プロバイダーの切り替え、または AI CLI ツール（Codex、Claude Code、Grok Build、Antigravity など、ローカル CLI から起動する AI 実行ツール）への委譲
- **Desktop AI アシスタント**: コマンドエディタでの質問応答・変更提案の適用と、診断画面での実行失敗の原因調査
- **多言語対応**: 英語 / 日本語

## はじめる

### 必要なもの

- **OS**: Linux（Ubuntu 24.04 で動作確認）、macOS（Sequoia で動作確認）、または Windows 11
  - デスクトップアプリは macOS Apple Silicon (arm64)、Linux x86_64、Windows x86_64 を対象とします
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)**: GuildBotics のビルドと CLI のインストールに使います
- **LLM API キー**（いずれか 1 つを事前に取得）:
  - Google Gemini API: [Google AI Studio](https://aistudio.google.com/app/apikey)
  - OpenAI API: [OpenAI Platform](https://platform.openai.com/api-keys)
  - Anthropic Claude API: [Anthropic Console](https://console.anthropic.com/settings/keys)
- **AI CLI ツール**（いずれか 1 つを事前にインストールして一度起動し、認証を済ませてください）:
  - [Antigravity CLI](https://github.com/google-antigravity/antigravity-cli)
  - [OpenAI Codex CLI](https://github.com/openai/codex/)
  - [Claude Code](https://docs.anthropic.com/en/docs/claude-code)（Claude Pro または Max サブスクリプションが必要）
  - [Grok Build](https://docs.x.ai/build/overview)
  - [GitHub Copilot CLI](https://docs.github.com/ja/copilot/concepts/agents/about-copilot-cli)

Codex・Claude Code・Grok Build・GitHub Copilot CLI・Antigravity CLI を利用する場合、メンバーはセッションを引き継いで前回の続きから作業を再開できます。認証方法、Slack スレッドやチケットとセッションの対応付け、実行権限の設定については [Codex・Claude Code・Grok Build・GitHub Copilot・Antigravity のセッション連携](docs/native_agent_runtime.ja.md)を参照してください。

### インストール

現在、デスクトップアプリの一般向けインストーラーは配布していません。リポジトリを取得し、ローカルでビルドします。ビルドには uv に加えて **Node.js 24 以上**と **Rust (rustup) stable 1.88 以上**（Linux では WebKitGTK 4.1 開発パッケージ）が必要です。macOS / Linux の手順は [desktop/README.md](desktop/README.md#1-前提ツール)、Windows 実機のセットアップ・NSIS build・smoke checklist は [Windows で GuildBotics Desktop をビルドする](docs/windows_desktop_build.ja.md)を参照してください。

デスクトップアプリの初回起動時に、以下が配置されます。

- macOS / Linux の `~/.guildbotics/bin/guildbotics`、または Windows の `%USERPROFILE%\.guildbotics\bin\guildbotics.exe`: AI CLI ツール / スキルが使う管理用 GuildBotics CLI
- macOS / Linux の `~/.local/bin/guildbotics`: 上記 CLI へ転送する小さな実行ファイル（shim）。Windows では代わりに NSIS installer が managed bin を user PATH へ追加し、uninstall 時は自分が追加した entry だけを削除します
- 検出済みの Codex / Claude Code / Grok Build / Antigravity CLI / GitHub Copilot CLI のユーザースキル用ディレクトリ配下の GuildBotics スキル。ユーザーが作成・編集したスキルは上書きしません

デスクトップアプリを使わない環境（ヘッドレスサーバーなど）では、`uv tool install guildbotics` で CLI を単体インストールできます（→ [サーバーで運用する](#サーバーで運用する)）。

### 初期セットアップ

デスクトップアプリを起動すると **プロジェクト** 設定が開くので、以下を設定します。

- エージェントの既定言語（英語/日本語）。コマンド、ロール定義、LLM への指示で使う
- ワークスペースフォルダの選択
- プロジェクトの説明文
- GitHub 連携を行うかどうか

**GitHub / Slack と連携する場合は、設定を始める前にサービス側の準備が必要です。** どちらの連携も任意で、あとから **設定** 画面で有効にできます。まず動かしてみたい場合は、GitHub 連携を「行わない」で進めてください。

- **GitHub**: 初期設定を完了するには、有効な GitHub Project の URL が必須です。先に [GitHub プロジェクトを作成する](#github-プロジェクトを作成する)（Todo / In Progress / Done のステータスを用意）と [AI エージェント用の GitHub アカウントを用意する](#ai-エージェント用の-github-アカウントを用意する)（アカウント種別の選択とトークン発行）を済ませてください。メンバーに GitHub アカウントを割り当てる場合は、そのメンバーのユーザー名・git メールアドレス・認証情報も必須になります
- **Slack**: メンバーが Slack で依頼を受けるには、Socket Mode の Slack App と bot / app トークンが必要です。準備する内容は [Slack で作業を依頼する](#slack-で作業を依頼する)を参照してください

GuildBotics では、プロジェクトの作業場所として選ぶフォルダを **ワークスペース** と呼びます。ワークスペースには、以下のようなプレーンテキストの設定ファイルが書き出されます。

- `.guildbotics/local/debug.env`: デバイス固有の非シークレットなデバッグ設定（ログレベル）
- `.guildbotics/config/secrets.yml`: OS キーチェーンに保存したシークレットのキー名一覧（値は含まない）
- `.guildbotics/config/team/project.yml`: プロジェクト定義
- `.guildbotics/config/intelligences/`: LLM と AI CLI ツールの設定

API キーやアカウントトークンの値は、利用可能な場合は OS キーチェーンに保存され、上記ファイルにはキー名だけが記録されます（→ [API キーとトークンの保存](#api-キーとトークンの保存)）。

プロジェクト設定に続けて、デスクトップアプリで以下を設定します。

- **LLM・AI CLIツール**: デフォルトの LLM、AI CLI ツールの選択と LLM API キーの設定
- **メンバー**: チームメンバーの追加と設定（GitHub アカウントを割り当てる場合は、上記の認証情報が必要です）
- **GitHub**: タスクボードの設定（GitHub を利用する場合のみ。GitHub Project の URL 自体は**プロジェクト**セクションで入力します）。独自ステータス名を使う場合の[レーンマッピング](#タスクボードの取り決め)と、`Agent` フィールドを設定します
- **検証**: **設定を検証** を押すと、LLM、AI CLI ツール、GitHub、Slack、Git を読み取り専用でチェックします。GitHub や Slack のデータは更新しません

### クイックスタート

セットアップが完了したことを、カスタムコマンドの実行で確認します。デスクトップアプリの **コマンド編集** 画面を開いてください。

初期セットアップの際、ワークスペースの `.guildbotics/config/commands/` にサンプルコマンドが配置されています。ここではそのうちの `translate` を使います。

1. コマンド一覧から `translate` を選びます。

2. 実行パネルの **入力文** に `Hello` と入力し、**保存して実行** を押します。

**出力** に翻訳結果（「こんにちは」など）が表示されれば成功です。設定したデフォルト LLM が呼び出され、カスタムコマンドが動作しています。

日常的に使うコマンドには、ホットキーを割り当てられます。

- **設定 → ショートカット**: **コマンド実行ウィンドウ** を開くホットキー。翻訳したい文字列をコピーしてから押せば、そのまま入力文として実行できます
- **コマンド編集画面のコマンドバーのホットキーチップ**: 押すとそのコマンドを直接実行します。入力が足りない場合はコマンド実行ウィンドウが開きます

### 次にやること

- いま開いているリポジトリでメンバーとペアプログラミングする → [メンバーと一緒に作業する](#メンバーと一緒に作業する)
- チケットから PR 作成までを任せる → [GitHub チケットを任せる](#github-チケットを任せる)
- Slack でメンバーに作業を依頼する → [Slack で作業を依頼する](#slack-で作業を依頼する)
- 定期処理や独自コマンドを作る → [決まった作業を自動で実行する](#決まった作業を自動で実行する) / [独自のコマンドを作る](#独自のコマンドを作る)

## メンバーと一緒に作業する

デスクトップアプリの初回起動時に、検出された Claude Code / Codex などのユーザースキル用ディレクトリへ **guildbotics スキル**が配置されます（→ [インストール](#インストール)）。このスキルを使って、いま開いているリポジトリでメンバーと一緒に作業します。

スキルはユーザー設定ディレクトリ（`~/.claude` や `~/.codex` など）に置かれるため、CLI から起動しても、そのツールのアプリから起動しても同じスキルが使われます。スキルが配置されているかは、デスクトップアプリの **設定 → LLM・AI CLIツール** で各ツールのスキル状態から確認できます。

前提は 2 つです。

- デスクトップアプリを一度起動していること（guildbotics スキルと管理用 CLI が配置されます）
- メンバーが設定済みであること

作業したいリポジトリで AI CLI ツールを起動し、**スキル名とメンバーを挙げて**作業を依頼します。この一言がスキルを呼び出す合図になります。

```text
guildbotics スキルを使い、メンバーの alice としてこの変更をコミットして push してください
```

AI CLI ツールはメンバーのプロフィール（役割、判断基準、話し方）と記憶を読み込み、以降はそのメンバーとして応答します。コミット、push、PR 作成、Slack 投稿などの外部操作はメンバー本人の名義で実行されます。

作業中に「これを覚えておいて」と伝えると、メンバーの記憶として保存されます。この記憶は、チケットや Slack 経由で作業を任せたときにも参照されます。

## GitHub チケットを任せる

GitHub Projects のチケットでメンバーに作業を依頼し、調査・実装・PR 作成までを任せる使い方です（デフォルトの `ticket_driven_workflow` を使用）。

**注**: GitHub 統合は任意です。GitHub 統合なしでも、Slack チャットワークフローやスケジュール実行によるコマンド自動化は利用できます。

### できること

- **タスクボードでのタスク依頼**: チケットの `Agent` フィールドでメンバーを選び、着手可能レーンへ移動すれば、メンバーがそのタスクを実行します
- **実行結果の確認**: タスクが完了すると、メンバーがコメント、PR、レビュー返信、リアクションのいずれかの形で結果を残します
- **Pull Request の作成**: コード変更が必要な場合、メンバーが作業ブランチを公開し、Pull Request を作成または再利用します
- **チケット作成**: follow-up ticket の作成を指示すると、メンバーがリポジトリに実 issue を作成します

### GitHub プロジェクトを作成する

GitHub Projects (v2) のプロジェクトを作成し、以下の列（ステータス）をあらかじめ追加しておきます。

- Todo（着手可能）
- In Progress（進行中）
- Done（完了）

既存プロジェクトのステータス名をそのまま使いたい場合は、後述のレーンマッピングで紐付けできます。

### AI エージェント用の GitHub アカウントを用意する

メンバーが GitHub にアクセスするためのアカウントを用意します。以下のいずれかの方法が利用可能です。

- **マシンアカウント**（マシンユーザー）
  - 「AIエージェントとタスクボードやPull Requestを通じて対話しながら進める」という雰囲気が味わえるという意味でおすすめの方法ですが、[GitHub の利用規約上](https://docs.github.com/ja/site-policy/github-terms/github-terms-of-service#3-account-requirements)、無料で作成できるマシンアカウントは、1ユーザーにつき1つだけとなっていますのでご注意ください。
- **GitHub App**
  - アカウント作成数に制限がないというメリットはありますが、**個人**アカウントの GitHub Project へのアクセスはできません。また、GitHub サイト上ではボットであることが明記されるため、少し雰囲気が削がれます。
- **代理エージェント**（自分自身のアカウントを AI エージェント用に利用する）
  - 最も簡単な利用方法です。ただし、この方法の場合、AIエージェントと対話しながら進めるというよりは自問自答しているという見た目になります。

**マシンアカウントを利用する場合**:

1. 作成したマシンアカウントを Project およびリポジトリに Collaborator として追加してください
2. **Classic** PAT (Personal Access Token) を発行してください。スコープは `repo` と `project` の 2 つを選択してください

**GitHub App を利用する場合**:

デスクトップアプリのメンバー編集画面（GitHub タブで「GitHub Apps」を選択）で「新規に App を登録」を選ぶと、GitHub 上への App 作成とインストールを半自動で行えます。ブラウザで作成とインストール先の選択を承認するだけで、App ID・秘密鍵・インストール ID などの設定値は自動で取り込まれるため、以下の手動手順は不要です。

手動で作成する場合は、GitHub App 作成の際に以下の Permission 設定を行ってください。

- **Repository permissions**: **Contents** / **Issues** / **Projects** / **Pull requests** / **Workflows** をそれぞれ Read & Write
- **Organization permissions**: **Projects** を Read & Write

既存 App に **Workflows** を追加した場合は、再利用する前に各インストール先で権限変更を承認してください。

GitHub App 作成後に以下の作業を行ってください。

1. GitHub App 設定ページで「Generate a private key」により `.pem` ファイルをダウンロードして、保存してください
2. 「Install App」からリポジトリ/組織にインストールを行い、**インストールID**を取得してください。インストール後に表示された画面の URL の末尾の数字（`.../settings/installations/<インストールID>`）がインストール ID です。設定時に利用するため、メモしておいてください

**代理エージェントを利用する場合**:

自分自身のアカウントを利用する場合も、**Classic** PAT を発行してください。スコープは `repo` と `project` の 2 つを選択してください。

### 認証情報と実行環境を準備する

- 各メンバーの GitHub 認証情報（PAT または GitHub App の設定値）を、デスクトップアプリのメンバー設定から GuildBotics に登録してください。GitHub / git への書き込みは、ローカルの `gh auth` ユーザーではなく、割り当てられたメンバーの認証情報で行われます
- チケット駆動の作業は、メンバーごとの作業ディレクトリ（既定: `<workspace>/.guildbotics/local/clones/<person_id>`）で行われます。リポジトリの複製、push、PR 作成、コメントは、メンバー自身が `guildbotics member` CLI 経由で実行します
- AI CLI ツールを対話的にも使う場合は、`gh`、直接のトークン / API 書き込み、`git push` を拒否または承認必須にすることを推奨します。これは利用者自身の GitHub アカウントへフォールバックすることを避けるための防止策であり、トークン流出を完全に技術的に封じ込めるサンドボックスではありません
- Codex を AI CLI ツールとして使う場合は、Codex CLI の認証とネットワーク到達性を確認してください:

  ```bash
  codex doctor
  ```

### 設定を検証する

デスクトップアプリの **設定 → 検証** で **設定を検証** を押すと、各アクティブメンバーの LLM・AI CLI ツール・GitHub・Slack・Git 設定が実際に利用可能かをチェックできます（GitHub / Slack へは読み取り専用でアクセスします）。

### タスクボードの取り決め

**カスタムフィールド**: タスクを実行するメンバーを選ぶ `Agent` フィールドが管理されます。GuildBotics が GitHub Project を最初に操作したときに自動で作成されるため、明示的なセットアップは不要です。GitHub App のメンバーは GitHub の assignee にできないため、メンバーの指定にはこのフィールドを使います。

**レーンマッピング**: GuildBotics は GitHub Projects のステータスを 3 つのレーン（着手可能 / 作業中 / 完了）として扱います。

- 既定では `Todo` が着手可能、`In Progress` が作業中、`Done` が完了です。標準的なボードなら設定は不要です
- 着手可能と完了の**間**にあるステータス（例: `In Review`）は、自動的に作業中として扱われます
- 着手可能より**前**のステータス（例: `Backlog`）と、完了**以降**のステータス（例: `Icebox`）は無視されます。途中レーンや保留レーンは、ボード列の並び順だけで追加できます
- 独自のステータス名を使っている場合は、デスクトップアプリの **設定 → GitHub** の **レーンマッピング** で対応付けてください。GUI を使わない場合は `team/project.yml` の `services.ticket_manager.lane_map` に指定します（→ [設定ファイル](#設定ファイル)）

### サービスを起動して作業を依頼する

デスクトップアプリの **サービス実行** 画面を開き、**実行** を押します。有効メンバーごとにワーカーが起動し、選択した経路の作業を順に実行します。

画面には 3 つの実行経路があり、それぞれ **サービス実行に含める** で個別に切り替えられます（切り替えはサービス停止中のみ）。

- **巡回実行コマンド**: 各メンバーの巡回実行コマンドを **巡回間隔（分）** ごとに実行します。GitHub アカウントを設定した新規メンバーの既定は チケット駆動ワークフローです。GitHub を使わないメンバーには巡回実行コマンドが設定されないため、**設定 → メンバー → 巡回** で選んでください
- **定期実行コマンド**: メンバー設定の定期実行コマンドを、指定した時刻に実行します
- **イベント起動**: Slack などのイベントを受信し、チャットワークフローへ渡します

チケット駆動ワークフローを使うには、**巡回実行コマンド** を含めた状態で開始します。**連続失敗で停止する回数**（既定 3 回）に達すると、そのメンバーのワーカーは停止します。

実行中の状況は **診断** 画面の **Global / システム** セッション（イベント / ログ）で確認でき、メンバーごとの作業結果は **アクティビティ** 画面にまとまります。

タスクを依頼するには、GitHub Projects のチケットを以下のように操作します。

1. チケットを作成し、対象の Git リポジトリを選択して Issue として保存
2. チケットにメンバーへの指示を記述（この内容がプロンプトとなるため、できるだけ具体的に記述）
3. `Agent` フィールドで対象のメンバーを選択
4. チケットを着手可能レーンへ移動

作業が始まると、メンバーとは次のようにやり取りします。

- メンバーは作業中に質問がある場合、チケットコメントで質問を投稿します。チケットコメントで回答すると、メンバーは定期チェックで回答を拾って作業を進めます
- タスクが完了すると、メンバーがコメント / PR URL / レビュー返信 / リアクションを残します
- チケットから作成された PR へのレビューは PR 上で書き込んでください。未対応の review thread は担当メンバーへ再委譲されます

止めるときは **停止** を押します。新しい作業の受付を止め、実行中の作業の完了を待ってから終了します。待たずに終了する場合は **強制停止** を押すと、実行中の作業をキャンセルします。

CLI からサービスを起動・停止する方法は [サーバーで運用する](#サーバーで運用する)を参照してください。

## Slack で作業を依頼する

メンバーが指定された Slack チャンネルを監視し、そこで受けた依頼を本人として処理します。「この PR のレビューコメントに対応して」のように依頼すると、メンバーは対象リポジトリを特定して作業ブランチを準備し、実際に調査・修正・GitHub 操作まで行ったうえで、その結果を Slack に返します。

Slack でどう振る舞うか（本文で返信する / リアクションだけ付ける / 何もしない / 確認の質問を返す / 対応できない理由を報告する）は、メンバー（AI CLI ツール）がメッセージ内容、自身の役割とプロフィール、チャンネルごとの参加条件から判断します。単なる雑談や、自分の役割で価値を足せない会話には無理に返信しません。

必要な設定は 2 つです。

1. **Slack App の作成**: デスクトップアプリの **設定 → メンバー → Slack** タブで **新規に App を登録** を選び、**Slack で App を作成** を押します。必要な scope・Socket Mode・イベント購読を設定済みの状態でブラウザが開くので、ワークスペースを選んで作成したあと、**OAuth & Permissions** で **Reinstall to Workspace** を実行してから 2 つのトークン（`xoxb-...` / `xapp-...`）をコピーします（作成直後の Bot トークンには scope が付与されないため、再インストールが必要です）
2. **メンバーへの登録**: 同じ Slack タブで、以下を設定します
   - **Slack Bot トークン**（`xoxb-...`）と **Slack App トークン**（`xapp-...`）を貼り付け、**App を検証** で bot 名・ワークスペース・権限・チャンネルへの参加状況を確認します
   - **チャンネル**: 監視するチャンネル名または ID を追加します
   - **会話への参加条件**: 積極的に参加 / 必要なときだけ参加 / メンションのみに対応 から選びます

すでに Slack App を作成済みの場合は **登録済みの App を使う** を選び、トークンの貼り付けから始められます。

設定内容はメンバーの `person.yml`（`message_channels`）に保存されます。

チャットイベントを受信するには、**サービス実行** 画面で **イベント起動** を含めてサービスを開始します。決まった時刻の定期投稿（ニュースダイジェストなど）は受信とは別経路で、定期実行コマンドとして `workflows/chat_post_command` を設定します。

Slack App を手動で作成する場合の手順（必要な scope の一覧）、複数メンバーでの接続共有、thread への参加条件の詳細、定期投稿の設定例は [Slack 連携ガイド](docs/slack_integration.ja.md)を参照してください。

## 決まった作業を自動で実行する

デスクトップアプリの **設定 → メンバー → 巡回** タブで、メンバーごとに 2 種類の自動実行を設定します。どちらも **サービス実行** 画面で開始したサービスが実行します。

- **巡回実行コマンド**: サービスの稼働中、**巡回間隔（分）** ごとにラウンドロビンで繰り返し実行します（例: タスクボードの巡回）。「このメンバーの巡回実行コマンドを設定する」をオンにして、保存済みコマンドを選びます。オフのメンバーは巡回実行しません
- **定期実行コマンド**: **定期実行を追加** でコマンドと実行時刻を指定します（例: 定期レポート、クリーンアップ）。時刻は 毎時 / 毎日 / 毎週 のプリセットか、**詳細 cron**（5 フィールドの cron 表記）で指定します

巡回間隔と、連続失敗でワーカーを止める回数は **サービス実行** 画面で指定します。

設定内容はメンバーの `person.yml` に保存されます。GUI を使わないサーバーでは、このファイルを直接編集します。

```yaml
person_id: alice
name: Alice
is_active: true

routine_commands:
  - workflows/ticket_driven_workflow

task_schedules:
  - command: examples/reports/morning_summary
    schedules:
      - "0 9 * * 1-5" # 平日午前9:00
```

cron 表記は標準の 5 フィールドに加え、実行時刻をランダム化する独自構文（`?` / `?(min-max)`。複数メンバーの同時実行回避などに使用）をサポートします。この構文は **詳細 cron** の入力欄にもそのまま指定できます。

cron 表記の詳細、ランダム化構文、スケジューラの内部動作、マルチエージェント構成の設定例は[スケジュール実行ガイド](docs/scheduling.ja.md)を参照してください。

## 独自のコマンドを作る

クイックスタートで実行した `translate` のように、独自のコマンドを定義して、手動実行・定期実行・Slack 定期投稿に使えます。作成と動作確認はデスクトップアプリの **コマンド編集** 画面で行います。

**AI アシスタントに作らせる**: **新規作成** で **AIに作成させる** を選び、やりたいことを書いて依頼すると、コマンド名・形式・ソースが提案されます。編集中のコマンドについては、**AIアシスタント** に質問したり、変更を依頼したりできます。

変更提案は、チャット履歴ではなくエディタ側の読み取り専用タブに「現在」「新規作成」「更新」として表示されます。「更新」タブは行番号付きの差分、「新規作成」タブは完成版ソースです。内容を確認して **変更を適用** を押すまで、ファイルは変更されません。

**動作確認**: **保存して実行** で、その場で実行結果（出力・イベント）を確認できます。**実行者** を切り替えると、メンバー個別の設定で実行できます。

**コマンドの種類**:

1. **Markdown コマンド** (`.md`): LLM プロンプトとして実行。テキスト処理、翻訳、要約などに最適
2. **Python スクリプト** (`.py`): プロジェクト情報やチームメンバー情報へアクセスできる形で実行。複雑な処理や API 連携に最適
3. **Shell スクリプト** (`.sh`): シェルコマンドとして実行
4. **YAML ワークフロー** (`.yml`): 複数のコマンドを組み合わせて実行

**コマンドの配置場所**（優先順位順）:

1. **メンバー毎のコマンド**: `.guildbotics/config/team/members/<person_id>/commands/`
2. **プロジェクト共有コマンド**: `.guildbotics/config/commands/`
3. **組み込みコマンド**: パッケージ内の `guildbotics/templates/` に配置（フォールバック。例: `workflows/ticket_driven_workflow`）

コマンド編集画面が読み書きするのはプロジェクト共有コマンドです。メンバー毎のコマンドはファイルを直接配置します。優先度の高いファイルに隠れて実行対象にならない場合は、画面上に警告が表示されます。

設定ディレクトリは既定でワークスペースの `.guildbotics/config` であり、環境変数 `GUILDBOTICS_CONFIG_DIR` で変更できます。

**一歩進んだ例**: Markdown コマンドは、フロントマターでテンプレートエンジンや子コマンド（`commands:`）を指定できます。以下は、クイックスタートで使ったサンプルコマンド `.guildbotics/config/commands/translate.md` の中身で、OS の表示言語を子コマンドで取得し、翻訳方向を切り替えています。

```markdown
---
description: 入力文をOSのUI言語と英語の間で相互翻訳します。OSのUI言語が英語の場合は日本語を使用します。
brain: default
template_engine: jinja2
inputs:
  message: required
commands:
  - name: os_ui_language
    command: functions/get_os_ui_language
---
入力メッセージは構造化データです。
{% if os_ui_language.language_code == "en" %}
`input`フィールドのテキストが日本語であれば英語に、英語であれば日本語に翻訳してください。
{% else %}
`input`フィールドのテキストが{{ os_ui_language.language_name }}であれば英語に、英語であれば{{ os_ui_language.language_name }}に翻訳してください。
{% endif %}
翻訳結果だけを返してください。
```

`functions/get_os_ui_language` は組み込みコマンドのため、ワークスペースに補助ファイルを配置する必要はありません。

**実行方法**:

- **コマンド編集** 画面の **実行**（入力文と追加引数を指定できます）
- ホットキーから開く **コマンド実行ウィンドウ**
- **設定 → メンバー → 巡回** タブでの巡回実行 / 定期実行（→ [決まった作業を自動で実行する](#決まった作業を自動で実行する)）
- Slack への定期投稿（`workflows/chat_post_command` と組み合わせて定期実行に設定）
- CLI の `guildbotics run <command_name> [args...]`（→ [サーバーで運用する](#サーバーで運用する)）

フロントマターの全オプション、コンテキスト注入、コマンド合成などの詳細は[カスタムコマンド開発ガイド](docs/custom_command_guide.ja.md)を参照してください。

## 運用リファレンス

### API キーとトークンの保存

GuildBotics は、シークレット（LLM API キーおよびアカウントトークン類）を可能な限りプレーンテキストファイルの外に保存します。

- **OS キーチェーン:** シークレット値は OS 秘密ストア（macOS キーチェーン、Windows 資格情報マネージャー、Linux Secret Service）に保存します。ワークスペース側には、キー名と世代だけを記録した非シークレットのインデックス `.guildbotics/config/secrets.yml` と、デバイス固有世代の `.guildbotics/local/secrets.json` を置きます。`.env` バックエンドはありません。
- **Windows の資格情報:** GuildBotics はシークレット値を UTF-8 の Credential Manager blob として保存するため、ASCII が中心の PEM 秘密鍵でも Windows の 2,560 byte 上限をすべて利用できます。import は書き込み前に全値を検証します。
- **優先順位:** 実環境変数 > OS キーチェーン。GuildBotics はワークスペースの `.env` を読みません。
- **GitHub App 秘密鍵:** メンバー保存時に PEM をキーチェーンへ吸収します。登録時に生成したファイルは OS の一時ディレクトリへ書き、吸収後に削除します。鍵の中身は環境変数には出しません。
- **交換形式:** `guildbotics secrets export` / `import` は dotenv を転送ファイルとしてだけ使います。`secrets set --from-file` は PEM などのファイルをキーチェーンへ取り込みます。

シークレットの管理には `guildbotics secrets` CLI を使います（サブコマンドとオプションの一覧は [CLI リファレンス](docs/cli_reference.md#guildbotics-secrets)を参照）。

```bash
guildbotics secrets status                        # OS 秘密ストアの利用可否と登録キー数
guildbotics secrets export --file secrets.env     # 引越用にシークレットを書き出し
guildbotics secrets import secrets.env            # 移行先マシンで読み込み
```

シークレットはワークスペースごとに保存されます（キーチェーンのエントリは `secrets.yml` の `store_id` で名前空間が分かれます）。対象ワークスペースはサブコマンドの前の `--workspace` で指定できます。省略時は選択中の active workspace が必須です。対象がどこに解決されたかは `guildbotics secrets status` の `workspace:` 行で常に確認できます。

```bash
guildbotics secrets --workspace /path/to/workspace status
guildbotics secrets --workspace /path/to/workspace list
```

### サーバーで運用する

非シークレットの設定はすべてワークスペース内のプレーンテキストファイルに保存されるため、一度セットアップすれば GUI のない環境（ヘッドレスサーバー等）へ移して CLI だけで運用できます。

1. 移行先で `uv tool install guildbotics` により CLI をインストールします
2. ワークスペースフォルダを移行先へコピーします
3. シークレットを移行します。移行元で `guildbotics secrets export --file ...`、移行先で `guildbotics secrets import ...` を実行します（エクスポートファイルは使用後に削除してください）。キーチェーンのエントリ自体がマシンの外に出ることはありません
4. キーチェーンの無いサーバーでは、OS 秘密ストアを用意するか、実行時の環境変数でシークレットを渡してください

**サービスの起動と停止**（デスクトップアプリの **サービス実行** 画面に相当します）:

```bash
guildbotics workspace status   # 対象ワークスペースと設定の確認
guildbotics start              # サービスを起動
guildbotics stop               # 新規受付を止め、実行中の作業の完了を待って終了
guildbotics kill               # 即座に強制停止
```

`guildbotics stop` をもう一度実行すると、実行中の作業をキャンセルします（GUI の **強制停止** に相当）。`--timeout` や `--force` などのオプションは [CLI リファレンス](docs/cli_reference.md#guildbotics-stop)を参照してください。

`guildbotics start` は既定でメンバーワーカー（巡回 / 定期 / キュー済みイベント）とイベントリスナーの両方を起動します。`--only` で実行内容を絞れますが、どちらの場合もメンバーワーカー自体は起動します。

- `--only scheduler`: 巡回実行コマンドと定期実行コマンドだけを実行し、イベントリスナーは起動しません（Slack イベントを受信しません）
- `--only events`: イベント受信とキュー済みイベントの実行だけを行い、巡回実行コマンドと定期実行コマンドは実行しません

**コマンドの実行**（デスクトップアプリの **コマンド編集** 画面での実行に相当します）:

```bash
guildbotics run <command_name> [args...]
echo "Hello" | guildbotics run translate
```

`--person` または `<command>@<person_id>` で実行メンバーを指定できます。

CLI とデスクトップアプリは共通のロックファイル（`~/.guildbotics/data/run/service.lock`）を使うため、同じマシンでサービスが二重起動することはありません。

### アカウント関連環境変数

**LLM API キー**:

- `GOOGLE_API_KEY`: Google Gemini API
- `OPENAI_API_KEY`: OpenAI API
- `ANTHROPIC_API_KEY`: Anthropic Claude API

**Slack アクセス**（メンバー毎、形式: `{PERSON_ID}_...`）:

- `{PERSON_ID}_SLACK_BOT_TOKEN`: Slack Bot Token
- `{PERSON_ID}_SLACK_APP_TOKEN`: Slack App-Level Token

**GitHub アクセス**（メンバー毎、形式: `{PERSON_ID}_...`）:

- `{PERSON_ID}_GITHUB_ACCESS_TOKEN`: マシンアカウント/代理エージェント用 PAT
- GitHub App の ID はメンバー YAML（`account_info.github_app_id` / `github_installation_id`）に置き、PEM は OS キーチェーンの `{PERSON_ID}_GITHUB_PRIVATE_KEY` に保存します

OS キーチェーンに保存されたシークレットは自動で読み込まれます。GuildBotics はワークスペースの `.env` を読みません。

### ワークスペースとデータの保存場所

使用中のワークスペースは `~/.guildbotics/data/active-workspace.json` に記録されます。CLI からの確認・変更には以下を使います。

```bash
guildbotics workspace status
guildbotics workspace current
guildbotics workspace use /path/to/workspace
guildbotics workspace migrate --from /path/to/source-checkout --to /path/to/guildbotics-workspace
```

`guildbotics member` コマンドが使うワークスペースは、次の順で解決されます。

1. サブコマンドの前の `--workspace <dir>` 指定
2. 明示的な `GUILDBOTICS_WORKSPACE_ROOT`、または `<workspace>/.guildbotics/config` を指す `GUILDBOTICS_CONFIG_DIR`
3. デスクトップアプリまたは `guildbotics workspace use` が記録した使用中のワークスペース

プロセスのカレントディレクトリはワークスペースとして扱いません。選択されたワークスペースから `GUILDBOTICS_CONFIG_DIR` が `<workspace>/.guildbotics/config` に設定されます。

GuildBotics が保存するローカルデータは、次の 3 種類です。

- 使用中のワークスペース情報や CLI スケジューラーの PID など、PC 全体で共有する管理情報は `$HOME/.guildbotics/data` に保存されます
- memory、会話の制御状態、task-run 証跡、Activity イベントなど、共有する永続状態は `<workspace>/.guildbotics/state` に保存されます
- 診断ログ、transcript、チャット cache、member clone、AI CLI session など、このマシンだけのデータは `<workspace>/.guildbotics/local` に保存されます

### 設定ファイル

**プロジェクト設定** (`team/project.yml`):

- `name`: プロジェクト名
- `description`: エージェント文脈として使う短いプロジェクト説明
- `language`: プロジェクト言語
- `services.ticket_manager`: GitHub Projects 設定
- `services.ticket_manager.lane_map`: 着手可能 / 作業中 / 完了レーンを GitHub Project のステータス名に対応付けます。Project が独自のステータス名を使う場合に設定します
- `services.code_hosting_service`: コードホスティングサービス設定（リポジトリ操作に使う GitHub owner）

**メンバー設定** (`team/members/<person_id>/person.yml`):

- `person_id`: 一意な識別子（英数字小文字、`-`、`_` のみ）
- `name`: 表示名
- `is_active`: AI エージェントとして動作するかどうか
- `profile.roles`: 役割の割り当て
- `routine_commands`: デフォルトルーチンコマンドの上書き
- `task_schedules`: cron ベースのスケジュールコマンド
- `message_channels`: 監視対象チャネル設定（`chat.enabled`, `chat.event_source=socket_mode`, `channel_id`/`name`）
- `profile.character`: 興味・嗜好・会話参加方針などのプロフィール

**LLM / AI CLI ツール設定**:

- `intelligences/cli_agent_mapping.yml`: デフォルトの AI CLI ツール選択
- `intelligences/native_agent_policy.yml`: Codex・Grok Build・GitHub Copilot CLI のファイルアクセス範囲（`workspace` または `host`）。新規ワークスペースの setup 時に作成され、デスクトップアプリの **LLM・AI CLIツール → 詳細設定**、または画面を利用できない環境でのファイル直接編集により設定します。ネットワークアクセスと確認を求めない実行方式は GuildBotics の連携内で固定します
- `intelligences/cli_agents/<tool>/*.yml`: AI CLI ツールごとの effort マッピング。実行できるのは Codex・Claude Code・Grok Build・GitHub Copilot CLI・Antigravity CLI のみで、他のツールに対応するには GuildBotics リポジトリへネイティブアダプタを実装します
- `team/members/<person_id>/intelligences/`: Codex・Grok Build・GitHub Copilot CLI の実行権限を含むメンバーごとの任意の上書き。既定ではチーム設定を継承します

設定可能な値とセキュリティ上の注意事項は、[Codex・Claude Code・Grok Build・GitHub Copilot・Antigravity のセッション連携](docs/native_agent_runtime.ja.md#設定)を参照してください。

### CLI リファレンス

CLI コマンドとオプションの完全な一覧は、ソースコードから生成される [CLI リファレンス（英語）](docs/cli_reference.md)を参照してください。

## トラブルシューティング

| 症状 | 最初に確認すること |
| --- | --- |
| `guildbotics` コマンドが見つからない | macOS / Linux では `~/.guildbotics/bin/guildbotics` を実行し、`~/.local/bin` の PATH を確認します。Windows では install 後に新しい shell を開き、user PATH の `%USERPROFILE%\.guildbotics\bin` を確認します |
| どのワークスペースが使われているか分からない | デスクトップアプリの **設定 → プロジェクト** で確認・変更できます。CLI では `guildbotics workspace status` / `guildbotics workspace use <path>` を使います |
| メンバーが動作しない・設定に不安がある | デスクトップアプリの **設定 → 検証** で LLM・AI CLI ツール・GitHub・Slack 設定を検証してください |
| GitHub に書き込めない | メンバーの PAT スコープ（`repo` + `project`）または GitHub App の Permission を確認してください。`guildbotics member context --person <person_id> --check-credentials` でも確認できます |
| Slack イベントを受信しない | Socket Mode、App-Level Token、bot events の設定と、**サービス実行** 画面で **イベント起動** を含めて開始しているか（CLI なら `--only scheduler` で起動していないか）を確認してください |
| コマンド実行が失敗した | デスクトップアプリの **診断** 画面で該当セッションを開き、ログを確認してください。AI アシスタントに原因を調べさせることもできます |
| スケジューラが止まった | **連続失敗で停止する回数**（既定: 3 回）に達するとワーカーが停止します。**診断** 画面で失敗原因を確認してから再起動してください |

**診断ログ**: 検索用の実行サマリーは `<workspace>/.guildbotics/local/run/diagnostics.jsonl` に記録され、イベント・ログ・span・入出力の全文は実行ごとの JSONL として `run/sessions/` に保存されます。デスクトップアプリの **診断** 画面では、実行履歴と最新の Global / system session の両方を確認できます。

**デバッグ出力**: 詳細なログを取得するための環境変数:

- `LOG_LEVEL`: `debug` / `info` / `warning` / `error`
- `AGNO_DEBUG`: Agno エンジンの追加デバッグ出力 (`true`/`false`)

transcript の詳細度（`standard` / `full`）と保持日数は、デスクトップアプリの **診断** 画面から設定し、`.guildbotics/config/transcripts.yml` に保存されます。
