# Custom Command Development Guide

GuildBotics custom commands let you teach agents arbitrary procedures. You can call an LLM with a prompt written in Markdown, operate external tools with shell scripts, or build full workflows in Python files.

- [Custom Command Development Guide](#custom-command-development-guide)
  - [1. Quick Start](#1-quick-start)
    - [1.1. Create a prompt file](#11-create-a-prompt-file)
    - [1.2. Invoke the command](#12-invoke-the-command)
    - [1.3. Select a member](#13-select-a-member)
  - [2. Variations of variable expansion](#2-variations-of-variable-expansion)
    - [2.1. Named arguments](#21-named-arguments)
    - [2.2. Jinja2 examples](#22-jinja2-examples)
    - [2.3. Using the `context` variable](#23-using-the-context-variable)
  - [3. Using the CLI agent](#3-using-the-cli-agent)
  - [4. Using built-in commands](#4-using-built-in-commands)
  - [5. Using subcommands](#5-using-subcommands)
    - [5.1. Naming subcommands and referencing outputs](#51-naming-subcommands-and-referencing-outputs)
  - [6. Using shell scripts](#6-using-shell-scripts)
  - [7. Using Python commands](#7-using-python-commands)
    - [7.1. Using arguments](#71-using-arguments)
    - [7.2. Invoking other commands](#72-invoking-other-commands)


## 1. Quick Start

### 1.1. Create a prompt file
Let’s start with a simple command that asks an LLM to translate text.

Create a prompt file named `translate.md` under your prompt configuration folder (default: `~/.guildbotics/config/prompts`) with the following content:

```markdown
If the following text is in ${1}, translate it to ${2}; if it is in ${2}, translate it to ${1}:
```

Notes:

- `${1}` and `${2}` are positional arguments. Values are provided at invocation time.


### 1.2. Invoke the command

Run `echo "こんにちは" | guildbotics run translate 英語 日本語` and you’ll get output like:

```
Hello
```

Note:
Before the LLM call, the prompt file is expanded as follows:

```
If the following text is in 英語, translate it to 日本語; if it is in 日本語, translate it to 英語:

こんにちは
```

This leads the LLM to respond with "Hello".

### 1.3. Select a member

If you have multiple members registered via `guildbotics add`, you must specify a member when running a command using the `<command>@<person_id>` form.

Example: `guildbotics run translate@yuki 英語 日本語`


## 2. Variations of variable expansion
In addition to positional arguments, you can use named arguments and the Jinja2 template engine. These enable more flexible prompt definitions.

### 2.1. Named arguments
Use the `${arg_name}` form to reference keyword arguments provided via `params`.

```markdown
Please translate the following text from ${source} to ${target}:
```

Invocation example:

```shell
$ echo "Hello" | guildbotics run translate source=英語 target=日本語
```

### 2.2. Jinja2 examples
You can leverage Jinja2 for more complex expansion. For example, reference variables with `{{ variable_name }}`.

```markdown
---
template_engine: jinja2
---
{% if target %}
Please translate the following text into {{ target }}:
{% else %}
Please translate the following text into English:
{% endif %}
```

When using Jinja2, add YAML front matter and set `template_engine: jinja2` as above.

Note:
YAML front matter is text at the beginning of a Markdown file starting and ending with `---`.
It is optional, but required when specifying the template engine or selecting a brain (described later).

Invocation examples:

```shell
$ echo "こんにちは" | guildbotics run translate
Hello

$ echo "こんにちは" | guildbotics run translate target=中国語
你好
```

### 2.3. Using the `context` variable
When using Jinja2, you can access the execution context via the `context` variable, such as current member information or team members.

```markdown
---
brain: none
template_engine: jinja2
---

Language code: {{ context.language_code }}
Language name: {{ context.language_name }}

ID: {{ context.person.person_id }}
Name: {{ context.person.name }}
Speaking style: {{ context.person.speaking_style }}

Team members:
{% for member in context.team.members %}
- {{ member.person_id }}: {{ member.name }}
{% endfor %}
```


## 3. Using the CLI agent

Specify `brain: cli` in YAML front matter to invoke a CLI agent such as OpenAI Codex or Google Gemini CLI. With a CLI agent, you can instruct the AI to read files, run system commands, and perform more advanced operations.

For example, create a file `summarize.md` with the following content:

```markdown
---
brain: cli
---
Read the first section of ${file} and summarize it in one line using ${language}.
```

Invocation example:

```shell
$ guildbotics run summarize file=README.md language=日本語 cwd=.
GuildBoticsはAIエージェントとタスクボードで協働するアルファ版ツールであり、将来的な互換性崩壊や重大障害・損害の恐れがあるため利用者は隔離環境で自己責任の下検証すべきと警告している。
```

For CLI agents, set the working directory for system commands via the `cwd` parameter.


## 4. Using built-in commands
You can use [built-in commands](../guildbotics/templates/intelligences/functions/) shipped with GuildBotics.

Invocation examples:

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

## 5. Using subcommands
You can chain multiple subcommands to build a workflow.

For example, create `get-time-of-day.md` as follows:

```markdown
---
commands:
  - script: echo "現在の時刻は`date`です"
  - command: functions/identify_item item_type=時間帯 candidates="早朝, 午前, 正午, 午後, 夕方, 夜, 深夜"
---
```

```shell
$ guildbotics run get-time-of-day
confidence: 1.0
label: 深夜
reason: 現在の時刻が23時36分であり、これは深夜の時間帯（通常23時から翌3時頃）に該当するためです。
```

List the commands to run in order under the `commands` array. Each command receives the previous command’s output as input.

- `script`: write a shell script inline
- `command`: invoke another prompt file or a built-in command

### 5.1. Naming subcommands and referencing outputs

You can set a `name` for each entry in `commands`:

```markdown
---
commands:
  - name: current_time
    script: echo "現在の時刻は`date`です"
  - name: time_of_day
    command: functions/identify_item item_type=時間帯 candidates="朝, 昼, 夜"
---
```

When `name` is set, you can reference that command’s output by the given name.

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
Good morning.
{% elif time_of_day.label == "夜" %}
Good evening.
{% else %}
Hello.
{% endif %}

{{ current_time }}
```

Running the above returns something like:

```text
Good evening.


現在の時刻は20:17:15です
```

- With `brain: none`, the LLM is not called; only subcommand outputs are used as the final result.
- With `template_engine: jinja2`, the Jinja2 template engine is enabled. It is recommended when referencing command outputs.

## 6. Using shell scripts
In addition to writing inline under the `script` key as above, you can also implement an external shell script and invoke it as a command.

For example, create `current-time.sh`:

```bash
#!/usr/bin/env bash

echo "現在の時刻は`date +%T`です"
```

After making the file executable, use the `command` key instead of `script` in your prompt file:

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
Good morning.
{% elif time_of_day.label == "夜" %}
Good evening.
{% else %}
Hello.
{% endif %}

{{ current_time }}
```

Handling arguments in shell commands:

```bash
#!/usr/bin/env bash

echo "arg1: ${1}"
echo "arg2: ${2}"
echo "key1: ${key1}"
echo "key2: ${key2}"
```

Invocation example:

```shell
$ guildbotics run echo-args a b key1=c key2=d
arg1: a
arg2: b
key1: c
key2: d
```


## 7. Using Python commands
With Python files, you can call APIs and embed complex logic.

For example, create `hello.py` with:

```python
def main():
    return "Hello, world!"
```

- Define the entry point as a function named `main`.

Invoke it like Markdown-based commands:

```shell
$ guildbotics run hello
Hello, world!
```

### 7.1. Using arguments

Python commands support three types of arguments:

- context: If the first parameter of `main` is named `context` / `ctx` / `c`, you can access the execution context. Typical use cases:
  - Retrieve team and person information
  - Invoke other commands
  - Access ticket management services or code hosting services
- positional arguments: Define as positional parameters of `main`.
- keyword arguments: Define as keyword parameters of `main`.

```python
from guildbotics.runtime.context import Context

def main(context: Context, arg1, arg2, key1=None, key2=None):
    print(f"arg1: {arg1}")
    print(f"arg2: {arg2}")
    print(f"key1: {key1}")
    print(f"key2: {key2}")
```

Invocation example:

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

Invocation example:

```shell
$ guildbotics run hello a b key1=c key2=d
arg[0]: a
arg[1]: b
kwarg[key1]: c
kwarg[key2]: d
```

### 7.2. Invoking other commands
From a Python command, you can call another command with `context.invoke`.

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
        message = "Good morning."
    elif time_of_day.label == "夜":
        message = "Good evening."
    else:
        message = "Hello."

    return f"{message}\n{current_time}"
```

- Because `invoke` is asynchronous, call it with `await`. Therefore, define `main` as `async def`.

