# Ticket Driven Workflow の rate limit 対応実装方針

## 目的

`workflows/ticket_driven_workflow` で AI CLI tool の rate limit が発生したとき、`workflows/chat_conversation_workflow` と同じ品質で次を満たす。

- チケット上のコメントから rate limit 起因で止まったことが分かる
- rate limit を agent retry / scheduler retry / routine 再選定で増幅しない
- Activity 画面に rate limit と reset 時刻が表示される
- issue comment の最後が agent の workflow error / rate limit 通知である間は、関連 PR review が残っていても同じ ticket を再実行しない

この対応は、ticket workflow だけへ場当たり的に if 文を追加するのではなく、chat workflow と ticket workflow が共有できる rate limit handling を core/capability 側へ切り出して実装する。

## 現状と原因

### 実例で確認できたこと

対象 trace:

- `6bdda3c992a943578cf29304318ce297`
- `e63de2dd8b4d495ab362e1408a201a5d`

どちらも `prompt_trace.jsonl` の `cli_agent.response` に次の marker が出ていた。

```text
GUILDBOTICS_CLI_AGENT_ERROR_JSON: {"category":"rate_limited","retry_after_text":"try again at 9:37 PM"}
```

つまり、CLI agent wrapper と `guildbotics/intelligences/brains/cli_agent.py` による rate limit 検出は成功している。

一方で、`diagnostics.jsonl` には同じ trace の `workflow.rate_limited` event がなく、Activity 画面が参照する rate limit 情報が記録されていなかった。

また、チケットには rate limit 専用文ではなく、通常の workflow error 文が投稿されていた。

### コード上の原因

`guildbotics/capabilities/completion_retry.py` の `run_with_completion_retry()` は、`CliAgentExecutionError(category="rate_limited")` を見つけると即 raise する。

これは正しい。rate limit は completion retry で回復する種類の失敗ではないため、同じ dispatch 内で再試行してはいけない。

問題は呼び出し元の差である。

- `guildbotics/templates/commands/workflows/chat_conversation_workflow.py`
  - rate limit 例外を捕捉する
  - Slack thread に suppressed workflow status notice を投稿する
  - `workflow.rate_limited` を diagnostics に記録する
  - pending event を processed 扱いにして外側 retry を止める

- `guildbotics/templates/commands/workflows/ticket_driven_workflow.py`
  - rate limit 例外を専用捕捉しない
  - 汎用例外処理へ落ちる
  - 汎用エラーコメントだけ投稿する
  - `workflow.rate_limited` を記録しない
  - routine 側では失敗扱いになり、次の routine tick で再選定され得る

### 「最後の issue comment が自分なのに再実行される」理由

`guildbotics/integrations/github/github_ticket_manager.py` の `_select_actionable_task()` は、working ticket では issue comment の最後が自分かどうかを見る前に、関連 PR を選び、未処理 review thread があるかを確認する。

現在の順序は概ね次の通り。

1. issue comments を読む
2. `Task.READY` なら最後の issue comment が自分かを見て抑止する
3. working ticket なら関連 PR を探す
4. 関連 PR に未処理 review thread があれば `pull_request_review` として選定する
5. 関連 PR がなければ issue comment の最後が user かを見る

そのため、issue comment の最後が agent のエラー通知でも、関連 PR に未処理 review thread があると ticket workflow が再起動する。

これは「PR 中だから起動した」という見方で正しい。ただし、workflow error / rate limit 通知を最後に出した後まで再起動するのは誤りである。

## 設計原則

### 1. 自然文キーワード判定をしない

次のような判定は禁止する。

- コメント本文に `rate limit` が含まれる
- コメント本文に `エラー` が含まれる
- persona が生成した文面の一部を正規表現で探す

理由:

- i18n で文言が変わる
- `talk_as()` により persona ごとの言い換えが入る
- ユーザーが同じ単語を引用しただけで誤判定する
- この repository の「自然言語理解をキーワード列挙で実装しない」ルールに反する

### 2. GitHub comment を source of truth にする

別の永続 store へ ticket workflow status を持たせる設計は避ける。

理由:

- GitHub 上の最新会話状態とローカル store の整合が必要になる
- workspace を変えた場合や別端末で動かした場合に抑止状態が失われる
- ticket selection は GitHub ticket の現在状態を読む責務なので、GitHub comment にある状態を読む方が自然

### 3. 「最後のメッセージが workflow status なら起動しない」を選定ロジックに入れる

`GitHubTicketManager` は「この ticket は今 actionable か」を判断する層である。

そのため、PR review thread を見る前に、issue comment の最新状態として次を確認する。

- 最新 comment が agent 自身の workflow status comment か
- その status が `rate_limited` または `failed` か
- `rate_limited` の場合、`retry_after_at` が未来か

これに該当する場合、関連 PR review thread が未処理でも選定しない。

ユーザーや reviewer がその後にコメントした場合、最新 comment は workflow status comment ではなくなるため、通常どおり再選定される。

## 判定に使う信号

ticket selection が workflow error / rate limit を判定するときに使ってよい信号は、GitHub comment の raw markdown body に含まれる **可視の workflow status block** だけにする。

HTML comment、不可視データ、Base64 文字列、GitHub rendering の副作用には依存しない。GitHub には issue comment に任意の structured field を付与する公式 API がないため、status はユーザーにも見える通常の comment として投稿する。

GitHub comment body を source of truth にする理由:

- GitHub REST API の issue comment response は raw markdown `body` を返す
- 別 store との同期が不要
- rate limit 起因で止まったことがチケット上で人間にも分かる
- user / reviewer が新しい comment を追加すると、最新 comment ではなくなるため自然に抑止が解除される

使う信号:

````markdown
**GuildBotics workflow status**

```guildbotics-workflow-status-v1
{"kind":"workflow_error","person_id":"aiko","reason":"rate_limited","retry_after_at":"2026-07-05T21:37:00+09:00","retry_after_text":"try again at 9:37 PM","routing":"suppress","run_id":"2004d62819f94b52b37a7dbf2d924c31","subject_id":"https://github.com/GuildBotics/GuildBotics/issues/256"}
```

AI CLIツールが rate limit 中のため、この workflow を今は続行できません。Reset: try again at 9:37 PM。新しい更新があるまで、この項目は自動再試行しません。
````

fenced block の info string は必ず `guildbotics-workflow-status-v1` にする。判定はこの fenced block の JSON だけを読む。見出しや説明文は人間向けであり、機械判定に使わない。

`kind` / `routing` の値は、既存の `guildbotics/integrations/chat_workflow_status.py` が Slack metadata 用に定義している `WORKFLOW_STATUS_KIND = "workflow_error"` / `WORKFLOW_STATUS_ROUTING_SUPPRESS = "suppress"` を import してそのまま使う。chat と GitHub で「workflow status の suppress 通知」という同じ概念に別語彙を作らない。

判定対象:

- GitHub issue comments を `created_at` / `timestamp` 昇順で sort した最後の comment
- その comment の `author_type` が `Message.ASSISTANT` である場合だけ
- その comment の raw markdown `body` から `parse_workflow_status_comment()` で抽出できる `guildbotics-workflow-status-v1` fenced block

判定に使わないもの:

- 表示本文の自然文
- `rate limit` / `エラー` / `Reset` などの単語
- persona が生成した文面
- GitHub URL path
- PR review thread の本文
- rendered HTML (`body_html`)
- text-only rendering (`body_text`)
- diagnostics store の過去 record
- local file / task run store の状態

`parse_workflow_status_comment()` が返した structured value だけを使って、次の条件を評価する。

```python
latest_comment = task.comments[-1] if task.comments else None
latest_status = (
    parse_workflow_status_comment(latest_comment.content)
    if latest_comment and latest_comment.author_type == Message.ASSISTANT
    else None
)
if latest_status and suppresses_ticket_selection(latest_status):
    return None
```

`suppresses_ticket_selection()` は次だけを見る。

- `kind == WORKFLOW_STATUS_KIND`（`"workflow_error"`）
- `routing == WORKFLOW_STATUS_ROUTING_SUPPRESS`（`"suppress"`）
- `reason`
- `retry_after_at`

`person_id` / `run_id` / `subject_id` は監査・debug・将来拡張用であり、初期実装では selection suppress の必須条件にしない。理由は、proxy agent、bot username、issue transfer、URL normalize 差異で不要な取りこぼしを起こさないためである。

ただし、`author_type == Message.ASSISTANT` は必須条件にする。ユーザーが workflow status block を引用しただけで抑止される誤判定を避けるためである。

## 実装対象ファイル

主な変更対象:

- `guildbotics/capabilities/workflow_rate_limits.py` (新規)
- `guildbotics/integrations/workflow_status_comment.py` (新規)
- `guildbotics/templates/commands/workflows/chat_conversation_workflow.py`
- `guildbotics/templates/commands/workflows/ticket_driven_workflow.py`
- `guildbotics/integrations/github/github_ticket_manager.py`
- `guildbotics/templates/locales/commands/workflows/common.en.yml`
- `guildbotics/templates/locales/commands/workflows/common.ja.yml`
- `guildbotics/templates/locales/commands/workflows/chat_conversation_workflow.en.yml`
- `guildbotics/templates/locales/commands/workflows/chat_conversation_workflow.ja.yml`
- `tests/guildbotics/capabilities/test_workflow_rate_limits.py` (新規)
- `tests/guildbotics/integrations/test_workflow_status_comment.py` (新規)
- `tests/guildbotics/templates/commands/workflows/test_ticket_driven_workflow.py`
- `tests/guildbotics/templates/commands/workflows/test_chat_conversation_workflow.py`
- `tests/guildbotics/integrations/test_github_ticket_manager.py`
- `tests/guildbotics/app_api/test_activity_history.py`

## 追加する共通 module

### `guildbotics/capabilities/workflow_rate_limits.py`

責務:

- 例外 chain から rate limit 情報を抽出する
- `workflow.rate_limited` diagnostics event を記録する
- retry-after 表示値を workflow 共通で扱う

この module は provider API を呼ばない。`CliAgentExecutionError.details` を読み、observability へ provider-neutral な event を記録するだけにする。

`retry_after_at` はこの module で計算しない。brain 側の `normalize_cli_agent_retry_after()`（`guildbotics/intelligences/brains/cli_agent.py`）が、marker の `retry_after_text`（例: `try again at 9:37 PM`）を tz 付き ISO 8601 へ正規化して `details["retry_after_at"]` に入れている。weekly limit など正規化できない表現の場合だけ空になる。この module は details の値をそのまま `WorkflowRateLimit` に詰め替える。

想定 API:

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkflowRateLimit:
    retry_after_at: str = ""
    retry_after_text: str = ""

    @property
    def retry_after_display(self) -> str:
        return self.retry_after_text or self.retry_after_at


def workflow_rate_limit_from_exception(exc: BaseException) -> WorkflowRateLimit | None:
    ...


def record_workflow_rate_limited(
    *,
    person_id: str,
    command: str,
    run_id: str,
    subject_id: str = "",
    source_event_id: str = "",
    retry_after: WorkflowRateLimit,
    default_source: str = "",
) -> None:
    ...
```

`workflow_rate_limit_from_exception()` は内部で既存の `find_cli_agent_execution_error(exc, category="rate_limited")` を使う。

`record_workflow_rate_limited()` が記録する event は次の形に揃える。

```python
record_correlated_event(
    event_type="workflow.rate_limited",
    default_source=default_source,
    person_id=person_id,
    command=command,
    attributes={
        "error.category": "rate_limited",
        "rate_limit.retry_after_at": retry_after.retry_after_at,
        "rate_limit.retry_after_text": retry_after.retry_after_text,
    },
    payload={
        "category": "rate_limited",
        "retry_after_at": retry_after.retry_after_at,
        "retry_after_text": retry_after.retry_after_text,
        "run_id": run_id,
        "subject_id": subject_id,
        "source_event_id": source_event_id,
    },
)
```

`subject_id` は ticket workflow では issue URL、chat workflow では Slack subject key 相当を入れる。空でもよいが、入れられる場合は入れる。

### `guildbotics/integrations/workflow_status_comment.py`

責務:

- GitHub comment へ投稿する外部可視 workflow status payload を定義する
- GitHub issue comment の raw markdown body から `guildbotics-workflow-status-v1` fenced block を生成・抽出する
- 抽出した status が selection を抑止すべきか判断する

この module は GitHub API を呼ばない。文字列 payload の生成・parse だけを担当する。`kind` / `routing` の定数は `chat_workflow_status.py` から import し、新しい値を定義しない。

想定 API:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from guildbotics.integrations.chat_workflow_status import (
    WORKFLOW_STATUS_KIND,
    WORKFLOW_STATUS_ROUTING_SUPPRESS,
)

WORKFLOW_STATUS_CODE_BLOCK = "guildbotics-workflow-status-v1"
WORKFLOW_STATUS_HEADING = "**GuildBotics workflow status**"


@dataclass(frozen=True)
class WorkflowStatusComment:
    reason: str
    routing: str
    person_id: str
    run_id: str
    subject_id: str = ""
    retry_after_at: str = ""
    retry_after_text: str = ""


def workflow_status_comment_payload(
    *,
    reason: str,
    person_id: str,
    run_id: str,
    subject_id: str = "",
    retry_after_at: str = "",
    retry_after_text: str = "",
) -> dict[str, object]:
    ...


def render_workflow_status_comment(
    *, body: str, payload: dict[str, object]
) -> str:
    ...


def parse_workflow_status_comment(body: str) -> WorkflowStatusComment | None:
    ...


def suppresses_ticket_selection(
    status: WorkflowStatusComment,
    *,
    now: datetime | None = None,
) -> bool:
    ...
```

workflow status comment の形式と JSON schema は「判定に使う信号」の実例のとおりとする。

制約:

- JSON は `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))` で生成する
- JSON は fenced code block 内に 1 行で入れる
- parse は `guildbotics-workflow-status-v1` info string の fenced block だけを読む
- fenced block は body 全体から探す。先頭一致・末尾一致に依存しない。`add_comment_to_ticket()` は issue author が自分以外なら `@author` mention を本文の先頭に付与し、proxy agent 構成では署名行 `⚙<person_id>` を末尾に付与するため、投稿時に組み立てた本文と GitHub 上の body は一致しない
- fenced block がないコメントは `None`
- 壊れた JSON は `None`
- `kind` が `workflow_error`（`WORKFLOW_STATUS_KIND`）でないものは `None`
- `routing` が `suppress` でないものは抑止対象にしない
- 人間向け本文、見出し、順序、空行は判定に使わない

`suppresses_ticket_selection()` のルール:

- `reason == "rate_limited"`:
  - `retry_after_at` が空なら抑止する
  - `retry_after_at` が ISO 8601 として parse できない場合は、空と同様に扱い抑止する
  - `retry_after_at` が未来なら抑止する
  - `retry_after_at` が過去なら抑止しない
- `reason == "failed"`:
  - 抑止する
  - ユーザー/ reviewer が次にコメントするまで再実行しない
- その他:
  - 抑止しない

時刻比較は offset-aware な datetime 同士で行う（`retry_after_at` は brain 側の正規化により tz 付きで入る）。offset の無い値は parse 失敗と同様に扱う。

`failed` を抑止する理由:

- 汎用 workflow error も、最後の会話状態が agent の失敗通知である限り、同じ trigger を繰り返しても同じ失敗コメントが増えるだけである
- ユーザーが追加情報や再実行指示をコメントした時点で最新 comment が user になり、抑止は自然に解除される

## i18n

common へ移すのは rate limit 文言だけにする。

追加 key:

```yaml
commands:
  workflows:
    common:
      rate_limited_escalation: "AI CLI tool is currently rate-limited, so this workflow cannot continue now. This item will not be retried automatically until there is a new update."
      rate_limited_escalation_with_reset: "AI CLI tool is currently rate-limited, so this workflow cannot continue now. Reset: %{retry_after}. This item will not be retried automatically until there is a new update."
```

日本語:

```yaml
commands:
  workflows:
    common:
      rate_limited_escalation: "AI CLIツールが rate limit 中のため、この workflow を今は続行できません。新しい更新があるまで、この項目は自動再試行しません。"
      rate_limited_escalation_with_reset: "AI CLIツールが rate limit 中のため、この workflow を今は続行できません。Reset: %{retry_after}。新しい更新があるまで、この項目は自動再試行しません。"
```

`incomplete_escalation` は共通化しない。chat の既存文言（「依頼を出し直してください」）は chat の会話文脈に合わせたものであり、ticket workflow の汎用エラー文は既存の `drivers.task_scheduler.task_error` を使い続けるため、共通の incomplete 文言は必要ない。既存 test（`tests/guildbotics/templates/commands/workflows/test_chat_conversation_workflow.py`）は `commands.workflows.chat_conversation_workflow.incomplete_escalation` key 経由で期待文言を組み立てているため、この key を動かすとテストも壊れる。

chat workflow の rate limit 文言参照は `commands.workflows.chat_conversation_workflow.rate_limited_escalation*` から common key へ切り替える。文末が「この event は自動再試行しません」から「新しい更新があるまで、この項目は自動再試行しません」へ変わる。これはユーザー可視の文言変更だが、chat と ticket で同じ状況を指す文言を一本化するための意図的な変更とする。既存 test `test_rate_limit_posts_suppressed_notice_and_marks_processed` は表示文言を `"11:44 AM"` の部分一致でしか検証していないため、この切り替えでは壊れない（確認済み）。

参照が無くなった `chat_conversation_workflow.{en,ja}.yml` の `rate_limited_escalation` / `rate_limited_escalation_with_reset` は削除する（未使用 key を残さない）。`incomplete_escalation` は chat 固有 key として残す。

## ticket workflow の変更

### 1. rate limit 例外を専用捕捉する

`guildbotics/templates/commands/workflows/ticket_driven_workflow.py` の `main()` で、現在の汎用 `except Exception` の前に rate limit 処理を入れる。

実装イメージ:

```python
run_id = uuid4().hex
try:
    return await _main(context, ticket_manager, run_id)
except Exception as error:
    rate_limit = workflow_rate_limit_from_exception(error)
    if rate_limit is not None:
        await _handle_ticket_rate_limit(
            context=context,
            ticket_manager=ticket_manager,
            task=task,
            run_id=run_id,
            retry_after=rate_limit,
        )
        return AgentResponse(
            status=AgentResponse.DONE,
            message=_rate_limited_summary(rate_limit),
            skip_ticket_comment=True,
        )
    message = await _build_task_error_message(context, error)
    await ticket_manager.add_comment_to_ticket(task, _workflow_status_body(...))
    raise
```

重要:

- rate limit は `raise` しない
- scheduler に失敗扱いを返さない
- `run_with_completion_retry()` 内の in-dispatch retry は既に止まっているので、ここでは外側 retry / 再選定を止める
- `AgentResponse.status` は現行 schema 上 `done|asking` なので、workflow 自体を「rate limit を扱い終えた」という意味で `done` にする

### 2. rate limit comment を投稿する

`_handle_ticket_rate_limit()` は次を行う。

1. 表示文を作る
2. workflow status comment として整形する
3. `ticket_manager.add_comment_to_ticket(task, body)` で投稿する
4. `record_workflow_rate_limited()` を呼ぶ

実装イメージ:

```python
async def _handle_ticket_rate_limit(
    *,
    context: Context,
    ticket_manager: TicketManager,
    task: Task,
    run_id: str,
    retry_after: WorkflowRateLimit,
) -> None:
    ticket_url = await ticket_manager.get_ticket_url(task, markdown=False)
    message = _workflow_rate_limit_notice_text(retry_after)
    body = render_workflow_status_comment(
        body=message,
        payload=workflow_status_comment_payload(
            reason="rate_limited",
            person_id=context.person.person_id,
            run_id=run_id,
            subject_id=ticket_url,
            retry_after_at=retry_after.retry_after_at,
            retry_after_text=retry_after.retry_after_text,
        ),
    )
    try:
        await ticket_manager.add_comment_to_ticket(task, body)
    except Exception:
        pass
    record_workflow_rate_limited(
        person_id=context.person.person_id,
        command="workflows/ticket_driven_workflow",
        run_id=run_id,
        subject_id=ticket_url,
        retry_after=retry_after,
        default_source="routine",
    )
```

投稿は必ず `ticket_manager.add_comment_to_ticket()` 経由で行う。proxy agent 構成では、この経路が署名行 `⚙<person_id>` を末尾に付与し、`_load_issue_comments()` の `get_author_type()` がその署名で `Message.ASSISTANT` と判定する。生の REST 呼び出しで投稿すると proxy agent の author_type 判定が壊れ、抑止が効かなくなる。

comment 投稿が失敗した場合（GitHub API エラーなど）は、chat workflow の `_escalate_incomplete()` と同様に例外を握りつぶし、`record_workflow_rate_limited()` の記録と DONE 返却まで進める。status comment が付かなかった ticket は次の routine tick で再選定され、rate limit が続いていれば再び投稿を試みる。投稿失敗中はコメントが増えないため、連投にはならない。

run id の扱い:

- `_main()` 内で `run_with_completion_retry()` に run id 生成を任せると、例外時に run id が呼び出し元へ返ってこない
- そのため `main()` で先に `run_id = uuid4().hex` を生成し、`_main()` 経由で `run_with_completion_retry(..., run_id=run_id)` に渡す（「1. rate limit 例外を専用捕捉する」の実装イメージのとおり）
- chat workflow は同じ性質を closure（`_invoke_chat_turn` 内で `current_run_id` を捕捉）で実現している。ticket 側は引数渡しの方が単純

変更後の `_main()`:

```python
async def _main(context: Context, ticket_manager: TicketManager, run_id: str) -> AgentResponse:
    ...
    completion, _run_id = await run_with_completion_retry(
        invoke=_invoke_ticket_turn,
        check_completion=...,
        max_attempts=_max_agent_attempts(),
        run_id=run_id,
    )
```

### 3. 汎用 workflow error comment も workflow status comment にする

rate limit 以外の汎用 error comment も、再選定ループを避けるため同じ workflow status block を含む comment として投稿する。

```python
message = await _build_task_error_message(context, error)
ticket_url = await ticket_manager.get_ticket_url(task, markdown=False)
message = render_workflow_status_comment(
    body=message,
    payload=workflow_status_comment_payload(
        reason="failed",
        person_id=context.person.person_id,
        run_id=run_id,
        subject_id=ticket_url,
    ),
)
await ticket_manager.add_comment_to_ticket(task, message)
raise
```

これは「最後のメッセージが workflow error なら起動しない」を実現するために必要である。

## chat workflow の変更

`chat_conversation_workflow.py` では、既存の `_record_rate_limited()` と `_workflow_status_notice_text()` を共通 helper へ寄せる。

変更後も chat 固有に残す責務:

- Slack thread へ suppressed notice を投稿する
- `FileConversationStateStore` に system notice を保存する
- pending event を processed にする

共通化する責務:

- rate limit 例外から retry-after を抽出する
- `workflow.rate_limited` を diagnostics に記録する
- rate limit notice 文面を生成する

既存 test `test_rate_limit_posts_suppressed_notice_and_marks_processed` は、挙動を変えずに通す。

## GitHubTicketManager の変更

### 1. issue comments 読み込み時に workflow status を判定できるようにする

現在 `_load_issue_comments()` は GitHub comment を `Message` に変換している。

`Message` に field を増やすと影響範囲が広いので、まずは `_select_actionable_task()` 内で `task.comments` の最新 `content` から `parse_workflow_status_comment()` を呼べばよい。

実装イメージ:

```python
latest_comment = task.comments[-1] if task.comments else None
latest_status = (
    parse_workflow_status_comment(latest_comment.content)
    if latest_comment and latest_comment.author_type == Message.ASSISTANT
    else None
)
if latest_status and suppresses_ticket_selection(latest_status):
    return None
```

この判定は、関連 PR を見る前に置く。

配置:

```python
task.comments = sorted(comments, key=lambda m: m.timestamp)
last_comment_is_mine = ...

if _latest_workflow_status_suppresses_selection(task.comments):
    return None

if task.status == Task.READY:
    ...

pull = await self._select_related_pull_request(...)
...
```

これにより、READY でも IN_PROGRESS でも同じ抑止が効く。

実装上の前提（確認済み）:

- `_load_issue_comments()` は GitHub REST の raw `body` をそのまま `Message.content` に入れているため、`task.comments[-1].content` を追加加工なしで `parse_workflow_status_comment()` に渡せる
- 最新 comment が assistant になると `_load_issue_comments()` が `mention_pending` を `False` に戻すため、READY 分岐の既存抑止（`last_comment_is_mine and not mention_pending`）と新しい suppress 判定は矛盾しない。READY に対する新判定は既存抑止の部分集合であり、置く位置が READY 分岐より前でも挙動退行はない

### 2. retry_after_at が過ぎた rate limit は再選定可能にする

`suppresses_ticket_selection()` が時刻を見て判断するため、`GitHubTicketManager` 側は単に helper を呼ぶだけにする。

テストでは `now` を注入しやすいように、helper に `now` 引数を持たせる。

`GitHubTicketManager` 側からは `now` を渡さなくてよい。

抑止の解除は「既存の選定ロジックに戻る」ことだけを意味する。reset 後に実際に選定されるかは既存判定に従う。

- 関連 PR に未処理 review thread がある場合は `pull_request_review` として選定される（issue #256 の実例はこの経路）
- PR がない working ticket は、最後の comment が assistant の status comment のままなので、既存の「最後の comment が自分なら選定しない」判定により選定されない。つまり reset 時刻を過ぎても自動再開はしない

後者は本対応前の挙動（汎用エラーコメント投稿後、ユーザーがコメントするまで停止する）と同じであり、この対応では変えない。reset 後の自動再開が必要になったら、期限切れ rate_limited status を「実行再開の trigger」として扱う拡張を別途設計する。

### 3. user / reviewer の新コメントで解除される

解除用の特別ロジックは不要。

GitHub comments は時刻順に sort され、最新 comment が user / reviewer であれば `author_type != Message.ASSISTANT` になり、workflow status parse は行われない。

その結果、通常の issue comment / PR review 判定へ進む。

### 4. 明示起動（WorkflowInvocation）には抑止を適用しない

desktop などから `WORKFLOW_INVOCATION_KEY` 経由で task を直接渡された場合、`ticket_driven_workflow.main()` は `get_task_to_work_on()` を通らないため、この抑止は効かない。明示起動はユーザーの再実行意図なので、抑止しないのが正しい。変更は不要。

## Activity 表示

Activity 側は既に `workflow.rate_limited` を読む実装になっている。

必要な対応は ticket workflow が同じ event を記録することだけである。

確認ポイント:

- `guildbotics/app_api/activity_history.py` の `_trace_status()` は `workflow.rate_limited` を `rate_limited` status にする
- `_rate_limit_from_records()` は `rate_limit.retry_after_at` / `rate_limit.retry_after_text` を読む
- desktop 側は `ActivityHistorySession.rate_limit` があれば表示できる

したがって frontend 変更は原則不要。

ただし regression test として、ticket workflow command の `workflow.rate_limited` event でも session rate limit が付くことを `tests/guildbotics/app_api/test_activity_history.py` に追加する。

## 期待される挙動

### rate limit 発生時

1. AI CLI tool が rate limit marker を出す
2. `CliAgentExecutionError(category="rate_limited")` が raise される
3. `run_with_completion_retry()` は retry せず即 raise する
4. ticket workflow が rate limit として捕捉する
5. ticket に rate limit 専用 comment を投稿する
6. comment に workflow status block が含まれる
7. `workflow.rate_limited` event を diagnostics に記録する
8. ticket workflow は scheduler に success として戻る
9. Activity 画面は rate limit と reset 時刻を表示する
10. 次回 routine tick では、`GitHubTicketManager` が最新 workflow status comment を見て同じ ticket を選定しない

### ユーザーが追加コメントした場合

1. 最新 issue comment が user comment になる
2. workflow status suppress 判定は効かない
3. ticket selection は通常どおり PR review / issue comment を判定する
4. ticket workflow は再実行される

### retry_after_at が過ぎた場合

1. 最新 issue comment は rate limit status のまま
2. `retry_after_at` が過去なら `suppresses_ticket_selection()` は false になり、抑止は解除される
3. 実際に選定されるかは既存ロジック次第となる
   - 関連 PR に未処理 review thread があれば `pull_request_review` として再実行される
   - PR がない場合は、最後の comment が assistant のままなので選定されない（本対応前と同じ挙動。ユーザーがコメントすれば再実行される）

`retry_after_at` がない場合は抑止を続ける。weekly limit など reset 時刻を正確に取れないケースで無限コメントを避けるためである。

## テスト計画

### `tests/guildbotics/capabilities/test_workflow_rate_limits.py`

追加する test:

- `workflow_rate_limit_from_exception()` が direct `CliAgentExecutionError(category="rate_limited")` を抽出する
- `CompletionRetryExhausted.last_error` に包まれた rate limit を抽出する
- category が違う場合は `None`
- details の `retry_after_at` / `retry_after_text` を `WorkflowRateLimit` に詰める
- `record_workflow_rate_limited()` が `workflow.rate_limited` event を記録し、attributes / payload が Activity で読める形になる

### `tests/guildbotics/integrations/test_workflow_status_comment.py`

追加する test:

- payload と人間向け本文から workflow status comment を render できる
- render した本文から `WorkflowStatusComment` を parse できる
- `guildbotics-workflow-status-v1` fenced block のない本文は `None`
- 壊れた JSON は `None`
- `reason=rate_limited` かつ未来 `retry_after_at` は selection suppress
- `reason=rate_limited` かつ過去 `retry_after_at` は suppress しない
- `reason=rate_limited` かつ `retry_after_at` 空は suppress
- `reason=failed` は suppress
- `routing` が `suppress` 以外なら suppress しない

### `tests/guildbotics/templates/commands/workflows/test_ticket_driven_workflow.py`

追加・更新する test:

- rate limit 例外時に ticket comment が 1 件投稿される
- comment に workflow status block が含まれる
- payload reason が `rate_limited`
- reset 表示がある場合、表の文面に retry_after_text が含まれる
- `workflow.rate_limited` event が記録される
- rate limit では `GUILDBOTICS_TICKET_MAX_ATTEMPTS=5` でも `context.invoke` が 1 回だけ
- rate limit では例外を raise せず、workflow が `AgentResponse(..., skip_ticket_comment=True)` を返す
- rate limit comment の投稿（`add_comment_to_ticket`）が失敗しても、`workflow.rate_limited` は記録され、workflow は DONE を返す
- 汎用 agent failure では既存どおり safe error comment を投稿し、例外を raise する
- 汎用 error comment に `reason=failed` の workflow status block が含まれる

rate limit 例外の作り方:

- `CliAgentExecutionError` を直接作るのが難しければ、既存 `tests/guildbotics/templates/commands/workflows/test_chat_conversation_workflow.py` の FakeInvokeContext と同じ作り方に寄せる
- 可能なら `CliAgentExecutionResult(error_category="rate_limited", error_details={...})` から `CliAgentExecutionError` を作る helper を test に置く

### `tests/guildbotics/templates/commands/workflows/test_chat_conversation_workflow.py`

既存 test を維持し、共通 helper 化後に次を確認する。

- rate limit notice は引き続き Slack thread へ投稿される
- processed event になる
- `workflow.rate_limited` event が記録される
- retry_after_text が表示される

既存の `test_rate_limit_posts_suppressed_notice_and_marks_processed` は表示文言を `"11:44 AM"` の部分一致で検証しているため、rate limit 文言の common key 切り替えでは壊れない。`incomplete_escalation` を参照する test は chat 固有 key のまま据え置くので影響しない。

### `tests/guildbotics/integrations/test_github_ticket_manager.py`

追加する test:

1. `test_working_ticket_with_latest_rate_limit_status_comment_is_not_selected_even_with_unhandled_pr_review`
   - issue comments の最後が assistant の workflow status comment
   - reason は `rate_limited`
   - retry_after_at は未来
   - 関連 PR に未処理 review thread がある
   - `get_task_to_work_on()` は `None`

2. `test_working_ticket_with_expired_rate_limit_status_allows_pr_review_selection`
   - 最新 assistant comment は rate limit status
   - retry_after_at は過去
   - 関連 PR に未処理 review thread がある
   - `trigger_reason == "pull_request_review"`

3. `test_working_ticket_with_latest_failed_status_comment_is_not_selected`
   - 最新 assistant comment は workflow status failed
   - 関連 PR に未処理 review thread がある
   - `get_task_to_work_on()` は `None`

4. `test_user_comment_after_workflow_status_reenables_selection`
   - assistant workflow status comment の後に reviewer/user comment がある
   - 関連 PR に未処理 review thread がある
   - `trigger_reason == "pull_request_review"` または `issue_comment`

5. `test_plain_assistant_comment_without_workflow_status_keeps_existing_behavior`
   - 最新 assistant comment に workflow status block がない
   - 既存仕様から変えない

6. `test_working_ticket_with_expired_rate_limit_status_and_no_pull_request_is_not_selected`
   - 最新 assistant comment は rate limit status
   - retry_after_at は過去
   - 関連 PR がない
   - `get_task_to_work_on()` は `None`（抑止解除後も既存の「最後の comment が自分なら選定しない」が効くことの確認）

### `tests/guildbotics/app_api/test_activity_history.py`

追加する test:

- `command="workflows/ticket_driven_workflow"` の `workflow.rate_limited` event でも `session.status == "rate_limited"` になる
- `session.rate_limit.retry_after_at/text` が入る

既存 Activity 実装は command に依存しないはずなので、退行防止として最小でよい。

## 実装順序

1. `workflow_status_comment.py` を追加し、unit test を通す
2. `workflow_rate_limits.py` を追加し、unit test を通す
3. common i18n key（rate_limited 系 2 key）を追加する
4. chat workflow の rate limit record/text を共通 helper へ移し、旧 `chat_conversation_workflow.{en,ja}.yml` の rate_limited 系 key を削除する
5. ticket workflow に run id 外出し、rate limit handler、汎用 error status block を追加する
6. GitHubTicketManager の `_select_actionable_task()` に最新 workflow status suppress 判定を追加する
7. Activity history の ticket rate limit test を追加する
8. 関連テストを実行する

この順序にすると、共通 helper を先に固められ、ticket workflow への変更が小さくなる。

## 確認コマンド

Python 変更なので最低限次を実行する。

```bash
uv run --no-sync ruff format --check guildbotics
uv run --no-sync ruff check guildbotics
uv run --no-sync mypy guildbotics
uv run --no-sync pylint guildbotics
uv run --no-sync python -m pytest \
  tests/guildbotics/capabilities/test_workflow_rate_limits.py \
  tests/guildbotics/integrations/test_workflow_status_comment.py \
  tests/guildbotics/templates/commands/workflows/test_ticket_driven_workflow.py \
  tests/guildbotics/templates/commands/workflows/test_chat_conversation_workflow.py \
  tests/guildbotics/integrations/test_github_ticket_manager.py \
  tests/guildbotics/app_api/test_activity_history.py
```

整形が必要な場合:

```bash
uv run --no-sync ruff format guildbotics
```

## 完了条件

次をすべて満たしたら完了とする。

- ticket workflow の rate limit comment から rate limit 起因と reset 情報が分かる
- rate limit 発生時、ticket workflow は同一 dispatch 内で retry しない
- rate limit 発生時、scheduler へ通常 failure として戻らない
- diagnostics に `workflow.rate_limited` が記録される
- Activity 画面に ticket workflow の rate limit が表示される
- 最新 issue comment が workflow status `rate_limited` で reset 前なら、関連 PR review が未処理でも ticket は選定されない
- 最新 issue comment が workflow status `failed` なら、ユーザー/ reviewer の次コメントまで ticket は選定されない
- ユーザー/ reviewer が追加コメントすると抑止が解除される
- 自然文キーワード判定が入っていない
- chat workflow の rate limit 挙動が退行していない
