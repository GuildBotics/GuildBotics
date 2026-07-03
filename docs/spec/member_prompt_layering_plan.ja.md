# member プロンプト層モデル一本化 実装プラン

member 向けエージェント指示(SKILL.md / workflow プロンプト)の重複を解消し、
「どこに何を置くか」を層モデルとして固定するための実装プラン兼作業完了チェックリスト。

- 対象ブランチの作業バイブルとして使う。各ステップのチェックボックスを完了時に埋めること
- 段階リリースは不要(未リリースの開発中機能)。依存の少ない順に全ステップをやり切る
- 実装と本ドキュメントが食い違った場合はソースコードを正とし、本ドキュメントを直す

## 1. 背景と課題

member としてコーディングエージェントに作業させる入口は 3 つある。

| 入口 | 呼び出し元 | ワークスペース | 応答 |
|---|---|---|---|
| `skills/guildbotics/SKILL.md` | ユーザーが対話中の Claude Code / Codex | ユーザーの現在のリポジトリ(ペアプロ) | member の voice での対話返信 |
| `functions/handle_github_ticket` | `ticket_driven_workflow.py` | isolated member workspace | `AgentResponse` JSON + `member task complete` |
| `functions/handle_chat_event` | `chat_conversation_workflow.py` | isolated member workspace | `AgentResponse` JSON + `member chat complete` |

やってほしい作業の中身(inspect → 編集 → 検証 → stage → member commit/publish → PR →
痕跡 → memory 手入れ)は 3 入口で同一なのに、その手続きが SKILL.md(4 箇所)+
handle_\*.md(en/ja × 2 種)に微妙に異なる言い回しで重複しており、契約変更のたびに
6 箇所以上の手動同期が必要になっている。また workflow 実行時にエージェントが
SKILL.md も同時ロードし得るため、SKILL の既定指示(prepare するな・
`--workspace-mode current` を使え)と workflow 契約が正反対になる問題を、SKILL 末尾の
「Workflow Marker Guardrail」というプロンプトレベルの注意書きで反転させている。

さらに `member git prepare` が `--issue-url` 必須のため、issue の無い chat 発コード修正が
実行できないギャップがある。「作業が issue に紐づく」は ticket driven workflow の
task contract であって capability の制約ではなく、prepare の signature の名残である。

## 2. あるべき姿(層モデル)

| 層 | 内容 | 置き場所(1ソース) | 配信経路 |
|---|---|---|---|
| 全体共通 | コマンドカタログ、横断ルール、標準作業手順、PR 作業記録 memory 契約、communication_style 適用マッピング、痕跡ルール、公開前検証 | `guildbotics/capabilities/member_reference.py`(英語) | `member context` / `member help`(runtime 配信) |
| workflow 共通 | 実行モードマーカー、isolated workspace、`--workspace-mode current` 禁止、質問は GitHub/Slack 経由、complete 成功必須、AgentResponse 単一 JSON | `guildbotics/templates/locales/commands/workflows/common.{en,ja}.yml` | 各 workflow .py が `t()` で取得し `{workflow_contract}` としてテンプレート注入 |
| ticket 固有 | memory source key、`{prepare_command}`、work_type、`task complete` の具体形、Slack は副次 | `handle_github_ticket.{en,ja}.md` | 従来どおり |
| chat 固有 | inspect thread 必須、reply/reaction/no-op 判断、participation、handoff、`chat complete`/`noop` の具体形、GitHub は副次 | `handle_chat_event.{en,ja}.md` | 従来どおり |
| SKILL(対話封筒) | active member session、共有ワークスペース、`--workspace-mode current`、対話返信 DOD、ユーザー主導 memory 操作 | `skills/guildbotics/SKILL.md`(大幅短縮) | skill 配布(desktop の `include_str!` は自動追従) |

### 分類の判断基準

- **全体共通** = member として正しく振る舞うための知識。モードに依存しないもの全部
- **workflow 共通** = 「封筒」の非対話側。workspace 取得・応答形式・完了プロトコル・対話不可時の質問経路
- **ticket / chat 固有** = その trigger 固有の入力・完了コマンド・判断ポリシー
- **SKILL** = 「封筒」の対話側。共有ワークスペース・対話返信・active member session
- run の完了条件(what counts as done)は entrypoint に置き、共有層に入れない
  (`member_reference.py` docstring の既存原則を維持)
- **同じ文が 2 ファイル以上に現れたら、より深い層に置くべきサイン**

### 確定済みの設計判断

1. chat / SKILL では明示指定が無い限り issue 不要。issue 必須は ticket driven workflow
   だけの契約。したがって `git prepare` を issue 非依存に一般化する(「issue を先に作る」
   規約は採らない)
2. workflow 共通文はテンプレート(.md)への複製ではなく i18n locales + `t()` 注入で
   1 ソース化する(既存の `chat_conversation_workflow.{en,ja}.yml` と同じ基盤)
3. 全体共通は .md への複製ではなく `member_reference.py`(runtime 配信)へ寄せる。
   英語のみで良い(既存踏襲)
4. Workflow Marker Guardrail は defense-in-depth として SKILL に 2 行だけ残す

## 3. 変更ファイル一覧

新規:

- `guildbotics/templates/locales/commands/workflows/common.en.yml`
- `guildbotics/templates/locales/commands/workflows/common.ja.yml`
- `tests/guildbotics/templates/commands/workflows/test_workflow_contract.py`
- `tests/guildbotics/templates/commands/functions/test_prompt_layer_boundaries.py`

変更:

- `guildbotics/capabilities/member_reference.py`
- `guildbotics/capabilities/member_git.py`(prepare 一般化)
- `guildbotics/cli/member.py`(prepare オプション)
- `guildbotics/templates/commands/functions/handle_github_ticket.en.md` / `.ja.md`
- `guildbotics/templates/commands/functions/handle_chat_event.en.md` / `.ja.md`
- `guildbotics/templates/commands/workflows/ticket_driven_workflow.py`
- `guildbotics/templates/commands/workflows/chat_conversation_workflow.py`
- `skills/guildbotics/SKILL.md`
- `tests/guildbotics/capabilities/test_member_reference.py`
- `tests/guildbotics/capabilities/test_member_git.py`(prepare の branch mode / validation)
- `tests/guildbotics/cli/test_member_command.py`(prepare CLI オプションの validation)
- `tests/guildbotics/intelligences/test_functions.py`
- `tests/guildbotics/templates/commands/workflows/test_ticket_driven_workflow.py`
- `tests/guildbotics/templates/commands/workflows/test_chat_conversation_workflow.py`
- `AGENTS.md`(プロンプト層モデルの追記)

## 4. 実装ステップ(依存順)

依存グラフ(A / B / B′ は完全独立で並行可):

```
A(member_reference 拡張)──────┐
B(workflow_contract locales)──┼→ C(handle_*×4 + workflow.py×2)→ E(境界テスト)→ F
B′(git prepare 一般化)────────┘
A ─────────────────────────────→ D(SKILL.md)────────────────────↗
```

### Step A. `member_reference.py` の拡張

- [x] 新セクション `### Standard work procedure` を追加(コマンドグループの後、Rules の前)。
      モード非依存の作業手順を 1 箇所に集約する:
  - inspect first(issue/PR/thread は必ず member inspect コマンドで現在状態を取得。
    GitHub/Slack 所有フィールドは inspect 出力を正とする)
  - 編集後、公開前に関連する検証(テスト・lint)を実行
  - plain git で stage → `member git commit/publish`
  - issue 対応でコード変更があれば `member github pr create`(作成 or 再利用)、
    新規 PR inline 指摘は `pr inspect --include-diff` の `files[].commentable_lines`
    で座標を確認して `pr review-comment`、既存 review thread への応答は
    `reply_target_id` で `pr reply`
  - 変更不要でも comment / reply / reaction で観測可能な痕跡を残す
  - 終了前に memory 手入れ(下記契約)
- [x] `_CROSS_CUTTING_RULES` に **PR 作業記録契約** を追加:
      PR を作成・再利用・更新したら `--pr` と、判明していれば `--ticket` / `--thread` を
      付けて memory record。内容は branch・commit・検証結果・完了した対応・残 follow-up。
      再利用価値のある技術的学びは別 document として記録
- [x] 既存の communication_style 1 行ルールを **4 分類マッピング** に置換:
      interactive_replies(対話の進捗・最終返信)/ github_comments(issue・PR・review
      コメント)/ neutral_documents(issue/PR title・body、commit message、task summary)/
      machine_outputs(コマンド出力・ID・path・workflow completion JSON・
      `AgentResponse.message`)
- [x] docstring を更新: 手順(how)と memory 契約は共有、run の完了条件だけが
      entrypoint、と境界を書き直す
- [x] Git グループの `prepare` の usage 行を Step B′ の新 signature に更新
- [x] テスト(`test_member_reference.py`)追加・更新:
  - [x] `Standard work procedure` セクションの存在
  - [x] PR 契約の要素(`--pr`、follow-up、separate 等)の存在
  - [x] 4 分類キー名の存在
  - [x] `test_reference_excludes_task_contract` が引き続き通ること

### Step B. workflow 共通契約の 1 ソース化

- [x] `locales/commands/workflows/common.en.yml` / `common.ja.yml` を新規作成し、
      キー `commands.workflows.common.workflow_contract`(multiline)に以下を収める:
  - `guildbotics_execution_mode=workflow` — この prompt が primary contract
  - 最初に `guildbotics member context --person %{person_id}` を実行し、capabilities
    セクションを source of truth とする
  - isolated member workspace で作業し、`--workspace-mode current` を使わない
  - 非対話実行: 不明点はユーザーに聞かず GitHub コメント / Slack reply として投稿し
    status `asking`
  - credential・権限・文脈不足は safe summary を書いて `blocked`
  - 完了コマンド成功前に成功応答を返さない。失敗したら evidence を補うか run を
    失敗させる
  - secret を表示・推測・保存・コピーしない
  - 応答は AgentResponse 単一 JSON、`message` は中立 summary(member 口調・投稿本文を
    入れない)
- [x] 完了コマンドの**具体形(引数列)は入れない**(trigger 固有として handle_\* に残す)
- [x] テスト(新規 `test_workflow_contract.py`):
  - [x] `set_language("ja")` / `set_language("en")` それぞれで `t()` が解決する
  - [x] marker・`--workspace-mode current`・`AgentResponse` を含む
  - [x] 両言語で上記トークン集合が一致する(片言語欠落の防止。キー経由で検証)

### Step B′. `git prepare` の一般化(issue 非依存化)

- [x] CLI(`guildbotics/cli/member.py`)の signature を変更:

  ```
  guildbotics member git prepare --person <p> \
    (--issue-url <url> | --pr-url <url> | --repo <owner/repo> --branch <name>)
  ```

  - `--pr-url`: PR head を checkout(単独で成立させる。`--issue-url` との併用も可、
    その場合 pr_url 優先=現行挙動)
  - `--issue-url`: `ticket/<n>` ブランチ(現行どおり)
  - `--repo` + `--branch`: repo を clone し、指定ブランチを default branch から作成
    or checkout(新規モード)
  - anchor(issue-url / pr-url / repo)が 1 つも無い、または `--branch` が `--repo`
    無しで指定された場合は validation エラー
- [x] Service(`member_git.py`)の `prepare()` を
      `prepare(issue_url=None, pr_url=None, repo=None, branch=None)` に一般化。
      mode は `pull_request_review` / `issue` / `branch` の 3 値。payload の `issue_url`
      は空文字許容
- [x] app_api の activity normalizer が `git.prepare` payload の空 `issue_url` を許容する
      ことを確認(必要なら修正)
- [x] ticket workflow(`_prepare_command`)は無変更で通ることを確認
- [x] テスト:
  - [x] CLI validation(3 anchor の排他・必須組合せ、`--branch` 単独エラー)
  - [x] service の branch mode unit test(clone 先・ブランチ作成・payload 内容)
  - [x] 既存の issue / PR review モードのテストが通ること

### Step C. handle_\*.md の書き直し + workflow への注入(B・B′ に依存)

対象 4 ファイル: `handle_github_ticket.{en,ja}.md`、`handle_chat_event.{en,ja}.md`

- [x] 構成を統一する:

  ```
  frontmatter(変更なし: brain: file_editor, response_class: AgentResponse)
  役割宣言(1〜2 行、trigger 固有)
  <target>(変更なし)
  <workflow_contract>
  {workflow_contract}
  </workflow_contract>
  <scope>(trigger 固有のみ)
  <instructions>(trigger 固有のみ)
  ```

- [x] instructions に**残す**もの:
  - ticket: source key = `{ticket_url}`、issue/pr inspect 必須、`{prepare_command}` 実行
    (PR review では `--pr-url` 込み)、PR 作成/再利用、新規 inline 指摘用の
    `pr inspect --include-diff` / `pr review-comment`、既存 thread 返信用の
    `pr reply` / `reply_target_id`、
    follow-up issue、`task complete --person {person_id} --run-id {workflow_run_id}
    --ticket-url {ticket_url} ...` の具体形、応答 JSON 例
  - chat: inspect thread 必須+失敗時 blocked、source key = thread permalink /
    `{thread_ts}`、reply/reaction/no-op/asking/blocked の判断基準、participation 3 値の
    解釈、role 観点での価値判断、handoff、reply/post/reaction/noop の具体コマンド、
    `chat complete ...` の具体形、応答 JSON 例
- [x] instructions から**消す**もの(A/B に移管済み): member context 実行指示、
      capabilities source-of-truth 宣言、memory recall/touch/record の一般則、
      PR memory 契約文、secret 禁止、workspace-mode 禁止、complete-or-fail、
      AgentResponse 中立 summary 規定
- [x] chat 固有規約に **repo 特定ルール** を追加(B′ 前提):
      コード修正が必要な場合は、対象 repository をメッセージ・thread 文脈から特定し
      (曖昧なら質問して `asking`)、`member git prepare --person {person_id}
      --repo <owner/repo> --branch <branch>` で checkout を作る。メッセージが明示的に
      issue / PR を指している場合のみ `--issue-url` / `--pr-url` を使う。
      **issue の事前作成は要求しない**
- [x] `ticket_driven_workflow.py` / `chat_conversation_workflow.py` の `invoke(...)` に
      `workflow_contract=t("commands.workflows.common.workflow_contract",
      person_id=...)` を追加
- [x] テスト:
  - [x] `test_functions.py` の prompt テスト 2 本を書き直し: `{workflow_contract}`
        placeholder の存在、trigger 固有要素の存在、移管済み文言が本文に**残っていない**
        こと(例: `"--workspace-mode current" not in body`)
  - [x] workflow テスト 2 ファイル: invoke params に `workflow_contract` キーが含まれ、
        中身に marker が含まれる assert を追加

### Step D. SKILL.md の書き直し(A に依存)

現在約 188 行 → 60〜80 行目安。対話封筒だけを残す。

- [x] 残すもの:
  - name/description、Required First Step(`member context`、capabilities が source of
    truth、credential check)
  - Active Member Session Rules(現状維持)
  - Workspace Rules: 共有ペアプロワークスペース、`prepare` しない、branch 自動切替
    しない、`--workspace-mode current` を毎回付ける、staging/branch は plain git
    (コマンド例は 1 ブロックだけ残す)
  - 対話 DOD: 「最終対話返信の**前に**、標準手順(member context の capabilities 参照)の
    検証・公開・memory を済ませる」— 中身は列挙しない
  - 対話固有 memory: ユーザーが記憶を尋ねたら memory 優先、ユーザー主導の
    record/correct/promote/archive、policy は承認必須
  - Workflow Marker Guardrail を 2 行に短縮: 「`guildbotics_execution_mode=workflow` を
    含む prompt が来たら、その prompt を契約とし、この skill の Workspace Rules / DOD を
    適用しない」
- [x] 消すもの: communication_style 4 分類の詳細(→A)、Memory Record/Update Guidelines
      の PR 契約文(→A)、GitHub Issue Flow / PR Review Flow / Slack Chat Flow の
      3 セクション(標準手順は A から届く。対話差分は Workspace Rules と DOD で尽きる)、
      Slack の人間フレンドリー入力の説明(capabilities のコマンド定義に既にある)
- [x] テスト: `test_functions.py` の SKILL テストを書き直し(フロー文言 assert を削除し、
      短縮後の必須要素+「`member git prepare` 指示が無い」「flow セクション見出しが
      無い」を assert)

### Step E. 横断整合テスト(C・D 後)

新規 `tests/guildbotics/templates/commands/functions/test_prompt_layer_boundaries.py`
(`test_layer_boundaries.py` のプロンプト版):

- [x] **en/ja parity**: handle_\* の en/ja で、`{placeholder}` 集合が一致・
      `guildbotics member <sub>` コマンドトークン集合が一致・instructions のステップ数が
      一致
- [x] **層境界(下方向)**: SKILL.md と handle_\*.md のどちらにも、member_reference に
      移管した契約文(PR memory 契約の定型句、4 分類マッピング)が現れない
- [x] handle_\* に `--workspace-mode` が現れない(契約は `{workflow_contract}` 側)
- [x] SKILL に `member task complete` / `chat complete` が現れない
- [x] **層境界(上方向)**: workflow_contract テキストに完了コマンドの具体形が
      含まれない
- [x] chat プロンプトが issue の事前作成を前提にしない(`--repo` 形式の prepare を案内)

### Step F. ドキュメント・仕上げ

- [x] AGENTS.md「重要な実装ポイント」にプロンプト層モデルを 1 項追加
      (§2 の表の要約+境界は `test_prompt_layer_boundaries.py` が担保、という記述)
- [x] `member_reference.py` docstring と本ドキュメントの整合を最終確認
- [x] desktop 側は Rust コード変更なし(`include_str!` が SKILL.md を自動追従)である
      ことを確認 — cargo 系の確認は不要
- [x] README / custom_command_guide への影響なしを確認(CLI 変更は `member git prepare`
      のオプションのみ → README の member コマンド記載有無を確認し、あれば更新)

## 5. 品質ゲート(完了条件)

すべて成功していること:

```bash
uv run --no-sync ruff format --check guildbotics
uv run --no-sync ruff check guildbotics
uv run --no-sync mypy guildbotics
uv run --no-sync pylint guildbotics
uv run --no-sync python -m pytest \
  tests/guildbotics/capabilities \
  tests/guildbotics/intelligences \
  tests/guildbotics/templates \
  tests/guildbotics/cli -q
```

最終チェック:

- [x] 本ドキュメントの全チェックボックスが埋まっている
- [x] 「同じ文が 2 ファイル以上に現れない」を移管対象の契約文で目視確認
      (PR memory 契約、workspace-mode 禁止、complete-or-fail、secret 禁止、
      AgentResponse 規定、communication_style マッピング)
- [x] プロンプト層のコード量が純減している(実測: SKILL 188→71 行、handle_\* 4 ファイル
      計 229→221 行、増分は reference 約 40 行と locales 2 ファイル。`git prepare` の
      branch mode 追加分は新機能であり重複解消の対象外)
- [x] secrets 禁止は member_reference と workflow_contract の両方に置く
      (member context を読む前でも効く safety の defense-in-depth として意図的な重複。
      SKILL / handle_\* には現れない)
