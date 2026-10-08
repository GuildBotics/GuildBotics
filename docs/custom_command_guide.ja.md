# カスタムコマンド開発ガイド

GuildBotics のカスタムコマンドは、エージェントに任意の処理手順を教えるための仕組みです。Markdown ファイルに記述したプロンプトでLLM呼び出しを行ったり、シェルスクリプトで外部ツールを操作したり、Python ファイルで本格的なワークフローを構築したりできます。

- [カスタムコマンド開発ガイド](#カスタムコマンド開発ガイド)
  - [1. クイックスタート](#1-クイックスタート)
    - [1.1. プロンプトファイルを作成する](#11-プロンプトファイルを作成する)
    - [1.2. コマンドを呼び出す](#12-コマンドを呼び出す)
    - [1.3. メンバーの指定](#13-メンバーの指定)
    - [1.4. コマンドが動く場所](#14-コマンドが動く場所)
  - [2. 変数展開のバリエーション](#2-変数展開のバリエーション)
    - [2.1. 名前付き引数の例](#21-名前付き引数の例)
    - [2.2. Jinja2 の例](#22-jinja2-の例)
    - [2.3. context 変数の利用](#23-context-変数の利用)
    - [2.4. Desktop の入力欄設定](#24-desktop-の入力欄設定)
  - [3. AI CLIツールの利用](#3-ai-cliツールの利用)
    - [3.1. 読み取り専用の宣言](#31-読み取り専用の宣言)
  - [4. 組み込みコマンドの利用](#4-組み込みコマンドの利用)
  - [5. サブコマンドの利用](#5-サブコマンドの利用)
    - [5.1. サブコマンドの名前付けと出力結果の参照](#51-サブコマンドの名前付けと出力結果の参照)
    - [5.2. スキーマ定義](#52-スキーマ定義)
    - [5.3. print コマンド](#53-print-コマンド)
    - [5.4. to\_html コマンド](#54-to_html-コマンド)
    - [5.5. to\_pdf コマンド](#55-to_pdf-コマンド)
  - [6. シェルスクリプトの利用](#6-シェルスクリプトの利用)
  - [7. Python コマンドの利用](#7-python-コマンドの利用)
    - [7.1. 引数の利用](#71-引数の利用)
    - [7.2. コマンドの呼び出し](#72-コマンドの呼び出し)
    - [7.3. 失敗を利用者に伝える](#73-失敗を利用者に伝える)
  - [8. 巡回（routine）コマンドの宣言](#8-巡回routineコマンドの宣言)


## 1. クイックスタート

### 1.1. プロンプトファイルを作成する
まずは、LLM に翻訳を依頼するシンプルなコマンドを作ってみましょう。

プロンプト格納用設定フォルダ（デフォルト: ワークスペースの `.guildbotics/config/commands`。設定ディレクトリは `GUILDBOTICS_CONFIG_DIR` で変更可能）に以下のような内容でプロンプトファイル `translate.md` を作成します。

```markdown
---
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

ポイント:

- 組み込みの汎用Pythonコマンド `functions/get_os_ui_language` がコマンドを実行するマシンのOSのUI言語（隔離環境には `LANGUAGE` で渡ります。[1.4. コマンドが動く場所](#14-コマンドが動く場所)を参照）を取得し、入力文を構造化データとして保持します。
- OSのUI言語が英語の場合は、翻訳先または翻訳元として日本語を使用します。
- 言語を実行時引数で指定する必要はありません。
- 翻訳、校正、推敲、要約などの意味処理には `brain: default` を使います。AI CLIによるファイルやツールへのアクセスが必要な場合は `brain: agent`、決定的なレンダリングだけを行う場合は `brain: none` を使います。`brain: none` は呼び出し側の入力文を受け取らないため、`inputs.message: required` かつ子コマンドがないMarkdownコマンドでは使用できません。実行時は `brain` の省略も `default` として解決されますが、生成コマンドとサンプルでは実行方式を曖昧にしないため明示します。


### 1.2. コマンドを呼び出す

OSのUI言語が日本語の環境で `echo "こんにちは" | guildbotics run translate` のように実行すると、次のような出力が得られます。

```
Hello
```

**メモ:**
このコマンドを実行すると、LLMの呼び出し前に以下のような形にプロンプトファイルの内容が展開されます。

```
入力メッセージは構造化データです。
`input`フィールドのテキストが日本語であれば英語に、英語であれば日本語に翻訳してください。
翻訳結果だけを返してください。

input: こんにちは
language_code: ja
language_name: 日本語
```

これにより、LLMは応答として "Hello" を返します。

### 1.3. メンバーの指定

コマンドを実行するメンバーは `<コマンド>@<person_id>` の形式（または `--person`）で指定します。

例: `guildbotics run translate@yuki`

メンバーを指定しなかった場合は、チームの既定の実行者として実行されます。既定の実行者は `team/project.yml` の `default_person_id`（GuildBotics デスクトップアプリの「メンバー」画面から設定できます）です。未設定の場合は、有効なエージェントメンバーのうち person_id 順で最初のメンバーが使われるため、設定なしでも実行できます。実行できるメンバーが1人もいない場合だけ、メンバーの指定を求めます。

### 1.4. コマンドが動く場所

コマンドとそのサブコマンドは、Markdown・YAML・Python・シェルスクリプトも、その中のAI CLIツールのターンも、すべて1つのエージェント隔離環境の中で動きます。コマンドを実行するマシン上で、GuildBotics がコマンドの開始時に起動し、終了時に破棄する Linux の microVM です。コマンドを書くときは、次の規則を前提にしてください。

- ホストの環境はコマンドに届きません。環境変数も PATH も認証情報も引き継ぎません。コマンドに伝わるホストの事実は2つの環境変数だけで、`LANGUAGE`（OS の UI 言語。`ja_JP` のような gettext の形）と `TZ`（IANA 名のタイムゾーン）です。`functions/get_os_ui_language` は前者を読みます。
- 作業ディレクトリに直接書けるのは、そこが読み書き可の許可先（受け渡しフォルダなど）に含まれるときだけです。それ以外の作業ディレクトリでは、`.git` を読み取り専用にした写しでコマンドが動き、コマンドが成功して終わったときに、変わった通常ファイルだけが元の作業ディレクトリへ書き戻されます（シンボリックリンクと `.git` の中は書き戻しません。詳しくは[ネイティブエージェント実行基盤](native_agent_runtime.ja.md)）。
- 作業ディレクトリの外のファイルには、隔離環境に許可したディレクトリを通してだけ届きます。受け渡しフォルダ `~/Documents/GuildBotics` は常に許可され、コマンドが `read_only` を宣言していなければ読み書きできます（[3.1](#31-読み取り専用の宣言)）。
- ネットワークには、`intelligences/agent_environment.yml` のワークスペース共通の `network:` が許す範囲でだけ届き、省略時はすべて拒否します。ネットワークに接続するコマンド（Web API やフィードの取得など）は、実行する前に接続先を `network:` で許可しておく必要があります。
- Python コマンドは GuildBotics 自身の Python 環境（Python 3.12 と GuildBotics・その依存）で、シェルスクリプトは隔離環境のベースイメージにあるツールで動きます。

ディレクトリの許可と `network:` については [エージェント隔離環境のアクセス許可](native_agent_runtime.ja.md#エージェント隔離環境のアクセス許可) を参照してください。



## 2. 変数展開のバリエーション
プロンプトファイルでは、位置引数、名前付き引数、Jinja2 テンプレートエンジンを利用できます。
これらの方法を使うと、より柔軟にプロンプトを記述できます。

### 2.1. 名前付き引数の例
`${arg_name}` の形式で、`params` に指定したキーワード引数に対応します。

```markdown
以下のテキストを${source}から${target}に翻訳してください:
```

コマンド呼び出し例:

```shell
$ echo "Hello" | guildbotics run translate source=英語 target=日本語
```

Markdown・YAML コマンドでは、ルート階層の `args` で名前付き引数の必須性と実行時のデフォルト値を宣言できます。

```yaml
args:
  file:
    required: true
  language:
    default: 日本語
```

`default` も `required: false` もない引数は必須です。`default` を宣言すると任意引数になり、その値は CLI と Desktop のどちらから実行しても適用されます。`required: true` と `default` は同時に指定できません。`args` にないプレースホルダーは、引き続き必須引数として自動検出されます。

### 2.2. Jinja2 の例
Jinja2 テンプレートエンジンを使用することで、より複雑な変数展開が可能になります。例えば、`{{ variable_name }}` の形式で変数を参照できます。

```markdown
---
template_engine: jinja2
---
{% if target %}
以下のテキストを{{ target }}に翻訳してください:
{% else %}
以下のテキストを英訳してください:
{% endif %}
```

jinja2 を使う場合は、上記のようにYAMLフロントマターを追加し、`template_engine` を `jinja2` として設定します。


**メモ:**
YAMLフロントマターはMarkdownファイルの冒頭に記述する `---` で始まり `---` で終わるテキストです。
設定が不要な場合は省略できますが、テンプレートエンジンの指定やbrainの指定 (後述) を行うときなどに記述が必要になります。


コマンド呼び出し例:

```shell
$ echo "こんにちは" | guildbotics run translate
Hello

$ echo "こんにちは" | guildbotics run translate target=中国語
你好
```

### 2.3. context 変数の利用
Jinja2 テンプレートエンジンを使用する場合、`context` 変数を利用して、実行コンテキストにアクセスできます。例えば、現在のメンバー情報を取得したり、チーム情報を参照したりできます。

```markdown
---
brain: none
template_engine: jinja2
inputs:
  message: hidden
---

言語コード: {{ context.language_code }}
言語名: {{ context.language_name }}

ID: {{ context.person.person_id }}
名前: {{ context.person.name }}
話し方: {{ context.person.speaking_style }}

チームメンバー:
{% for member in context.team.members %}
- {{ member.person_id }}: {{ member.name }}
{% endfor %}
```

- `brain: none` を指定すると、LLM呼び出しが行われず、サブコマンドの出力のみが最終結果として返されます。

### 2.4. Desktop の入力欄設定

Markdown の YAML フロントマターまたは YAML コマンドのメタデータに `inputs` を指定すると、Desktop の手動実行画面に表示する入力欄を制御できます。Python コマンドでは、モジュールレベルの静的な `COMMAND_METADATA` マッピングに同じ設定を記述します。

```python
COMMAND_METADATA = {
    "inputs": {
        "message": "hidden",
    },
}
```

`COMMAND_METADATA` は、文字列をキーに持つ辞書リテラルでなければなりません。GuildBotics はカタログ構築時にコマンドを import せず Python AST で読み取るため、`COMMAND_METADATA = build_metadata()` のような動的な宣言は拒否されます。

| 項目 | 値 | デフォルト |
| --- | --- | --- |
| `defined_args` | `auto`, `hidden` | `auto` |
| `extra_args` | `hidden`, `optional` | `hidden` |
| `message` | `hidden`, `optional`, `required` | `optional` |

`defined_args: auto` は `args` で宣言した引数、`${...}` プレースホルダーから検出した引数、または Python の `main` シグネチャの引数を表示します。Desktop は必須の宣言済み・検出済み引数に `*` を付け、宣言されたデフォルト値を入力欄のプレースホルダーとして表示します。`extra_args: optional` は自由形式の「追加引数」欄を有効にします。`message: required` の場合、入力文が空の間は実行できません。

翻訳する文章や推敲するメールなど、コマンドが処理する主要な自由記述本文には `inputs.message` を使います。本文を必須にする場合は `inputs.message: required` を宣言すると、Desktop は「入力文」欄を表示し、その値をコマンドメッセージ / `Context.pipe` として渡します。`args` は翻訳先言語、ファイル、出力オプションなど、本文とは独立した値にだけ使います。

デフォルト値は省略します。例えば、呼び出し側の入力文を使用しないコマンドには次の指定だけが必要です。

```yaml
inputs:
  message: hidden
```

Desktop はバリデーションを通らない編集中のソースも保存しますが、コマンドとして有効になるまでは実行を無効にします。これにより、未完成のドラフトを失わずに編集を続けられます。

## 3. AI CLIツールの利用

YAML フロントマターで `brain: agent` を指定すると、OpenAI Codex や Antigravity CLI などといったAI CLIツールの呼び出しができます。AI CLIツールを用いると、割り当てられた GuildBotics member にファイルの読み込みやシステムコマンドの実行など、より高度な操作を指示できます。

例えば、`summarize.md` というファイルを作成し、次のように記述します。

```markdown
---
brain: agent
args:
  file:
    required: true
  language:
    default: 日本語
inputs:
  message: hidden
---
${file}の最初のセクションを読み、その内容を${language}を用いて、1行で要約してください
```

コマンド呼び出し例:

```shell
$ guildbotics run summarize file=README.md
GuildBoticsはAIエージェントとタスクボードで協働するアルファ版ツールであり、将来的な互換性崩壊や重大障害・損害の恐れがあるため利用者は隔離環境で自己責任の下検証すべきと警告している。
```

AI CLIツールはコマンドの作業ディレクトリで動きます。作業ディレクトリは、コマンドの実行方法で決まります。

- `guildbotics run`: `--cwd` で指定したディレクトリ。省略時は shell の作業ディレクトリ
- デスクトップアプリからの手動実行: 画面で指定したディレクトリ。指定が無ければ受け渡しフォルダ（`~/Documents/GuildBotics`）
- スケジューラが実行するコマンド（巡回コマンドと定期実行のコマンド）: 受け渡しフォルダ（[8. 巡回（routine）コマンドの宣言](#8-巡回routineコマンドの宣言)）
- サブコマンド: `commands:` のエントリの `cwd:`（[5. サブコマンドの利用](#5-サブコマンドの利用)）、または `context.invoke` の `cwd=`（[7.2. コマンドの呼び出し](#72-コマンドの呼び出し)）。相対パスは呼び出し元コマンドの作業ディレクトリを基準に解決し、どちらも無ければ呼び出し元コマンドの作業ディレクトリで動きます

### 3.1. 読み取り専用の宣言

何も変更しないコマンドは、メタデータで `read_only: true` を宣言できます（Python コマンドでは `COMMAND_METADATA` に `"read_only": True`）。宣言はコマンドのもので、そのコマンドの実行は、サブコマンドとAI CLIツールのターンも含めてすべて隔離環境の中で読み取り専用に閉じ込められます（ホストから bind するものはすべて読み取り専用になり、作業ディレクトリは隔離環境自身の空のディレクトリになり、ネットワークはプロバイダのAPIだけに届きます）。そのかわり、実行中の手動コマンドやそのメンバーの巡回実行と並んで動かせます。

`inspects` を宣言すると、ワークスペース自身の状態を読み取り専用で見せられます。`diagnostics` は記録済みの実行、`config` はワークスペースの設定と同梱テンプレートです。

```markdown
---
brain: agent
read_only: true
inspects: [diagnostics]
---
直近の失敗した実行の記録を読み、原因を1段落で説明してください。
```

決めるのは実行したコマンドの宣言だけで、サブコマンド自身の宣言は見ません。コマンド自身のシェルスクリプトや Python のコードも同じ隔離環境の中で動くので、宣言はそれらも閉じ込めます。



## 4. 組み込みコマンドの利用
GuildBotics内に存在する[組み込みコマンド](../guildbotics/templates/commands/)を利用することも可能です。

`ask` は、標準入力の依頼を `--cwd` で指定したディレクトリ上でメンバーに実行させます。たとえば alice に現在の作業ツリーのレビューを頼むには、依頼文を `mktemp -d` で作った自分専用のディレクトリ（Windows ではユーザーの一時ディレクトリの下に新しく作ったランダムな名前のディレクトリ）に UTF-8 ファイルとして保存し、macOS/Linux では次を実行します。

```shell
"$HOME/.guildbotics/bin/guildbotics" run ask --person alice --cwd "/path/to/repo" < "/path/to/temporary-request.txt"
```

Windows では `guildbotics` を使い、ファイルの UTF-8 テキストを標準入力へパイプで渡します。コマンドの終了後は、失敗時も含めてそのディレクトリごと削除してください。スキルで対話中なら、この呼び出しは対話中のメンバーが行います。

`ask` は `brain: agent` を使い、message は必須です。同じ作業ツリーの未コミット変更を読み、結果を標準出力のテキストで返します。レビュー依頼なら読み取りのみ、修正依頼なら依頼された編集と検証を行います。作業ツリーが読み書き可の許可先に含まれなければメンバーは写しで作業し、成功して終わったときに通常ファイルの変更だけが作業ツリーへ書き戻されます。commit と push は委任先では行わず、書き戻された変更を依頼元が確認して公開します。先に GuildBotics のワークスペースを選び（`--cwd` が選ぶのは作業ツリーであり、設定を読むワークスペースではありません）、このマシン上の委任先メンバーのエージェント隔離環境と AI CLI のログインを準備してください。host から起動し（コマンド自体は `--cwd` を作業ディレクトリとして隔離環境の中で動きます）、呼び出し元の timeout を長めにするかバックグラウンドで起動して、数分かかる結果を待ちます。隔離環境やログインが使えない場合は、既存 runtime の拒否理由が返ります。

依頼する前に、同じワークスペースを選んだ GuildBotics Desktop を開いてください。host の `guildbotics run` は利用可能な Desktop で実行されます。Desktop が閉じている場合や別のワークスペースを選んでいる場合は手元で実行します。Desktop へ送った後のエラーや通信切断では手元で再実行せず終了し、メンバーが実行中の場合はその拒否理由を返します。 Desktop 経由では完了時にまとめて結果が返り、途中のログは転送されません。

macOS では、**システム設定 → プライバシーとセキュリティ → ファイルとフォルダ**で、GuildBotics を起動しているアプリに書類フォルダへのアクセスを一度許可してください。開発中（`tauri dev`）は、起動に使ったターミナルや Visual Studio Code が対象です。環境の状態表示と turn の開始前にディレクトリへのアクセスを確認し、許可がなければ CLI と Desktop に同じ拒否理由を表示します。

コマンド呼び出し例:

```shell
$ guildbotics run functions/talk_as topic=システムでエラーが発生して解決方法調査中
author: Yuki Nakamura
author_type: Assistant
content: すみません、今システムの方でエラーが出てしまいまして…！現在、この解決策について、急ぎ調査を進めているところです。皆さんの業務に支障が出ないよう、責任を持って迅速に対応いたしますね！
```

```shell
$ echo "こんにちは！今日はいい天気ですね" | guildbotics run functions/identify_item item_type=会話タイプ candidates="質問 / 雑談 / 依頼"
confidence: 0.95
label: 雑談
reason: ユーザーは単に挨拶をしており、特定の質問や依頼をしていません。これは雑談の開始と判断されます。
```

```shell
$ echo "現在の時刻は`date`です" | guildbotics run functions/identify_item item_type=時間帯 candidates="早朝, 午前, 正午, 午後, 夕方, 夜, 深夜"
confidence: 1.0
label: 深夜
reason: 現在の時刻が23時36分であり、これは深夜の時間帯（通常23時から翌3時頃）に該当するためです。
```

### 4.1. 依存ライブラリの脆弱性アラートの確認

未解決のアラートを1ページ、または個別のアラートを取得します。

```shell
guildbotics run repository/security_alerts --person alice repo=org/repo
guildbotics run repository/security_alerts --person alice repo=org/repo alert=42
guildbotics run repository/security_alerts --person alice repo=org/repo state=resolved page_size=10 output=json
```

コマンドはワークスペースの `services.code_hosting_service` を使い、通常の microVM 内で読み取り専用として動作します。LLM は呼び出しません。他のコマンドと同様に実行環境の準備が必要です。現在の対応サービスは `github` で、未設定・未対応のサービスは明示的にエラーになります。GitHub では、メンバーの認証情報（GitHub App または fine-grained PAT）に **Dependabot alerts: Read-only** と対象リポジトリへのアクセスが必要です。既存の App はインストール先で追加権限を承認し、既存の PAT はトークンの権限を編集して、organization が要求する承認を済ませてください。手順は [GitHub アカウントの準備](../README.ja.md#ai-エージェント用の-github-アカウントを用意する)を参照してください。

`output=json` は `{ "repo": "org/repo", "alerts": [...], "continuation": null }` という JSON テキストを返し、空配列と `null` を保持します。各アラートは文字列の `id`、URL、状態、パッケージ、エコシステム、マニフェストパス、重要度、アドバイザリの `identifiers`（`{ "type": "CVE", "value": "..." }` の配列）、概要、説明、`affected_versions`、`patched_version`、作成・更新日時を持ちます。未提供の任意項目は `null`、識別子は空配列になります。Markdown では未提供の任意項目を省略し、修正版の報告がない場合だけ明示します。見出しには取得する状態と、各アラートの ID・パッケージ・重要度を表示します。一覧には概要を、個別表示には他の項目の後ろに引用ブロックで説明の全文を表示します。対象は依存ライブラリの脆弱性です。

`state` は `open`（既定）、`resolved`、`dismissed`、`page_size` は1～100（既定30）です。GitHub では resolved を fixed に変換し、dismissed に手動・自動の両方の却下を含めます。一覧にだけ条件を適用し、`alert=<id>` は個別取得です。ID とリポジトリ名はサービスが定義する文字列で、GitHub ではアラート番号と `owner/name` を使います。

1回の実行で1ページ返します。`continuation` があれば、同じリポジトリ・条件に `continuation=<返された値>` を加えて続けてください。Markdown にはメンバー・リポジトリ・状態・ページサイズを維持した次ページの実行コマンドを表示します。空ページは取得成功ですが、アクセス失敗はエラーです。サイズ上限を超えた場合は JSON を切り詰めず失敗します。一覧では `page_size` を下げてください。上限を超える個別アラートは返せません。GitHub の403・404には権限不足、未承認の追加権限、参照できないリソース、Dependabot alerts の無効化など複数の原因があります。レート制限では時間を置いて再実行してください。

後続の Python コマンドは `context.pipe`、または `await context.invoke("repository/security_alerts", repo="org/repo", output="json")` の返り値を JSON として読み取れます。型付きの結果を直接使うこともできます。

```python
async def main(context, repo):
    page = await context.get_code_hosting_service().read(
        "dependency_alerts", repo, parameters={"state": "open", "page_size": 10},
    )
    return [alert.package for alert in page.items]
```

member CLI は `guildbotics member repository read --person alice --resource dependency_alerts --repo org/repo --params '{"state":"open","page_size":10}'` です。共通形式の `items` と `continuation` を返します。個別取得は `--identifier 42` を指定し、条件と continuation は渡しません。

Issue・PR・CI は、LLM を使わない同梱コマンドで確認できます。

```shell
# Issue の本文・ラベル・担当者・コメント・Project・関連 PR
guildbotics run repository/issue_inspect --person alice repo=org/repo number=42
# PR の情報・レビューの判定と会話・コメント可能な差分座標
guildbotics run repository/pr_inspect --person alice repo=org/repo number=43 include_comments=true include_diff=true
# host が確認した完了可否と、失敗した Actions のログ末尾
guildbotics run repository/pr_checks --person alice repo=org/repo number=43 failed_logs=true log_tail_bytes=65536
```

結果は JSON です。実行環境の準備が必要です。実行中の workflow や委任コマンドの中では、`python -m guildbotics.runtime.command_entry repository/pr_inspect repo=org/repo number=43 include_comments=true` で呼び出します。同じ microVM・メンバー・メインコマンドのアクセス契約で動きます。Python コマンドからは `context.invoke("repository/pr_inspect", repo="org/repo", number="43")` でも呼べます。

各コマンドはレビューのスレッド内コメントも含めてページを取得します。ファイル差分には行ごとのコメント座標も含むため、1 ページ 5 ファイルずつ取得します。途中の取得失敗、同じ continuation の繰り返し、1 リソース 100 ページ超過、集約結果のサイズ超過はエラーとなり、部分取得を完全な結果として返しません。Project のフィールドは 1 項目 100 件までで、超過もエラーです。取得できないファイル差分には `patch_available=false` を付けます。差分の欠落や切り詰めは `patch_complete=false` とし、全体も `diff_complete=false` にします。取得ファイル数が PR の `changed_files` より少なければエラーにします。各ファイルのパスと patch 本文は 1 回だけ返し、`commentable_lines` には行番号と side を返します。大きな結果は `member repository read` でページごとに確認してください。

共通の取得リソースは `issues`、`pull_requests`、`issue_comments`、`issue_timeline`、`issue_projects`、`pull_request_reviews`、`pull_request_files`、`pull_request_threads`、`review_thread_comments`、`pull_request_readiness` です。いずれも `identifier` に Issue または PR の番号を指定します。一覧は `page_size`（1〜100、既定 30）を受け取り、`review_thread_comments` は取得済みスレッドの `node` も必要です。個別の Issue・PR は条件や continuation を受け取りません。`pull_request_readiness` は `failed_logs` と `log_tail_bytes`（1〜65536、既定 65536）を受け取り、push 後とタスク完了時にも使う host の判定結果を返します。`repository/pr_inspect` はその結果を `checks` に含め、`repository/pr_checks` は直接返します。タスク完了時には、コマンドの返した値を信用せず、host が GitHub を再確認します。そのために別の実行環境は起動しません。host が取得した作業対象は `inspected` として記録し、trace のタイトルに使いますが、Activity の書き込みには数えません。

readiness は host サービス自身の通信を使うため、内部の compare 応答には参照ページ用の受信上限を適用しません。返すページの上限は維持し、JSON のエスケープと付帯情報を除いた残りを失敗ログで分け合います。短くしたログは `truncated=true` と `tail_limit_bytes` で分かります。GitHub の権限エラーは必要な権限名を示し、HTTP が成功でも GraphQL の権限不足・レート制限はエラーとして案内します。

共通のサービス・結果型は `integrations/code_hosting_service.py` に置き、host の integration factory が設定から実装を選びます。コマンド側は既存の member grant 越しに同じインターフェースを使います。認証、許可した API 経路、応答の変換、ページ送りはサービス固有の実装が担当します。continuation は API の接続先と取得条件に結び付けられ、認可情報を持たず、読み取りのたびに検証されます。トークンは host に保持します。ページ送りのリンクからは一意な `after`（アラート）または `page`（REST の一覧）だけを取り出し、元の許可済み経路と条件で次の要求を組み立てます。GitHub が `/repositories/{id}/...` という URL を返しても、その URL はリクエストしません。リダイレクトも追いません。リソース追加時は共通契約・サービス側の対応・テストを追加します。任意の URL・HTTP メソッド・ヘッダー・GraphQL は受け付けず、アラートの状態変更もできません。既存の CI Dependabot digest は別の定期ワークフローです。

## 5. サブコマンドの利用
複数のサブコマンドを組み合わせて一連の処理を行うことができます。

例えば、`get-time-of-day.md` というファイルを作成し、次のように記述します。

```markdown
---
inputs:
  message: hidden
commands:
  - script: echo "現在の時刻は`date`です"
  - command: functions/identify_item item_type=時間帯 candidates="早朝, 午前, 正午, 午後, 夕方, 夜, 深夜"
  - prompt: 現在の時間帯にふさわしい挨拶をしてください
---
```

```shell
$ guildbotics run get-time-of-day
こんばんは。夜分にようこそ。何かお手伝いできることはありますか？
```

実行するコマンドを `commands` 配列に順番に指定します。各コマンドは前のコマンドの出力を受け取り、処理を続けます。

- `script` にはシェルスクリプトを直接記述できます。
- `command` は別のプロンプトファイルや組み込みコマンドを呼び出す方法です。
- `prompt` にはLLM呼び出しを行うプロンプトを記述できます。

上記のようにフロントマターの記述のみでMarkdown本文が必要ない場合は、以下のようにYAMLファイルとして保存しても問題ありません。

ファイル名例: `get-time-of-day.yml`

```yaml
commands:
  - script: echo "現在の時刻は`date`です"
  - command: functions/identify_item item_type=時間帯 candidates="早朝, 午前, 正午, 午後, 夕方, 夜, 深夜"
  - prompt: 現在の時間帯にふさわしい挨拶をしてください
```

`---` で囲まれたYAMLフロントマター部分のみを抜き出して `.yml` ファイルとして保存したものも、`.md` ファイルと同様にコマンドとして利用できます。


### 5.1. サブコマンドの名前付けと出力結果の参照

`commands` 配列内の各エントリには `name` 属性を指定することもできます。

```markdown
---
commands:
  - name: current_time
    script: echo "現在の時刻は`date`です"
  - name: time_of_day
    command: functions/identify_item item_type=時間帯 candidates="朝, 昼, 夜"
---
```

`name` を指定すると、そのコマンドの出力結果に対して指定した名前でアクセス可能になります。
`name` を省略した inline コマンドの出力名は `<ファイル名>__N` です。`<ファイル名>` はファイルのパスからディレクトリ・言語接尾辞・拡張子を除いた名前です。`N` はそのコマンド自身の `commands:` の全エントリ（名前付きも含む）での位置を 1 から数えます。親コマンドや別名から呼び出しても出力名は変わりません。
同じファイル名のコマンドは `Context.shared_state` の出力キーも共有します。両方の結果を保持する場合は、各 inline エントリに異なる `name` を指定してください。


```markdown
---
commands:
  - name: current_time
    script: echo "現在の時刻は`date +%T`です"
  - name: time_of_day
    command: functions/identify_item item_type=時間帯 candidates="朝, 昼, 夜"
brain: none
template_engine: jinja2
---
{% if time_of_day.label == "朝" %}
おはようございます。
{% elif time_of_day.label == "夜" %}
こんばんは。
{% else %}
こんにちは。
{% endif %}

{{ current_time }}
```

上記のコマンドを実行すると、以下のような結果を返します。

```text
こんばんは。

現在の時刻は20:17:15です
```

- `brain: none` を指定すると、LLM呼び出しが行われず、サブコマンドの出力のみが最終結果として返されます。
- `template_engine: jinja2` を指定すると、Jinja2 テンプレートエンジンが有効になります。コマンドの出力結果にアクセスする際には Jinja2 テンプレートを利用することをおすすめします。

### 5.2. スキーマ定義

LLM呼び出しを行う `prompt` コマンドに対しては、schemaで応答のスキーマを定義し、response_classで応答クラスを指定することができます。これにより、LLMの応答を構造化されたデータとして扱うことが可能になります。

```markdown
---
schema: |
    class Ranking:
        package: str
        detail: str
        line_rate: float
        reason: str

    class Rankings:
        items: list[Ranking]

    class Task:
        title: str
        description: str
        priority: int

    class TaskList:
        tasks: list[Task]
commands:
  - script: |
      pytest tests/ --cov=guildbotics --cov-report=xml >/dev/null 2>&1
      cat coverage.xml |grep line-rate
  - prompt: |
      この情報を解析して、テスト実装の対応優先度が高いパッケージのトップ3についてRankings形式のJSONとして出力してください。
    response_class: Rankings
  - name: task_list
    prompt: |
      この分析情報に基づいて、優先度が高い順に、TaskList形式のJSONで、すぐに着手可能なテスト実装タスク定義を最大5つまで提案してください。
    response_class: TaskList
template_engine: jinja2
brain: none
---
{% for task in task_list.tasks %}
- [ ] {{ task.title }} (priority: {{ task.priority }})
{% endfor %}
```

呼び出し例:

```shell
$ guildbotics run coverage
- [ ] utils/fileio.py の単体テストを追加 (priority: 1)
- [ ] utils/git_tool.py の動作とエラー処理のテストを追加 (priority: 2)
- [ ] drivers/command_runner.py と drivers/task_scheduler.py の統合的単体テストを追加 (priority: 3)
- [ ] utils/import_utils.py のインポート処理とエッジケースのテストを追加 (priority: 4)
- [ ] intelligences/functions.py のビジネスロジックと外部呼び出しのモックテストを追加 (priority: 5)
```

### 5.3. print コマンド

`print` は、LLM を呼び出さずにテキストを生成・整形するためのコマンドです。`commands` 配列の `print` キーの値として、その場に直接記述します。

```yaml
commands:
  - print: こんにちは。
```

呼び出し例:

```shell
$ guildbotics run greet
こんにちは。
```

print コマンドでは Jinja2 テンプレートエンジンが有効になっているため、変数展開や条件分岐も利用可能です。

```yaml
commands:
  - name: current_time
    script: echo "現在の時刻は`date +%T`です"
  - name: time_of_day
    command: functions/identify_item item_type=時間帯 candidates="朝, 昼, 夜"
  - print: |
      {% if time_of_day.label == "朝" %}
      おはようございます。
      {% elif time_of_day.label == "夜" %}
      こんばんは。
      {% else %}
      こんにちは。
      {% endif %}

      {{ current_time }}
```

上記のコマンドを実行すると、以下のような結果を返します。

```text
こんばんは。

現在の時刻は20:17:15です
```

### 5.4. to_html コマンド

`to_html` は Markdown テキストを HTML に変換するためのコマンドです。

以下の定義例では、直前のコマンド出力 (`cat README.ja.md`) を HTML に変換し、`tmp/summary.html` に保存します。

```yaml
commands:
  - script: cat README.ja.md
  - to_html: tmp/summary.html
```

以下のように明示的にパラメータを指定することも可能です。

```yaml
commands:
  - to_html:
      input: reports/summary.md
      css: assets/summary.css
      output: tmp/summary.html
```

- `input` パラメータに指定されたパスのファイルを読み込んで変換対象とします。未指定の場合は直前のコマンド出力を変換します。
- `output` で変換後の HTML を保存するパスを指定できます。
- `css` で任意の CSS ファイルを指定できます。

### 5.5. to_pdf コマンド

`to_pdf` は Markdown または HTML を PDF に変換するためのコマンドです。

```yaml
commands:
  - to_pdf:
      input: reports/summary.md
      css: assets/summary-print.css
      output: tmp/summary.pdf
```

- `input` パラメータに指定されたパスのファイルを読み込んで変換対象とします。未指定の場合は直前のコマンド出力を変換します。
- `output` で変換後の PDF を保存するパスを指定できます。
- `css` で任意の CSS ファイルを指定できます。


## 6. シェルスクリプトの利用
シェルスクリプトは、上記のように script キーを使って直接記述する方法の他に、外部のシェルスクリプトファイルとして記述してコマンドとして呼び出すことが可能です。

シェルスクリプトは、Windows を含めホストの OS によらず、隔離環境の Linux の上で動きます。実行権限があり shebang（`#!`）で始まるスクリプトはそれ自体を実行し（shebang に従います）、それ以外のスクリプトは `bash` で実行します（Windows のホストからマウントしたファイルは常に実行権限を持ちます）。

例えば、`current-time.sh` というファイルを作成し、次のように記述します。

```bash
#!/usr/bin/env bash

echo "現在の時刻は`date +%T`です"
```

このファイルに実行権限を与えた上で、プロンプトファイル内では `script` キーの代わりに `command` キーを使って呼び出します。

```markdown
---
commands:
  - name: current_time
    command: current-time
  - name: time_of_day
    command: functions/identify_item item_type=時間帯 candidates="朝, 昼, 夜"
brain: none
template_engine: jinja2
---
{% if time_of_day.label == "朝" %}
おはようございます。
{% elif time_of_day.label == "夜" %}
こんばんは。
{% else %}
こんにちは。
{% endif %}

{{ current_time }}
```

コマンド呼び出し時の引数は、以下のように扱えます。

```bash
#!/usr/bin/env bash

echo "arg1: ${1}"
echo "arg2: ${2}"
echo "key1: ${key1}"
echo "key2: ${key2}"
```

呼び出し例:

```shell
$ guildbotics run echo-args a b key1=c key2=d
arg1: a
arg2: b
key1: c
key2: d
```


## 7. Python コマンドの利用
Python ファイルを使うと、API 呼び出しや複雑なロジックを組み込めます。

例えば、以下のような内容で `hello.py` というファイルを作成します。

```python
def main():
    return "Hello, world!"
```

- `main` 関数をエントリポイントとして定義します。

呼び出しは md ファイルの場合と同様に、以下のように行います。

```shell
$ guildbotics run hello
Hello, world!
```

### 7.1. 引数の利用

Python コマンドでは、以下の3種類の引数を利用することができます。

- context: `main` 関数の最初の引数として `context` / `ctx` / `c` のいずれかを指定すると、実行コンテキストにアクセスできます。以下のような用途で利用できます:
  - team や person の情報取得。
  - 別コマンドの呼び出し。
  - メンバーとしてのチャットへの投稿やリアクション（`context.get_chat_service()`。メンバーのチャットコマンドを実行します。チケット管理サービスは隔離環境の中では使えません）。
- 位置引数: `main` 関数の位置引数として定義します。
- キーワード引数: `main` 関数のキーワード引数として定義します。


```python
from guildbotics.runtime.context import Context

def main(context: Context, arg1, arg2, key1=None, key2=None):
    print(f"arg1: {arg1}")
    print(f"arg2: {arg2}")
    print(f"key1: {key1}")
    print(f"key2: {key2}")
```

呼び出し例:

```shell
$ guildbotics run hello a b key1=c key2=d
arg1: a
arg2: b
key1: c
key2: d
```


```python
from guildbotics.runtime.context import Context

def main(context: Context, *args, **kwargs):
    for i, arg in enumerate(args):
        print(f"arg[{i}]: {arg}")

    for k, v in kwargs.items():
        print(f"kwarg[{k}]: {v}")
```

呼び出し例:

```shell
$ guildbotics run hello a b key1=c key2=d
arg[0]: a
arg[1]: b
kwarg[key1]: c
kwarg[key2]: d
```

`main()` の必須引数が不足している場合、GuildBotics は関数を呼び出す前に引数名を含む理由を返します。利用者が作成した Python コマンドにも適用されます。例えば、`repository/security_alerts` が `repo` の不足を伝えたら、`repo=GuildBotics/GuildBotics` を渡します。Desktop には理由が、CLI には `Error: <理由>` が表示されます。関数本体の中で発生した `TypeError` は実装上のエラーとして扱います。

### 7.2. コマンドの呼び出し

context.invoke を利用すると、Python コマンドから別のコマンドを呼び出せます。

```python
from datetime import datetime
from guildbotics.runtime.context import Context


async def main(context: Context):
    current_time = f"現在の時刻は{datetime.now().strftime('%H:%M')}です"

    time_of_day = await context.invoke(
        "functions/identify_item",
        message=current_time,
        item_type="時間帯",
        candidates="朝, 昼, 夜",
    )

    message = ""
    if time_of_day.label == "朝":
        message = "おはようございます。"
    elif time_of_day.label == "夜":
        message = "こんばんは。"
    else:
        message = "こんにちは。"

    return f"{message}\n{current_time}"
```

- invoke は非同期関数なので、`await` を付けて呼び出します。そのため、`main` 関数も `async def` として定義する必要があります。

### 7.3. 失敗を利用者に伝える

チャットの操作が想定内の失敗を報告したときは、利用者が対処できるように理由をそのまま伝えます。

```python
from guildbotics.commands.errors import CommandError
from guildbotics.integrations.chat_service import ChatServiceError


async def main(context, channel_id):
    chat = context.get_chat_service()
    try:
        await chat.post_message(channel_id, "Report is ready.")
    except ChatServiceError as exc:
        raise CommandError(str(exc)) from exc
```

`context.get_chat_service()` で得たサービスの `post_message` などの呼び出しが `ChatServiceError` を投げます。これを `CommandError` に変換すると、Desktop には理由が、CLI には `Error: <理由>` が表示されます。ほかの想定内の失敗でも、利用者に見せてよい本文で `CommandError` を投げてください。

API キーを使う推論の失敗は、例外名と取得できた「報告されたステータス」を表示し、プロバイダのエラー本文は表示しません。ステータスは HTTP 応答が無くても SDK の既定値の場合があります（例えば Agno の既定値は502です）。窓口の時間切れや結果サイズ超過は、それぞれの理由をそのまま表示します。同梱の `examples/reports/tools/fetch_ai_news` は通信に失敗すると、`intelligences/agent_environment.yml` で `news.google.com` への通信許可を確認するよう案内します。

## 8. 巡回（routine）コマンドの宣言

コマンドは、自身をメンバーの巡回（routine）実行の候補として宣言できます。巡回候補はメンバーの巡回設定で選択肢として表示され、選択されたものをスケジューラが定期的に実行します。

宣言はコマンド自身のメタデータで行います。これにより、巡回候補を追加する際に edition 側のリストを編集する必要がなくなります。

- Markdown / YAML コマンド: YAML フロントマターに `routine: true` を追加する。
- Python コマンド: module-levelの`COMMAND_METADATA` mappingに`"routine": True`を追加する。

```markdown
---
description: 未対応チケットを定期的に確認する。
routine: true
---
...
```

```python
COMMAND_METADATA = {
    "name": "チケット確認",
    "description": "未対応チケットを定期的に確認します。",
    "routine": True,
}


async def main(context) -> None:
    ...
```

スケジューラは巡回コマンドを呼び出し側からの入力なしで実行するため、巡回候補は呼び出し側の引数や入力文を要求しない必要があります。`routine: true` を宣言したコマンドは、`inputs.defined_args: auto` によって呼び出し側へ必須引数を表示する場合、または `inputs.message: required` の場合、一覧に残ったまま理由付きで「実行不可」と表示されます。`inputs.defined_args: hidden` の場合、プレースホルダはワークフロー内部から供給されるため、巡回実行の可否には影響しません。

巡回コマンドは、サービスをデスクトップアプリから開始しても `guildbotics start` で開始しても、受け渡しフォルダ（`~/Documents/GuildBotics`）で動きます。相対パスで書き出したファイルはここに出ます。


## 9. モデルエフォート（effort）の指定

「モデルにどれだけ考えさせるか」を、プロバイダ中立の 3 つのラベル `low` / `default` / `high` で指定できます。ラベルから実際のプロバイダ設定への翻訳は設定 YAML とアダプタが担当するため、コマンド側はラベルだけを扱います。

### 9.1. 指定方法と解決順位

フロントマターで既定値を宣言します。

```markdown
---
brain: agent
effort: high
---
リポジトリ全体を調査し、修正方針をまとめてください。
```

実行時に上書きする場合は、通常の `key=value` パラメータとして渡します（専用の CLI オプションはありません）。

```shell
guildbotics run summarize file=README.md effort=high
```

解決順位はすべての brain（LLM API 経路・AI CLIツール経路のいずれも）で共通です。

1. 実行時指定（`effort=<level>`、およびチャットワークフローの自動判定結果）
2. フロントマターの `effort:`
3. 未指定

**実行時の `effort=default` は、フロントマターの `effort: high` を明示的に打ち消します。**「指定なし」と「`default` 指定」は別物である点に注意してください。

### 9.2. `default` と未指定の意味

`default` と未指定はどちらも「介入しない」を意味します。LLM API 経路では毎回モデルを生成し直すため、これはモデル既定値での実行と同じです。

一方、ネイティブAI CLIツールの経路では**セッションが継続する場合があります**。model や effort を変更しても provider のセッションは維持され、Claude と Grok は次の turn で新しい設定を渡します。再開時に設定を消した場合、Claude は最後に記録した値を送り直し、Grok は provider のセッションに保存された値を維持します。設定を消すだけでは provider の既定値に戻りません。会話が新しいセッションに切り替わると、設定で指定した値がない限り、新しいセッションは provider の既定値で始まります。

### 9.3. モデル定義 YAML の schema

`intelligences/models/<provider>/*.yml` に任意の `effort:` ブロックを書けます。各レベルの値は `parameters` へ浅くマージされます。

```yaml
model_class: agno.models.openai.OpenAIChat
parameters:
  id: gpt-5-mini
effort:
  low:
    reasoning_effort: low
  high:
    reasoning_effort: high
```

- キーは `low` / `high` のみ。値は必ずマッピング（オブジェクト）
- **`default:` は書けません**（エラーになります）。`default` は「介入しない」という意味なので、マッピングを書いても決して適用されないためです。常に効かせたい設定は `parameters:` に直接書いてください
- `parameters` への浅いマージなので、`id` を差し替えればレベルごとに別モデルを使えます。`parameters:` 自体は常に適用される設定で、AI CLIツール定義も同じ構造を持ちます
- パラメータ名と型はプロバイダごとに異なります。OpenAI は `reasoning_effort`（文字列）、Anthropic は `thinking: {type, budget_tokens}`（ネスト）、Gemini は `thinking_budget`（整数）です

スロットは `models/<provider>/<スロット名>.yml` に置かれますが、パッケージ同梱テンプレートは `default.yml` だけです。**スロットのファイルに `effort:` キーが無い場合、そのプロバイダの `default.yml` の `effort:` を継承します**。明示的に「マッピング無し」にしたい場合は `effort: {}` と書いてください（キーが無い＝継承、空マッピング＝無介入）。

#### `effort_fields:`（任意）

同じファイルに `effort_fields:` を書くと、そのプロバイダが受け付ける設定を宣言できます。デスクトップの設定画面はこの宣言だけを見て型付きの入力欄を生成し、保存時に未知のキーや型違いを拒否します。宣言が無いプロバイダは JSON 直接編集にフォールバックし、検証も行いません。

```yaml
effort_fields:
  - key: thinking.type          # ドット記法でネストしたキーを指す
    type: enum
    values: [enabled, disabled]
  - key: thinking.budget_tokens
    type: integer
    minimum: 1024
  - key: id
    type: model_id
```

`type` は `enum` / `integer` / `boolean` / `string` / `model_id` のいずれかです。この宣言はプロバイダ側の知識であり、画面はキーの意味を一切知りません。

### 9.4. AI CLIツール設定 YAML の schema

AI CLIツールの定義はモデル定義と同じ2階層です。

```
cli_agents/<tool>/default.yml     ツール既定（全スロットがここから継承する）
cli_agents/<tool>/<スロット名>.yml  スロット専用の定義
```

`cli_agent_mapping.yml` はスロットからこのパスを指します（`models/<provider>/<スロット名>.yml` を指すのと同じ形）。スロット専用の定義に書かなかったキーはツール既定から継承されるため、同じツールを複数スロットで別々のモデル・エフォートで使い分けられます。

どちらのファイルにも `parameters:` と `effort:` を書けます。モデル定義と同じ関係で、`parameters:` は**常に適用される設定**、`effort.<level>` はその上に重なるレベル別の上書きです。

```yaml
parameters:        # エフォートに関係なく常に効く
  model: <モデル>
effort:            # low / high のときだけ上書き
  high:
    model: <強いモデル>
```

`default` や未指定のときはエフォート層が適用されないため、モデルを常に固定したい場合は `parameters:` に書きます。設定として書くのはこの 2 つのキーだけで、ツール自体はビルトインのアダプタが動かします。同梱の既定ファイルはこのほかに `effort_fields:`（前節の型付き編集用の宣言）を持ちますが、これはツールに同梱されるプロバイダ知識であり、利用者が設定する項目ではありません。

同梱ツールはすべて既定のマッピングと `effort_fields:` を持ち、設定なしで `low` / `high` が機能します。codex は `turn/start` の model / effort、Claude Code は `--model` / `--effort`、Grok と Copilot は ACP のセッション設定項目 model / reasoning effort、`agy --print` はコマンドラインの `--model` または `--effort` に翻訳します（この2つは併用できないため、両方を設定したスロットではモデルを採用します）。新しい AI CLI ツールへ対応するには、本リポジトリにネイティブアダプタを実装します。`effort_fields:` もそこで宣言するもので、ワークスペースに YAML を置いてツールを追加する経路はありません。

```yaml
# intelligences/cli_agents/codex/default.yml
effort:
  low:
    effort: low
  high:
    model: <強いモデルの ID>
    effort: high
```

ブロック内のキーはプロバイダ固有です。コアが解釈するのは共通キー `model` のみです。アダプタは自分が扱えるキーの allowlist を持ち、未知のキーは警告ログに出して無視します（黙って捨てません）。

- codex: `model` / `effort` を `turn/start` で毎ターン送信。`model/list` の `supportedReasoningEfforts` で検証し、非対応値は警告して落とします
- claude: `model` / `effort` を `--model` / `--effort` に翻訳し、`--resume` と一緒に渡します。再開時にどちらかを設定しなかった場合は、その値を会話の記録から送り直します。`low` / `medium` / `high` / `xhigh` / `max` 以外の effort は警告のうえ落とします
- grok: セッションの作成・再開後に明示的に指定した `model` / `reasoning_effort` を ACP の `session/set_config_option` で設定し、Grok が確認した値を検査してからプロンプトを送ります。指定しなかった値は Grok がセッションに保存した値を維持します。この2つ以外のキーは警告のうえ無視されます

### 9.5. mapping が無いレベルを指定した場合

`high` を指定したのにモデル定義やツール設定に `high` の mapping が無い場合、**エラーにはならず、警告ログを出して無介入のまま実行を続けます**。プロバイダごとに mapping の整備状況は異なるため、実行を止める方が害が大きいという判断です。

このとき、プロバイダ中立のラベルがそのままプロバイダへ渡ることはありません。Codex のようにラベルと同じ語彙（`low` / `high`）を持つツールでも同じで、値の供給元は常に mapping だけです。ラベルをフォールバックに使うと、diagnostics が `unsupported` と記録した実行に限って介入が起きることになり、記録と実挙動が食い違います。

### 9.6. ワークフローの既定動作

- **チケット駆動ワークフロー**: 自動判定はしません。`functions/handle_github_ticket` のフロントマターが `effort: high` を宣言しており、チケット対応は基本的に重い処理であるという前提が標準状態で効きます
- **チャットワークフロー**: 判断エンジンが未処理バッチごとに、起動要否・リアクションと、対応に必要な `default`／`high` を一括判定します。ローカルファイル作業・リポジトリ横断調査・規約確認が必要な判断のいずれかが明確に必要なら `high`、すべて不要なら `default` です。作業要否が不明、または判定・記録に失敗した場合は effort を変更しません。

判定結果はチャットの選別がスレッドに保存し、一度 `high` になったら下げません。エージェント起動時に、ワークフローがこの値を `functions/handle_chat_event` の実行時 effort として渡します。モデル・推論パラメーターへの変換には会話処理側のスロットの既存マッピングを使います。保存値も判定結果もない場合は実行時 effort を追加せず、通常の設定解決に従います。リアクション・行動なしの経路では effort を変更しません。

判断エンジン自身のモデル・推論量は判断用スロットの設定に従い、判定結果の effort を判断自身に適用しません。チャット判断の設定と動作は [Slack 連携ガイド](slack_integration.ja.md#チャット判断エンジン)を参照してください。

### 9.7. diagnostics での確認

エフォートの決定は trace / diagnostics の詳細に記録されます（activity history には出ません。エフォートは診断情報であり、activity history の関心事はドメイン上の成果です）。

記録されるのは安全な allowlist に限られます。

- `requested`: 指定された値そのまま
- `resolved`: 実際に採用したレベル
- `model`: 実効モデル ID
- `applied_keys`: エフォートレベル自身が適用したパラメータの**キー名のみ**（値は記録しません。ツールの常時適用設定も含みません）
- `unsupported`: 明示指定に対して mapping が無かったか

実効パラメータの生値は記録しません。`api_key` や headers、client 設定などが混入し得るためです。

## 10. チャットの新着を確認してから公開する

チャットワークフローでは、返信・リアクション・Git push・GitHubへの書き込みの直前に、元のスレッドの新着を確認します。

```bash
guildbotics member chat updates --person alice
```

このコマンドはSlack APIを呼ばず、永続化されたイベントキューを読みます。`new_messages` は、この実行の入力または前回の確認以降に届いたメッセージを返します。全件を読んで作業や返信内容を再検討し、確定操作の直前に再確認してください。`up_to_date` なら操作を進められます。`catching_up` はWorkspace同期に伴う受信メッセージの保存待ちです。数秒待って再確認し、この状態だけを理由に公開や `blocked` 完了を行わないでください。`unavailable` は受信状態を確認できないことを意味します。公開操作を見送り、そのturn内に復旧しなければ `blocked` で完了します。

書き込みコマンド側でも、確認漏れや確認後の新着があれば操作を拒否します。GitHubや別チャンネルへの書き込みでも、確認対象は依頼元のチャットスレッドです。確認したメッセージのIDを実行に記録します。追加の新着は、その配信後に返信・投稿・リアクション・GitHubへの書き込み・Gitの公開操作が記録された場合だけ done/asking 完了時に処理済みにします。blockedやno-opだけで終わった実行の新着は、次の実行に向けてキューに残します。最終確認の後に届くメッセージまで防ぐものではありません。
