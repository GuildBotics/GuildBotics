# Ticket Driven Workflow 改善方針

現在の実装では、ticket workflow は GitHub ProjectV2 から起動対象を選び、member workspace root を cwd として
CLI agent に ticket / PR URL と起動理由を渡す。GitHub/git 書き込みは workflow ではなく
`guildbotics member ...` capability が担い、agent は最後に `guildbotics member task complete` を記録する。
workflow はその task-run status を検証し、成功時に追加 comment を投稿しない。失敗時だけ safe error comment を投稿する。

## 対象チケット

- [#156 チケット駆動ワークフローのシンプル化](https://github.com/GuildBotics/GuildBotics/issues/156)
- [#170 作業記録機能追加 (ticket driven workflow)](https://github.com/GuildBotics/GuildBotics/issues/170)
- [#167 チケット駆動ワークフローをイベント起動に変更する](https://github.com/GuildBotics/GuildBotics/issues/167)

## 基本方針

今回の基本方針は次の通り。

- 後方互換は考慮しない。
- GitHub ProjectV2 は廃止せず、作業対象を見つけるための緩いキュー / トリガーとして残す。
- GuildBotics は GitHub / git / PR 操作を細かく内製しない。
- 実作業は現代の CLI agent に委譲し、GuildBotics は対象チケットまたは PR と作業方針を短く渡す。
- Mode / Role / Retrospective lane を ticket driven workflow の通常フローから外す。

対応順は #156 → #170 → #167 とする。

## 現行実装の課題

### ticket_driven_workflow が多くの責務を抱えている

現行の `guildbotics/templates/commands/workflows/ticket_driven_workflow.py` は、次の処理を一つの workflow に持っている。

- GitHub ProjectV2 から作業対象を取得する。
- チケットの Status を `Ready` から `In Progress`、`In Progress` から `In Review` へ移動する。
- チケット本文とコメントから `Message` 履歴を構築する。
- 担当 agent か、mention されているかを判定する。
- `Role` を LLM で推定して Project custom field へ保存する。
- `Mode` を LLM で推定して Project custom field へ保存する。
- `comment` / `edit` / `ticket` の mode command に dispatch する。
- `Retrospective` status の場合は `workflows/retrospective` を呼ぶ。
- 実行結果をチケットコメントへ投稿する。

この構造は、LLM API呼び出しを前提とした旧世代の技術に基づいた設計であり、CLI agent が GitHub / git / PR 操作を自律的に実行できる現在の状況においては過剰である。
特に Mode / Role 判定は ProjectV2 custom field と結びついており、利用者と実装の両方に余計な複雑さを作っている。

### edit_mode が PR / git 操作を内製している

現行の `guildbotics/templates/commands/workflows/modes/edit_mode.py` は、次の処理を行う。

- CLI agent にファイル編集を依頼する。
- git diff から commit message を生成する。
- commit / push を行う。
- PR description を生成する。
- PR を作成する。
- PR review comments を取得する。
- review comment が ACK でよいか、修正が必要かを判定する。
- inline review thread に返信する。

#156 の目的は「CLI agent が GitHub チケットや PR を読めることを前提に、GuildBotics 側の独自処理を減らす」ことである。そのため、`edit_mode.py` のような内製 PR 対応ロジックは除去する。

### GitHubTicketManager が ProjectV2 custom field に強く依存している

現行の `guildbotics/integrations/github/github_ticket_manager.py` は、次の custom field を管理している。

- `Agent`
- `Mode`
- `Role`
- `Owner`
- `Due Date`
- `Priority`

特に `Mode` と `Role` は workflow の分岐と LLM 判定に直接つながっている。#156 ではこれらを通常フローから削除する。
`Owner` も workflow 制御には使わず、担当判定は GitHub assignee と `Agent` field に寄せる。`Priority` / `Due Date` は存在する場合に候補の並び順へ使うだけの任意情報とし、必須 custom field にはしない。

ただし、ProjectV2 自体は作業キューとして有用である。Project lane と担当 agent を見れば、scheduler polling で「今この agent が対応すべきチケット」を判定できる。そのため ProjectV2 は残すが、役割を限定する。

### 現行のイベント runner は chat 専用である

現行の `guildbotics/drivers/event_listener_runner.py` と `guildbotics/runtime/event_listener.py` は Slack chat workflow を前提としている。GitHub issue / PR event をそのまま流し込む設計にはなっていない。

#167 は #156 後に、簡略化された ticket workflow の入力に GitHub event を渡す形で実装する方がよい。

## #156 の決定方針

### 後方互換は考慮しない

既存の `Mode` / `Role` custom field、`Retrospective` lane、mode command 分岐との互換維持は行わない。

これにより、次のような中途半端な実装を避ける。

- 旧 Mode があれば旧 mode command を呼び、新しい場合は CLI agent に委譲する。
- 旧 Role があれば active role を切り替え、なければ default role を使う。
- Retrospective lane だけ旧 workflow を残す。
- `In Review` lane だけ旧 PR review logic を使う。

ticket driven workflow は新方針に一本化する。

### ProjectV2 はキュー / トリガーとして残す

GitHub ProjectV2 は、作業対象を見つけるための緩いキューとして使う。

ProjectV2 に期待する責務は次のみに限定する。

- チケットが workflow 対象かどうかを lane で判断する。
- どの agent が対応すべきかを、GitHub assignee または `Agent` field で判断する。
- 作業済みチケットを `Done` として除外する。

ProjectV2 に期待しない責務は次の通り。

- 作業の種類を `Mode` で指定する。
- 実行時 role を `Role` で指定する。
- レビュー対応か通常実装かを lane 名で厳密に判定する。
- Retrospective の起点として使う。

### lane の意味は緩くし、GitHub Projects の既定に寄せる

workflow 判定に必要な lane の意味は、次の2つを基本とする。

- 着手可能 lane: 新規に着手してよいチケット。既定値は `Todo`。
- 完了 lane: 対象外のチケット。既定値は `Done`。

GitHub Projects の標準的な Status option に合わせ、独自設定がない場合は `Todo` / `In Progress` / `Done` で動くようにする。これにより、GitHub Project を新規作成した利用者は lane mapping を設定しなくても ticket driven workflow を使える。

`In Progress` / `In Review` は必須 lane として扱わない。存在する場合でも同じ「作業中」扱いにする。

作業対象かどうかは board 上の Status option の**並び順（position）**で判定する。着手可能 lane と完了 lane を境界とし、その**間**に位置する lane（例: `In Review`）を作業中 lane とみなす。着手可能 lane より**前**の lane（例: `Backlog`）と、完了 lane **以降**の lane（例: `Icebox`）は対象外とする。これにより、`Backlog` のような未着手 lane や `Icebox` のような保留 lane を、追加設定なしで誤起動から除外できる。`lane_map.working` は「着手時にチケットを移す書き込み先 lane」として用い、作業対象の判定そのものには使わない。

なお、境界の外（着手可能 lane より前 / 完了 lane 以降）の lane は、担当 agent への mention や未対応コメントがあっても起動対象としない。これらの明示シグナルは作業対象 window 内の lane でのみ評価する。

この方針により、ProjectV2 の board 設計を利用者に強く要求しない。

### 作業中チケットの起動要否は PR 状況で判断する

`In Progress` と `In Review` を分けず、作業中チケットでは PR の有無と状態で起動要否を判断する。

判定方針は次の通り。

1. 完了 lane の item は無視する。
2. 着手可能 lane で担当 agent が一致する item は起動対象にする。
3. 着手可能 lane 以外かつ完了 lane 以外の item は作業中候補とする。
4. 作業中候補に紐づく PR がない場合:
   - issue 側に最後の agent 応答以降のユーザーコメントまたは担当 agent mention があれば起動する。
   - それ以外は起動しない。
5. 作業中候補に紐づく PR がある場合:
   - PR が open で、未対応 review comment / review thread があれば起動する。
   - PR が open で未対応 review がなければ起動しない。
   - PR が merged または closed の場合は完了 lane へ移動する。完了 lane が見つからない場合は防御的 fallback として以後の対象外として扱う。

ここで重要なのは、PR review comment の内容の解釈や修正判断は GuildBotics では行わないことである。GuildBotics は「起動すべきか」だけを軽く判定し、実際の対応は CLI agent に渡す。

PR の紐付けは GitHub API で issue に関連する PR を取得する方法を primary とする。issue comment の `Output:` に含まれる PR URL は、GitHub API で関連 PR を取得できない場合の fallback として扱う。複数 PR が見つかる場合は open PR を優先し、open PR がなければ `updated_at` が新しい PR を使う。

### CLI agent への作業指示を中心にする

workflow は対象チケットまたは PR の URL を CLI agent に渡し、GitHub 操作と git 操作を任せる。

通常チケットの場合の指示例:

```text
チケット <URL> の内容を確認し、妥当な対応を実施してください。
必要に応じて修正、コミット、push、PR 作成、チケットコメントを行ってください。
```

PR review 対応の場合の指示例:

```text
PR <URL> にレビューコメントがあります。
内容を確認し、妥当であれば修正、コミット、push をお願いします。
また修正をした場合もしなかった場合もレビュースレッドへのコメント追加をお願いします。
```

チケット作成依頼の場合も、GuildBotics が `identify_next_tasks` で作成候補を抽出して `create_tickets` するのではなく、CLI agent に GitHub issue 作成を依頼する。

### Status 更新は最小限にする

GuildBotics が行う Status 更新は次に限定する。

- 着手可能 lane のチケットを起動対象として採用した場合、可能なら作業中 lane へ移動する。
- PR が merged / closed になった場合、可能なら完了 lane へ移動する。

作業中 lane は必須ではないため、移動先は次の順に決める。

1. `lane_map.working` が設定されていれば、その lane へ移動する。
2. `lane_map.working` が未設定なら、既定の `In Progress` が存在する場合にそこへ移動する。
3. どちらもなければ移動しない。

完了 lane が存在しない設定は diagnostics で検出する。ただし runtime 中に完了 lane を解決できない場合は、ProjectV2 の status 更新は行わない。この場合も、起動要否は GitHub 上の最新コメント、PR review thread、reaction を見て判定する。

### lane mapping 設定を任意の上書き設定として再導入する

ProjectV2 を緩いキュー / トリガーとして残す以上、lane 名に関する制約は完全には消えない。ただし、GitHub Projects の既定に近い `Todo` / `In Progress` / `Done` で賄える場合は、利用者が lane mapping を設定しなくても動くようにする。

一方で、既存の GitHub Project では `Todo` / `Done` ではなく、`Ready` / `Completed` / `完了` のような独自 status 名が使われることがある。その場合だけ、GUI から新方針に必要な最小限の lane mapping を上書き設定できるようにする。

過去には `config verify` の対話的ステータスマッピング機能が存在したが、GUI に等価機能がないため削除され、`project.yml` の `services.ticket_manager.status_map` 手編集に移行していた。#156 後は GUI を主導線にするため、手編集前提だけでは運用上詰まりやすい。

ただし、旧 `status_map` をそのまま復活させる必要はない。旧仕様の `new` / `ready` / `in_progress` / `in_review` / `retrospective` / `done` 全体を設定対象にすると、#156 の目的である lane 制約の緩和に反する。新しい設定は、workflow 判定に必要な意味だけに絞る。

既定値:

- 着手可能 lane: `Todo`
- 完了 lane: `Done`
- 作業中へ移動する lane: `In Progress`

任意設定で上書きできる値:

- `ready`: 新規着手トリガー。
- `done`: 完了済みとして除外する出口。
- `working`: 着手可能 lane から採用したチケットを移動する作業中 lane。

`In Progress` / `In Review` は同じ作業中扱いにするため、別々の設定としては持たない。PR review 対応かどうかは lane 名ではなく PR の状態と未対応 review comment の有無で判断する。

設定例:

```yaml
services:
  ticket_manager:
    lane_map:
      ready: Todo
      done: Done
      working: In Progress
```

この設定は既定値と同じなので、省略してもよい。独自 lane 名を使う場合だけ保存する。

`working` が未設定、または ProjectV2 に存在しない場合は、着手可能 lane から採用した後も lane 移動を行わない。これはエラーではなく許容する。重複起動は GitHub 上の最新コメント、PR review thread、reaction の状態により抑止する。

着手可能 lane または完了 lane が ProjectV2 に存在しない場合は、ticket driven workflow の基本判定ができない。この場合は diagnostics で error として扱い、runtime 起動前に設定修正を促す。

`lane_map` と旧 `status_map` は同時に扱わない。後方互換を考慮しない方針のため、#156 実装時に `status_map` は廃止し、setup service / template / README / diagnostics は `lane_map` 前提へ更新する。

### GitHub 上の状態を正として起動要否を判定する

起動要否は GitHub 上の観測可能な状態を正として判定する。

基本原則:

- GuildBotics は毎回 GitHub の最新 issue / PR 状態を取得して判定する。
- 最後の応答者が自分または自分の proxy agent であれば、追加対応は不要とみなす。
- 最後の応答者がユーザー、reviewer、または他 agent であれば、担当条件を満たす場合に起動候補とする。
- 起動した CLI agent が対応不要と判断した場合も、GitHub 上にコメントまたは reaction で痕跡を残す。
- event-driven 実装後も、event は再判定のきっかけに過ぎない。最終的な起動要否は GitHub の現状態で判断する。

issue comment の判定:

- comment がない着手可能 lane の担当 issue は起動対象にする。
- 最後の issue comment が自分または自分の proxy agent のコメントなら起動しない。
- 最後の issue comment がユーザーまたは他者のコメントで、担当 issue または担当 agent mention がある場合は起動対象にする。
- 自分の proxy agent がコメント末尾に signature を付ける場合、その signature を使って自分の応答として識別する。
- reaction だけで対応済みにしたい場合は、担当 agent が付けた reaction を起動抑止の根拠として扱う。ただし、reaction の種類と意味は実装時に絞る。

PR review の判定:

- PR が merged または closed の場合は起動せず、可能なら完了 lane へ移動する。
- open PR に未対応 review thread がある場合は起動対象にする。
- review thread の最後のコメントが自分または自分の proxy agent なら、その thread は対応済みとみなす。
- review thread の最後のコメントが reviewer なら、その thread は未対応とみなす。
- top-level review comment / PR conversation comment も同様に、最後の relevant comment が自分か reviewer かで判定する。
- 担当 agent が reaction で ACK 済みの review comment は起動対象から外す。
- review comment の内容が妥当か、修正が必要か、コメント返信だけでよいかは GuildBotics では判定しない。CLI agent に委譲する。

## この方針で削除・縮小できるもの

削除または通常フローから除外できるもの:

- `Mode` custom field
- `Role` custom field
- `Owner` custom field による workflow 制御
- 旧 `status_map`
- `Task.get_available_modes()` の ticket workflow 依存
- `identify_mode`
- `identify_role`
- `identify_next_tasks` の ticket workflow 利用
- `workflows/modes/comment_mode`
- `workflows/modes/edit_mode`
- `workflows/modes/ticket_mode`
- ticket driven workflow からの `workflows/retrospective` 呼び出し
- `Retrospective` lane 前提の README / setup / diagnostics
- GuildBotics 側の commit / push / PR 作成 / PR review 返信の内製ロジック

残すもの:

- GitHub ProjectV2
- `Status` field
- GitHub assignee 判定
- GitHub assignee で表現できない member 用の `Agent` field
- GitHub assignee の assignable check
- issue comment / PR review thread / reaction の読み取り
- GitHub API による issue linked PR 取得
- issue comment / AgentResponse の `Output:` PR URL fallback
- runtime error などの最小限の system comment 投稿
- 最小限の PR 状態確認
- 最小限の Project status 更新

これにより、GuildBotics は「GitHub ProjectV2 を巡回して agent に作業対象を渡す runtime」として単純化され、具体的な GitHub / git / PR 操作は CLI agent に任せられる。

## #156 の実施内容

### 1. ticket workflow を単一委譲フローへ変更する

`guildbotics/templates/commands/workflows/ticket_driven_workflow.py` を、Mode dispatch ではなく単一の CLI agent 委譲フローへ変更する。

削除または通常フローから除外する処理:

- `identify_role`
- `identify_mode`
- `_mode_to_command_name`
- `workflows/modes/comment_mode`
- `workflows/modes/edit_mode`
- `workflows/modes/ticket_mode`
- `workflows/retrospective` 呼び出し
- `Mode` / `Role` の `update_ticket`

残す処理:

- ticket manager から次の作業対象を取得する。
- 対象の URL と状態から CLI agent 向け指示を生成する。
- CLI agent を起動する。
- 必要最低限の Project status 更新を行う。
- エラー時にチケットコメントへ失敗内容を残す。

### 2. CLI agent 委譲用 command を追加する

現行の `functions/edit_files` は、cwd 配下のファイル編集に主眼がある。GitHub issue / PR の読み取り、PR 作成、review thread 返信まで任せるには、専用 command を追加する方が意図が明確である。

候補:

- `functions/handle_github_ticket`
- `functions/handle_ticket_work`

この command は `AgentResponse` を返す。

prompt には次を含める。

- 対象 issue URL
- 対象 PR URL がある場合は PR URL
- 作業種別の短い説明
- 作業記録ファイルのパス
- 完了時に期待する出力形式
- 不明点がある場合は ticket comment / PR thread に質問すること
- レビューコメントが妥当でない場合は、修正せず理由を review thread に返すこと

### 3. GitHubTicketManager を trigger source として整理する

`GitHubTicketManager` の責務を次に絞る。

- ProjectV2 item を取得する。
- Status field を読み取る。
- GitHub assignee と `Agent` field から担当判定する。
- issue comments を読み取る。
- issue URL を返す。
- 必要最低限の Status 更新を行う。
- runtime error など、workflow 側でしか判断できない最小限の system comment を追加する。

削除する責務:

- `Mode` custom field の作成・更新・読み取り。
- `Role` custom field の作成・更新・読み取り。
- `Owner` custom field に基づく workflow 制御。
- `identify_next_tasks` による task 作成のための Project field 設定。
- Retrospective status を特別扱いする処理。

`Agent` field は、GitHub assignee だけで担当 agent を表現できない場合のために残す。GitHub assignee として検証可能な human / bot account では assignee を優先する。一方で、代理エージェント、または GitHub assignee として検証できない GitHub App / bot / 外部 agent を member として使う場合は `Agent` field を必須にする。
通常の作業コメント、質問、PR review thread 返信、対応不要の ACK は CLI agent の責務とし、`GitHubTicketManager` には戻さない。

### 4. ProjectV2 item の候補抽出を新しい lane 方針に合わせる

`get_task_to_work_on` 相当の処理は、固定順 `Retrospective` → `In Review` → `In Progress` → `Ready` で lane を巡回する実装から、前述の lane 方針に基づいて ProjectV2 item を候補抽出する実装へ変更する。

実装タスク:

- ProjectV2 item を取得し、各 item の Status option を有効な lane 名へ正規化する。
- 有効な lane 名は `lane_map` があればそれを使い、未設定なら `ready=Todo`、`done=Done`、`working=In Progress` を使う。
- 完了 lane の item は候補から除外する。
- GitHub assignee または `Agent` field で担当 agent を判定する。ただし、issue comment / body に担当 agent への明示 mention がある場合は候補にできる。
- 着手可能 lane の item を新規作業候補として優先する。
- 着手可能 lane 以外かつ完了 lane 以外の item は作業中候補として扱い、GitHub 上の issue comment / PR review 状態判定へ渡す。
- 起動すべき item が複数ある場合は priority / due date / created_at で並べる。

`Priority` / `Due Date` は workflow の制御必須項目ではないが、存在する場合は並び順に利用してよい。

### 5. PR 状況判定を trigger 用に限定する

PR review 対応の起動判定では、現行の `edit_mode.py` のように内容を解釈しない。

実装するのは、前述の「GitHub 上の状態を正として起動要否を判定する」方針に必要な情報取得だけに限定する。

- GitHub API で issue に関連する PR を取得する。
- GitHub API で関連 PR を取得できない場合だけ、issue comments または過去の AgentResponse の `Output:` から PR URL を fallback として見つける。
- 複数 PR が見つかった場合は open PR を優先し、open PR がない場合は `updated_at` が新しい PR を選ぶ。
- PR が open / merged / closed のどれかを取得する。
- PR review thread / PR conversation comment / reaction を取得し、起動要否判定に渡す。

review comment の妥当性判断、修正要否判断、ACK、返信文作成は CLI agent に委譲する。

### 6. retrospective を ticket driven workflow から外す

`Retrospective` status と `workflows/retrospective` は、ticket driven workflow の通常フローから外す。

既存の retrospective command 自体は、必要であれば standalone command として残せる。ただし、Project lane に移動しただけで自動実行される導線は削除する。

README / docs / setup UI の説明からも、ticket driven workflow の capability としての Retrospective は削除する。

### 7. setup service / diagnostics / desktop 表示を更新する

setup service:

- GitHub Project 設定を `status_map` から `lane_map` へ変更する。
- `lane_map` は任意の上書き設定として扱う。未設定時は `ready=Todo`、`done=Done`、`working=In Progress` を使う。
- 既定テンプレートから旧 `status_map` を削除し、`Retrospective` 前提を外す。
- ProjectV2 custom field の説明から `Mode` / `Role` を外す。
- lane 説明を「GitHub Projects 既定の `Todo` / `In Progress` / `Done` でそのまま使える。独自 lane 名の場合だけ上書きする」に変更する。

diagnostics:

- GitHub Project access check は残す。
- `Mode` / `Role` custom field の存在を前提にしない。
- GitHub assignee で担当判定できる member は、GitHub の assignable check で検証する。
- 代理エージェント、または assignable check で担当判定できない GitHub App / bot / 外部 agent を member として使う場合は、ProjectV2 の `Agent` field を必須として検証する。未設定または field が存在しない場合は error とする。
- ProjectV2 の Status options を取得し、有効な着手可能 lane / 完了 lane が存在するか検証する。`lane_map` 未設定時は `Todo` / `Done` を検証する。
- 作業中へ移動する lane は任意扱いにする。`lane_map.working` が設定されている場合はその存在を検証し、存在しない場合は warning とする。未設定時は既定の `In Progress` が存在すれば利用する。既定の `In Progress` が存在しない場合は info とし、runtime 起動を止める error にはしない。
- `In Review` という固定名は存在チェックしない。PR review 対応は lane 名ではなく PR 状態で判断する。

desktop:

- `ticket_driven_workflow` が GitHub 必須 routine であることは維持する。
- UI 表示は「巡回して ProjectV2 から作業対象を探す」説明へ寄せる。
- Mode / Role / Retrospective を前提にした文言があれば削除する。
- GitHub setup に Project lane mapping の設定 UI を追加する。
- lane mapping UI は詳細設定として扱い、GitHub Projects 既定 lane を使う場合は設定不要であることを明示する。
- Project の Status options を取得できる場合は select で選ばせる。
- Status options を取得できない場合は手入力 fallback を許可する。

README / docs:

- GitHub ProjectV2 は task board ではなく、agent assignment と lane を見る queue として説明する。
- 利用者に `Mode` / `Role` field 設定を求めない。
- `Retrospective` lane を通常セットアップから削除する。
- GitHub Projects 既定 lane なら設定不要であること、独自 lane 名の場合のみ `lane_map.ready` / `lane_map.done` / `lane_map.working` を設定することを説明する。

### 8. GUI lane mapping 設定を追加する

desktop の setup 画面に、GitHub ProjectV2 lane mapping の設定 UI を追加する。

目的:

- GitHub Projects の既定 lane を使う場合は、追加設定なしで ticket driven workflow を使えるようにする。
- 既存 GitHub Project の lane 名が `Todo` / `Done` と一致しない場合でも GUI だけで上書き設定できるようにする。
- `project.yml` 手編集を通常導線から外す。
- #156 後の lane 制約を、利用者に分かる言葉で明示する。

配置:

- GitHub 連携設定セクション内に置く。
- GitHub 連携を無効にしている場合は表示しない。
- `ticket_driven_workflow` を使うための詳細設定として扱う。初期状態では GitHub Projects 既定値を表示し、利用者が変更しなくても保存できる。

入力項目:

- `着手可能 lane`
  - `lane_map.ready` に保存する。
  - 既定値は `Todo`。
  - 説明: この lane にある担当チケットを GuildBotics が新規作業として拾う。
- `完了 lane`
  - `lane_map.done` に保存する。
  - 既定値は `Done`。
  - 説明: この lane にあるチケットは GuildBotics の対象外にする。
- `作業中へ移動する lane`
  - `lane_map.working` に保存する。
  - 既定値は `In Progress`。空欄も許可する。
  - 説明: `着手可能 lane` から拾ったチケットを、起動時にこの lane へ移動する。未設定の場合は lane 移動しない。

UI 挙動:

- Project の Status options を backend から取得できる場合は select で選択させる。
- 取得できない場合、または GitHub 認証前の場合は text input で入力できるようにする。
- `着手可能 lane` と `完了 lane` が同じ値の場合は保存前 validation error にする。
- `作業中へ移動する lane` が `着手可能 lane` または `完了 lane` と同じ場合は warning または validation error にする。基本は error が望ましい。
- `作業中へ移動する lane` は空欄を許可する。
- 既存 Project から取得した options に `着手可能 lane` または `完了 lane` が存在しない場合は保存前 validation error にする。
- 既存 Project から取得した options に `作業中へ移動する lane` が存在しない場合は warning を出す。

backend API:

- `ProjectSetupInput` / `ProjectUpdateInput` に `lane_map` 相当の field を追加する。
- `ProjectConfigSnapshot` に保存済み lane mapping を返す。
- Project status options を返す API を追加または既存 diagnostics の取得処理を再利用する。
- API は GitHub を更新せず、Status options の読み取りだけを行う。

保存形式:

```yaml
services:
  ticket_manager:
    name: GitHub
    owner: GuildBotics
    project_id: "1"
    url: https://github.com/orgs/GuildBotics/projects/1
    lane_map:
      ready: Todo
      done: Done
      working: In Progress
```

`lane_map` が未設定の場合も、上記と同じ既定値で扱う。保存時は、既定値から変更されていない場合に `lane_map` を省略してもよいし、明示的に保存してもよい。どちらにするかは GUI の実装時に決める。

validation:

- GitHub 連携有効時、着手可能 lane と完了 lane は有効値を持つ必要がある。未入力なら既定値 `Todo` / `Done` を使う。
- 作業中へ移動する lane は空欄を許可する。
- `lane_map` の値は空白を trim して保存する。
- 空文字は未設定として扱う。
- Status options を取得済みで、有効な着手可能 lane / 完了 lane が存在しない場合は保存前 validation error にする。
- Status options を取得できない場合は手入力保存を許可するが、diagnostics で GitHub Project access と lane 存在確認を行う。

diagnostics:

- GitHub Project の Status options を取得する。
- 有効な着手可能 lane が存在するか確認する。`lane_map.ready` 未設定時は `Todo` を確認する。
- 有効な完了 lane が存在するか確認する。`lane_map.done` 未設定時は `Done` を確認する。
- `lane_map.working` は設定されている場合のみ存在確認する。
- 旧 `status_map` の存在は warning とし、#156 後の設定では使わないことを示す。

テスト:

- setup service が `lane_map` を YAML に保存する。
- setup service が既存 `lane_map` を snapshot に復元する。
- `lane_map` 未設定時に `Todo` / `Done` / `In Progress` の既定値で扱われる。
- GitHub 連携無効時は `lane_map` を保存しない。
- `ready` / `done` が空の場合に既定値が使われる。
- `working` は空でも保存できる。
- desktop form で Status options から `ready` / `done` / `working` を選択して保存 payload に入る。
- desktop form で `ready` と `done` が同じ場合に保存できない。
- diagnostics が有効な着手可能 lane / 完了 lane の存在を検証する。
- diagnostics が `working` 未設定を error にしない。

## #170 の実施方針

#170 は #156 の後に実装する。

目的は、ticket / PR 対応中の作業記録を agent 個人の作業メモとして残し、別セッションや別 context でも引き継げるようにすることである。

### 作業記録の性質

作業記録は memory そのものではなく、まずは作業中の引き継ぎファイルとして扱う。これは起動要否判定に使う状態管理ではなく、CLI agent と後続セッションが作業内容を把握するための記録である。

記録する内容:

- 対象 ticket / PR
- 作業目的
- 調査したファイルや検索語
- 実施したコマンド
- 判断したこと
- 採用しなかった案と理由
- 未解決事項
- 次にやるべきこと
- 作成した branch / commit / PR
- レビューコメント対応状況

### 保存先

永続保存先は `~/.guildbotics/data` 配下に置く。

候補:

```text
~/.guildbotics/data/ticket_work_logs/<person_id>/<owner>/<repo>/<issue_number>.md
```

ただし、CLI agent の既存 prompt は cwd 配下のファイル操作を前提にしている。そのため、CLI agent に直接 home 配下の絶対パスを編集させるのではなく、次のどちらかにする。

1. workflow 側が永続ファイルを作成・追記し、CLI agent には内容を prompt として渡す。
2. workspace repo 内に `.guildbotics-worklog.md` のような一時作業記録を置き、終了時に workflow 側が `~/.guildbotics/data` へ同期する。

後者の方が CLI agent の既存制約に合う。

### CLI agent への指示

#156 で追加する ticket handling command の prompt に、作業記録ファイルの扱いを含める。

例:

```text
作業開始時に <worklog path> を確認してください。
作業中に分かったこと、実行した調査、判断、未解決事項を追記してください。
別セッションがこのファイルだけで作業を引き継げる粒度で記録してください。
```

## #167 の実施方針

#167 は #156 と #170 の後に実装する。

目的は、ticket driven workflow を polling 中心から event-driven に寄せることである。

### #156 後のイベント入力

#156 後の workflow は、最終的に次のどちらの入力でも動くようにする。

- polling により ProjectV2 から見つかった issue / PR URL
- GitHub event により渡された issue / PR URL

この共通入力により、polling と event-driven で実作業処理を共有できる。

### EventListenerRunner との関係

現行の `EventListenerRunner` は Slack chat event 専用である。GitHub event を扱うには、次のどちらかを選ぶ。

1. 汎用 event runner に抽象化する。
2. GitHub 専用 runner を追加する。

最初は GitHub 専用 runner を追加する方が安全である。Slack chat event と GitHub issue / PR event は payload も subscription 単位も重複排除の基準も異なるため、無理に共通化すると複雑になる。

ただし、dispatch 先の workflow は #156 で簡略化した ticket workflow と共有する。

### GitHub event で扱う対象

初期対象:

- issue opened
- issue edited
- issue comment created
- issue assigned
- project item status changed
- pull request review submitted
- pull request review comment created
- pull request closed / merged

event-driven 実装後も、取りこぼし対策として低頻度 polling は残してよい。event は即時起動、polling は補正という位置づけにする。

## 実装順序

### Phase 1: #156 の core workflow

目的: ProjectV2 から起動対象を判定し、CLI agent に issue / PR URL を渡して実作業を委譲する。着手可能 lane の新規 ticket と、作業中 ticket の issue comment / PR review trigger は同じ候補抽出と起動判定に乗るため、同じ phase で実装する。

実施内容:

- `ticket_driven_workflow.py` の mode dispatch を削除する。
- ticket handling 用 command を追加する。
- `GitHubTicketManager` から `Mode` / `Role` custom field の作成・更新を削除する。
- 有効な着手可能 lane / 完了 lane 中心の lane 判定に変更する。`lane_map` 未設定時は `Todo` / `Done` を使う。
- 着手可能 lane の ticket 起動時に可能なら作業中 lane へ移動する。既定の移動先は `In Progress` とする。
- GitHub 上の最新 issue comment に基づく起動判定を追加する。
- issue に関連する PR 取得と PR 状態取得を trigger 判定に限定して実装する。
- PR 紐付けは GitHub API を primary、issue comment / AgentResponse の `Output:` URL を fallback とする。
- 未対応 PR review comment / review thread がある場合に CLI agent を起動する。
- review comment / thread の対応済み判定は、最後の relevant comment が自分側か reviewer 側か、および自分側 reaction があるかで行う。
- PR merged / closed 時に可能なら完了 lane へ移動する。
- retrospective 自動実行と旧 mode command の通常経路呼び出しを外す。

完了条件:

- 着手可能 lane の assigned ticket で CLI agent が起動される。
- GitHub Projects 既定の `Todo` / `In Progress` / `Done` で独自設定なしに動く。
- `Mode` / `Role` がなくても workflow が動く。
- 旧 mode command が通常経路で呼ばれない。
- `In Progress` / `In Review` の区別なしに作業中 ticket を扱える。
- 未対応 review がある PR だけ起動される。
- GitHub 上で最後の応答者が自分側になっている comment / thread は再起動されない。

テスト:

- GitHub Projects 既定の `Todo` ticket が担当 agent に割り当てられている場合に起動される。
- `lane_map.ready` に独自 lane 名を設定した場合、その lane の ticket が起動される。
- 完了 lane の ticket は無視される。既定では `Done` を完了 lane とする。
- 着手可能 lane でも担当 agent が違う場合は無視される。
- mention がある場合は担当 agent が反応できる。
- `Mode` / `Role` field がなくても workflow が動く。
- `Retrospective` status が特別扱いされない。
- 着手可能 lane 以外の作業中 ticket で、最後の issue comment が自分側ではない場合だけ起動される。
- GitHub API で issue linked PR を取得できる場合、その PR を trigger 判定に使う。
- GitHub API で issue linked PR を取得できない場合、`Output:` の PR URL を fallback として使う。
- 複数 PR がある場合は open PR を優先し、open PR がない場合は `updated_at` が新しい PR を使う。
- PR に未対応 review comment がある場合だけ起動される。
- PR が merged / closed の場合に完了 lane へ移動する。
- GitHub 上で自分側が最後に応答済みの issue comment / review thread が重複処理されない。

### Phase 2: #156 の GUI / setup / diagnostics

目的: core workflow の新仕様を、GUI、setup service、diagnostics、利用者向け説明に反映する。ここでは workflow の起動判定や PR 判定のメインロジックは追加しない。

実施内容:

- setup service の GitHub Project 説明と `status_map` を `lane_map` 前提へ更新する。
- desktop setup に lane mapping 設定 UI を追加する。
- diagnostics を有効な着手可能 lane / 完了 lane 中心へ変更する。
- desktop i18n / UI の Mode / Role / Retrospective 前提文言を削除する。
- README / docs を新仕様へ更新する。

完了条件:

- GUI で GitHub Projects 既定 lane なら追加設定不要であることが分かる。
- 独自 lane 名の場合だけ GUI から `lane_map` を設定できる。
- 利用者向け説明に `Mode` / `Role` / `Retrospective` 必須前提が残っていない。
- GUI から見ても ProjectV2 は queue / assignment / status 管理として説明されている。

テスト:

- setup service が `lane_map` を保存し、snapshot に復元する。
- `lane_map` 未設定時に `Todo` / `Done` / `In Progress` の既定値で扱われる。
- GitHub 連携無効時は `lane_map` を保存しない。
- desktop form で Status options から `ready` / `done` / `working` を選択して保存 payload に入る。
- desktop form で `ready` と `done` が同じ場合に保存できない。
- diagnostics が GitHub assignee として検証できる member では `Agent` field を必須にしない。
- diagnostics が代理エージェント、または assignable check で担当判定できない GitHub App / bot / 外部 agent では `Agent` field 未設定を error にする。
- diagnostics が有効な着手可能 lane / 完了 lane の存在を検証する。
- diagnostics が `working` 未設定を error にしない。

### Phase 3: #170

目的: ticket / PR 対応の作業記録を残す。

実装前に決めること:

- 作業記録の workspace 内一時ファイル名。

実施内容:

- ticket work log store を追加する。
- workflow 開始時に作業記録を読み込む。
- CLI agent prompt に作業記録の参照・追記指示を含める。
- workspace 内一時ファイルと永続保存先の同期方式を実装する。

完了条件:

- 同じ ticket の次回起動時に前回作業記録が prompt に入る。
- 別セッションでも作業記録を読める。
- 作業記録がない場合でも workflow が動く。

テスト:

- 作業記録ファイルが作成される。
- 既存作業記録が次回 prompt に含まれる。
- workspace 内一時ファイルの内容が永続保存先に同期される。
- 作業記録がない場合でも workflow が失敗しない。

### Phase 4: #167

目的: GitHub event による即時起動を追加する。

実装前に決めること:

- GitHub event の受け口を webhook server とするか、GitHub App / API polling hybrid とするか。

実施内容:

- GitHub event listener / webhook receiver / event store の方式を決める。
- GitHub event を ticket workflow の共通入力に変換する。
- event id / delivery id による重複排除を実装する。
- polling と event の両方から同じ workflow を呼べるようにする。

完了条件:

- issue comment / PR review comment の event で対象 agent の workflow が起動する。
- 同じ event が重複配送されても二重実行しない。
- polling fallback が残り、event 取りこぼし時も復旧できる。

テスト:

- GitHub event payload が ticket workflow input に変換される。
- event id / delivery id で重複排除される。
- 対象外 event は無視される。
- event-driven と polling が同じ ticket handling path を使う。
