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

Run `echo "Hello" | guildbotics run translate English Japanese` and you’ll get output like:

```
こんにちは
```

Note:
Before the LLM call, the prompt file is expanded as follows:

```
If the following text is in English, translate it to Japanese; if it is in Japanese, translate it to English:

Hello
```

This leads the LLM to respond with "こんにちは".

### 1.3. Select a member

If you have multiple members registered via `guildbotics add`, you must specify a member when running a command using the `<command>@<person_id>` form.

Example: `guildbotics run translate@yuki English Japanese`


## 2. Variations of variable expansion
In addition to positional arguments, you can use named arguments and the Jinja2 template engine. These enable more flexible prompt definitions.

### 2.1. Named arguments
Use the `${arg_name}` form to reference keyword arguments provided via `params`.

```markdown
Please translate the following text from ${source} to ${target}:
```

Invocation example:

```shell
$ echo "Hello" | guildbotics run translate source=English target=Japanese
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

$ echo "Hello" | guildbotics run translate target=Chinese
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

- With `brain: none`, the LLM is not called; only subcommand outputs are used as the final result.

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
$ guildbotics run summarize file=README.md language=English cwd=.
GuildBotics is an alpha tool for collaborating with AI agents and a task board; users should test in isolated environments due to potential breaking changes and risks.
```

For CLI agents, set the working directory for system commands via the `cwd` parameter.


## 4. Using built-in commands
You can use [built-in commands](../guildbotics/templates/intelligences/functions/) shipped with GuildBotics.

Invocation examples:

```shell
$ guildbotics run functions/talk_as topic="Investigating a production error and mitigation steps"
author: Yuki Nakamura
author_type: Assistant
content: Sorry — we’re seeing an error in production. I’m actively investigating the root cause and mitigation options to minimize impact. I’ll share updates and a remediation plan shortly.
```

```shell
$ echo "Hi! It's a beautiful day." | guildbotics run functions/identify_item item_type="Conversation type" candidates="Question / Chit-chat / Request"
confidence: 0.95
label: Chit-chat
reason: The user is simply greeting and making small talk, not asking a specific question or making a request.
```

```shell
$ echo "The current time is `date`." | guildbotics run functions/identify_item item_type="Time of day" candidates="Early morning, Morning, Noon, Afternoon, Evening, Night, Late night"
confidence: 1.0
label: Late night
reason: The current time is 23:36, which falls in the late night period (typically 11pm–3am).
```

## 5. Using subcommands
You can chain multiple subcommands to build a workflow.

For example, create `get-time-of-day.md` as follows:

```markdown
---
commands:
  - script: echo "The current time is `date`."
  - command: functions/identify_item item_type="Time of day" candidates="Early morning, Morning, Noon, Afternoon, Evening, Night, Late night"
---
```

```shell
$ guildbotics run get-time-of-day
confidence: 1.0
label: Late night
reason: The current time is 23:36, which falls in the late night period (typically 11pm–3am).
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
    script: echo "The current time is `date`."
  - name: time_of_day
    command: functions/identify_item item_type="Time of day" candidates="Morning, Afternoon, Night"
---
```

When `name` is set, you can reference that command’s output by the given name.

```markdown
---
commands:
  - name: current_time
    script: echo "The current time is `date +%T`."
  - name: time_of_day
    command: functions/identify_item item_type="Time of day" candidates="Morning, Afternoon, Night"
brain: none
template_engine: jinja2
---
{% if time_of_day.label == "Morning" %}
Good morning.
{% elif time_of_day.label == "Night" %}
Good evening.
{% else %}
Hello.
{% endif %}

{{ current_time }}
```

Running the above returns something like:

```text
Good evening.

The current time is 20:17:15.
```

- With `brain: none`, the LLM is not called; only subcommand outputs are used as the final result.
- With `template_engine: jinja2`, the Jinja2 template engine is enabled. It is recommended when referencing command outputs.

## 6. Using shell scripts
In addition to writing inline under the `script` key as above, you can also implement an external shell script and invoke it as a command.

For example, create `current-time.sh`:

```bash
#!/usr/bin/env bash

echo "The current time is `date +%T`."
```

After making the file executable, use the `command` key instead of `script` in your prompt file:

```markdown
---
commands:
  - name: current_time
    command: current-time
  - name: time_of_day
    command: functions/identify_item item_type="Time of day" candidates="Morning, Afternoon, Night"
brain: none
template_engine: jinja2
---
{% if time_of_day.label == "Morning" %}
Good morning.
{% elif time_of_day.label == "Night" %}
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
    current_time = f"The current time is {datetime.now().strftime('%H:%M')}."

    time_of_day = await context.invoke(
        "functions/identify_item",
        message=current_time,
        item_type="Time of day",
        candidates="Morning, Afternoon, Night",
    )

    message = ""
    if time_of_day.label == "Morning":
        message = "Good morning."
    elif time_of_day.label == "Night":
        message = "Good evening."
    else:
        message = "Hello."

    return f"{message}\n{current_time}"
```

- Because `invoke` is asynchronous, call it with `await`. Therefore, define `main` as `async def`.
