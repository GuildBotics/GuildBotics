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
    - [2.4. Configuring Desktop inputs](#24-configuring-desktop-inputs)
  - [3. Using the AI CLI tool](#3-using-the-ai-cli-tool)
  - [4. Using built-in commands](#4-using-built-in-commands)
  - [5. Using subcommands](#5-using-subcommands)
    - [5.1. Naming subcommands and referencing outputs](#51-naming-subcommands-and-referencing-outputs)
    - [5.2. Schema definition](#52-schema-definition)
    - [5.3. Print command](#53-print-command)
    - [5.4. to\_html command](#54-to_html-command)
    - [5.5. to\_pdf command](#55-to_pdf-command)
  - [6. Using shell scripts](#6-using-shell-scripts)
  - [7. Using Python commands](#7-using-python-commands)
    - [7.1. Using arguments](#71-using-arguments)
    - [7.2. Invoking other commands](#72-invoking-other-commands)
  - [8. Declaring a routine (patrol) command](#8-declaring-a-routine-patrol-command)


## 1. Quick Start

### 1.1. Create a prompt file
Let’s start with a simple command that asks an LLM to translate text.

Create a prompt file named `translate.md` under your prompt configuration folder (default: `.guildbotics/config/commands` in the workspace; the configuration directory can be overridden with `GUILDBOTICS_CONFIG_DIR`) with the following content:

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
The input message is structured data.
{% if os_ui_language.language_code == "en" %}
If the text in the `input` field is in Japanese, translate it to English; if it is in English, translate it to Japanese.
{% else %}
If the text in the `input` field is in {{ os_ui_language.language_name }}, translate it to English; if it is in English, translate it to {{ os_ui_language.language_name }}.
{% endif %}
Return only the translated text.
```

Notes:

- The built-in Python command `functions/get_os_ui_language` supplies the OS UI language and preserves the input text as structured data.
- When the OS UI language is English, the command uses Japanese as the other language.
- You do not need to provide languages as invocation arguments.
- Use `brain: default` for semantic processing such as translation, proofreading, rewriting, or summarization. Use `brain: agent` when the command needs AI CLI file or tool access. Use `brain: none` only for deterministic rendering; it does not receive the caller's input message. A Markdown command with `inputs.message: required`, `brain: none`, and no child command is therefore invalid. Omitting `brain` still resolves to `default` at runtime, but generated commands and examples declare it explicitly to make the intended execution mode unambiguous.


### 1.2. Invoke the command

With English configured as the OS UI language, run `echo "Hello" | guildbotics run translate` and you’ll get output like:

```
こんにちは
```

Note:
Before the LLM call, the prompt file is expanded as follows:

```
The input message is structured data.
If the text in the `input` field is in Japanese, translate it to English; if it is in English, translate it to Japanese.
Return only the translated text.

input: Hello
language_code: en
language_name: English
```

This leads the LLM to respond with "こんにちは".

### 1.3. Select a member

Specify the member that runs a command with the `<command>@<person_id>` form (or `--person`).

Example: `guildbotics run translate@yuki`

When no member is named, the command runs as the team default: the member stored in `default_person_id` of `team/project.yml` (set it from the Members screen of the GuildBotics Desktop app). Without that setting, the first active agent member in person ID order is used, so a command runs even before anything is configured. Only a team with no member that can execute commands asks you to select one.


## 2. Variations of variable expansion
Prompt files support positional arguments, named arguments, and the Jinja2 template engine. These enable more flexible prompt definitions.

### 2.1. Named arguments
Use the `${arg_name}` form to reference keyword arguments provided via `params`.

```markdown
Please translate the following text from ${source} to ${target}:
```

Invocation example:

```shell
$ echo "Hello" | guildbotics run translate source=English target=Japanese
```

For Markdown and YAML commands, use a root-level `args` mapping to declare whether each named argument is required and to provide runtime defaults:

```yaml
args:
  file:
    required: true
  language:
    default: English
```

An argument is required when it has neither `default` nor `required: false`. A declared default makes the argument optional and is applied by both CLI and Desktop execution. `required: true` and `default` cannot be combined. Placeholders that are not listed under `args` continue to be discovered as required arguments.

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
inputs:
  message: hidden
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

### 2.4. Configuring Desktop inputs

Use `inputs` in Markdown front matter or YAML command metadata to control fields on the Desktop manual-run screen. Python commands put the same mapping in the static module-level `COMMAND_METADATA` mapping.

```python
COMMAND_METADATA = {
    "inputs": {
        "message": "hidden",
    },
}
```

`COMMAND_METADATA` must be a literal, string-keyed dictionary. GuildBotics reads it with Python AST parsing and never imports the command while building the catalog, so calls such as `COMMAND_METADATA = build_metadata()` are rejected.

| Field | Values | Default |
| --- | --- | --- |
| `defined_args` | `auto`, `hidden` | `auto` |
| `extra_args` | `hidden`, `optional` | `hidden` |
| `message` | `hidden`, `optional`, `required` | `optional` |

`defined_args: auto` displays arguments declared under `args`, arguments discovered from `${...}` placeholders, or parameters from a Python `main` signature. The Desktop marks declared or discovered required arguments with `*` and shows declared defaults as field placeholders. `extra_args: optional` enables the free-form additional-arguments field. A required message prevents execution while the input text is empty.

Use `inputs.message` for the command's primary free-form text, such as a sentence to translate or an email to polish. For required caller text, declare `inputs.message: required`; Desktop then presents its Input text field and supplies the value as the command message / `Context.pipe`. Use `args` only for independent values such as a target language, file, or output option.

Omit default values. For example, a command that does not use caller text needs only:

```yaml
inputs:
  message: hidden
```

Desktop saves invalid work-in-progress source, but disables execution until the source passes command validation. This lets you keep and continue editing an incomplete draft without making it runnable.

## 3. Using the AI CLI tool

Specify `brain: agent` in YAML front matter to invoke an AI CLI tool such as OpenAI Codex or Google Antigravity. With an AI CLI tool, you can instruct the assigned GuildBotics member to read files, run system commands, and perform more advanced operations.

For example, create a file `summarize.md` with the following content:

```markdown
---
brain: agent
args:
  file:
    required: true
  language:
    default: English
inputs:
  message: hidden
---
Read the first section of ${file} and summarize it in one line using ${language}.
```

Invocation example:

```shell
$ guildbotics run summarize file=README.md cwd=.
GuildBotics is an alpha tool for collaborating with AI agents and a task board; users should test in isolated environments due to potential breaking changes and risks.
```

For AI CLI tools, set the working directory for system commands via the `cwd` parameter.


## 4. Using built-in commands
You can use [built-in commands](../guildbotics/templates/commands/functions/) shipped with GuildBotics.

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
inputs:
  message: hidden
commands:
  - script: echo "The current time is `date`."
  - command: functions/identify_item item_type="Time of day" candidates="Early morning, Morning, Noon, Afternoon, Evening, Night, Late night"
  - prompt: Please provide a suitable greeting for the current time of day.
---
```

```shell
$ guildbotics run get-time-of-day
Good evening.
```

List the commands to run in order under the `commands` array. Each command receives the previous command’s output as input.

- `script`: write a shell script inline
- `command`: invoke another prompt file or a built-in command
- `prompt`: call an LLM with a prompt written in Markdown

If you only need the front matter description and no Markdown body, as shown above, you can save it as a YAML file.

Example filename: `get-time-of-day.yml`

```yaml
commands:
  - script: echo "The current time is `date`."
  - command: functions/identify_item item_type="Time of day" candidates="Early morning, Morning, Noon, Afternoon, Evening, Night, Late night"
  - prompt: Please provide a suitable greeting for the current time of day.
```

You can also extract only the YAML front matter enclosed in `---` and save it as a `.yml` file, which can be used as a command just like `.md` files.


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

### 5.2. Schema definition
For `prompt` commands that call an LLM, you can define the response schema with `schema` and specify the response class with `response_class`. This allows you to handle the LLM response as structured data.

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
      This information is analyzed to output the top 3 packages with the highest priority for test implementation in JSON format as Rankings.
    response_class: Rankings
  - name: task_list
    prompt: |
      Based on this analysis, please propose up to 5 immediately actionable test implementation tasks in JSON format as TaskList, sorted by priority.
    response_class: TaskList
template_engine: jinja2
brain: none
---
{% for task in task_list.tasks %}
- [ ] {{ task.title }} (priority: {{ task.priority }})
{% endfor %}
```

Invocation example:

```shell
$ guildbotics run coverage
- [ ] Add unit tests for utils/fileio.py (priority: 1)
- [ ] Add tests for utils/git_tool.py's operations and error handling (priority: 2)
- [ ] Add integrated unit tests for drivers/command_runner.py and drivers/task_scheduler.py (priority: 3)
- [ ] Add tests for utils/import_utils.py's import processing and edge cases (priority: 4)
- [ ] Add business logic and external call mock tests for intelligences/functions.py (priority: 5)
```

### 5.3. Print command

`print` is a command for generating and formatting text without calling the LLM. It is described directly in place as the value of the `print` key in the `commands` array.

```markdown
commands:
  - print: Hello.
```
Invocation example:

```shell
$ guildbotics run greet
Hello.
```

In the print command, the Jinja2 template engine is enabled, so you can also use variable expansion and conditional branching.

```yaml
commands:
  - name: current_time
    script: echo "The current time is `date +%T`."
  - name: time_of_day
    command: functions/identify_item item_type="Time of day" candidates="Morning, Afternoon, Night"
  - print: |
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

### 5.4. to_html command

`to_html` is a command for converting Markdown text to HTML.

In the following definition example, the output of the previous command (`cat README.ja.md`) is converted to HTML and saved to `tmp/summary.html`.

```yaml
commands:
  - script: cat README.ja.md
  - to_html: tmp/summary.html
```

You can also explicitly specify parameters as follows.

```yaml
commands:
  - to_html:
      input: reports/summary.md
      css: assets/summary.css
      output: tmp/summary.html
```

- `input`: Specify the path of the input Markdown file. If omitted, the output of the previous command is used as input.
- `output`: Specify the path to save the generated HTML file. If omitted, the generated HTML string is returned as the command result.
- `css`: Specify the path of the CSS file to apply to the generated HTML.

### 5.5. to_pdf command
`to_pdf` is a command for converting Markdown or HTML to PDF.


```yaml
commands:
  - to_pdf:
      input: reports/summary.md
      css: assets/summary-print.css
      output: tmp/summary.pdf
```

- `input`: Specify the path of the input file to convert. If omitted, the output of the previous command is used as input.
- `output`: Specify the path to save the generated PDF file. If omitted, the generated PDF is returned as a Base64 string.
- `css`: Specify the path of the CSS file to apply to the generated PDF.


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

## 8. Declaring a routine (patrol) command

A command can declare itself as a candidate for a member's routine (patrol) execution. Routine candidates are the commands offered in the member's patrol settings, and the scheduler runs the selected ones periodically.

Declare it in the command's own metadata, so adding a routine never requires editing an edition-side list:

- Markdown / YAML commands: add `routine: true` to the YAML front matter.
- Python commands: add `"routine": True` to the module-level `COMMAND_METADATA` mapping.

```markdown
---
description: Periodically reconcile open tickets.
routine: true
---
...
```

```python
COMMAND_METADATA = {
    "name": "Reconcile tickets",
    "description": "Periodically reconcile open tickets.",
    "routine": True,
}


async def main(context) -> None:
    ...
```

Because the scheduler runs a routine with no caller-supplied input, a routine candidate must not require caller-supplied arguments or a message. A command that declares `routine: true` stays listed but is marked ineligible when required arguments remain visible through `inputs.defined_args: auto` or when `inputs.message: required`. With `inputs.defined_args: hidden`, placeholders are supplied internally by the workflow and do not affect routine eligibility.


## 9. Specifying model effort

How hard the model should think is expressed with three provider-neutral labels: `low` / `default` / `high`. Translating a label into concrete provider settings is the job of the configuration YAML and the adapters, so a command only ever deals with the label.

### 9.1. How to specify it, and the resolution order

Declare the default in the frontmatter:

```markdown
---
brain: agent
effort: high
---
Investigate the whole repository and propose a fix.
```

To override it at run time, pass it as an ordinary `key=value` parameter (there is no dedicated CLI option):

```shell
guildbotics run summarize file=README.md cwd=. effort=high
```

The order is the same for every brain (both the LLM API path and the AI CLI tool path):

1. the runtime value (`effort=<level>`, and the chat workflow's automatic assessment)
2. the frontmatter `effort:`
3. unspecified

**A runtime `effort=default` explicitly cancels a frontmatter `effort: high`.** "Unspecified" and "specified as `default`" are not the same thing.

### 9.2. What `default` and unspecified mean

Both mean "do not intervene". On the LLM API path a model is built fresh for every run, so this is the same as running on the model's own defaults.

On the native AI CLI tool path, however, **the session continues**. For a continued session, "do not intervene" means "**keep the settings that session already has**"; returning to the model defaults is not guaranteed. That happens only once the conversation rotates and a new session begins.

Rotation is decided by a fingerprint of the effective settings (resolved level + model + provider-specific settings). Moving between an empty fingerprint (nothing stated) and a non-empty one is "keep", and does not rotate. Only **two differing non-empty** fingerprints rotate the session, with the reason `settings_changed`. An adapter that can re-send its settings on every turn (codex) never rotates for this.

### 9.3. Model definition YAML schema

`intelligences/models/<provider>/*.yml` may carry an optional `effort:` block. The settings for a level are shallow-merged into `parameters`.

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

- Keys must be `low` or `high`, and every value must be a mapping
- **`default:` is rejected.** `default` means "do not intervene", so a mapping for it could never be applied. Settings that should always apply belong in `parameters:` instead
- Because the merge targets `parameters`, replacing `id` lets a level switch models entirely. `parameters:` itself always applies, and AI CLI tool definitions carry the same block
- Parameter names and types differ per provider: OpenAI uses `reasoning_effort` (string), Anthropic `thinking: {type, budget_tokens}` (nested), Gemini `thinking_budget` (integer)

Slots live at `models/<provider>/<slot>.yml`, but only `default.yml` is packaged. **A slot file with no `effort:` key inherits the `effort:` of its provider's `default.yml`.** To state "no mapping" explicitly, write `effort: {}` (an absent key inherits; an empty mapping means no intervention).

#### `effort_fields:` (optional)

The same file may declare which settings the provider accepts. The desktop settings screen builds typed controls from this declaration alone, and rejects unknown keys or wrong types on save. A provider that declares nothing falls back to editing raw JSON, with no validation.

```yaml
effort_fields:
  - key: thinking.type          # dotted paths address nested keys
    type: enum
    values: [enabled, disabled]
  - key: thinking.budget_tokens
    type: integer
    minimum: 1024
  - key: id
    type: model_id
```

`type` is one of `enum` / `integer` / `boolean` / `string` / `model_id`. The declaration is provider knowledge; the screen never learns what any key means.

### 9.4. AI CLI tool configuration YAML schema

AI CLI tool definitions use the same two levels as model definitions:

```
cli_agents/<tool>/default.yml     the tool's own default, which every slot inherits
cli_agents/<tool>/<slot>.yml      a slot's own definition
```

`cli_agent_mapping.yml` points a slot at one of these paths, exactly as the model mapping points at `models/<provider>/<slot>.yml`. A slot definition inherits every key it does not state from its tool's default, so one tool can serve several slots with different models and efforts.

Either file may carry `parameters:` and `effort:`, related exactly as they are in a model definition: `parameters:` **always applies**, and `effort.<level>` overlays it.

```yaml
parameters:        # applies whatever effort was asked for
  model: <model>
effort:            # overlays it for low / high only
  high:
    model: <stronger model>
```

`default` and unspecified apply no overlay, so a model that should always be used belongs in `parameters:`. These two keys are all there is to configure: the tool itself is driven by its built-in adapter. The shipped defaults additionally declare `effort_fields:` -- the typed-editing descriptors of the previous section -- but that is provider knowledge shipped with the tool, not something to configure.

Every shipped tool carries a working default mapping plus `effort_fields:`, so `low` and `high` do something before you configure anything: codex takes model/effort on `turn/start`, Claude Code takes a model and a thinking budget, `grok agent stdio` takes model and reasoning effort as launch options, `copilot --acp` takes them as the `model` and `reasoning_effort` session config options, and `agy --print` takes `--model` or `--effort` on its command line (the two are mutually exclusive, so a slot that sets both keeps the model). Supporting a new AI CLI tool means implementing a native adapter for it in this repository, which is also where its `effort_fields:` are declared -- there is no way to add a tool from a workspace YAML file.

```yaml
# intelligences/cli_agents/codex/default.yml
effort:
  low:
    effort: low
  high:
    model: <a stronger model id>
    effort: high
```

The keys inside a block are provider-specific. The core understands only the common `model` key, which it uses for the settings fingerprint. Each adapter holds an allowlist of the keys it can act on and warns about the rest rather than dropping them silently.

- codex: sends `model` / `effort` on every `turn/start`. Both are validated against `model/list` (`supportedReasoningEfforts`); an unsupported value is warned about and dropped
- claude: translates `model` / `effort` into the `--model` / `--effort` launch flags. They are fixed when the session starts, so changing them starts a fresh session; an effort outside `low` / `medium` / `high` / `xhigh` / `max` is warned about and dropped
- grok: passes `model` / `reasoning_effort` to `grok agent stdio` as launch options. They are fixed for the life of the process, so changing them starts a fresh session; keys outside that pair are warned about and ignored

### 9.5. Requesting a level that has no mapping

If `high` is requested but the model definition or tool configuration has no `high` mapping, this is **not an error**: a warning is logged and the run continues without intervention. Providers differ in how far their mappings are filled in, and stopping the run would do more harm than running without the overlay.

The provider-neutral label is never passed through to the provider as a fallback. That holds even for a tool whose own vocabulary happens to match it (Codex accepts `low` / `high` too): the mapping is always the only source of these values. Falling back to the label would make the run intervene precisely when diagnostics recorded it as `unsupported`, so the record and the behaviour would disagree.

### 9.6. Workflow defaults

- **Ticket-driven workflow**: no automatic assessment. `functions/handle_github_ticket` declares `effort: high` in its frontmatter, so the assumption that ticket work is heavy holds out of the box
- **Chat workflow**: once per incoming event, an LLM (`functions/assess_effort`) answers `default` or `high`. A request that needs work on local files — or that asks for an issue to be drafted for a repository or for a design or implementation policy decision about one — is `high`; an ordinary conversational reply is `default`. `low` is never produced automatically because no criterion for choosing it has been defined (it remains available for explicit configuration)

The chat assessment only ever **promotes**. An assessment below the thread's stored level is not adopted, and a thread already at `high` skips the call entirely. This state is stored per person_id, so it is one member's view of the thread rather than a single value shared across it.

The assessment runs on `brain: default` (the LLM API path) and the assessing command declares `effort: low` for itself. Note that "the `default` slot is cheap" is not guaranteed — a costly model can be configured there.

**In a CLI-only setup (no LLM API key) the automatic assessment does not work.** When no LLM model is configured the call is skipped, one warning is logged, and the stored value is used. To raise the effort in such a setup, state it explicitly in the frontmatter or as a runtime parameter.

### 9.7. Reading it in diagnostics

Each effort decision is recorded in the trace / diagnostics detail. It is not shown in the activity history: effort is diagnostic information, whereas the activity history is about domain outcomes.

Only a safe allowlist is recorded:

- `requested`: the value as supplied
- `resolved`: the level actually adopted
- `model`: the effective model id
- `applied_keys`: the **names** of the parameters the effort level itself applied (never their values, and never the tool's always-applied baseline settings)
- `unsupported`: whether an explicit request found no mapping

Raw effective parameter values are never recorded, because `api_key`, headers, and client configuration can sit alongside them.
