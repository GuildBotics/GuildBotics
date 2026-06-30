# Desktop 人間メンバー設定の整理方針

## Summary

Desktop アプリのメンバー設定画面では、人間メンバーを AI agent の実行主体としてではなく、チーム内の実在人物・役割・外部アカウント参照として扱う。

この方針に合わせて、人間メンバーの設定 UI から AI agent 実行にしか使わない項目を除去する。一方で、人間メンバーであること、実行対象にならないこと、GitHub 上では人間アカウントとして扱うことを理解しやすくする read-only UI は残す。

## 背景

現在の実装では、人間メンバーは Desktop 設定保存時に `is_active: false` 固定で保存される。

```text
person_type == "human" -> is_active: false
```

runtime 側も `is_active` なメンバーだけを scheduler worker / event listener の対象にする。したがって、人間メンバーは通常運用では patrol、scheduled command、routine command、incoming chat workflow の実行主体にならない。

一方で、設定画面には agent 実行用の文体・人格・個別 intelligence・patrol 設定などが人間メンバーにも見えており、実際の利用モデルとずれている。

また、CLI/API には明示的に `--person <human>` を指定した場合に人間メンバーを context / diagnostics / command 実行対象にできる経路が残っている。この到達可能性は、人間メンバーの通常ユースケースではなく、実行境界の不足として扱う。

## 人間メンバーの役割

人間メンバーは次の情報を表す。

- チーム内の実在人物
- その人物が持つ役割や判断観点
- Slack 上の user id
- GitHub 上の user account
- avatar など、UI 上の識別情報

人間メンバーは次のものではない。

- scheduler / event listener が実行する AI worker
- CLI agent / LLM / model 設定を持つ実行主体
- Slack bot / Socket Mode app token を持つ購読主体
- GitHub App / machine user / proxy agent として認証する主体

## 確定した UI 方針

### 人間メンバーに残す項目

次の項目は人間メンバーでも表示・編集対象として残す。

- `person_id`
- `name`
- `person_type`
- `roles`
- `Slack User ID`
- `GitHub username`
- `Git email`
- avatar

`roles` は残す。たとえば、人間だけが `product` / `owner` / `domain_expert` のようなロールを持ち、開発方向性やプロダクト判断の観点を担う構成は自然に発生する。

現行 chat workflow でも、Slack 上で mentionable な別メンバーを handoff 候補にするとき、そのメンバーの roles が参照される。人間メンバーの roles は、AI agent が「どの観点を誰に振るべきか」を判断する材料になる。

### 人間メンバーに disabled のまま残す項目

次の UI は削除せず、現状と同じく disabled / read-only 表示として残す。

- `Active` switch
- `GitHub account type` selector

`Active` switch は、人間メンバーが runtime worker にならないことを明示する UX として有用である。操作不可である理由も画面上で説明する。

`GitHub account type` selector は、人間メンバーが GitHub 上では `human` account として扱われ、GitHub Apps / machine user / proxy agent ではないことを明示する UX として有用である。人間メンバーでは `human` 固定で表示し、操作不可とする。

### 人間メンバーから除去する項目

次の項目は人間メンバーの通常ユースケースでは使わないため、Desktop 設定画面から除去する。

- `speaking_style`
- `character`
- `relationships`
- `Intelligence` タブ
- `Patrol` タブ
- GitHub Apps 認証項目
  - installation id
  - app id
  - private key path
- PAT / machine user / proxy agent 用 GitHub access token
- agent 用 Slack 設定
  - Slack channel subscriptions
  - participation policy
  - Slack bot token
  - Slack app token

agent 用 Slack 設定は、現状でも人間メンバーには `Slack User ID` のみ表示されており、bot/app token や channel subscription は表示されない。今後もこの方針を維持する。

## 実装上の注意点

### 1. 保存 payload と backend の挙動を揃える

frontend では既に、人間メンバーの保存 payload で次を空にしている。

- `slack_bot_token`
- `slack_app_token`
- `slack_channels`
- `slack_channel_participation`
- `routine_commands`
- `task_schedules`

backend の `SimplePersonSetupService.build_person_config()` も、人間メンバーでは `is_active: false`、`routine_commands: []`、`task_schedules: []` 相当になるようにしている。

UI から項目を消す場合も、この backend 側の guard は維持する。UI は誤入力防止、backend は永続化境界の正規化として二重に守る。

### 2. roles は必須入力として残す

人間メンバーの roles は、AI agent の人格設定ではなくチーム内の責務・判断観点として扱う。

したがって、`speaking_style` / `character` / `relationships` を人間メンバーから削除しても、roles validation は人間メンバーにも残す。

### 3. Basic タブの validation を person_type で分岐する

現状の member validation は、`speaking_style`、`characterArchetype`、`characterTraits`、`characterInterests`、`characterJoinWhen`、`characterAvoidWhen`、`characterContributionStyle` を必須として扱う。

人間メンバーからこれらの UI を削除する場合、validation も `personType === "human"` では要求しないように分岐する必要がある。

`canSubmit` の条件にも同じ分岐が必要である。UI だけを非表示にして validation を残すと、人間メンバーを保存できなくなる。

### 4. member request の character / speaking_style / relationships を空にする

人間メンバーでは、保存時に次を空値として扱う。

- `speaking_style: ""`
- `relationships: ""`
- `character: {}`

既存設定にこれらが残っている場合の扱いは実装時に決める。

推奨は、Desktop で人間メンバーを保存したタイミングで新方針に合わせて空へ正規化することである。既存 YAML に残った古い値を読み取り表示し続けると、削除方針と矛盾する。

### 5. Intelligence タブは人間メンバーでは表示しない

人間メンバーは CLI agent / LLM 実行主体ではないため、member-level intelligence override は設定対象にしない。

ただし backend には既存の member override ディレクトリが残っている可能性がある。Desktop UI から人間メンバーを保存したときに既存 override を削除するか、単に UI で到達不能にするかは実装時に明確に決める。

推奨は、実行境界を塞ぐ変更と合わせて、human member の intelligence override は不要データとして削除または無視することである。

### 6. Patrol タブは人間メンバーでは表示しない

人間メンバーは `is_active: false` 固定で worker が起動しないため、Patrol 設定は意味を持たない。

UI ではタブ自体を出さない。backend では引き続き `routine_commands` / `task_schedules` を永続化しない。

### 7. GitHub タブは人間向けに縮約する

人間メンバーの GitHub タブでは、次だけを扱う。

- disabled の `GitHub account type = human`
- `GitHub username`
- `Git email`
- `resolve` 操作
- `GitHub auth not required` の説明

GitHub App / machine user / proxy agent の認証欄は人間メンバーでは表示しない。

GitHub diagnostics では、人間メンバーは GitHub Project の `Agent` field ではなく GitHub assignee として扱われる。人間メンバーの username が GitHub user として解決できない場合は設定不備として扱う。

### 8. Slack タブは人間向けに維持する

人間メンバーの Slack タブでは `Slack User ID` を残す。

これは avatar import だけでなく、chat workflow が Slack user id を person id に対応づけ、thread participant label や handoff candidate を構成するために使う。

### 9. 実行経路の guard を追加する

UI 整理だけでは、人間メンバーを AI 実行主体として扱う抜け道が残る。

次の経路は、今後 human member を実行対象にしないよう guard を入れる。

- `guildbotics run --person <human>`
- `guildbotics run <command>@<human>`
- `guildbotics member context --person <human>`
- Desktop/API diagnostics の `person_id=<human>` 指定での LLM / CLI / Slack bot 実行チェック

`member context` は特に注意が必要である。現在は active/type でフィルタせず member を解決するため、人間メンバーでも AI 実行用 context が取得できる。人間メンバーを実行主体にしない方針なら、error にするか、人間用の非実行参照情報だけを返す別 endpoint / mode へ分ける。

### 10. diagnostics は人間メンバー向け check set を分ける

人間メンバーに対しては、LLM call、CLI agent detection、Slack bot credential、Socket Mode app token のような実行主体向け check を走らせない。

人間メンバー向け diagnostics を残す場合は、次のような静的 check に限定する。

- person config が読める
- roles が設定されている
- Slack User ID が形式として妥当
- GitHub username が設定されている
- GitHub username が user account として解決できる

### 11. テスト観点

実装時は少なくとも次をテストする。

- 人間メンバーでは Basic タブに roles は残り、speaking style / character / relationships は表示されない。
- 人間メンバーでは Active switch が disabled のまま表示され、説明文が出る。
- 人間メンバーでは Intelligence タブと Patrol タブが表示されない。
- 人間メンバーでは GitHub account type が `human` 固定 disabled で表示され、GitHub auth 項目は表示されない。
- 人間メンバーでは Slack User ID が表示され、agent 用 Slack token / channel 設定は表示されない。
- 人間メンバー保存 payload は `speaking_style: ""`、`relationships: ""`、`character: {}`、`routine_commands: []`、`task_schedules: []` になる。
- backend は人間メンバーに `is_active: false` を保存し、patrol 設定を YAML に出力しない。
- chat workflow の handoff candidates は、Slack User ID と roles を持つ人間メンバーを候補にできる。
- human member を command / diagnostics の実行主体にする経路が適切に拒否される。

## 参照した現行実装

- `desktop/src/setup/SetupPage.tsx`
  - member 設定 UI、保存 payload、validation。
- `guildbotics/editions/simple/setup_service.py`
  - person.yml の読み書き、human の `is_active` / patrol 設定正規化。
- `guildbotics/drivers/task_scheduler.py`
  - `is_active` な member のみ worker を起動。
- `guildbotics/drivers/event_listener_runner.py`
  - `is_active` な member のみ Slack event subscription 対象。
- `guildbotics/templates/commands/workflows/chat_conversation_workflow.py`
  - Slack user id から person label を構成し、roles を handoff candidates に渡す。
- `guildbotics/app_api/diagnostics.py`
  - person_id 指定時に inactive member も対象にできる現行挙動、GitHub human assignment check。
- `guildbotics/runtime/member_context.py`
  - explicit person 解決時に active/type でフィルタしない現行挙動。

## Open Questions

- Desktop で人間メンバー保存時、既存の member intelligence override directory を削除するか、UI 到達不能にするだけにするか。
- `guildbotics member context --person <human>` は error にするか、人間向け read-only context として再定義するか。
- human member diagnostics を残す場合、既存 `/diagnostics/scenario` に分岐を入れるか、人間向けの軽量 check endpoint を別にするか。
