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
  - [3. CLIエージェントの利用](#3-cliエージェントの利用)
  - [4. 組み込みコマンドの利用](#4-組み込みコマンドの利用)
  - [5. サブコマンドの利用](#5-サブコマンドの利用)
    - [5.1. サブコマンドの名前付けと出力結果の参照](#51-サブコマンドの名前付けと出力結果の参照)
    - [5.2. スキーマ定義](#52-スキーマ定義)
  - [6. シェルスクリプトの利用](#6-シェルスクリプトの利用)
  - [7. Python コマンドの利用](#7-python-コマンドの利用)
    - [7.1. 引数の利用](#71-引数の利用)
    - [7.2. コマンドの呼び出し](#72-コマンドの呼び出し)


## 1. クイックスタート

### 1.1. プロンプトファイルを作成する
まずは、LLM に翻訳を依頼するシンプルなコマンドを作ってみましょう。

プロンプト格納用設定フォルダ（デフォルト: `~/.guildbotics/config/prompts`）に以下のような内容でプロンプトファイル `translate.md` を作成します。

```markdown
以下のテキストが${1}であれば${2}に、${2}であれば${1}に翻訳してください:
```

ポイント:

- `${1}` や `${2}` は位置引数を表します。コマンド呼び出し時に値が渡されます。


### 1.2. コマンドを呼び出す

`echo "こんにちは" | guildbotics run translate 英語 日本語` のように実行すると、次のような出力が得られます。

```
Hello
```

**メモ:**
このコマンドを実行すると、LLMの呼び出し前に以下のような形にプロンプトファイルの内容が展開されます。

```
以下のテキストが英語であれば日本語に、日本語であれば英語に翻訳してください:

こんにちは
```

これにより、LLMは応答として "Hello" を返します。

### 1.3. メンバーの指定

`guildbotics add` コマンドにより、複数のメンバーを登録している場合、コマンド実行時に `<コマンド>@<person_id>` の形式でメンバーを指定する必要があります。 

例: `guildbotics run translate@yuki 英語 日本語`



## 2. 変数展開のバリエーション
プロンプトファイルの変数展開方法としては、上記で説明した位置引数のほかに、名前付き引数や Jinja2 テンプレートエンジンを利用することもできます。
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

## 3. CLIエージェントの利用

YAML フロントマターで `brain: cli` を指定すると、OpenAI Codex や Gemini CLI などといったCLIエージェントの呼び出しができます。CLIエージェントを用いると、ファイルの読み込みやシステムコマンドの実行など、より高度な操作をAIに指示できます。

例えば、`summarize.md` というファイルを作成し、次のように記述します。

```markdown
---
brain: cli
---
${file}の最初のセクションを読み、その内容を${language}を用いて、1行で要約してください
```

コマンド呼び出し例:

```shell
$ guildbotics run summarize file=README.md language=日本語 cwd=.
GuildBoticsはAIエージェントとタスクボードで協働するアルファ版ツールであり、将来的な互換性崩壊や重大障害・損害の恐れがあるため利用者は隔離環境で自己責任の下検証すべきと警告している。
```

CLI エージェントでは、`cwd` パラメータでCLIエージェントがシステムコマンドを実行する際の作業ディレクトリを指定する必要があります。



## 4. 組み込みコマンドの利用
GuildBotics内に存在する[組み込みコマンド](../guildbotics/templates/intelligences/functions/)を利用することも可能です。

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
      # pytest tests/ --cov=guildbotics --cov-report=xml >/dev/null 2>&1
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
- [ ] drivers/custom_command_runner.py と drivers/task_scheduler.py の統合的単体テストを追加 (priority: 3)
- [ ] utils/import_utils.py のインポート処理とエッジケースのテストを追加 (priority: 4)
- [ ] intelligences/functions.py のビジネスロジックと外部呼び出しのモックテストを追加 (priority: 5)
```


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


