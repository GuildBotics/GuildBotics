# GuildBotics Member Capability による Chat Workflow 委譲 実装計画

## Summary

`chat_conversation_workflow` は、Slack event の受信、返信要否判定、返信生成、Slack 投稿、reaction、thread state 更新を 1 つの Python workflow 内で実行している。

Ticket workflow は PR #183 で次の経路へ移行した。

```text
ticket driven workflow
  -> CLI agent (guildbotics skill)
  -> guildbotics member CLI
```

Chat workflow も同じ考え方へ移行し、Slack への投稿や reaction のような member としての外部書き込みを `guildbotics member chat ...` capability 経由に集約する。

### 3者の関係性

この設計の中心は、`chat workflow`、`CLI agent / skill`、`GuildBotics member chat capability` の 3 者を直列の委譲関係にすることである。

```text
incoming Slack event
  -> chat workflow
     -> CLI agent (guildbotics skill / handle_chat_event prompt)
        -> guildbotics member chat ... CLI
           -> MemberChatCapabilityService
              -> ChatService / Slack API
        -> guildbotics member chat complete
     -> chat workflow verifies run completion / evidence
     -> chat workflow updates local state
```

3 者の関係は次のように固定する。

- `chat workflow` は orchestration layer である。Slack event の配送、重複防止、prompt payload 構築、CLI agent 起動、run completion 検証、local state 更新を担当する。
- `CLI agent / skill` は会話判断 layer である。member context、thread context、profile を読んで、reply / reaction-only / no-op / asking / blocked を判断する。
- `GuildBotics member chat capability` は external side-effect boundary である。Slack write credential を解決し、Slack post / reply / reaction を実行し、run evidence を記録する唯一の境界にする。

このため、呼び出し方向と信頼境界は次のように扱う。

- Workflow は CLI agent を起動するが、Slack post / reply / reaction を直接実行しない。
- CLI agent は Slack API、Slack token、生 HTTP 書き込みを直接使わない。Slack への副作用は必ず `guildbotics member chat ...` CLI を通す。
- Member chat capability は CLI agent を呼び返さない。自然言語判断や返信本文生成は capability では行わない。
- Workflow は CLI agent の自然言語 stdout を処理結果として信頼しない。`member chat complete` と `RunStore` evidence だけを正とする。
- Slack への副作用が成功した事実は member chat capability が evidence として記録し、workflow はその evidence を読んで processed event / thread state を更新する。
- Interactive run では `chat workflow` を通らず、利用者が起動した CLI agent / skill から `guildbotics member chat ...` を直接呼べる。この場合も Slack write boundary は同じ member chat capability である。

この関係性により、workflow 経由と interactive CLI session 経由のどちらでも「GuildBotics member として Slack に参加する」操作境界を同一にする。

最終形は次の責務分担にする。

```text
chat workflow
  -> Slack event を受け取る
  -> edit/delete/self message/processed event のような明らかな早期除外を行う
  -> thread state / participant labels を組み立てる
  -> member workspace root を cwd として CLI agent を起動する
  -> agent 終了後に chat run completion / evidence を検証する
  -> Slack 投稿 evidence に基づき processed event と thread state を更新する

CLI agent / skill
  -> member context を確認する
  -> thread context と member profile を踏まえて返信・reaction・no-op を判断する
  -> Slack への投稿 / reaction は guildbotics member chat ... のみを使う
  -> 最後に guildbotics member chat complete を記録する

GuildBotics member chat capability
  -> Person の Slack credential / profile / role を使う唯一の chat write 境界
  -> Slack post / reply / reaction / channel resolve / identity を service + CLI として提供する
  -> workflow run と interactive run の両方から使える public capability にする
```

## 実装前提

- 段階リリースは考慮しない。feature flag、旧経路 fallback、旧 prompt との併存期間は作らず、最も効率の良い順序で一気に最終形へ置き換える。
- 後方互換は考慮しない。既存の workflow 内 Slack 直接投稿経路、旧 chat prompt 分割、旧テストの互換維持は不要。
- 修正差分の小ささではなく、修正後の最終コード量と保守性を優先する。共通化・責務整理・削除を積極的に行う。
- 置き換え後に不要になったコード、prompt、helper、test fixture、docs 記述は迷わず削除する。削除タイミングは、二重実装が最短で消える順序を選ぶ。
- 自然言語理解が必要な判断をキーワード列挙や場当たり的な文字列マッチで実装しない。必要な判断は CLI agent prompt、既存 LLM 判定基盤、または汎用的な判定関数へ寄せる。
- `.gitignore` 対象の `tmp/`、`memo/`、`.guildbotics/`、`dist/` などは参照しない。

実装時の参照優先度は source code を正とし、README や docs は補助情報として扱う。ただしこの文書で確定している UX / architecture 方針は、実装時に README / skill / tests へ反映する。

## 現行実装の読み始めポイント

実装者は最初に次のファイルを読む。

- `guildbotics/templates/commands/workflows/chat_conversation_workflow.py`: 現行 chat workflow。Slack event 処理、返信生成、投稿が集中している。
- `guildbotics/templates/commands/functions/handle_chat_event.{ja,en}.md`: 返信要否・reaction・no-op の判断を CLI agent へ委譲する現行の chat prompt 入口。
- `guildbotics/templates/commands/functions/handle_github_ticket.{ja,en}.md`: ticket workflow が CLI agent へ委譲するときの prompt 例。
- `guildbotics/templates/commands/workflows/ticket_driven_workflow.py`: PR #183 後の薄い workflow と run completion 検証の例。
- `guildbotics/cli/member.py`: `guildbotics member ...` CLI の既存実装。
- `guildbotics/capabilities/task_runs.py`: ticket workflow の evidence / completion store。今回 `RunStore` に汎用化する。
- `guildbotics/integrations/chat_service.py` と `guildbotics/integrations/slack/slack_chat_service.py`: Slack post / reaction 抽象。
- `skills/guildbotics/SKILL.md`: interactive / workflow の member capability 利用ルール。

## 確定したユーザビリティ方針

### CLI agent を chat 対応の主担当にする

Incoming chat workflow は CLI agent へ委譲する。

- Workflow は配送、重複防止、文脈構築、完了検証を担当する。
- CLI agent は返信本文の作成、投稿するかどうかの判断、reaction-only / no-op の判断を担当する。
- Slack 投稿 / reaction は `guildbotics member chat ...` 経由に限定する。

これにより、workflow 経由でも interactive CLI session 経由でも「GuildBotics member として Slack 会話に参加する」体験を揃える。

### no-op は Slack に投稿しない

Agent が「返信しない」と判断した場合、Slack には何も投稿しない。

- Workflow run の証跡として内部 run evidence は記録する。
- Slack 上に no-op コメントや reaction は残さない。
- Chat は ticket より会話密度が高いため、対応不要の痕跡を外部に残すとノイズになる。

### reaction-only は正式な完了アクションにする

Slack では本文返信より reaction の方が自然なケースがある。

- `chat_reaction` evidence を run completion の有効な evidence として扱う。
- `react_only` は `done` として完了可能にする。
- semantic reaction は `ack` / `agree` / `celebrate` / `support` の 4 種類に固定し、Slack 固有 emoji への変換は `ChatService` / Slack service 側に閉じ込める。

### personal memory は削除する

Personal memory は chat workflow の責務から外す。

- Agent prompt に永続記憶の文脈は渡さない。
- 投稿後の永続記憶更新、retention gate、transition 記録作成は行わない。
- 記憶管理用の member capability は提供しない。

### member chat CLI は workflow 専用にしない

`guildbotics member chat ...` は public member capability として提供する。

- Workflow run では自動実行される。
- Interactive run では Codex / Claude Code / Antigravity CLI などから、利用者の承認フローに乗せて実行できる。
- これにより「CLI agent 上で GuildBotics member として Slack に返信する」機能を workflow なしでも利用できる。

### scheduled post は別対象にする

`workflows/chat_post_command` と `workflows/chat/chat_scheduled_post_workflow` は今回の主対象から外す。

- Incoming chat reply と scheduled post は UX が異なる。
- まず incoming chat workflow の member capability 化を完了させる。
- 既存 scheduled post workflow は今回の実装では変更しない。`guildbotics member chat post` への移行は次の独立タスクとして扱う。

ただし、`member chat post` 自体は interactive use と将来の scheduled post 移行のために今回実装する。

### 投稿承認 UX

- Workflow run では Slack 投稿 / reaction を自動実行する。常駐 workflow は都度承認できないため。
- Interactive run では agent shell の通常のコマンド承認フローに乗せる。
- 長文本文は argv ではなく `--body-file` / `--body-stdin` で渡し、承認 UI で内容を確認しやすくする。

## Final Architecture

### 新しい capability 境界

新設する境界は `member chat capability` とする。

- Python service: `guildbotics/capabilities/member_chat.py`
- CLI entrypoint: `guildbotics member chat ...`
- Workflow prompt: `guildbotics/templates/commands/functions/handle_chat_event.{ja,en}.md`
- Run completion: `guildbotics member chat complete`

`member chat capability` は Slack token を直接 agent prompt へ渡さない。`guildbotics member ...` の active workspace / `.env` 自力ロードにより、member の Slack credential を CLI 内で解決する。

### Chat run evidence

Ticket workflow の `TaskRunStore` と同様、chat workflow も agent が完了証跡を残すまで成功扱いしない。

`TaskRunStore` は `RunStore` 相当に汎用化する。`ChatRunStore` は新設しない。ticket / chat で別 store を持つと completion / evidence / redaction / status validation が重複するため。

汎用化後は ticket 固有の `ticket_url` 前提を store の中心概念にしない。run の subject を明示する。

- ticket run: `subject_type="ticket"`、`subject_url=<issue_or_pr_url>`、`subject_id=<owner/repo#number>`。
- chat run: `subject_type="chat"`、`subject_url=""`、`subject_id="slack:<channel_id>:<thread_ts>:<event_id>"`。

Chat run の evidence type は少なくとも次を持つ。

- `chat_reply`: thread reply を投稿した。
- `chat_post`: channel へ通常投稿した。
- `chat_reaction`: reaction を追加した。
- `chat_noop`: 投稿も reaction も不要と判断した。

Completion status は `done` / `asking` / `blocked` に固定する。

- `done`: reply / post / reaction / noop のいずれかで処理済み。
- `asking`: 追加情報が必要で、Slack へ質問返信を投稿した。
- `blocked`: credential 不足、Slack access 不足、必要な文脈不足などで処理できない。

`done` は `chat_noop` のみでも完了可能にする。`asking` は Slack への質問投稿 evidence を必須にする。`blocked` は write evidence がなくても完了可能にするが、`summary` に安全な理由を必須とする。

### Workflow に残す処理

Workflow は次の処理を残す。

- `IncomingChatEvent` の読み取り。
- Slack bot identity の取得。
- edit/delete event の処理済み記録。
- self message / already processed event / 明らかな対象外 event の早期除外。
- thread state / thread messages の読み取り。
- participant labels の構築。
- thread context の維持。
- CLI agent 起動。
- run completion / evidence の検証。
- Slack 投稿 evidence に基づく processed event 記録。
- 投稿済み bot message の thread message 追記。
- thread participants / topic / latest focus の保存。

### CLI agent に委譲する処理

CLI agent は次を担当する。

- `guildbotics member context --person <person_id>` の確認。
- member の role / profile / communication style に基づく返信判断。
- latest message / thread messages / thread context の理解。
- `reply` / `react_only` / `noop` / `asking` / `blocked` の判断。
- Slack 投稿本文の作成。
- `guildbotics member chat reply` / `reaction add` / `post` / `complete` の実行。

Workflow は agent の自然言語 stdout を信頼して処理済みにしない。`member chat complete` と evidence を正とする。

### `should_react` の扱い

現行の `workflows/chat/should_react.py` は、明示 mention、他 agent mention、自分自身の message、thread continuation などを判定している。

最終形では、明らかな deterministic gate だけを workflow-local helper として残す。LLM を使う follow-up / ambient join 判定は `handle_chat_event` の agent prompt へ統合し、旧 `chat_followup_should_reply` prompt は削除する。

`chat_thread_context` / `chat_reply_intent` / `chat_reply_actionable` / `chat_followup_should_reply` の責務は `handle_chat_event` に統合する。workflow から複数 prompt を順に呼ぶ構造は残さない。

### Slack service API

`ChatService` は今回の最小 API として次を提供する。

- `get_bot_identity`
- `resolve_channel_id`
- `post_message`
- `add_reaction`
- `list_channel_events`
- `list_thread_events`

Workflow は routing に必要な incoming event metadata だけを prompt payload に渡し、Slack thread 本文は prompt に詰め込まない。interactive / non-interactive のどちらでも、thread 本文は agent が `member chat inspect thread` 経由で取得して判断する。

### Interactive member chat

Interactive run では、ユーザーの現在の coding session から次のように使える。

```bash
guildbotics member context --person yuki
guildbotics member chat inspect thread --person yuki --service slack --message-url 'https://example.slack.com/archives/C123/p1777554000000000'
guildbotics member chat inspect channel --person yuki --service slack --channel-name general --oldest-ts 1777467600.000000 --latest-ts 1777554000.000000 --limit 50
guildbotics member chat post --person yuki --service slack --channel-name general --body-file post.md
guildbotics member chat reply --person yuki --service slack --message-url 'https://example.slack.com/archives/C123/p1777554000000000' --body-file reply.md
guildbotics member chat reaction add --person yuki --service slack --message-url 'https://example.slack.com/archives/C123/p1777554000000000' --reaction ack
```

Interactive run では workflow run id は必須にしない。run id がない場合は evidence を記録せず、単発 command result のみを返す。

## Public Interfaces

### `guildbotics member chat identity`

```bash
guildbotics member chat identity \
  --person <person_id> \
  [--service slack] \
  [--format json|markdown]
```

現在の member の Slack bot identity を返す。

返却 JSON:

- `service`
- `user_id`
- `display_name`
- `person_id`

### `guildbotics member chat inspect thread`

```bash
guildbotics member chat inspect thread \
  --person <person_id> \
  --service slack \
  (--message-url <slack_message_url> | (--channel-id <channel_id> | --channel-name <channel_name>) --thread-ts <thread_ts>) \
  [--limit <1..200>] \
  [--format json|markdown]
```

Slack thread の内容を取得する。Interactive run では、返信や判断の前に `--message-url` で対象 thread を読む。

返却 JSON:

- `service`
- `mode`
- `channel_id`
- `thread_ts`
- `messages[]`

### `guildbotics member chat inspect channel`

```bash
guildbotics member chat inspect channel \
  --person <person_id> \
  --service slack \
  (--channel-id <channel_id> | --channel-name <channel_name>) \
  [--oldest-ts <oldest_ts>] \
  [--latest-ts <latest_ts>] \
  [--limit <1..200>] \
  [--format json|markdown]
```

Slack channel の直近または指定期間の内容を取得する。自然言語の「今日」「直近 2 時間」などは agent が Slack timestamp に変換して渡す。

返却 JSON:

- `service`
- `mode`
- `channel_id`
- `oldest_ts`
- `latest_ts`
- `messages[]`

### `guildbotics member chat post`

```bash
guildbotics member chat post \
  --person <person_id> \
  --service slack \
  (--channel-id <channel_id> | --channel-name <channel_name>) \
  (--body-file <path> | --body-stdin) \
  [--run-id <run_id>] \
  [--format json|markdown]
```

Channel に通常投稿する。

返却 JSON:

- `service`
- `channel_id`
- `message_ts`
- `thread_ts`
- `posted`

### `guildbotics member chat reply`

```bash
guildbotics member chat reply \
  --person <person_id> \
  --service slack \
  ((--channel-id <channel_id> | --channel-name <channel_name>) --thread-ts <thread_ts> | --message-url <slack_message_url>) \
  (--body-file <path> | --body-stdin) \
  [--run-id <run_id>] \
  [--format json|markdown]
```

Thread reply を投稿する。Interactive run では `--message-url` を優先し、利用者に Slack timestamp を調べさせない。`--channel-name` / `--thread-ts` は agent が既に thread timestamp を把握している場合の補助入力とする。

返却 JSON:

- `service`
- `channel_id`
- `message_ts`
- `thread_ts`
- `posted`

### `guildbotics member chat reaction add`

```bash
guildbotics member chat reaction add \
  --person <person_id> \
  --service slack \
  ((--channel-id <channel_id> | --channel-name <channel_name>) --message-ts <message_ts> | --message-url <slack_message_url>) \
  --reaction ack|agree|celebrate|support \
  [--run-id <run_id>] \
  [--format json|markdown]
```

Semantic reaction を追加する。Interactive run では `--message-url` を優先し、Slack emoji 名や timestamp は CLI 引数に出さない。

返却 JSON:

- `service`
- `channel_id`
- `message_ts`
- `reaction`
- `reacted`

### `guildbotics member chat complete`

```bash
guildbotics member chat complete \
  --person <person_id> \
  --run-id <run_id> \
  --service slack \
  --channel-id <channel_id> \
  --thread-ts <thread_ts> \
  --event-id <event_id> \
  --status done|asking|blocked \
  --summary-file <path> \
  [--format json|markdown]
```

Chat workflow run を完了する。必要な evidence がない場合は fail-closed する。

`done` は `chat_reply` / `chat_post` / `chat_reaction` / `chat_noop` のいずれかがあれば完了可能。`asking` は `chat_reply` または `chat_post` を必須にする。

### `guildbotics member chat noop`

```bash
guildbotics member chat noop \
  --person <person_id> \
  --run-id <run_id> \
  --service slack \
  --channel-id <channel_id> \
  --thread-ts <thread_ts> \
  --event-id <event_id> \
  --reason-file <path> \
  [--format json|markdown]
```

Slack には投稿せず、対応不要の evidence だけを記録する。

## Workflow Prompt

`functions/handle_chat_event.{ja,en}.md` を追加する。

Prompt には次を渡す。

- GuildBotics execution mode: `guildbotics_execution_mode=workflow`
- person id
- workflow run id
- service name
- channel id
- event id
- message ts
- thread ts
- latest message
- thread messages
- participant labels
- member profile
- previous thread context
- project language
- available `guildbotics member chat ...` commands

Agent への必須指示:

- 最初に `guildbotics member context --person <person_id>` を実行する。
- Slack token / raw Slack API / 直接 token 書き込みを使わない。
- Slack への投稿 / reaction は必ず `guildbotics member chat ...` 経由にする。
- 投稿不要なら `member chat noop` を実行する。
- 最後に `member chat complete` を実行する。
- `member chat complete` が失敗した場合、成功応答を返さない。
- 最終 stdout は `AgentResponse` JSON だけにする。
- `AgentResponse.message` は workflow 用の中立 summary とし、member 口調にしない。

## 実装タスク

実装は次の順序で行う。段階リリース用の中間互換層は作らないが、この順序にすると依存関係が自然に解ける。

1. `RunStore` 汎用化と既存 ticket tests の更新。
2. `MemberChatCapabilityService` と `guildbotics member chat ...` CLI の追加。
3. `handle_chat_event.{ja,en}.md` の追加。
4. `chat_conversation_workflow.py` の委譲型への作り直し。
5. 旧 chat prompt / helper / tests の削除。
6. skill / README / docs の更新。
7. Python 品質チェックと関連 pytest。

### 1. Run store を chat に対応させる

- `guildbotics/capabilities/task_runs.py` を `RunStore` 相当に汎用化する。
- `chat_runs.py` は新設しない。
- `ticket_url` 固定の field 名を subject 汎用 field へ置き換える。
- ticket run と chat run の subject schema を実装する。
- evidence type に `chat_reply` / `chat_post` / `chat_reaction` / `chat_noop` を追加する。
- `done` / `asking` / `blocked` ごとの required evidence を定義する。
- secret redaction を維持する。
- 既存 ticket workflow の completion 検証が重複コードにならないように共通化する。

### 2. `MemberChatCapabilityService` を追加する

- `guildbotics/capabilities/member_chat.py` を追加する。
- `Person` / `Team` / `logger` から `ChatService` を生成する。
- Slack identity / channel resolve / post / reply / reaction を service method として実装する。
- service result を stable JSON に正規化する。
- `aclose()` を実装し、Slack HTTP client を閉じる。
- Slack API error は secret を含まない `MemberCapabilityError` 相当に変換する。

### 3. `guildbotics member chat ...` CLI を追加する

- `guildbotics/cli/member.py` に `chat` group を追加する。
- `identity` / `post` / `reply` / `reaction add` / `noop` / `complete` を実装する。
- 長文入力は `--body-file` / `--body-stdin`、`--summary-file`、`--reason-file` を使う。
- `--run-id` がある write command では evidence を記録する。
- `member context` の `available_member_commands` に chat commands を追加する。
- 出力形式は write command を JSON default、identity を markdown or JSON にする。

### 4. `handle_chat_event` prompt を追加する

- `guildbotics/templates/commands/functions/handle_chat_event.ja.md` を追加する。
- `guildbotics/templates/commands/functions/handle_chat_event.en.md` を追加する。
- `brain: agent` を指定し、ticket workflow の `handle_github_ticket` と同じ AI CLI tool 委譲経路を使う。
- `response_class: guildbotics.intelligences.common.AgentResponse` を指定する。
- 既存の `chat_reply_actionable` / `chat_followup_should_reply` / `chat_reply_intent` / `chat_thread_context` の役割を統合する指示にする。

### 5. `chat_conversation_workflow` を薄く作り直す

- 旧 `_build_reply_text_via_command` 中心の分散 invoke を削除する。
- Workflow 内で deterministic gate を行う。
- agent prompt 用 payload を 1 回で構築する。
- `workflow_run_id` を生成する。
- `cli_agent_env` に run id と data dir を渡す。
- `cwd` は `get_workspace_path(person_id)` にする。
- `context.invoke("functions/handle_chat_event", ...)` を実行する。
- agent 終了後に chat run completion を検証する。
- evidence に基づき processed event / thread state / posted bot message を更新する。
- no-op completion の場合は Slack state に bot message を追加しない。
- 置き換えで不要になる helper / prompt 呼び出し / fallback reply は削除する。

### 6. `should_react` と chat prompt 群を整理する

- deterministic gate として必要な処理だけ残す。
- LLM 判定用の `chat_followup_should_reply` は `handle_chat_event` に統合して削除する。
- `chat_reply_intent` / `chat_thread_context` / `chat_reply_actionable` も `handle_chat_event` に統合して削除する。
- workflow から複数 prompt を順に呼ぶ構造は残さない。

### 7. Slack service を member capability から使う

- `member chat inspect thread` / `member chat inspect channel` を実装する。
- Slack `conversations.replies` 相当 API は `member chat inspect thread` からだけ使う。
- Workflow payload は routing に必要な incoming event metadata に絞り、Slack thread 本文は渡さない。
- Agent は `member chat inspect thread` の結果だけを thread 本文の正として判断する。
- `SlackChatService` の inspect / post / reaction の result payload を member capability で使いやすい形に保つ。

### 8. Skill を更新する

- `skills/guildbotics/SKILL.md` に chat member capability を追加する。
- Slack chat flow を追加する。
- GitHub/git write と同様、Slack write も `guildbotics member chat ...` 経由に限定する。
- Workflow run では `guildbotics_execution_mode=workflow` を見て、workflow prompt を主契約として扱う補助ガードレールにする。`SKILL.md` に ticket / chat workflow の詳細手順は重複して持たせない。
- Interactive run では active member session の会話スタイルを使い、Slack 投稿本文は `communication_style` に従う。

### 9. README / docs を更新する

- `README.ja.md` / `README.md` の Slack chat workflow 節を更新する。
- Chat workflow が member capability 経由で Slack write を行うことを記載する。
- `guildbotics member chat ...` の interactive use を記載する。
- scheduled post は今回の対象外であり、既存 `chat_post_command` は次の独立タスクの整理対象であることを記載する。
- docs/spec の ticket plan に残っている「chat は scope 外」記述と矛盾しないよう、今回の計画への参照を追加する。

### 10. テストを更新する

- `tests/guildbotics/capabilities/test_member_chat.py`
  - identity / post / reply / reaction / noop / complete
  - evidence 記録
  - secret redaction
  - Slack API error の safe error
- `tests/guildbotics/cli/test_member_command.py`
  - `member chat ...` CLI contract
  - body file / stdin の排他
  - run id あり/なし
  - missing file / empty body
- `tests/guildbotics/templates/commands/workflows/test_chat_conversation_workflow.py`
  - incoming event が `handle_chat_event` に委譲される
  - reply evidence で processed event / thread state が更新される
  - reaction-only が完了扱いになる
  - noop が Slack 投稿なしで完了扱いになる
  - completion なしは成功扱いしない
- `tests/guildbotics/templates/commands/workflows/test_should_react_command.py`
  - `should_react.py` を削除するなら、このテストも削除する。
  - deterministic gate helper を workflow-local に残すなら、`test_chat_conversation_workflow.py` 側へ必要な assertion を移す。
- `tests/guildbotics/integrations/slack/test_slack_chat_service.py`
  - 追加 API があれば Slack request payload を検証する。

### 11. 不要コードを削除する

置き換え後に不要になるものは残さない。

- `chat_reply_actionable` prompt
- `chat_followup_should_reply` prompt
- `chat_reply_intent` prompt
- `chat_thread_context` prompt
- workflow 内の fallback placeholder reply
- 分散 invoke 用 helper
- 削除対象 prompt の tests

## 完了条件

- Incoming Slack event は `chat_conversation_workflow -> CLI agent -> guildbotics member chat ...` の経路で処理される。
- Slack post / reply / reaction は workflow から直接実行されない。
- Agent が `member chat complete` を記録しない場合、workflow は成功扱いしない。
- `chat_reply` / `chat_reaction` / `chat_noop` の各 evidence で expected state update が行われる。
- no-op は Slack に何も投稿しない。
- reaction-only は正式な完了として扱われる。
- Interactive session から `guildbotics member chat ...` を使って member として Slack 投稿できる。
- 旧 LLM prompt 呼び出しの分散構造は削除され、`handle_chat_event` に統合されている。
- README / skill / tests が新経路に揃っている。

## 受け入れテスト

受け入れテストは unit / integration test とは別に、実 Slack workspace と実 CLI agent で行う。Slack token や app token の値はログ、prompt、スクリーンショット、テスト記録に残さない。

### 1. インタラクティブモード（workflow を使わない）

目的: Codex / Claude Code などの対話セッションで、利用者が自然文で依頼したときに、GuildBotics skill が member context を確認し、内部実行コマンドとして `guildbotics member chat ...` を選び、member として Slack に参加できることを確認する。このモードで検証する自然文から command への変換は GuildBotics 本体の自然言語 parser ではなく、Codex / Claude Code が読み込む GuildBotics skill の責務とする。

前提:

- Desktop app または `guildbotics workspace use <workspace>` で active workspace が設定済み。
- 対象 member の `.env` に `{PERSON_ID}_SLACK_BOT_TOKEN` が設定済み。
- 対象 Slack channel に bot が招待済み。
- Codex / Claude Code から GuildBotics skill を利用できる。
- Agent から `~/.guildbotics/bin/guildbotics` が実行できる。

手順:

1. Codex / Claude Code の通常対話セッションで、対象 repository または任意の作業ディレクトリを開く。
2. ユーザーは CLI コマンドを明示せず、自然文で次のように依頼する。

```text
GuildBotics skill を使って、<person_id> として Slack の次のメッセージリンクのスレッドに返信してください。
<Slack message link>
返信内容は、<返信したい内容> です。
```

3. Agent がユーザーに CLI コマンドの組み立てを求めず、最初に member context を確認することを確認する。期待される内部コマンドは `guildbotics member context --person <person_id>` 相当。
4. Agent が必要に応じて Slack identity を確認することを確認する。期待される内部コマンドは `guildbotics member chat identity --person <person_id> --service slack` 相当。
5. Agent が Slack message link を `--message-url` として渡し、まず `guildbotics member chat inspect thread ...` で対象 thread の内容を取得することを確認する。
6. Agent が取得した thread 内容を踏まえて返信本文を member の `communication_style.interactive_replies` に従って作成し、body file または stdin 経由で `guildbotics member chat reply ...` を実行することを確認する。ユーザーが `channel_id` / `thread_ts` や `guildbotics member chat reply` の完全なコマンド列を指示しなくても、skill が適切な member command へ変換することを確認する。
7. Slack 上で、対象 thread に member の返信が投稿されたことを確認する。
8. チャネル内容の確認も自然文で依頼する。

```text
GuildBotics skill を使って、<person_id> として Slack の #<channel_name> の直近 2 時間の内容を確認し、必要なら要点を教えてください。
```

9. Agent が channel 名を `--channel-name` として渡し、自然言語の期間を Slack timestamp に変換して `guildbotics member chat inspect channel ...` を内部で実行することを確認する。
10. チャネルへの直接投稿も自然文で依頼する。

```text
GuildBotics skill を使って、<person_id> として Slack の #<channel_name> に直接投稿してください。
投稿内容は、<投稿したい内容> です。
```

11. Agent が channel 名を `--channel-name` として渡し、`guildbotics member chat post ...` を内部で実行することを確認する。
12. Slack 上で、対象 channel に member の投稿が追加されたことを確認する。
13. reaction-only も自然文で依頼する。

```text
GuildBotics skill を使って、<person_id> として Slack の次のメッセージリンクに acknowledgment の reaction だけ付けてください。
<Slack message link>
```

14. Agent が `ack` / `agree` / `celebrate` / `support` の semantic reaction へ正規化し、Slack message link を `--message-url` として渡して `guildbotics member chat reaction add ...` を内部で実行することを確認する。
15. Slack 上で semantic reaction が Slack emoji に変換され、message に追加されたことを確認する。

期待結果:

- Interactive run では、自然文依頼から skill 経由で `inspect thread` / `inspect channel` / `reply` / `post` / `reaction add` が実行できる。
- 既存 Slack thread への返信依頼では、agent が返信前に `inspect thread` で内容を確認する。
- ユーザーが完全な `guildbotics member chat ...` コマンドを提示しなくても、agent が member context と skill 指示に基づいて適切な command を選ぶ。
- ユーザーが Slack channel id、`thread_ts`、`message_ts` を調べなくても、channel 名と Slack message link から実行できる。
- workflow run id が無くても interactive command として実行できる。
- Slack write は `guildbotics member chat ...` 経由でのみ行われ、raw Slack API / token 直接利用を行わない。
- 長文本文は argv ではなく body file または stdin で渡せる。
- 出力 JSON に token / secret / private key などが含まれない。
- member context の `communication_style` に従った本文を agent が作成できる。

### 2. ノンインタラクティブモード（workflow 経由）

目的: Slack incoming event が `chat_conversation_workflow -> CLI agent -> guildbotics member chat inspect thread -> guildbotics member chat ... -> member chat complete` の経路で処理され、workflow が run evidence だけを正として local state を更新することを確認する。

前提:

- Desktop app または `guildbotics workspace use <workspace>` で active workspace が設定済み。
- 対象 member の `.env` に `{PERSON_ID}_SLACK_BOT_TOKEN` と `{PERSON_ID}_SLACK_APP_TOKEN` が設定済み。
- Slack App の Socket Mode / Event Subscriptions が有効。
- 対象 channel が `person.yml` の `message_channels` に設定され、`chat.enabled: true`。
- 対象 member の CLI agent が `guildbotics member chat ...` を実行できる。

手順:

1. `guildbotics start` で event listener runner を含めて起動する。`--only scheduler` は使わない。
2. Slack で対象 bot に明示 mention する message を投稿する。
3. CLI agent の実行ログまたは diagnostics で、`functions/handle_chat_event` が member workspace root を `cwd` として起動されたことを確認する。
4. Agent が最初に `guildbotics member context --person <person_id>` を実行することを確認する。
5. Agent が返信・reaction・no-op 判断前に `guildbotics member chat inspect thread --person <person_id> --service slack --channel-id <channel_id> --thread-ts <thread_ts>` を実行し、取得した thread messages を読んで判断することを確認する。
6. Agent が Slack write に `guildbotics member chat reply` または `guildbotics member chat reaction add` または `guildbotics member chat noop` を使うことを確認する。
7. Agent が最後に `guildbotics member chat complete --run-id <workflow_run_id> ...` を実行することを確認する。
8. `chat_reply` case: Slack thread に返信が投稿され、local state に processed event と bot message が保存されることを確認する。
9. `chat_reaction` case: Slack message に reaction が追加され、processed event は保存されるが bot message は追加されないことを確認する。
10. `chat_noop` case: Slack には何も投稿されず、processed event は保存されることを確認する。
11. `inspect thread` を失敗させ、agent が Slack へ投稿・reaction せず、safe summary で `blocked` complete することを確認する。
12. `complete` を実行しない失敗 agent を一時的に設定し、workflow が success 扱いにしないことを確認する。

期待結果:

- Workflow は Slack post / reply / reaction を直接呼ばない。
- Workflow は Slack thread 本文を直接取得しない。thread 取得は agent が `guildbotics member chat inspect thread` 経由で行う。
- Agent は `inspect thread` に失敗した場合、不完全な local cache に fallback せず、Slack へ投稿・reaction せず、`blocked` complete する。
- Workflow は agent stdout ではなく `RunStore` の completion / evidence を検証して success を判断する。
- `done` は `chat_reply` / `chat_post` / `chat_reaction` / `chat_noop` のいずれでも完了できる。
- `asking` は `chat_reply` または `chat_post` evidence が無い場合に fail-closed する。
- `blocked` は safe summary があれば write evidence 無しで完了でき、secret を含まない。
- 同一 Slack event の再配送では processed event により二重投稿しない。

## 必須確認コマンド

Python 実装後は最低限次を実行する。

```bash
uv run --no-sync ruff format --check guildbotics
uv run --no-sync ruff check guildbotics
uv run --no-sync mypy guildbotics
uv run --no-sync pylint guildbotics
uv run --no-sync python -m pytest \
  tests/guildbotics/capabilities/test_member_chat.py \
  tests/guildbotics/cli/test_member_command.py \
  tests/guildbotics/templates/commands/workflows/test_chat_conversation_workflow.py \
  tests/guildbotics/integrations/slack/test_slack_chat_service.py \
  tests/guildbotics/templates/commands/workflows/test_ticket_driven_workflow.py \
  tests/guildbotics/capabilities/test_task_runs.py
```

`ruff` の対象は AGENTS.md の「エージェント作業時の品質確認」に合わせて `guildbotics` とする。テストコードは関連 pytest で挙動を検証し、今回の変更対象テストに明らかな整形・lint 問題を持ち込まない範囲で確認する。既存の無関係な `tests/` 全体 lint をこの計画の完了条件にはしない。

`RunStore` 汎用化で ticket workflow 側へ影響が出るため、chat 関連テストだけで完了にしない。

## 非対象

- scheduled post workflow の全面置き換え。
- Slack 以外の chat provider。
- Slack App / Socket Mode 設定 UI の刷新。
- token exfiltration の技術的封じ込め。
