# Ticket Driven Workflow の Issue コメント UX 実装方針

「Ticket driven workflow の対応完了時にチケットにコメント追加されないケースがある」という不具合報告への対応として以下を実施する。

## 実装方針

### 1. `handle_github_ticket` を修正する

`guildbotics/templates/commands/functions/handle_github_ticket.ja.md` / `.en.md` に、次を明記します。

- Issue 起点で PR を作成・再利用・更新した場合は、元 Issue に `member github issue comment` で短い結果コメントを投稿する
- コメントには PR URL、実施概要、確認結果を含める
- `task complete --summary-file` は内部 summary であり、GitHub 投稿の代替ではない
- `AgentResponse.message` も GitHub 投稿の代替ではない

### 2. `member_reference` の標準作業手順にも追加する

agent は workflow prompt だけでなく `member context` を source of truth として読むので、`guildbotics/capabilities/member_reference.py` 側にも同じ UX 契約を入れるべきです。

個別 prompt だけだと、将来別 workflow や interactive member 作業でまた抜けます。

ただし、`member_reference` は workflow 個別の手順ではなく、member capability 全体の標準作業手順です。そこに「Issue 起点なら必ずコメント」と書くと、チケットコメントを書くべきでないケースでも過剰投稿しやすくなります。

なので、`member_reference` では強制文ではなく、**観測可能なアウトカムを残す原則**に留めます。

例:

> 完了時は、作業の入口に対応した場所に外部可視の痕跡を残す。Issue 起点のコード変更なら元 Issue または関連 PR、PR review 起点なら review thread / PR conversation、Slack 起点なら Slack thread を優先する。重複投稿や明示的に不要な投稿は避ける。

一方で、`handle_github_ticket` の workflow prompt にはもう少し具体的に書いてよいです。

> Issue 対応で PR を作成・再利用・更新した場合は、元 Issue に PR URL 付きの短い結果コメントを投稿する。ただし同じ run で既に同等のコメントを投稿済み、または ticket 本文/ユーザー指示がコメント不要を明示している場合は重複投稿しない。

分担はこうです。

- `member_reference`: 汎用原則。入口に対応した場所へ可視アウトカムを残す、重複投稿を避ける
- `handle_github_ticket`: Ticket Driven Workflow の具体ルール。Issue 起点 PR では原則 Issue コメント
- `AgentResponse.message`: GitHub 投稿の代替ではない

### 3. テストは prompt / 契約の退行防止に寄せる

この修正では実行時ロジックを変えないため、`TaskRunStore` や workflow retry のテストは増やさない。

追加・更新するテストは、契約文の置き場所と日英整合を守るための prompt / reference test に寄せる。

具体的には次を確認する。

- `tests/guildbotics/templates/commands/functions/test_prompt_layer_boundaries.py`
  - `handle_github_ticket.en.md` / `.ja.md` の両方に、Issue 対応で PR を作成・再利用・更新した場合は元 Issue に `member github issue comment` で結果コメントを投稿する趣旨が含まれること。
  - 結果コメントに PR URL と確認結果を含める趣旨が日英両方に含まれること。
  - `task complete --summary-file` と `AgentResponse.message` が GitHub 投稿の代替ではない趣旨が日英両方に含まれること。
  - この具体ルールは `handle_github_ticket` 固有であり、`handle_chat_event` へ混入していないこと。
- `tests/guildbotics/capabilities/test_member_reference.py`
  - `capability_reference_text()` の standard work procedure に、作業の入口に対応した場所へ外部可視の痕跡を残す汎用原則が含まれること。
  - 同じ汎用原則に、重複投稿や明示的に不要な投稿を避ける趣旨が含まれること。
  - `member_reference` 側には「Issue 起点なら必ずコメント」のような Ticket Driven Workflow 固有の強制ルールを入れないこと。

テストは、長い文面全文一致ではなく、責務境界を表す sentinel phrase を確認する程度に留める。文言修正のたびに壊れる brittle なテストにはしない。
