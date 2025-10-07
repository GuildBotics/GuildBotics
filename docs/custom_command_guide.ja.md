# カスタムコマンド開発ガイド

GuildBotics のカスタムコマンドは、エージェントに任意の処理手順を教えるための仕組みです。Markdown ファイルに記述したプロンプトでLLM呼び出しを行ったり、Python ファイルで本格的なワークフローを構築したり、シェルスクリプト (`.sh`) で外部ツールを操作したりできます。このガイドでは、初めての方でも理解しやすいよう、まずはシンプルな例から始めて少しずつ機能を掘り下げていきます。


## 1. クイックスタート: 1分で作る最初のコマンド

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

1. **名前付き引数**: `${arg_name}` の形式で、`params` に指定したキーワード引数に対応します。
2. **Jinja2**: Jinja2 テンプレートエンジンを使用することで、より複雑な変数展開が可能になります。例えば、`{{ variable_name }}` の形式で変数を参照できます。

### 2.1. 名前付き引数の例

```markdown
以下のテキストを${source}から${target}に翻訳してください:
```

コマンド呼び出し例:

```shell
$ echo "Hello" | guildbotics run translate source=英語 target=日本語
```

### 2.2. Jinja2 の例
jinja2 を使う場合は、プロンプトファイルの先頭に以下のようにYAMLフロントマターを追加し、`template_engine` を `jinja2` に設定します。

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

## 3. CLIエージェントの利用

YAML フロントマターで `brain: cli` を指定すると、OpenAI Codex や Gemini CLI などといったCLIエージェントの呼び出しができます。CLIエージェントを用いると、ファイルの読み込みやシステムコマンドの実行など、より高度な操作が可能になります。

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
commands:
  - script: echo "現在の時刻は`date`です"
  - command: functions/identify_item item_type=時間帯 candidates="早朝, 午前, 正午, 午後, 夕方, 夜, 深夜"
```

```shell
$ guildbotics run get-time-of-day
confidence: 1.0
label: 深夜
reason: 現在の時刻が23時36分であり、これは深夜の時間帯（通常23時から翌3時頃）に該当するためです。
```

実行するコマンドを `commands` 配列に順番に指定します。各コマンドは前のコマンドの出力を受け取り、処理を続けます。

- `script` にはシェルスクリプトを直接記述できます。
- `command` は別のプロンプトファイルや組み込みコマンドを呼び出す方法です。

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

{{ current_time}}
```

上記のコマンドを実行すると、以下のような結果を返します。

```text
こんばんは。


現在の時刻は20:17:15です
```

- `brain: none` を指定すると、LLM呼び出しが行われず、サブコマンドの出力のみが最終結果として返されます。
- `template_engine: jinja2` を指定すると、Jinja2 テンプレートエンジンが有効になります。コマンドの出力結果にアクセスする際には Jinja2 テンプレートを利用することをおすすめします。


## 6. Python コマンドの利用
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

### 6.1. 引数の利用

Python コマンドでは、以下の3種類の引数を利用することができます。

- context: `main` 関数の最初の引数として `context` を受け取ると、実行コンテキストにアクセスできます。主として別コマンドの呼び出しに使用します。
- 位置引数: `main` 関数の位置引数として定義します。
- キーワード引数: `main` 関数のキーワード引数として定義します。


```python
def main(context, *args, **kwargs):
    for i, arg in enumerate(args):
        print(f"arg[{i}]: {arg}")

    for key, value in kwargs.items():
        print(f"{key}: {value}")
```

呼び出し例:

```shell
$ guildbotics run hello key1=value1 key2=value2
arg[0]: key1=value1
arg[1]: key2=value2
key1: value1
key2: value2
```

### 6.2. コマンドの呼び出し
context.invoke を利用すると、Python コマンドから別のコマンドを呼び出せます。

```python
from datetime import datetime


async def main(context):
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


