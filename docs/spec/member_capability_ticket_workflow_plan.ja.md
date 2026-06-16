# GuildBotics Member Capability による Ticket Workflow 簡素化 実装計画

## Summary

今回の一括 PR では、`ticket_driven_workflow` から git / GitHub / PR publish / review reply / follow-up issue 作成の実装を外し、CLI agent が GuildBotics 専用 CLI capability 経由で「メンバーとして」操作する構造へ移行する。

最終形は次の責務分担にする。

```text
ticket workflow
  -> ProjectV2 から起動対象を選ぶ
  -> member workspace root を cwd として CLI agent を起動する
  -> agent に ticket/PR URL と起動理由だけを渡す
  -> agent 終了後に member task completion を検証する
  -> agent failure / task completion failure 時だけ system comment を残す

CLI agent / skill
  -> ticket/PR を理解し、調査・編集・判断を行う
  -> GitHub/git 書き込みは guildbotics member ... のみを使う
  -> 最終的なコメント/PR/review reply/follow-up issue まで自分で publish する

GuildBotics member capability
  -> Person の credential / profile / role を使う唯一の操作境界
  -> GitHub/git/将来 chat 操作を service + CLI として提供する
  -> credential は active workspace / GUILDBOTICS_ENV_FILE から CLI 自身がロードする（後述）
```

段階リリースは不要なので、旧経路との互換層は作らない。PR 内では新 capability を先に実装し、最後に workflow を薄く差し替えて旧 publish ロジックを削る。

本計画で特に重要なのは次の3点であり、Final Architecture の冒頭でまとめて確定させる。

1. **Credential / config 解決**: agent subprocess の環境変数は剥奪される（Codex は `*TOKEN*` を全削除する）。そのため `guildbotics member ...` は agent の cwd や env 手渡しに依存せず、active workspace または明示 workspace から config / `.env` を自力ロードする。
2. **managed CLI / skill 配布**: Mac 版 desktop app は同梱済み `guildbotics-cli` と GuildBotics skill を初回起動時に安定した場所へ配置する。Codex / Claude Code / Gemini CLI / GitHub Copilot CLI からは `~/.guildbotics/bin/guildbotics` と user skill を使う。
3. **ガードレールは経路別**: 「CLI agent が `gh` など内部標準コマンドを直接使って *利用者自身のアカウント* で操作してしまう」ことを防ぐ仕組みは、ノンインタラクティブ経路（GuildBotics が agent を spawn）とインタラクティブ経路（利用者がデスクトップアプリを起動）で別物になる。1つの「強力なガードレール」で両経路を覆えると考えない。
4. **workflow の薄型化に伴う既存ガードの撤去**: 現行 `ticket_driven_workflow._main` は「agent が直接 commit したら `RuntimeError`」というインラインガードを持つ。新方式では agent が publish 経由で commit するため、このガードは撤去する。

## Final Architecture

### 新しい境界

- 新設する境界名は **member capability** とする。
- 実体は Python service 層 + `guildbotics member ...` CLI サブコマンド。
- スキルや CLI agent は GitHub token / GitHub App secret / PAT を直接受け取らない。
- GitHub/git 書き込みは、必ず `guildbotics member ... --person <person_id>` を通す。
- `gh` や利用者ローカル認証の直接利用はプロンプト + 経路別ガードレールで抑止するが、本質的な狙いは「`guildbotics member` を通したときだけ *メンバーの* アカウントで操作される」ことを保証することである。

### Credential / config 解決（最重要・必須設計）

`guildbotics member ...` は member の GitHub credential を**環境変数の継承に依存せず**解決する。これは机上の理想ではなく、次の実装事実から必須になる。

- `Person.get_secret(key)` は `os.environ["<PERSON_ID>_<KEY>"]`（例: `OTOTADANA_GITHUB_ACCESS_TOKEN`）だけを読む（`guildbotics/entities/team.py`）。無ければ `KeyError`。
- これらの env は `guildbotics run` / `start` 実行時に **cwd 直下の `.env`** を読み込んで設定される（`guildbotics/cli/__init__.py` の `_load_env_from_cwd`、`guildbotics/app_api/runtime.py` も `cwd / ".env"`）。固定の home ベース secret 置き場は存在しない。
- CLI agent の subprocess 環境は `CliAgentBrain._execute_script` の `_isolate_github_write_credentials` で GitHub 系 env が剥奪される。さらに **Codex は `*TOKEN*` / `*KEY*` / `*SECRET*` を含む env を model 実行シェルから全削除する**（`guildbotics/templates/intelligences/cli_agents/codex-cli.yml` のコメント参照）。
- agent の cwd は member workspace root になる（後述）。そこに `.env` は無い。

→ よって、agent が（特に Codex 内で）`guildbotics member ...` を呼ぶと、`OTOTADANA_GITHUB_ACCESS_TOKEN` は env から消えており、cwd にも `.env` が無いため `get_secret` が `KeyError` で落ちる。これを解消する設計を以下に確定する。

**設計（採用案 = active workspace + CLI 自力ロード）**

- Desktop app は workspace 選択時に active workspace state を `~/.guildbotics/data/active-workspace.json` へ保存する。
- `guildbotics workspace use <workspace_dir>` も同じ active workspace state を保存する。確認は `guildbotics workspace current` / `guildbotics workspace status`。
- `guildbotics member ...` は group 共通 option として `--workspace <workspace_dir>` を受け取る。これは受け入れテストや headless 運用で active workspace を使わず明示指定したい場合の入口。
- `guildbotics member ...` の workspace 適用優先順:
  1. `--workspace <workspace_dir>`
  2. 既に明示されている `GUILDBOTICS_CONFIG_DIR`
  3. cwd の `.guildbotics/config`
  4. active workspace state
  5. 既存 fallback（`~/.guildbotics/config` → package templates）
- workspace が選ばれた場合、`GUILDBOTICS_CONFIG_DIR=<workspace>/.guildbotics/config` を設定し、`<workspace>/.env` が存在すれば `GUILDBOTICS_ENV_FILE=<workspace>/.env` も設定する。
- これにより、CLI agent の skill から `/path/to/workspace` や env を毎回渡す必要はない。desktop app で選択済みの workspace が安定した共有状態になる。

- 環境変数 **`GUILDBOTICS_ENV_FILE`（secret を含む `.env` の絶対パス）** を導入する。
  - 名前に `TOKEN` / `KEY` / `SECRET` を含まないため Codex に削除されない。値はパスであり secret 値そのものではないため、secret を agent 環境に直接載せずに済む。
  - ただし agent が shell でそのパスを読める場合、`.env` の中身へ到達できる可能性は残る。本計画の防御対象は「利用者アカウントへのフォールバック防止」であり、token exfiltration の技術的封じ込めは今回 scope 外とする。
- `guildbotics member ...` は起動時に、次の優先順で `.env` を解決し、`os.environ` へロードしてから person/secret を解決する。
  1. workspace 適用で設定された `GUILDBOTICS_ENV_FILE`
  2. 明示済みの `GUILDBOTICS_ENV_FILE` が指す絶対パス
  3. （未設定時のフォールバック）cwd 直下の `.env`
  - どちらでも見つからず必要な secret も未設定なら **fail-closed**（「member credential を解決できません」という安全なメッセージで非ゼロ終了。生 token の入力を促したりはしない）。
- `_load_env_from_cwd`（および scheduler / app_api の起動経路）で `.env` をロードした際に、`os.environ["GUILDBOTICS_ENV_FILE"]` を**その `.env` の絶対パスに設定**する。これにより：
  - ノンインタラクティブ経路では、scheduler → agent → `guildbotics member` の孫プロセスまで `GUILDBOTICS_ENV_FILE` が自動伝播し、`guildbotics member` が同じ `.env` を再ロードできる。
  - secret 値そのものは agent 環境に置かない（パスのみ伝播）。
- インタラクティブ / 素の CLI 経路では、通常は desktop app が保存した active workspace を使う。headless や複数 workspace の切替では `guildbotics workspace use <dir>` または `guildbotics member --workspace <dir> ...` を使う。`GUILDBOTICS_ENV_FILE` / `GUILDBOTICS_CONFIG_DIR` の手動指定は fallback として残すが、通常 UX にはしない。
- `guildbotics member` は team/person 設定の解決に `GUILDBOTICS_CONFIG_DIR`（`guildbotics/utils/fileio.py` 既存）も使う。

この案は「メンバーのアカウントで操作させる」という目的に必要十分であり、常駐デーモン等は追加しない。`.env` ファイルが agent からも読める点は、本計画の脅威モデル（後述：守りたいのは *利用者アカウントへのフォールバック*。生 token の exfiltration は対象外）では問題にしない。token exfiltration まで技術的に封じたい場合は将来の hardening 課題とする。

### ガードレール・モデル（経路別）

元リクエストが要求する「強力なガードレール」は、「CLI agent が `gh` など内部標準コマンドを直接使い、*その端末で認証済みのアカウント（通常は利用者自身）* で GitHub を操作してしまう」ことの防止である。token の exfiltration 防止ではない。この狙いに対し、ガードレールは agent の起動経路ごとに分けて設計する。

**経路A: ノンインタラクティブ（GuildBotics が agent を spawn）**

scheduler / `guildbotics run` → ticket workflow → `context.invoke` → `CliAgentBrain._execute_script` で agent を起動する経路。

- 機械的ガードレール **あり**: 既存 `_isolate_github_write_credentials`（`guildbotics/intelligences/brains/cli_agent.py`）を**維持する**。`GH_TOKEN` / `GITHUB_TOKEN` / `GH_CONFIG_DIR` を剥がし、`GIT_ASKPASS` / `SSH_AUTH_SOCK` 等も外し、`GIT_CONFIG_GLOBAL=/dev/null`・`GIT_SSH_COMMAND` を no-key にする。
- 効果: プロンプトが破れて agent が `gh issue comment` や `git push` を叩いても、認証情報が無いため**失敗する**。利用者の `~/.config/gh` ログインにフォールバックできない。結果 agent は「失敗するか `guildbotics member` を使うか」に追い込まれ、利用者アカウントでの操作が物理的に発生しない。これがプロンプト破れに対する backstop。
- 注意: この機械層は `CliAgentBrain` の中にしか無いので、**経路A でのみ**働く。

**経路B: インタラクティブ（利用者が Claude Code / Codex デスクトップを起動）**

利用者がデスクトップアプリを起動し、その中で GuildBotics skill を使う経路。spawn するのはデスクトップアプリであり GuildBotics ではない。

- GuildBotics 側に機械的ガードレールは**作れない**（agent は利用者の `gh` ログイン・`GH_TOKEN`・ssh-agent をそのまま継承する）。
- 代替の機械的縛りとして **クライアント側のツール拒否設定**をスキル導入手順に同梱・明記する。
  - 拒否/承認必須にする対象: `gh`、`git push`、直接 token/API 書き込み。
  - Claude Code / Codex の具体的な設定 syntax は実装時に各クライアントの現行仕様を確認してから README に記載する。計画書では対象操作と期待効果を仕様とし、未検証の syntax を正として固定しない。
- これに加えて **人間の都度承認**が最終防壁になる（インタラクティブ経路の本質）。
- 事後検証性: member 操作は `guildbotics member` を通すと proxy agent には署名（`⚙<person_id>`）が付くため、想定外のアカウント/署名なしの投稿を後から識別できる。

**全経路共通（プロンプト層）**

- SKILL.md と `handle_github_ticket` プロンプトで「GitHub/git 書き込みは `guildbotics member ...` のみ。`gh` / 生 git / 直接 token 利用はしない」と明記する。

**正直な限界（docs/spec に明記する）**

- いずれも「完全な技術的禁止」ではない（agent が意図的に認証を再構成する余地は残る）。
- ただし**既定では利用者アカウントへのフォールバック経路が成立しない**状態にはできる：経路A は env 剥奪で、経路B はクライアント拒否設定 + 人間承認で。
- exfiltration（生 token 持ち出し）は本計画の防御対象外であることを明記する。

### 今回やる範囲

- GitHub ticket / PR / review / reaction / issue 作成 capability。
- git workspace prepare / commit / push capability。
- `guildbotics member` の active workspace / credential 自力ロードと fail-closed。
- ticket workflow の単一委譲化（HEAD コミットガード撤去を含む）。
- Codex / Claude 等から使える薄い GuildBotics skill source とクライアント拒否設定の導入手順。
- docs/spec/README/setup/diagnostics/test 更新。

### 今回やらない範囲

- chat workflow の全面 agent 委譲。
- Slack 用 `guildbotics member chat ...` サブコマンド。
- GitHub event-driven workflow。
- 作業記録永続化 #170。
- GitHub webhook receiver。
- token exfiltration の技術的封じ込め（将来 hardening）。

ただし CLI namespace と service 構造は、将来 `guildbotics member chat post/reply/react` を追加できる形にする。

## Public Interfaces

### CLI: member group 全体の共通仕様

- 既存 `guildbotics run` とは別に `member` group を追加する。肥大化を避けるため `guildbotics/cli/member.py` に実装し、`guildbotics/cli/__init__.py` から import 登録する（実装を分離すると決め切る）。
- group のエントリで `.env` を解決・ロードする（上記 Credential 解決の優先順）。`run` の `_load_env_from_cwd` 相当を member 用に拡張した共通関数を使う。
- すべての write command は `--person` 必須。
- body/title などの長文は argv で受けず、必ず `--*-file` を使う。
- command failure は `click.ClickException` に変換し、stderr に secret を出さない。
- JSON 出力は agent/テスト向けに安定 schema、Markdown 出力は人間/LLM 向け本文にする。
- default format: inspect/context は `markdown`、write/publish は `json`。

### CLI: member context

```bash
guildbotics member context --person <person_id> [--check-credentials] [--format markdown|json]
```

役割:

- 指定 member の非 secret context を表示する。skill / desktop agent が最初に呼ぶ想定。
- 出力に含める: `person_id` / `name` / `person_type` / active/default role summary・description / profile / `speaking_style` / LLM がそのまま従える `communication_style`（`interactive_replies` / `github_comments` / `neutral_documents` / `machine_outputs`）/ GitHub username / proxy agent signature（該当時）/ available member commands / safety note（GitHub/git writes は `guildbotics member ...` 経由のみ）。
- `communication_style` は interactive reply と GitHub comment には member 口調を適用し、workflow `AgentResponse.message`、JSON、path、ID、commit message、PR title/body などには neutral / machine output style を適用することを明示する。これによりノンインタラクティブ workflow の JSON 応答が人格口調で壊れないようにする。
- 含めない: access token / GitHub App private key path の中身 / env secret value / Slack token。
- 既定では credential が無くても非 secret context を表示できる。`credential_status` は `unchecked` とする。
- `--check-credentials` 指定時だけ GitHub credential 解決を必須にし、解決できなければ fail-closed（非ゼロ終了・secret を出さない安全メッセージ）。導入テストと workflow 前提確認ではこの option を使う。

### CLI: Git workspace

```bash
guildbotics member git prepare \
  --person <person_id> \
  --issue-url <github_issue_url> \
  [--pr-url <github_pr_url>] \
  [--format json|markdown]
```

動作:

- issue URL から owner/repo/issue number を解決する。
- member workspace root は `get_workspace_path(person_id)`（`~/.guildbotics/data/workspaces/<person_id>`）を使う。
- repo checkout は `<workspace_root>/<repo-name>`。
- 通常 issue は `ticket/<issue_number>` branch を checkout。PR review は PR head branch を checkout。
- clone/fetch/pull には member credential を使う。
- 返却 JSON: `repo` / `repo_path` / `branch` / `default_branch` / `issue_url` / `pr_url` / `mode`(`issue`|`pull_request_review`)。

重要な実装条件:

- workflow は CLI agent の cwd を member workspace root にする。agent が編集する repo は cwd 配下（`<workspace_root>/<repo>`）に作るため、Codex workspace-write sandbox でも編集できる。
- 既存 `GitTool.__init__` は default branch の checkout/pull と、checkout 時の reset/clean を行う（`guildbotics/utils/git_tool.py`）。`prepare` ではこの destructive 整備を許可してよいが、`publish` では絶対に使わない。

```bash
guildbotics member git commit \
  --person <person_id> \
  --repo-path <path> \
  --message-file <path> | --message-stdin \
  [--format json|markdown]
```

- `repo_path` の current branch を対象にする。dirty/untracked があれば `git add -A` → commit。push は行わない。
- `--message-stdin` を使うと、CLI agent の承認画面に heredoc で commit message 本文を表示しやすい。対話モードではこれを優先する。
- 返却 JSON: `repo_path` / `branch` / `commit_sha`(or null) / `has_changes` / `status`。

```bash
guildbotics member git push \
  --person <person_id> \
  --repo-path <path> \
  [--format json|markdown]
```

- `repo_path` の current branch を対象にする。remote branch が無い、または local が ahead の場合だけ push する。
- member credential を使う。
- 返却 JSON: `repo_path` / `branch` / `pushed` / `status`。

```bash
guildbotics member git publish \
  --person <person_id> \
  --repo-path <path> \
  --message-file <path> | --message-stdin \
  [--format json|markdown]
```

- 互換用の合成コマンド。`git commit` → `git push` を順に行う。
- `repo_path` の current branch を対象にする。dirty/untracked があれば `git add -A` → commit → push。変更が無ければ commit せず push 要否だけ確認する。
- member credential を使う。
- 返却 JSON: `repo_path` / `branch` / `commit_sha`(or null) / `pushed` / `has_changes` / `status`。

```bash
guildbotics member git branch create \
  --person <person_id> \
  --repo-path <path> \
  --branch <branch_name> \
  [--format json|markdown]
```

- `repo_path` の current branch から新しい local branch を作成して checkout する。既存 branch は上書きしない。
- 返却 JSON: `repo_path` / `branch` / `previous_branch` / `status`。

制約:

- `repo_path` は member workspace root 配下のみ許可（validation）。
- `message-file` は存在する通常ファイルのみ。空 commit message は error。
- **commit/push/publish は checkout しない**。`git.Repo(repo_path)` で既存 checkout を開き、git config / auth env / commit / push のみ行う。checkout/reset/clean は行わない（作業差分を消さない）。

### CLI: GitHub issue

```bash
guildbotics member github issue inspect \
  --person <person_id> \
  --url <github_issue_url> \
  [--format markdown|json]
```

- issue body/comments/assignees/labels/project metadata/linked PR candidates を取得。proxy signature を解釈し「自分の応答か」が分かる情報を返す。読み取り専用。

```bash
guildbotics member github issue comment \
  --person <person_id> \
  --url <github_issue_url> \
  --body-file <path> \
  [--format json|markdown]
```

- issue にコメント投稿。`person_type == proxy_agent` の場合は本文末尾に `⚙<person_id>` を自動付与（`get_proxy_agent_signature` を流用）。author mention は自動挿入しない（必要なら agent が本文に明示）。
- 返却 JSON: `comment_id` / `html_url` / `author` / `created_at`。

```bash
guildbotics member github issue create \
  --person <person_id> \
  --repo <owner/repo または repo> \
  --title-file <path> \
  --body-file <path> \
  [--add-to-project/--no-add-to-project] \
  [--format json|markdown]
```

- **repository の実 issue を作成する**。`--add-to-project` の既定は true で、configured ProjectV2 に追加する（既存 `_get_project_item_id` の `addProjectV2ItemById` を流用）。
- Status は明示設定しない（GitHub Projects 側の default/workflow に任せる）。`repo` が owner なしなら configured owner を使う。
- 返却 JSON: `issue_number` / `issue_url` / `project_item_id`(or null)。

> 挙動変更の注意: 旧 `GitHubTicketManager.create_tickets` は ProjectV2 の **draft issue** を作っていた。本 capability は **実 issue** を作る。follow-up issue が draft ではなく実 issue になる点を docs と Acceptance に明記する。

### CLI: GitHub PR

```bash
guildbotics member github pr inspect \
  --person <person_id> \
  --url <github_pr_url> \
  [--include-comments] \
  [--format markdown|json]
```

- PR title/body/state/merged/head/base/review threads/conversation comments を取得。`--include-comments` 時は各 review thread について `root_comment_id` / `latest_comment_id` / `resolved` / `outdated` / `replyable` / `reply_target_id` を含める。読み取り専用。
- `reply_target_id` は agent がそのまま `pr reply --reply-target-id` に渡すための値。agent に root/latest/resolved/outdated の raw 構造を解釈させない。

```bash
guildbotics member github pr create \
  --person <person_id> \
  --repo <owner/repo または repo> \
  --head <branch> \
  [--base <branch>] \
  (--title-file <path> --body-file <path> | --content-stdin) \
  [--issue-url <github_issue_url>] \
  [--draft auto|true|false] \
  [--format json|markdown]
```

- same-repository PR を作成。同じ head/base branch の open PR が既にあれば新規作成せず既存 PR を返す。
- `--base` 未指定時は repository default branch を向け先にする。
- `--content-stdin` は stdin の 1 行目を PR title、空行を挟んだ残りを PR body として読む。CLI agent の承認画面に heredoc で PR title/body を表示しやすいため、対話モードではこれを優先する。
- `--issue-url` 指定時は body に `Closes #<issue_number>` を付与（既に含まれていれば重複させない）。
- `--draft auto` は proxy agent なら draft、それ以外は ready。
- 返却 JSON: `pr_number` / `pr_url` / `created` / `draft` / `head` / `base`。
- 既存 `GitHubCodeHostingService.create_pull_request` のロジックを流用する。

```bash
guildbotics member github pr comment \
  --person <person_id> \
  --url <github_pr_url> \
  --body-file <path> \
  [--format json|markdown]
```

- PR conversation comment を投稿。proxy agent signature を自動付与。返却 JSON は issue comment と同形。

```bash
guildbotics member github pr reply \
  --person <person_id> \
  --url <github_pr_url> \
  --reply-target-id <review_comment_id> \
  --body-file <path> \
  [--format json|markdown]
```

- PR inline review comment thread に reply する。
- **エンドポイントは `POST /repos/{owner}/{repo}/pulls/{pull_number}/comments/{comment_id}/replies`**（`pull_number` 必須。`--url` から解決する）。既存 `respond_to_comments` / `InlineCommentThread.add_reply`（`guildbotics/integrations/code_hosting_service.py` / `github_code_hosting_service.py`）のロジックを流用し、reply 経路を再実装しない。
- `reply-target-id` は `pr inspect --include-comments` が返した `reply_target_id` のみ受け付ける。resolved / outdated thread は GitHub 画面と同様に root comment への reply を許可し、`resolved=true` / `outdated=true` は agent が文脈を理解するための情報として返す。reply-to-reply として不適切な id は fail-closed する。proxy agent signature を自動付与。
- 返却 JSON: `reply_comment_id` / `html_url` / `created_at`。

### CLI: GitHub reaction

```bash
guildbotics member github reaction add \
  --person <person_id> \
  --repo <owner/repo または repo> \
  --target issue-comment|pr-review-comment \
  --comment-id <comment_id> \
  --reaction +1|eyes|heart|hooray|rocket|laugh|confused|-1 \
  [--format json|markdown]
```

- GitHub reaction を追加（対応不要 ACK 等を agent が明示的に残すため）。`target` ごとに正しい REST endpoint を呼ぶ。既存 `add_reaction_to_comment` を流用。
- 返却 JSON: `reaction_id` / `content` / `comment_id`。

### CLI: workflow task completion

ワークフロー経由の agent 実行では、`AgentResponse(done)` だけを成功条件にしない。agent が何もせず `done` を返すとチケットが沈黙して停滞するため、workflow 専用の完了報告 capability を必須にする。

```bash
guildbotics member task complete \
  --person <person_id> \
  --run-id <workflow_run_id> \
  --ticket-url <github_issue_url> \
  --status done|asking|blocked \
  --summary-file <path> \
  [--format json|markdown]
```

- workflow が agent 起動前に `workflow_run_id` を生成し、prompt input と環境変数 `GUILDBOTICS_TASK_RUN_ID` の両方で渡す。
- `member github ...` / `member git ...` の write command は、`GUILDBOTICS_TASK_RUN_ID` または `--run-id` がある場合、secret を含まない操作記録を `~/.guildbotics/data/task-runs/<run_id>.jsonl` に append する。
- `task complete` は同じ run id の操作記録を読み、少なくとも 1 件の write evidence（`issue_comment` / `pr_comment` / `pr_reply` / `reaction_add` / `pr_create` / `git_publish` / `issue_create`）がある場合のみ成功する。
- `status=asking` は `issue_comment` / `pr_comment` / `pr_reply` のいずれかを必須 evidence とする。
- `status=done` は、コード変更がある場合は `git_publish` + `pr_create`、対応不要の場合は `reaction_add` または comment/reply を evidence として残す。agent が「対応不要」と判断した場合でも無痕跡の完了は許可しない。
- 返却 JSON: `run_id` / `status` / `summary` / `evidence_count` / `evidence_types` / `completed_at`。

```bash
guildbotics member task status \
  --run-id <workflow_run_id> \
  [--format json|markdown]
```

- workflow が agent 実行後に呼ぶ検証用 command。
- `completed == true` かつ `status in ["done", "asking", "blocked"]` の場合のみ workflow success とみなす。
- 未完了、evidence 不足、破損した記録は non-zero / JSON status error として扱い、workflow は safe error comment 経路に乗せる。

## Service Layer Changes

### 新設 package: `guildbotics/capabilities/`

新規 package を `guildbotics/capabilities/` に置く（決定。workflow 専用 integration ではなく、member として外部操作する横断 capability のため）。

### 新設: `guildbotics/capabilities/member_github.py` — MemberGitHubCapabilityService

責務:

- URL parsing（github.com と GitHub Enterprise 両対応）
- GitHub REST/GraphQL client 生成
- issue inspect/comment/create
- PR inspect/create/comment/reply
- reaction add
- ProjectV2 add item
- proxy signature append
- GitHub Enterprise 互換の web/API base 解決

既存流用:

- `create_github_client(person, base_url)` / `get_person_github_token(person, base_url)` / `is_proxy_agent(person)` / `get_proxy_agent_signature(person)`（`guildbotics/integrations/github/github_utils.py`）
- `GitHubCodeHostingService` の PR create / reply / reaction ロジック
- `GitHubTicketManager` の ProjectV2 helper（`_get_project_item_id` 等）は共通化して移すか、重複を避けて再利用する。

注意:

- 現行 PR URL parser は `https://github\.com/...` 固定（`GitHubTicketManager._parse_pull_request_url`、`has_pr_review_comments` の `startswith("https://github.com/")`）。新 service では configured API/web base から GitHub Enterprise も扱える parser にする。
- `ticket_manager.base_url`（`base_url`）と `code_hosting_service`（`api_base_url`）で base の出所が分かれている。新 service は project service config を吸収し API base を一箇所で決める。
- comment/reply には proxy signature を必ず付ける（既存の「自分の応答」判定 `get_author_type` が機能するため）。
- secret 値は返却しない。trace/log にも出さない。

### 新設: `guildbotics/capabilities/member_git.py` — MemberGitWorkspaceService

責務:

- member workspace root 解決 / repo clone・fetch / issue branch・PR head branch checkout / commit・push / repo path validation
- **destructive `prepare` と non-destructive `publish` の分離**

重要:

- `prepare` は作業開始前の整備なので reset/clean を許可してよい。
- `publish` は agent が編集した後なので reset/clean を絶対にしない。`git.Repo(repo_path)` で既存 checkout を開き、git config / auth env / commit / push のみ行う。
- 既存 `GitTool.__init__` は default branch checkout/pull を行うため、publish path でそのまま呼ぶと作業差分を壊す。`GitTool` に non-destructive 経路を足すのではなく、`MemberGitWorkspaceService` 側で `prepare`（GitTool 流用可）と `publish`（生 `git.Repo` 操作）を明確に分けて実装する。

### Credential / Context / identity loading

- member group の共通エントリで workspace / `.env` をロードする helper を新設する。`--workspace`、明示 `GUILDBOTICS_CONFIG_DIR`、cwd `.guildbotics/config`、active workspace、fallback の優先順を実装し、workspace `.env` を見つけたら `os.environ["GUILDBOTICS_ENV_FILE"]` に必ず設定する。member group / scheduler / app_api 起動から同じ helper を共有する。
- person/team 解決の共通 helper を**新設する**（決定）。

```python
def resolve_member_context(person_identifier: str) -> tuple[Context, Person]
```

- `guildbotics/drivers` の既存 `run_command._resolve_person` は public 化せず、上記 helper に解決ロジックを集約して run_command 側からも使う形にする（重複を避ける）。
- `--person` は person_id/name の両方を許可。
- active member に限定しない（manual skill で inactive member を検査したい場合があるため）。
- write command でも、明示 `--person` なら inactive member を許可する（active は scheduler 対象の概念であり manual 操作とは別、と仕様化する）。

## Skill / Prompt Contract

### Skill source

`skills/guildbotics/SKILL.md` を追加する。

目的:

- Codex / Claude Code / Gemini CLI / GitHub Copilot CLI / desktop app から同じ考え方で使える薄い skill source。実処理はすべて `guildbotics member ...` に委譲する。

内容:

- まず `guildbotics member context --person <person>` を実行する。
- GitHub/git/chat 操作は通常 CLI/`gh`/user account を直接使わない。
- GitHub/git 書き込みは `guildbotics member github ...` / `guildbotics member git ...` のみ。
- issue/PR 作業の標準手順: `member context` → `issue/pr inspect` → `git prepare` → 調査/編集/テスト → 必要なら `git publish` → 必要なら `pr create` → `issue/pr comment` / `pr reply` / `reaction add`。
- workflow から呼ばれた場合は、最後に必ず `guildbotics member task complete --run-id <workflow_run_id> ...` を実行し、完了状態と evidence を記録する。
- 不明点は GitHub コメントで質問。対応不要なら reaction/comment で痕跡を残す。
- secrets を表示・推測・保存しない。

導入 / 配布（今回の実装対象に含める）:

- リポジトリ内に skill source を置く。
- Mac 版 desktop app はビルド済み `guildbotics-cli` を同梱し、初回起動時（または setup 画面表示時）に `~/.guildbotics/bin/guildbotics` へコピーする。
- `~/.local/bin/guildbotics` は存在しない場合、または既存ファイルが GuildBotics managed shim の場合だけ作成・更新する。利用者が手動で入れた CLI は上書きしない。
- Codex 向け: `$CODEX_HOME/skills/guildbotics/SKILL.md` または検出済みの `~/.codex/skills/guildbotics/SKILL.md` へ desktop app が配置する。**併せて、Codex の承認ポリシーで `gh` / `git push` を承認必須/拒否にする設定例を書く。**
- Claude Code 向け: `$CLAUDE_HOME/skills/guildbotics/SKILL.md` または検出済みの `~/.claude/skills/guildbotics/SKILL.md` へ desktop app が配置する。**併せて、`gh` / `git push` / 直接 token/API 書き込みを拒否または承認必須にするクライアント権限設定例を書く。**
- Gemini CLI 向け: `$GEMINI_HOME/skills/guildbotics/SKILL.md` または検出済みの `~/.gemini/skills/guildbotics/SKILL.md` へ desktop app が配置する。
- GitHub Copilot CLI 向け: `$COPILOT_HOME/skills/guildbotics/SKILL.md` または検出済みの `~/.copilot/skills/guildbotics/SKILL.md` へ desktop app が配置する。
- user skill は GuildBotics desktop が配置した未編集 skill だけを更新し、ユーザー作成またはユーザー編集済みの `SKILL.md` は上書きしない。
- skill は thin wrapper とし、GitHub API schema や token 取り扱いの詳細を持たせない。操作詳細は `guildbotics member ... --help` と GuildBotics 実装を正とする。
- skill 内のコマンド例は `"$HOME/.guildbotics/bin/guildbotics" member ...` を優先する。bare `guildbotics` は shim / PATH が使える場合の fallback とする。
- **互換検証**: Codex / Claude Code / Gemini CLI / GitHub Copilot CLI で単一 `SKILL.md` が認識されることを導入確認項目に含める。クライアント差異がある場合は、共通本文 + クライアント別の最小ラッパー、という形に分けることを許容する。

導入前提として docs に明記するもの:

- 通常の desktop 導入では、利用者が PATH / `GUILDBOTICS_CONFIG_DIR` / `GUILDBOTICS_ENV_FILE` を CLI agent に手渡ししない。desktop app が active workspace と managed CLI / skill を配置する。
- headless / 開発時の fallback として、`guildbotics workspace use <workspace_dir>` または `guildbotics member --workspace <workspace_dir> ...` を使えること。
- member workspace は `~/.guildbotics/data/workspaces/<person_id>` 配下に作られること。
- skill 導入後の smoke check は `~/.guildbotics/bin/guildbotics workspace status` と `~/.guildbotics/bin/guildbotics member context --person <person_id> --check-credentials` が成功すること。

### handle_github_ticket command

対象: `guildbotics/templates/commands/functions/handle_github_ticket.ja.md` / `.en.md`

変更:

- frontmatter の `response_class` を `guildbotics.intelligences.common.AgentResponse` に変更する。
- `GitHubTicketAgentResult` 前提の `commit_message` / `pr_title` / `pr_body` / `review_replies` / `new_tickets` 指示を削除する。
- 現行プロンプトの「GitHub 書き込みは禁止」（instruction #3）を「GitHub/git 書き込みは必ず `guildbotics member ...` 経由」へ変更する。
- agent への標準手順（context → inspect → prepare → 編集 → publish → pr create → comment/reply/reaction）を明記する。
- workflow から呼ばれた場合、最後に `guildbotics member task complete --person <person_id> --run-id <workflow_run_id> ...` を必ず実行するよう明記する。`task complete` が失敗したら `done` を返さず、agent 実行を失敗扱いにする。
- 完了時は簡潔な `AgentResponse` JSON のみ返す。実際の comment/PR/reply は agent が `guildbotics member` で実行済みであることを期待する。

新プロンプトに渡す session_state:

- `person_id` / `ticket_url` / `pull_request_url` / `trigger_reason` / `work_type` / `language` / `member_workspace`(workspace root path) / `workflow_run_id` / `github_capability_help`(or command examples)

返却:

```json
{ "status": "done", "message": "対応内容の短い要約。GitHub へのコメント/PR/reply は必要に応じて投稿済み。" }
```

```json
{ "status": "asking", "message": "GitHub に質問コメントを投稿しました。回答待ちです。" }
```

### CLI agent cwd

ticket workflow は CLI agent 起動時、repo path ではなく **member workspace root** を cwd にする。

- agent が `guildbotics member git prepare` で repo checkout を作る。
- Codex workspace-write sandbox で cwd 配下の repo を編集できる。
- workflow が git checkout を握らずに済む。

## Ticket Workflow Changes

対象: `guildbotics/templates/commands/workflows/ticket_driven_workflow.py`

最終責務:

- `ticket_manager.get_task_to_work_on()`
- `context.update_task(task)`
- trace attributes 設定（`_ticket_trace_attributes` 維持）
- READY ticket の working lane 移動（`_move_task_to_working_if_ready` 維持）
- `ticket_url` / `pull_request_url` / `trigger_reason` 等の prompt input 組み立て
- `context.invoke("functions/handle_github_ticket", ..., cwd=get_workspace_path(context.person.person_id))`
- `guildbotics member task status --run-id <workflow_run_id>` で完了報告を検証する
- agent 結果と task completion status を `AgentResponse` として返す
- 例外時に safe error comment を投稿して re-raise（`add_comment_to_ticket` + `_build_task_error_message` 維持）

削除する処理（named helper）:

- `get_git_tool` / `get_branch_name` / `_checkout_work_branch` / `_load_review_context` / `_format_review_context` / `_normalize_agent_result` / `_publish_result` / `_post_agent_question` / `_create_ticket_drafts` / `_build_ticket_draft_tasks` / `_apply_pull_request_review_replies` / `_thread_reply_target_comment_id` / `_done_message` / `_format_created_ticket_message` / `_append_created_ticket_message` / `_format_issue_comments`

削除する処理（`_main` 内インライン。**named helper になっていないので見落とし注意**）:

- **HEAD コミットガード**: 現行 `_main` の
  ```python
  initial_head = git_tool.repo.head.commit.hexsha
  ...
  if git_tool.repo.head.commit.hexsha != initial_head:
      raise RuntimeError("CLI agent created a git commit directly. ...")
  ```
  を撤去する。新方式では agent が `guildbotics member git publish` 経由で commit するため HEAD は当然変化する。このガードを残すと正常系で誤って `RuntimeError` になる。
- `git_tool` / `code_hosting_service` の生成と利用全般。

削除する依存 import:

- `GitHubTicketAgentResult` / `CodeHostingService` / `GitTool` / `get_person_github_token`（workflow からは不要になる）。

残す処理:

- `_move_task_to_working_if_ready` / `_ticket_trace_attributes` / `_build_task_error_message` / `_work_type` / 最小限の prompt input helper。

エラー時:

- workflow は従来通り `ticket_manager.add_comment_to_ticket(task, safe_message)` を呼ぶ。これは workflow でしか判断できない system failure 用として残す。
- 通常の質問/完了コメントは agent responsibility。

成功時:

- workflow は `member task status` で `completed == true` を確認する。未完了・evidence 不足なら成功扱いにせず、safe error comment 経路に乗せる。
- workflow は GitHub に追加コメントしない。
- `AgentResponse.skip_ticket_comment` は workflow では参照しない（model に残してよい）。

**「黙った失敗」への対応:**

- 旧方式では workflow が clone/checkout/commit/PR を握っていたため、失敗は例外として顕在化した。新方式では prepare/publish が agent 任せになるため、agent が「clone もせず `status: done` を返す」と workflow は何も投稿せずチケットが沈黙して停滞しうる。
- 最低限の担保: `guildbotics member git prepare` 等の capability コマンドは失敗時に**非ゼロ終了**する（CLI 共通仕様）。agent script（`codex exec` / `claude -p`）はコマンド失敗を検知でき、最終的に agent プロセスが非ゼロ終了すれば `CliAgentBrain._raise_if_execution_failed` が拾い、workflow の error comment 経路に乗る。
- これに加えて、workflow 経由では `guildbotics member task complete` を必須にし、workflow は agent 終了後に `task status` を検証する。agent が capability を呼ばずに `done` を返しても完了記録が無いため success にならない。
- 完了報告は「作業の正しさ」を保証するものではないが、少なくとも「何らかの member write capability を通した痕跡があること」と「agent が workflow run を完了扱いにしたこと」を機械的に検証するための監査点になる。

## GitHubTicketManager Changes

対象: `guildbotics/integrations/github/github_ticket_manager.py`

残す責務:

- ProjectV2 item 取得 / Status field・lane_map / assignee・Agent field 担当判定 / issue comments 読み取り / linked PR・PR state・review thread state 読み取り / 最小限の Project status 更新 / workflow error comment 投稿 / `get_ticket_url`。
- 起動判定（current simplified spec を維持）: ready lane + assigned/mention → 起動 / working lane + issue comment/mention → 起動 / linked PR open + unhandled review thread → 起動 / linked PR merged → done lane move / last response・reaction が自分側なら抑止。

削除/縮小:

- 通常作業 comment publish は workflow から呼ばれなくなる。
- `create_tickets` は ticket workflow の通常経路から外れる（setup/他用途で必要なら残す）。follow-up issue 作成は `member github issue create`（実 issue）に移行。
- `has_pr_review_comments` など旧互換 helper が未使用なら削除。
- hardcoded `https://github.com` parser（`_parse_pull_request_url` 等）は新 capability 側へ寄せるか汎用化する。

## Models Cleanup

対象: `guildbotics/intelligences/common/__init__.py`

- `GitHubTicketAgentResult` / `GitHubTicketDraft` / `GitHubReviewReply` が新 workflow で不要になれば削除する。
- 削除前に他テスト/コードの参照を grep して消す（少なくとも現行 `ticket_driven_workflow.py` が import）。
- `AgentResponse` は残す。`skip_ticket_comment` は互換として残してよいが、新 ticket workflow では使わない（完全削除は blast radius が広いので今回の主目的から外す）。

## Chat / Slack Decision

この ticket workflow 計画では chat workflow の全面対応は行わない。Chat / Slack 側は後続計画 `docs/spec/member_capability_chat_workflow_plan.ja.md` で、同じ member capability 境界へ移行する。

ticket workflow 実装時点での将来対応:

- CLI group は `guildbotics member github ...` / `guildbotics member git ...` とし、後続で `guildbotics member chat ...` を足せる構造にする。
- service package は `capabilities` として GitHub 専用名に閉じない。
- chat workflow は後続計画で、Slack write boundary を `guildbotics member chat ...` へ集約する。

## Implementation Order For One PR

### Step 1: Interface / scaffolding

- `guildbotics/capabilities/` package を追加し、`MemberGitHubCapabilityService` / `MemberGitWorkspaceService` の型・空実装を置く。
- `guildbotics/cli/member.py` に `member` group を追加し `__init__.py` から登録。
- **member group 共通の workspace / `.env` ロード（`--workspace` → 明示 config/cwd config → active workspace → fallback）と `resolve_member_context` helper を実装。CLI / scheduler / AppRuntime の env loader を共通化し、workspace `.env` をロードしたら絶対パスを `GUILDBOTICS_ENV_FILE` に設定する。** credential-required command と `context --check-credentials` は credential 解決不可なら fail-closed。
- `member context` を最初に実装。
- CLI 共通 helper（JSON dump / Markdown output / body・title file reader / safe URL・repo parser）を追加。
- `member task complete/status` の storage と schema を実装する。最初は `~/.guildbotics/data/task-runs/<run_id>.jsonl` の append-only 記録でよい。

### Step 2: GitHub read capability

- `issue inspect` / `pr inspect` / URL parser / GitHub Enterprise・API base 対応 / proxy signature・self response 判定の表示。
- tests: mocked httpx / fake service。

### Step 3: GitHub write capability

- `issue comment` / `issue create`（実 issue + ProjectV2 add item）/ `pr comment` / `pr reply`（正しい replies endpoint）/ `pr create`（既存 PR 再利用・`Closes #N`）/ `reaction add` / proxy signature append。
- write command は `GUILDBOTICS_TASK_RUN_ID` があれば task-run evidence を記録する。
- tests: payload, endpoint, signature, no secret leakage。

### Step 4: Git workspace capability

- `git prepare` / `git publish` / repo path validation / same-repo PR head checkout。
- `MemberGitWorkspaceService` で destructive prepare と non-destructive publish を分離。
- `git publish` は `GUILDBOTICS_TASK_RUN_ID` があれば task-run evidence を記録する。
- tests: temporary git repos / mocked token auth。**publish が reset/clean しないこと、auth env が log/output に漏れないことを検証。**

### Step 5: Skill source and command prompt

- `skills/guildbotics/SKILL.md` 追加。desktop app による Codex / Claude Code / Gemini CLI / GitHub Copilot CLI user skill への自動配置 + **クライアント拒否設定例**（`gh`/`git push`）を docs に追加。
- desktop app に同梱する `guildbotics-cli` と first-launch install（`~/.guildbotics/bin/guildbotics` / managed shim / skills）を追加。
- active workspace（desktop app / `guildbotics workspace use` / `member --workspace`）の導入前提を docs に追加。env 手渡しは fallback として説明する。
- skill 導入 smoke check（`~/.guildbotics/bin/guildbotics workspace status` と `member context --check-credentials`）を docs に追加。
- `handle_github_ticket.ja.md/en.md` を `AgentResponse` + member CLI usage に変更。`brain: file_editor` は維持。prompt examples を実コマンド名に一致させる。

### Step 6: ticket workflow replacement

- `ticket_driven_workflow.py` を薄い委譲 flow に差し替える。
- **`_main` のインライン HEAD コミットガードを撤去**。`GitTool` / `CodeHostingService` / `GitHubTicketAgentResult` を除去。
- cwd は `get_workspace_path(context.person.person_id)`。session_state に member workspace root と `workflow_run_id` を渡し、agent env に `GUILDBOTICS_TASK_RUN_ID` を設定する。
- agent 実行後に `member task status --run-id <workflow_run_id>` を検証し、未完了/evidence 不足なら success にしない。
- success 時 GitHub comment を投稿しない。agent 非ゼロ終了・task completion 未記録・evidence 不足時のみ safe error comment。

### Step 7: cleanup

- unused model/helper/tests を削除または更新。`GitHubTicketAgentResult` 等が未使用なら削除。
- old ticket workflow tests を新仕様へ置換。
- docs/spec を最終設計へ更新。README / README.ja の ticket workflow 説明を更新。
- diagnostics/setup 文言に旧 publish model / Mode / Role / Retrospective 前提が残っていないか確認。

### Step 8: verification

```bash
uv run --no-sync ruff format --check guildbotics tests
uv run --no-sync ruff check guildbotics tests
uv run --no-sync mypy guildbotics
uv run --no-sync pylint guildbotics
uv run --no-sync python -m pytest tests/guildbotics/templates/commands/workflows/test_ticket_driven_workflow.py
uv run --no-sync python -m pytest tests/guildbotics/capabilities tests/guildbotics/cli tests/guildbotics/integrations tests/guildbotics/intelligences
```

最終的に可能なら full pytest。

```bash
uv run --no-sync python -m pytest tests/ --cov=guildbotics --cov-report=xml
```

desktop は基本触らず docs/spec 更新に留める。触った場合のみ desktop checks を実行する。

## Test Plan

### CLI tests — `tests/guildbotics/cli/test_member_command.py`

- `member context` が person profile/role/GitHub username を返す。
- `member context` が secret を返さない。
- `member context` は credential 不在でも非 secret context を返し、`credential_status=unchecked` を示す。
- `member context --check-credentials` は credential 解決に成功すると `credential_status=ok` を返し、解決できなければ fail-closed する。
- unknown person は available list 付き error。
- write command は `--person` 必須。
- body/title file missing は validation error。
- JSON output が parse 可能 / Markdown output が主要情報を含む。
- inactive person も明示指定なら実行できる。
- **active workspace / `--workspace` / `GUILDBOTICS_ENV_FILE` 指定で credential が解決され、credential-required command または `context --check-credentials` で workspace `.env`・cwd `.env`・secret env が無ければ fail-closed（非ゼロ終了・secret を出さない安全メッセージ）。**
- `member task complete/status` が run id ごとに完了状態と evidence を記録/取得する。evidence 不足、unknown run id、破損記録は safe error。

### GitHub capability tests — `tests/guildbotics/capabilities/test_member_github.py`

- issue URL parsing: github.com / GitHub Enterprise configured web base。
- issue inspect が expected REST/GraphQL endpoint を呼ぶ。
- issue comment が correct endpoint/body を送る。
- proxy agent は signature 付与 / non-proxy は付与しない。
- issue create が**実 repository issue**を作る。`--add-to-project` true で ProjectV2 add item mutation を呼ぶ。
- PR inspect が review thread ごとに `root_comment_id` / `latest_comment_id` / `resolved` / `outdated` / `replyable` / `reply_target_id` を返す。
- PR create が existing open PR を再利用 / issue URL から `Closes #N` 付与。
- **PR reply が `/repos/{owner}/{repo}/pulls/{pull_number}/comments/{comment_id}/replies` を呼ぶ。**
- PR reply は `pr inspect --include-comments` が返した `reply_target_id` のみ受け付ける。root comment / latest comment / resolved thread / outdated thread / reply-to-reply 不適切 id の扱いを fixture で固定する。resolved / outdated thread は reply 可能、reply-to-reply 不適切 id は fail-closed とする。
- reaction add が target ごとに正しい endpoint を呼ぶ。
- API error は secret を含まない exception に変換される。
- write command が `GUILDBOTICS_TASK_RUN_ID` 指定時に task-run evidence を記録する。

### Git workspace tests — `tests/guildbotics/capabilities/test_member_git.py`

- prepare が workspace root 配下に clone / issue mode が `ticket/<issue_number>` branch / PR mode が head branch checkout。
- publish が dirty files を commit/push / 変更なしなら commit しない / **checkout/reset/clean を行わない**。
- repo_path が member workspace 外なら error / commit message file が空なら error。
- auth env が logs/output に漏れない。
- publish が `GUILDBOTICS_TASK_RUN_ID` 指定時に task-run evidence を記録する。

### ticket workflow tests — `tests/guildbotics/templates/commands/workflows/test_ticket_driven_workflow.py`

- READY ticket で working lane move を試みる。
- `functions/handle_github_ticket` を呼ぶ。invoke kwargs の `cwd` が member workspace root。
- prompt params に `person_id`, `ticket_url`, `pull_request_url`, `trigger_reason`, `member_workspace`, `workflow_run_id` が入る。
- agent env に `GUILDBOTICS_TASK_RUN_ID` が入る。
- invoke 成功後に `member task status --run-id <workflow_run_id>` を検証する。
- success 時に issue comment / PR review reply を投稿しない。commit/PR 作成を呼ばない。
- `AgentResponse(done)` / `AgentResponse(asking)` は task completion 検証後にのみ返す。workflow は追加 ticket comment を投稿しない。
- `AgentResponse(done)` でも task completion が無い場合は success にせず safe error comment 経路に乗せる。
- invoke が例外（agent 非ゼロ終了）を投げた場合、または task completion 検証に失敗した場合だけ safe error comment を投稿する。
- no task の場合は `None`。
- **HEAD コミットガードが撤去され、agent がコミット相当の状態でも `RuntimeError` を投げない**（旧ガード前提テストは削除）。

削除/置換: `GitHubTicketAgentResult.review_replies` 前提 / new_tickets draft 作成 / commit diff → PR 作成 / workflow 側 PR conversation comment 投稿テスト。

### prompt / brain tests — `tests/guildbotics/intelligences/test_functions.py` 等

- `handle_github_ticket` の response class が `AgentResponse`。
- prompt に `guildbotics member` usage が含まれる / direct `gh` write 禁止が含まれる。
- prompt に `member task complete` 必須指示が含まれる。
- prompt に `GitHubTicketAgentResult` schema 指示が残っていない。

### diagnostics/setup docs tests

既存テストが落ちる場合のみ更新。diagnostics は GitHub capability の存在までは要求しない。ticket routine が GitHub required は維持。Mode/Role/Retrospective 前提文言が残らない。

## Documentation Updates

更新対象: `docs/spec/ticket_driven_workflow_simplification.ja.md` / `README.md` / `README.ja.md` / 必要なら `docs/custom_command_guide.{en,ja}.md`。

内容:

- ticket workflow は ProjectV2 trigger + member CLI delegation へ移行。GitHub/git operations は member capability であり workflow logic ではない。
- CLI agent / skill は `guildbotics member ...` を使う。`gh` 直接利用は推奨しない。
- CLI agent への skill 導入手順 + **クライアント拒否設定**（`gh`/`git push`）。
- 導入前提（managed CLI / active workspace / skill 配置 / member credential / workspace path。env 手渡しは fallback）。
- **ガードレールの経路別仕様と「完全な技術的禁止ではない」限界を明記する。**
- follow-up issue 作成は `member github issue create`（実 issue）。review reply は `member github pr reply`。
- chat workflow / Slack capability はこの ticket 計画の scope 外。後続の chat 計画で同じ member capability に乗せる。

## Acceptance Criteria

- `ticket_driven_workflow.py` に commit/push/PR create/review reply/follow-up issue draft publish と HEAD コミットガードが残っていない。
- CLI agent は member workspace root を cwd として起動される。
- `handle_github_ticket` は `AgentResponse` を返す。
- GitHub/git 書き込み操作は `guildbotics member ...` で実行できる。
- workflow 経由の agent 実行では `guildbotics member task complete/status` が必須で、完了報告が無い `AgentResponse(done)` は success にならない。
- credential-required command と `member context --check-credentials` は active workspace / `GUILDBOTICS_ENV_FILE` / cwd `.env` から person credential を解決し、解決できなければ fail-closed する。通常の `member context` は非 secret context を表示し、secret を output/log/trace に出さない。
- proxy agent の GitHub comment/reply には signature が付く。
- workflow success 時、GuildBotics は追加 comment を投稿しない。agent failure / task completion failure 時のみ safe error comment を投稿する。
- existing ProjectV2 trigger 判定は維持される。
- ノンインタラクティブ経路で `_isolate_github_write_credentials` が維持され、`gh`/ambient git による利用者アカウント操作が成立しない。
- インタラクティブ経路向けに `gh`/`git push` のクライアント拒否設定手順が docs にある。
- chat workflow はこの ticket 計画では壊さず、後続の chat 計画で member capability 化する。
- README/docs/spec が新構造とガードレール経路別仕様を説明している。
- 関連 Python lint/type/test が通る。

## Acceptance Test Plan

実装後の受け入れテストは unit/integration とは別に、実際の導入と agent 実行で確認する。

### 1. 導入テスト

目的: Codex / Claude Code / Gemini CLI / GitHub Copilot CLI から GuildBotics skill と `guildbotics member ...` CLI を利用できる状態を確認する。

事前準備:

- テスト用 GitHub repository と ProjectV2 / テスト用 member（human/machine/proxy のいずれか）/ member の GitHub credential を `.env` に用意。
- Mac 版 desktop app をインストールし、一度起動する。開発中は `scripts/desktop-build-all.sh` で作った DMG、または `scripts/desktop-dev-tauri.sh` で起動した dev app を使う。
- Desktop app で対象 workspace を選択し、setup / diagnostics を完了する。これにより active workspace が `~/.guildbotics/data/active-workspace.json` に保存される。
- 初回起動後に次を確認する。

```bash
~/.guildbotics/bin/guildbotics --help
~/.guildbotics/bin/guildbotics workspace status
~/.guildbotics/bin/guildbotics member context --person <person_id> --check-credentials --format json
```

- `~/.local/bin/guildbotics` は既存の手動インストールを上書きしないため、受け入れテストでは安定パス `~/.guildbotics/bin/guildbotics` を使う。

CLI agent 導入確認（各クライアントで同様）:

- desktop app が配置した `skills/guildbotics/SKILL.md` が user skill として認識されることを確認する。
- 推奨クライアント拒否設定（`gh` / `git push`）を適用する。
- 次のプロンプトで、具体コマンドをユーザーが明示しなくても SKILL が managed CLI を選び、`member context` 相当の確認を実行することを確認する。

```text
GuildBotics skill を使って、<person_id> として振る舞うための context を確認してください。
GitHub や git の書き込み操作は行わず、credential check を含めて context の要点だけを要約してください。
```

期待挙動:

- SKILL が `~/.guildbotics/bin/guildbotics` を優先して `member context --check-credentials ...` 相当を実行する。bare `guildbotics` が使われた場合でも、managed shim または同等の version であることを確認する。
- 出力 JSON から member 名/person_id/person_type/role・profile の要点/GitHub username/利用可能な `guildbotics member ...` 操作を要約する。
- credential 解決に成功し、`credential_status=ok` が確認できる。
- access token / private key / env secret が出力に含まれない。
- `gh` / GitHub API 直接呼び出し / git 操作を実行しない。

GuildBotics 設定確認:

- `~/.guildbotics/bin/guildbotics workspace status` が active workspace path / config dir / env file を表示する。
- `~/.guildbotics/bin/guildbotics member context --check-credentials` 出力に member profile/role/GitHub username/`credential_status=ok` が含まれ、secret が含まれない。
- `~/.guildbotics/bin/guildbotics member github issue inspect --person <person_id> --url <issue_url>` が成功する。
- `~/.guildbotics/bin/guildbotics member git prepare --person <person_id> --issue-url <issue_url>` が member workspace 配下に checkout を作る。
- `~/.guildbotics/bin/guildbotics member git publish` は member workspace 外の `repo-path` を拒否する。
- `~/.guildbotics/bin/guildbotics member --workspace <workspace_dir> context --person <person_id> --check-credentials` が active workspace に依存せず成功する。
- 通常の `~/.guildbotics/bin/guildbotics member context` は credential 不在でも非 secret context を出せる。
- active workspace / `GUILDBOTICS_ENV_FILE` / cwd `.env` のいずれも無い環境では、`member context --check-credentials` が secret を出さず fail-closed する。

合格条件:

- Codex / Claude Code / Gemini CLI / GitHub Copilot CLI で skill を認識でき、各クライアントから managed CLI の `member context --check-credentials` が動く。
- GuildBotics CLI が member credential を使える。
- secret が CLI output / logs / prompt trace に出ない。
- member workspace が想定パスに作られる。
- 利用者が CLI agent に `/path/to/workspace` や `GUILDBOTICS_ENV_FILE` を毎回渡さなくても動く。

### 2. スキル動作確認（インタラクティブ）

目的: デスクトップアプリで、ユーザーが明示的に skill を使い、member として GitHub issue/PR 対応を対話的に進められ、`gh`/ローカル認証ではなく `guildbotics member ...` を使うことを確認する。

通常 issue 対応シナリオ: テスト issue → 「GuildBotics skill を使って `<person_id>` としてこの issue に対応して」 → agent が context 取得 → `issue inspect` → 必要なら `git prepare` → 調査/編集/テスト → `git publish` → `pr create` → `issue comment`。

質問シナリオ: 情報不足 issue → agent が推測実装せず `issue comment` で質問 → 最終応答が「質問投稿済み・回答待ち」。

PR review 対応シナリオ: review comment 付き PR → `pr inspect --include-comments` → 妥当なら編集/publish/`pr reply` → 不要でも `pr reply` または reaction で痕跡。

クライアント拒否設定の検証: agent が `gh` / `git push` を試みた場合、クライアントが承認要求 or 拒否し、利用者が拒否できることを確認する。

合格条件:

- GitHub/git 書き込みに `guildbotics member ...` を使う。`gh pr create` 等・直接 token 利用を行わない。
- GitHub 上の comment/PR/review reply の author が member credential に対応する。proxy agent は末尾に `⚙<person_id>`。
- 不明点で推測実装せず質問する。対応不要でも痕跡を残す。

### 3. ワークフロー動作確認（ノンインタラクティブ）

目的: `ticket_driven_workflow` が ProjectV2 から対象を拾い、非対話 CLI agent 実行で member capability 経由の作業を完了し、workflow 自体が commit/PR/comment/review reply を publish しないことを確認する。

通常 issue / PR review / 質問 / エラー の各シナリオは従来通り。特に:

- **エラーシナリオ**: capability command が失敗する条件を作り、agent が非ゼロ終了 → workflow が safe error comment を投稿。error comment にローカル path / token / private key / traceback が含まれない。
- **ガードレール検証**: agent script の env に `GH_TOKEN` 等が無く、agent が `gh`/`git push` を試みても失敗すること（利用者アカウントで操作されないこと）。

合格条件:

- ticket workflow は ProjectV2 trigger と agent 起動に集中。GitHub/git publish は agent が `guildbotics member ...` 経由で行う。
- success 時に追加 comment 無し。failure 時、または task completion 未記録/evidence 不足時だけ safe error comment。
- ready→working / merged・closed PR→done lane 移動は可能な場合のみ。同一 ticket/PR が重複処理されない。
- secret が output/log/prompt trace に出ない。

### 受け入れテストで見つけるべき回帰

- skill が古い `GitHubTicketAgentResult` schema を参照している。
- agent が `gh` 直接利用やローカル GitHub 認証に依存している。
- `guildbotics member git publish` が作業差分を reset/clean して消す。
- workflow が success 時に issue comment を二重投稿する / HEAD コミットガードが残って正常系で `RuntimeError`。
- proxy agent signature が付かず自分の応答判定が崩れる。
- **agent が capability を一切呼ばずに `done` を返し、workflow が success 扱いにしてチケットを沈黙させる。** 新方式では `member task complete/status` 未完了として検出する。
- active workspace が保存されず、CLI agent 内の `guildbotics member` が workspace / credential 解決に失敗する。
- managed CLI が配置されず、CLI agent が古い `guildbotics` や別環境の CLI を使う。
- desktop app が既存の手動 `~/.local/bin/guildbotics` を上書きする。
- 特定の CLI agent では動くが別の CLI agent では managed CLI / active workspace / skill 配置/拒否設定の都合で動かない。
- member workspace が cwd 外になり Codex workspace-write sandbox で編集できない。
- secret が CLI output / stderr / prompt trace / diagnostics に混入する。

## Assumptions

- 後方互換は不要。
- ガードレールの狙いは「利用者自身のアカウントへのフォールバック防止」であり、生 token の exfiltration 防止は対象外。
- ガードレールは経路別: ノンインタラクティブは `_isolate_github_write_credentials`（維持）、インタラクティブはクライアント拒否設定 + 人間承認。いずれも完全な技術的禁止ではない。
- **member credential は active workspace / `GUILDBOTICS_ENV_FILE`（fallback: cwd `.env`）から `guildbotics member` が自力ロードする。agent 環境変数の継承には依存しない（Codex は `*TOKEN*` を削除するため）。**
- Desktop app は同梱済み `guildbotics-cli` と skill を安定配置し、interactive skill 経路では `~/.guildbotics/bin/guildbotics` を優先する。
- Slack capability はこの ticket 計画では実装しない。後続の chat 計画で `guildbotics member chat ...` として実装する。
- App API 直呼びではなく CLI を skill の安定入口にする（CLI の裏側実装は将来変えてよい）。
- ProjectV2 は trigger source として残す。
- member workspace は `~/.guildbotics/data/workspaces/<person_id>` を使う。
- CLI agent subprocess は member workspace root を cwd にする。
- GitHub App / PAT / proxy agent の認証方式は既存 `github_utils.py` を正とする。
