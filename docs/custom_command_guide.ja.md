# カスタムコマンド開発ガイド

GuildBotics のカスタムコマンドは、エージェントに任意の処理手順を教えるための仕組みです。Markdown ファイルに記述したプロンプトでLLM呼び出しを行ったり、シェルスクリプトで外部ツールを操作したり、Python ファイルで本格的なワークフローを構築したりできます。

- [カスタムコマンド開発ガイド](#カスタムコマンド開発ガイド)
  - [1. クイックスタート](#1-クイックスタート)
    - [1.1. プロンプトファイルを作成する](#11-プロンプトファイルを作成する)
    - [1.2. コマンドを呼び出す](#12-コマンドを呼び出す)
    - [1.3. メンバーの指定](#13-メンバーの指定)
  - [2. 変数展開のバリエーション](#2-変数展開のバリエーション)
    - [2.1. 名前付き引数の例](#21-名前付き引数の例)
    - [2.2. Jinja2 の例](#22-jinja2-の例)
    - [2.3. context 変数の利用](#23-context-変数の利用)
    - [2.4. Desktop の入力欄設定](#24-desktop-の入力欄設定)
  - [3. AI CLIツールの利用](#3-ai-cliツールの利用)
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

- 組み込みの汎用Pythonコマンド `functions/get_os_ui_language` がOSのUI言語を取得し、入力文を構造化データとして保持します。
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
$ guildbotics run summarize file=README.md cwd=.
GuildBoticsはAIエージェントとタスクボードで協働するアルファ版ツールであり、将来的な互換性崩壊や重大障害・損害の恐れがあるため利用者は隔離環境で自己責任の下検証すべきと警告している。
```

AI CLIツールでは、`cwd` パラメータでAI CLIツールがシステムコマンドを実行する際の作業ディレクトリを指定する必要があります。



## 4. 組み込みコマンドの利用
GuildBotics内に存在する[組み込みコマンド](../guildbotics/templates/commands/functions/)を利用することも可能です。

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
  - チケット管理サービスやコードホスティングサービスへのアクセス。
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
guildbotics run summarize file=README.md cwd=. effort=high
```

解決順位はすべての brain（LLM API 経路・AI CLIツール経路のいずれも）で共通です。

1. 実行時指定（`effort=<level>`、およびチャットワークフローの自動判定結果）
2. フロントマターの `effort:`
3. 未指定

**実行時の `effort=default` は、フロントマターの `effort: high` を明示的に打ち消します。**「指定なし」と「`default` 指定」は別物である点に注意してください。

### 9.2. `default` と未指定の意味

`default` と未指定はどちらも「介入しない」を意味します。LLM API 経路では毎回モデルを生成し直すため、これはモデル既定値での実行と同じです。

一方、ネイティブAI CLIツールの経路では**セッションが継続します**。継続中のセッションに対する「介入しない」は「**そのセッションの現在の設定を維持する**」を意味し、モデル既定値へ戻ることは保証されません。既定値に戻るのは、会話がローテーションして新しいセッションが始まったあとです。

セッションのローテーションは、実効設定のフィンガープリント（解決したレベル + モデル + プロバイダ固有設定）で判定します。空（＝何も指定していない）と非空の間の遷移は「維持」であり、ローテーションしません。**非空同士が異なるときだけ**、`settings_changed` としてローテーションします。設定を毎ターン送り直せるアダプタ（codex）は、そもそもローテーションしません。

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

同梱ツールはすべて既定のマッピングと `effort_fields:` を持ち、設定なしで `low` / `high` が機能します。codex は `turn/start` の model / effort、Claude Code は model と思考予算、`grok agent stdio` は起動オプションの model / reasoning effort、`copilot --acp` はセッション設定項目 `model` / `reasoning_effort`、`agy --print` はコマンドラインの `--model` または `--effort` に翻訳します（この2つは併用できないため、両方を設定したスロットではモデルを採用します）。新しい AI CLI ツールへ対応するには、本リポジトリにネイティブアダプタを実装します。`effort_fields:` もそこで宣言するもので、ワークスペースに YAML を置いてツールを追加する経路はありません。

```yaml
# intelligences/cli_agents/codex/default.yml
effort:
  low:
    effort: low
  high:
    model: <強いモデルの ID>
    effort: high
```

ブロック内のキーはプロバイダ固有です。コアが解釈するのは共通キー `model` のみで、フィンガープリント計算に使われます。アダプタは自分が扱えるキーの allowlist を持ち、未知のキーは警告ログに出して無視します（黙って捨てません）。

- codex: `model` / `effort` を `turn/start` で毎ターン送信。`model/list` の `supportedReasoningEfforts` で検証し、非対応値は警告して落とします
- claude: `model` / `effort` を起動フラグ `--model` / `--effort` に翻訳します。セッション開始時に固定されるため、変更時は新しいセッションを開始します。`low` / `medium` / `high` / `xhigh` / `max` 以外の effort は警告のうえ落とします
- grok: `model` / `reasoning_effort` を `grok agent stdio` の起動オプションとして渡します。プロセス起動時に固定されるため、変更時は新しいセッションを開始します。この2つ以外のキーは警告のうえ無視されます

### 9.5. mapping が無いレベルを指定した場合

`high` を指定したのにモデル定義やツール設定に `high` の mapping が無い場合、**エラーにはならず、警告ログを出して無介入のまま実行を続けます**。プロバイダごとに mapping の整備状況は異なるため、実行を止める方が害が大きいという判断です。

このとき、プロバイダ中立のラベルがそのままプロバイダへ渡ることはありません。Codex のようにラベルと同じ語彙（`low` / `high`）を持つツールでも同じで、値の供給元は常に mapping だけです。ラベルをフォールバックに使うと、diagnostics が `unsupported` と記録した実行に限って介入が起きることになり、記録と実挙動が食い違います。

### 9.6. ワークフローの既定動作

- **チケット駆動ワークフロー**: 自動判定はしません。`functions/handle_github_ticket` のフロントマターが `effort: high` を宣言しており、チケット対応は基本的に重い処理であるという前提が標準状態で効きます
- **チャットワークフロー**: 受信イベントごとに 1 回、LLM（`functions/assess_effort`）が `default` / `high` の二値で判定します。ローカルファイルを扱う作業依頼のほか、リポジトリに対する issue 起票や設計・実装方針の判断を伴う依頼も `high`、通常の会話応答は `default` です。判定基準を定義していないため、`low` は自動判定の出力に含まれません（明示指定用の語彙としては残ります）

チャットの判定は**昇格のみ**です。スレッドの保存値より低い判定は採用されず、すでに `high` のスレッドでは判定呼び出し自体を省略します。この状態は person_id ごとに保存されるため、「Slack スレッド全体で 1 つ」ではなく、そのメンバーから見たスレッドの状態です。

判定は `brain: default`（LLM API 経路）で動き、判定コマンド自身が `effort: low` を宣言しています。ただし「`default` スロット＝安価」は保証されません（高価なモデルを `default` に設定することもできます）。

**AI CLIツールのみの構成（LLM API キーなし）では、この自動判定は動作しません。** LLM モデルが未構成の場合は判定呼び出しをスキップし、警告を 1 回出して保存値のまま続行します。この構成でエフォートを上げたい場合は、フロントマターか実行時パラメータで明示してください。

### 9.7. diagnostics での確認

エフォートの決定は trace / diagnostics の詳細に記録されます（activity history には出ません。エフォートは診断情報であり、activity history の関心事はドメイン上の成果です）。

記録されるのは安全な allowlist に限られます。

- `requested`: 指定された値そのまま
- `resolved`: 実際に採用したレベル
- `model`: 実効モデル ID
- `applied_keys`: エフォートレベル自身が適用したパラメータの**キー名のみ**（値は記録しません。ツールの常時適用設定も含みません）
- `unsupported`: 明示指定に対して mapping が無かったか

実効パラメータの生値は記録しません。`api_key` や headers、client 設定などが混入し得るためです。
