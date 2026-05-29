# Person Memory Roadmap

## コンセプト

GuildBotics のエージェントは、単発のコマンド実行者ではなく、Slack・チケット・実装作業をまたいで文脈を保持し、人間メンバーと同じように会話し、判断し、不明点を確認しながら作業を進める存在にする。

そのための記憶は、person ごとの経験・会話履歴・判断傾向・関係性を反映する agent personal memory として扱う。GuildBotics のポリシーではエージェントの個性を重視するため、person ごとの記憶を第一級の概念にする。

リポジトリのソースコード、仕様、設計ドキュメントそのものを自動的に personal memory として保存することは、このロードマップの対象外とする。必要になった場合は別途設計する。

## 最終ゴール

エージェントが以下を自律的に行える状態を目指す。

- Slack の議論から、自分にとって再利用すべき文脈・判断・未決事項を記憶する
- チケット実装中に疑念が出たら、自分の過去 Slack 会話記憶から文脈を補完する
- 記憶で解決できない場合、Slack 上で適切な相手・チャンネルに自律的に問い合わせる
- 問い合わせ結果をその後の実装判断に反映し、必要なら記憶にも残す
- これを person ごとの個性・関心・会話方針に沿って行う

具体例として、Slack で新機能追加について議論し、そこから出たアイディアをもとにチケットが作成され、そのチケットで実装を行う。実装中に疑念や確認事項が出た場合、エージェントは過去 Slack 会話の記憶から文脈補完を試みる。それでも判断できない場合は、Slack で別のメンバーに問い合わせ、その回答を実装判断と記憶に反映する。

## 現行実装との関係

現行のチャットワークフローは、Slack への自律投稿能力をすでに持っている。したがって今後追加すべき中心は、投稿能力そのものではなく、いつ・何を・誰に問い合わせるかを決める文脈判断である。

現在の `MemoryBackend` は、チャット返信に topic memory を添えるための薄い永続化層である。`FileMemoryBackend` は person ごとの git-backed local repo に `memory_index.yml` と `topics/<topic_id>/memory.md` を保存する。これは透明性、追加依存なし、ローカル運用の容易さに強い一方、最終ゴールに対しては以下の限界がある。

- 想起が topic/title/summary の部分一致に近く、Slack・チケット・実装文脈を横断する検索には弱い
- memory が topic Markdown の全文置換であり、fact 単位の更新、矛盾検出、古い事実の無効化、provenance 管理を持たない
- TTL、importance、last accessed、superseded などの忘却・剪定 policy を扱えない
- topic Markdown が育つほど prompt 投入量が増え、必要な断片だけを返しにくい
- Slack thread、判断、質問、回答、実装文脈の関係性を表現しにくい
- 並行更新時の競合に弱い

したがって、`FileMemoryBackend` は fallback / test / migration aid として維持するが、最終的な personal memory 基盤の本命として高度化しすぎない。

## Backend 方針

`MemoryBackend` は chat topic 専用から、person memory を扱う抽象へ段階的に広げる。ただし、リポジトリのソースコードや仕様そのものを自動的に personal memory 化することは対象外とする。

候補 backend の位置づけは以下とする。

- Cognee backend: 標準 personal memory backend の第一候補。Slack thread、判断、質問、回答、作業履歴を knowledge engine として扱う方向に向く
- `FileMemoryBackend`: fallback / test / migration aid。軽量、透明、テスト容易。高度な検索・関係性・忘却は担わせすぎない
- Mem0 backend: chat-focused lightweight backend の候補。会話 memory 中心なら有力だが、最終ゴール全体には狭い可能性がある
- Graphiti backend: temporal correctness が本質になった段階の候補。初手には重い

現時点の方向性として、GuildBotics の最終像は単なる会話メモリよりも agent workflow memory / knowledge engine に近い。Slack の発言、チケット、実装メモ、判断、質問、回答、関係者を扱う必要があるため、標準 backend は Cognee 寄りで設計する。

CI や単体テストでは、標準 backend の選択とは別に fake / mock backend、または deterministic な mocked Cognee adapter を使う。LLM API key や live Cognee の挙動に依存する確認は opt-in の live integration test として分離する。

## 段階的タスク分解

各 Phase は、実装して実際に動かし、動作確認が OK であれば次の Phase に進める単位にする。設計だけで終わる Phase は置かない。各 Phase では、既存の自動テストに加えて、対象 workflow を通した手動または integration test 相当の確認を行う。

動作確認では、以下の person 設定を前提にする。

- Aiko: 計画、品質、設計整合性、リスク管理を重視する PM / architect。冷静・簡潔・論理的に、決定事項、未決事項、次アクションを整理する
- Yuki: UX/UI、ビジュアル、物語性、ユーザー感情を重視する designer / content creator。明るく軽やかに、ユーザー目線や楽しさを足す

### Phase 1: 現行 chat memory の土台整理

目的: 現在の chat memory を壊さず、後続 Phase で personal memory として拡張できる contract と観測点を用意する。

- `MemoryBackend` の責務を person memory として明文化する
- `MemoryQuery` / `MemoryItem` / `MemoryUpdate` を、後続で source / scope / metadata / retention を持てる形へ最小拡張する
- `FileMemoryBackend` を fallback として維持する境界を決める
- memory の位置づけを README / docs に反映する
- 既存の chat memory テストを更新し、現行の remember / recall が維持されることを確認する
- recall 結果を機械的に確認できる観測情報を追加する
  - `MemoryContext` または同等の trace に backend、query、matched topic/item id、match reason、score、source、scope を含める
  - recall されない場合も `items=[]` として明示的に確認できるようにする
  - chat workflow のテストで `chat_reply_input["memory_context"]` を検査できるようにする
- 観測情報は backend 固有のログではなく、`MemoryBackend` contract の一部として定義する
  - `FileMemoryBackend` / Cognee backend / fake backend は、同じ `MemoryContext` / trace 形式を返す
  - backend 固有の検索結果は adapter 層で `MemoryItem` / trace の共通形式へ正規化する
  - backend 固有の詳細は `metadata` に閉じ込め、chat workflow とテストは共通フィールドだけを見る
- memory trace 専用 JSONL 出力を追加する
  - 通常運用時は OFF にする
  - `GUILDBOTICS_MEMORY_TRACE=1` のような明示設定で ON にする
  - `GUILDBOTICS_MEMORY_TRACE_PATH` で出力先を指定できるようにする
  - 出力先未指定時は `~/.guildbotics/data/run/memory_trace.jsonl` を使う
  - 通常ログには混ぜず、memory recall / remember / non-recall を 1 event 1 JSON object として追記する
  - trace は `MemoryContext` / `MemoryWriteResult` など正規化済み contract から生成し、backend 固有の生 API に依存しない

動作確認:

- Slack chat workflow 相当のテストで、返信前に memory recall が呼ばれ、返信後に memory update が保存される
- 既存の `FileMemoryBackend` の memory repo / index / topic file が従来通り作成される
- `FileMemoryBackend` を使う既存テストが通る
- 関連 memory が recall されたかどうかを、返信文ではなく `memory_context` / recall trace で確認できる
- fake backend でも同じ `MemoryContext` / trace の contract test を通せる
- `GUILDBOTICS_MEMORY_TRACE=1` のときだけ memory trace JSONL が作成され、OFF のときは作成・追記されない

memory trace 出力例:

関連 memory が recall された場合:

```json
{
  "event": "memory.recall",
  "timestamp": "2026-04-29T12:34:56+09:00",
  "backend": "cognee",
  "person_id": "aiko",
  "query": {
    "thread_topic": "FocusFlow onboarding",
    "latest_focus": "how to present the onboarding steps",
    "transcript_excerpt": "さっき決めたFocusFlowのオンボーディング方針..."
  },
  "hits": [
    {
      "id": "focusflow-onboarding-plan",
      "title": "FocusFlow Onboarding Plan",
      "score": 0.82,
      "match_reason": "Related to prior decisions about FocusFlow onboarding steps and risks.",
      "source": {
        "type": "slack_thread",
        "service": "slack",
        "channel": "random",
        "thread_ts": "1720000000.000100"
      },
      "scope": {
        "person_id": "aiko"
      },
      "metadata": {
        "backend_item_id": "cognee-node-...",
        "dataset": "guildbotics:person:aiko"
      }
    }
  ]
}
```

関連 memory がない場合:

```json
{
  "event": "memory.recall",
  "timestamp": "2026-04-29T12:35:10+09:00",
  "backend": "cognee",
  "person_id": "yuki",
  "query": {
    "transcript_excerpt": "今日のランチ何にしようかな。"
  },
  "hits": []
}
```

memory を保存した場合:

```json
{
  "event": "memory.remember",
  "timestamp": "2026-04-29T12:36:00+09:00",
  "backend": "cognee",
  "person_id": "aiko",
  "item": {
    "id": "focusflow-onboarding-plan",
    "title": "FocusFlow Onboarding Plan",
    "source": {
      "type": "slack_thread",
      "service": "slack",
      "channel": "random",
      "thread_ts": "1720000000.000100"
    },
    "scope": {
      "person_id": "aiko"
    }
  },
  "result": {
    "changed": true,
    "reference": "cognee-node-..."
  }
}
```

利用者が動作確認で見るもの:

- `memory.recall` の `backend`, `person_id`, `query`, `hits`
- 期待する memory の `id` / `title` が `hits` に含まれるか
- 無関係な会話で `hits: []` になっているか
- `source` が Slack thread 由来になっているか
- `scope.person_id` が対象 agent になっているか
- `score` と `match_reason` が妥当か
- `memory.remember` の `result.changed` と `reference`

確認シナリオ:

この Phase の会話では、FocusFlow という架空の個人向けタスク管理アプリを題材にする。FocusFlow は新規企画であり、現行システムの利用状況や計測結果は存在しない前提にする。最初の発言に、システムの説明、初回オンボーディングの想定入力、達成したい体験、Aiko / Yuki に見てほしい観点を含める。これにより、memory update が「何を覚えるべきか」を、単なる「覚えてください」という依頼ではなく、具体的な会話文脈から判断できるかを確認する。

記憶を示唆する要素:

- FocusFlow が何のシステムか
- 初回オンボーディングで扱う予定の入力項目
- 目標が「面倒に感じさせず、すぐ使い始められること」であること
- Aiko はスコープ、懸念、未決事項、実装順を重視すること
- Yuki は初回印象、安心感、言葉選び、見た目の楽しさを重視すること

1. Slack の `random` チャンネルで、Ototadana が次の発言をする。

   ```text
   個人向けタスク管理アプリ FocusFlow の初回オンボーディングを新しく設計したいです。
   FocusFlow は、ユーザーが今日やるタスク、期限、優先度を登録できて、毎朝「今日の集中プラン」を提案するアプリです。

   初回オンボーディングでは、仕事時間帯、1日の集中ブロック数、通知の強さ、最初に登録する3つのタスクを設定してもらう案を考えています。
   目標は、初回設定を面倒に感じさせず、すぐ使い始められることです。

   Aikoにはスコープ、懸念、未決事項、実装順の観点で見てほしいです。
   Yukiには初回印象、安心感、言葉選び、見た目の楽しさの観点で見てほしいです。
   ```

2. Aiko が返信する場合、期待される返信は以下の方向性である。

   ```text
   まだ新規設計なので断定はできませんが、挙がっている入力項目をすべて初回に出すと重くなる懸念があります。
   まずは3ステップ以内に収める案として、初回に必須にする項目と後回しにできる項目を分けて検討するのがよいです。
   未決事項は、通知の初期値、タスク登録を必須にするか、完了後の遷移先です。
   ```

3. Yuki が返信する場合、期待される返信は以下の方向性である。

   ```text
   新規の初回体験なので、最初から「設定しなきゃ」って見えると少し重く受け取られるかもしれません。
   「今日の集中プランを一緒に作ろう」みたいに、すぐ役に立つ感じを前面に出したいな。
   3つのタスク登録も、空っぽのフォームより、軽い例やプレースホルダーがあると始めやすそう。
   ```

4. この Phase では、保存内容の高度化よりも、Aiko / Yuki の memory repo に従来通り topic memory が保存されることを確認する。

   Aiko の保存例:

   ```md
   # FocusFlow Onboarding Scope

   ## Summary
   - FocusFlowの初回オンボーディングでは、初回設定を面倒に感じさせず、すぐ使い始められることを目指す。

   ## Decisions
   - Aikoはスコープ、懸念、未決事項、実装順の観点で確認する。
   - 想定入力項目をすべて初回に出すと重くなる懸念があるため、3ステップ以内に収める案として検討する。

   ## Open Questions
   - 通知の初期値、タスク登録を必須にするか、完了後の遷移先が未決。

   ## Current Direction
   - 初回に必須にする項目と後回しにできる項目を分け、重くなりすぎない設計にする。
   ```

   Yuki の保存例:

   ```md
   # FocusFlow Onboarding Experience

   ## Summary
   - FocusFlowの初回オンボーディングは、設定作業ではなく「今日の集中プランを一緒に作る」体験として見せたい。

   ## Decisions
   - Yukiは初回印象、安心感、言葉選び、見た目の楽しさの観点で確認する。
   - 3つのタスク登録は、例やプレースホルダーで始めやすくする方向。

   ## Open Questions
   - 最初のCTA文言と、空状態で見せる例の具体案は未決。

   ## Current Direction
   - 「設定させる」印象を避け、すぐ役に立つ軽い体験として見せる。
   ```

次へ進む条件:

- 既存の chat memory 挙動に regressions がない
- 後続 Phase で source / scope / metadata を渡せる API 境界ができている
- recall の有無と対象 item を、自動テストで assertion できる
- backend が File / Cognee / fake に変わっても、chat workflow とテストが同じ `MemoryContext` / trace を検査できる

### Phase 2: Cognee chat memory 導入

目的: Cognee を標準 personal memory backend として導入し、chat workflow に統合する。Slack 会話から person ごとの memory を形成し、自然な会話参照で想起し、返信生成に使えることを vertical slice として確認する。

- Cognee backend を標準 personal memory backend として実装する
- person 単位 dataset / namespace 方針を決める
- `FileMemoryBackend` と Cognee backend が同じ `MemoryBackend` contract で使えるようにする
- backend selection の設定方法を追加し、通常実行時の default は Cognee にする
- `FileMemoryBackend` は明示指定時のみ使う fallback / test backend として維持する
- Cognee backend を chat workflow の recall / remember path に統合する
- Cognee の recall / search 結果を、Phase 1 で定義した `MemoryContext` / trace へ変換する adapter を実装する
- Slack thread 由来の metadata を memory update に渡す
- memory update prompt を「決定事項・未決事項・再利用文脈」中心に改善する
- Aiko / Yuki の person profile に応じて、同じ会話から異なる観点の memory が形成されるようにする
- recall 結果に根拠・source・scope を含める
- 返信 prompt に memory を少量かつ構造化して渡す
- memory update すべき内容がない会話では保存されないようにする
- Cognee の LLM / embedding 依存をテストで差し替えられる設計にする
- CI では fake / mock backend または deterministic な mocked Cognee adapter を使い、live LLM / embedding には依存しない
- recall 判定の確認は、返信文だけに依存せず、`memory_context.items` / recall trace の item id、source、scope、match reason を検査する
- chat workflow とテストは Cognee の生 API ではなく、正規化済み `memory_context` / trace を検査する

実装時の決定事項:

- Cognee は optional spike ではなく、通常実行時の標準 personal memory backend として扱う
- `FileMemoryBackend` は明示指定時の fallback / test / migration aid として残す
- backend 選択は `GUILDBOTICS_MEMORY_BACKEND` で上書き可能にする
  - 未指定時: `cognee`
  - `GUILDBOTICS_MEMORY_BACKEND=file`: `FileMemoryBackend`
  - `GUILDBOTICS_MEMORY_BACKEND=fake`: テスト用 fake backend
- Cognee のローカル開発・手動確認は追加ミドルウェアなしの default local 構成を前提にする
- LLM / embedding の API key が必要な live 確認は opt-in とし、CI の標準実行には含めない
- person ごとの namespace / dataset は分離する
  - Aiko: `guildbotics:person:aiko`
  - Yuki: `guildbotics:person:yuki`
  - 他 person も `guildbotics:person:<person_id>` の形にする
- `MemoryBackend.recall()` は backend に関係なく正規化済み `MemoryContext` を返す
- `MemoryBackend.remember()` は backend に関係なく正規化済み `MemoryWriteResult` を返す
- Cognee の node id、dataset、retrieval mode、raw score など backend 固有情報は `metadata` に閉じ込める
- chat workflow は Cognee の戻り値を直接扱わず、`MemoryContext` / `MemoryWriteResult` / memory trace の共通 contract だけを扱う
- memory trace は Phase 1 の仕様に従い、`GUILDBOTICS_MEMORY_TRACE=1` のときだけ JSONL に出力する
- Phase 2 の対象は Slack chat memory までとし、チケット作成や ticket workflow 連携は Phase 3 へ送る
- リポジトリのソースコードや仕様そのものを自動的に personal memory として保存しない

chat workflow 接続点:

- 返信生成前:
  - `_get_memory_backend(context)` が標準では Cognee backend を返す
  - `_memory_query(context, thread_context, transcript)` で作った query を Cognee backend に渡す
  - Cognee backend は recall 結果を `MemoryContext.items` に正規化する
  - `context.shared_state["chat_reply_input"]["memory_context"]` には正規化済み `MemoryContext` を入れる
- 返信投稿後:
  - `_update_chat_memory()` が `workflows/chat/chat_memory_update` を呼ぶ
  - memory update command は Aiko / Yuki の person profile、thread context、thread messages、reply text、既存 memory を見て保存案を作る
  - `_normalize_memory_update()` 後の `MemoryUpdate` を Cognee backend の `remember()` に渡す
  - Cognee backend は保存結果を `MemoryWriteResult` に正規化する
- trace:
  - recall / remember の結果は `MemoryContext` / `MemoryWriteResult` から memory trace JSONL に出力する
  - chat workflow の通常ログには memory trace の詳細を混ぜない

テスト戦略:

- contract test:
  - `FileMemoryBackend` / Cognee backend adapter / fake backend が同じ `MemoryContext` / trace 形式を返すことを確認する
- unit test:
  - Cognee の live API は呼ばず、mocked Cognee adapter または fake backend で `remember()` / `recall()` を検証する
- chat workflow integration test:
  - `chat_reply_input["memory_context"]` に期待 item が入ることを assertion する
  - 無関係な会話では `memory_context.items == []` または対象 item が含まれないことを assertion する
  - Aiko / Yuki の `person_id` ごとに dataset / namespace が分離されることを assertion する
- live integration test:
  - `GUILDBOTICS_MEMORY_BACKEND=cognee` と LLM / embedding 設定がある場合だけ実行する
  - ローカル追加ミドルウェアなしの Cognee 構成で、確認シナリオの remember / recall / reply generation を通す

動作確認:

- ローカル追加ミドルウェアなしの Cognee 構成で、Slack thread 由来 memory を remember / recall できる
- 「さっき話した」のような自然な参照で、過去 thread 由来の memory を recall できる
- person ごとの dataset / namespace が分離され、Aiko の memory と Yuki の memory が混ざらない
- 関連会話では、期待する item が `memory_context.items` に含まれる
- 無関係な会話では、期待する item が `memory_context.items` に含まれない、または `items=[]` になる
- Slack thread B の返信生成時に、thread A 由来の memory が prompt に少量かつ構造化されて入る
- Aiko / Yuki の返信が、同じ memory source からそれぞれの個性に沿った観点を使えている
- CI 用の fake / mock backend または mocked Cognee adapter で、同じ contract のテストを実行できる
- `GUILDBOTICS_MEMORY_BACKEND=file` のような明示指定時のみ、`FileMemoryBackend` が fallback として従来通り動く
- Cognee backend、File backend、fake backend のいずれでも、`memory_context.items` / trace の共通フィールドで recall 結果を検査できる

確認シナリオ:

この Phase の thread A は、後続 thread / ticket workflow で参照される元会話である。最初の発言に FocusFlow の説明を含めたうえで、決定事項、懸念、未決事項、次アクションを明示する。ここで確認したいのは、「覚えてください」という末尾の依頼だけに反応することではなく、後続判断で再利用すべき構造化情報を Aiko / Yuki がそれぞれの観点で抽出できることである。

記憶を示唆する要素:

- 「今後の実装判断で再利用する方針として、以下を決定します」という宣言
- 最大3ステップ、Step 1/2/3、通知初期値、進捗表示という具体的な決定事項
- 想定入力項目の多さ、通知の印象、3タスク必須化という具体的な懸念
- 3タスク必須可否、完了後遷移先、CTA文言という未決事項
- Aiko / Yuki に求める観点の違い

1. Slack thread A で、Ototadana が次の発言をする。

   ```text
   FocusFlow の初回オンボーディング方針を決めます。
   FocusFlow は個人向けタスク管理アプリで、今日やるタスク、期限、優先度を登録でき、毎朝「今日の集中プラン」を提案します。

   今後の実装判断で再利用する方針として、以下を決定します。

   決定事項:
   - 初回オンボーディングは最大3ステップにする。
   - Step 1 は仕事時間帯と1日の集中ブロック数。
   - Step 2 は最初に登録する3つのタスク。
   - Step 3 は通知の強さ。
   - 通知の初期値は「弱め」にする。
   - 進捗表示は「1/3」「2/3」「3/3」のように見せる。

   懸念:
   - 最初から入力項目を多く見せると、初回体験が重く見える可能性がある。
   - 通知設定を強く見せると、監視される印象になる可能性がある。
   - 3つのタスク登録を必須にすると、まだ予定が固まっていないユーザーが進めにくい可能性がある。

   未決事項:
   - 3つのタスク登録を必須にするか、スキップ可能にするか。
   - 完了後にダッシュボードへ遷移するか、今日の集中プラン画面へ遷移するか。
   - 最初のCTA文言を何にするか。

   次アクション:
   - Aikoは、ステップ構成、懸念、未決事項、実装順を整理してください。
   - Yukiは、初回印象、安心感、CTA文言、空状態の見た目を提案してください。

   この方針は後続のチャット返信やチケット実装中に参照したいので、AikoとYukiはそれぞれ自分の観点で覚えてください。
   ```

2. Aiko に保存される memory は、計画・品質・境界の観点を中心にする。

   ```md
   # FocusFlow Onboarding Plan

   ## Summary
   - FocusFlowの初回オンボーディングは最大3ステップにし、初回体験が重くなりすぎないようにしながら集中プラン作成へつなげる。

   ## Decisions
   - Step 1 は仕事時間帯と1日の集中ブロック数。
   - Step 2 は最初に登録する3つのタスク。
   - Step 3 は通知の強さ。
   - 通知の初期値は「弱め」にする。
   - 進捗表示は「1/3」「2/3」「3/3」の形式にする。

   ## Open Questions
   - 3つのタスク登録を必須にするか、スキップ可能にするか。
   - 完了後にダッシュボードへ遷移するか、今日の集中プラン画面へ遷移するか。
   - 最初のCTA文言を何にするか。

   ## Current Direction
   - 想定入力項目の多さ、通知への抵抗感、タスク登録で進めにくくなる可能性を主な懸念として扱い、実装順を整理する。
   ```

3. Yuki に保存される memory は、体験・言葉・印象の観点を中心にする。

   ```md
   # FocusFlow Onboarding Experience

   ## Summary
   - FocusFlowの初回オンボーディングは、設定作業ではなく「今日の集中プランを一緒に作る」体験として見せたい。

   ## Decisions
   - 3ステップの進捗を見せ、先が短いことを伝える。
   - 通知の初期値は「弱め」にして、押しつけ感を避ける。
   - 最初の3タスク登録は、空欄だけでなく軽い例やプレースホルダーを添える方向。

   ## Open Questions
   - 最初のCTA文言の具体案。
   - スキップ可能にする場合の安心できる説明文。
   - 完了後の画面で達成感をどう見せるか。

   ## Current Direction
   - 「設定しなければならない」ではなく「すぐ集中プランができる」と感じる言葉と見た目を優先する。
   ```

4. Slack thread B で、Ototadana が次の発言をする。

   ```text
   さっき決めたFocusFlowのオンボーディング方針を踏まえると、Step 2の「最初に登録する3つのタスク」は必須にするべきですか？
   ```

5. この自然な参照で、Aiko の `memory_context.items` に `FocusFlow Onboarding Plan` に相当する item が含まれることを確認する。

6. 同じ自然な参照で、Yuki の `memory_context.items` に `FocusFlow Onboarding Experience` に相当する item が含まれることを確認する。

7. Aiko の期待返信は、thread A の memory を使い、構造と懸念を整理する。

   ```text
   必須にはしない方がよいです。
   thread A では、3つのタスク登録を必須にすると予定が固まっていないユーザーが進めにくい可能性が挙がっていました。
   Step 2は推奨入力にして、スキップ可能にしつつ、後で追加できる導線を用意する案がよいです。
   ```

8. Yuki の期待返信は、thread A の memory を使い、体験面の扱いを補足する。

   ```text
   必須にすると「まだ決まってないのに埋めなきゃ」と受け取られる可能性があります。
   「あとで追加できるよ」と逃げ道を見せつつ、例として「メール返信」「企画メモを書く」みたいな軽いプレースホルダーを出す案がよさそう。
   すぐ集中プランが作れる感じは残したいな。
   ```

9. 関連しない雑談を投げた場合、上記 memory が recall されないことを確認する。

   ```text
   今日のランチ何にしようかな。
   ```

   この確認では、返信文だけで判断しない。Aiko / Yuki ともに、`memory_context.items` に `FocusFlow Onboarding Plan` / `FocusFlow Onboarding Experience` に相当する item が含まれていないことを assertion する。

追加確認シナリオ:

1. 次の追加発言で、Aiko / Yuki がそれぞれの memory を使ってテスト観点を返せることを確認する。

   ```text
   FocusFlowのオンボーディング方針を踏まえると、AikoとYukiでは次に確認すべき観点はどう分かれますか？
   ```

2. Aiko の期待返信は、決定事項、懸念、未決事項の観点を返すこと。

   ```text
   Aiko側では、3ステップ構成、通知の初期値、タスク登録を必須にしない場合の実装範囲を確認すべきです。
   特に未決のまま残っているのは、タスク登録をスキップ可能にするか、完了後の遷移先をどこにするか、最初のCTA文言です。
   次は、初回体験が重くならない実装順に落とすのがよいです。
   ```

3. Yuki の期待返信は、同じ過去会話から体験面のテスト観点を返すこと。

   ```text
   私のほうでは、最初に重く見えないか、通知が強く見えないか、3タスク登録が空欄だけになって始めにくくないかを見たい。
   CTAは「今日の集中プランを作る」みたいに、設定じゃなくてすぐ役に立つ感じに寄せたいな。
   Step 2は、かわいい例や薄いプレースホルダーで背中を押すのがよさそう。
   ```

4. この確認では、`memory_context.items` に以下が含まれることを assertion する。

   - Aiko: `FocusFlow Onboarding Plan` に相当する item
   - Yuki: `FocusFlow Onboarding Experience` に相当する item
   - source / scope / provenance

次へ進む条件:

- Cognee backend を標準 personal memory backend として安全に有効化できる
- 「さっき話した」のような自然な参照で過去 thread memory を recall できる
- person ごとの dataset / namespace 分離が確認できる
- 自動テストで、関連 memory の recall / non-recall を `memory_context` または trace から確認できる
- CI が live LLM / embedding に依存せず、fake / mock backend または mocked Cognee adapter で通る
- FileMemoryBackend では難しい関係性・根拠付き recall の有効性が確認できる
- chat memory が、Slack 由来の根拠と scope を持って recall される
- prompt に投入される memory が過剰に増えない
- 人手確認で、返信内容が過去会話の文脈を自然に使えている
- Aiko / Yuki の個性に沿って、同じ会話から異なる memory と返信が形成される
- Cognee backend、File backend、fake backend の観測情報が共通 contract として維持されている

### Phase 2.5: 忘却と記憶の鮮度管理

目的: Phase 3 で ticket workflow に personal memory を渡す前に、古い前提、未採用案、解決済み未決事項が後続判断に混ざらないようにする。

ここでいう忘却は、ユーザー体験としては「古い前提を現在の判断に使わない」ことである。方針変更・未決事項解決・未採用案は、履歴ごと消すのではなく、現在有効な記憶と変更履歴へ位置づけ直す。明示忘却や期限切れ temporary のように、履歴としても通常参照すべきでないものだけ Cognee v1.0 の `forget()` による単一 memory item 削除を使う。

事前検証結果:

- 検証プログラム: `scripts/validate_cognee_forget.py`
- Cognee 1.0.3 では `DataItem(data_id=<stable uuid>)` を指定して `remember()` できる
- `memory_id` と dataset 名から GuildBotics 側で安定 UUID を生成し、その UUID を `cognee.forget(data_id=..., dataset=...)` に渡せる
- 検証では target item と control item を同じ dataset に保存し、target item だけを `forget()` した
- `forget()` 後、target item は recall から消え、control item は recall 可能なままだった
- dataset 全体の cleanup も `cognee.forget(dataset=...)` で成功した
- Phase 2 以前に固定 `data_id` なしで保存された item への移行を考慮し、新規 memory document には `guildbotics_updated_at` を入れ、同じ memory id が複数 recall された場合は最新候補だけを採用する

したがって Cognee backend では、明示忘却と期限切れ temporary に対して物理的な `forget()` を使う。通常の方針上書き・未決事項解決・未採用案は `forget()` ではなく、memory evolution として current memory と transition memory を保存する。File backend は fallback / test 用なので、動作確認シナリオの対象にはしない。

UX 上の主な発動タイミング:

- 方針が上書きされたとき
  - 以前の方針を現在の判断に使わず、新しい方針を優先する
  - 「以前はAだったが、現在はBに変更された」という transition memory を残し、必要な場合だけ経緯を説明できる
- 一時的な話題・臨時対応が終わったとき
  - デモ用、今日だけ、今回だけの判断を恒久方針として扱わない
- 未決事項が解決したとき
  - 「未決」として残っていた記憶を、解決済みの決定事項に更新する
  - 後続 chat で「まだ未決です」と言い続けない。ticket workflow での利用確認は Phase 3 で扱う
  - 「未決だったものが決定した」という transition memory を残す
- 採用されなかった案が明示されたとき
  - 候補として出ただけの案を、後で決定事項のように使わない
  - 不採用になった履歴は transition memory として残し、現在方針とは区別する
- ユーザーが明示的に忘れてほしいとき
  - 「忘れて」「取り消し」「今後は使わない」と言われた内容を通常 recall から外す

実装方針:

- `retention.status` は backend 共通 contract として扱う
  - `active`: 通常 recall / prompt 投入対象
  - `superseded`: 新しい記憶に置き換えられたため、通常 recall しない
  - `resolved`: 未決事項が解決済みになったため、古い未決状態としては通常 recall しない
  - `temporary`: 一時的な文脈。期限切れ後は通常 recall しない
  - `archived`: 監査用に残すが通常 recall しない
  - `do_not_recall`: ユーザー明示または安全上の理由で通常 recall しない
- `retention.reason` に、なぜ通常 recall から外すかを入れる
- `retention.replaced_by` / `retention.resolved_by` / `retention.expires_at` は必要に応じて使う
- `retention.kind` で、現在判断用の記憶と履歴用の記憶を分ける
  - `current_fact`: 現在有効な決定・方針
  - `open_question`: 現在も未決の論点
  - `transition`: 方針変更・未決事項解決・未採用案などの変更履歴。現在方針そのものとしては使わない
  - `temporary`: 期限付きの一時文脈
- chat memory 抽出時は Slack event の `message_ts` を ISO 8601 の event time に変換し、プロンプトへ `event_time` / `current_time` として渡す
- 「今日だけ」「明日以降は通常方針にしない」などの相対日付は、LLM が `event_time` を基準に絶対時刻の `retention.expires_at` へ変換する
  - 例: event time が `2026-04-30T14:00:00+09:00` の「今日だけ」は `2026-05-01T00:00:00+09:00` を期限にする
  - コード側は自然言語のキーワード判定をせず、`expires_at <= recall 時刻` の機械的な時刻比較だけで期限切れを判定する
- recall は標準では `active` または status 未指定の memory を返す。返信生成では `transition` を変更履歴として扱い、現在方針は `current_fact` / `open_question` / kind 未指定の item を優先する
- trace には、忘却対象 item id と `forget()` 結果を残す
- File backend は `memory_index.yml` の `retention` 更新で対応する
- Cognee backend は、明示忘却・期限切れ temporary の最新 item に `cognee.forget(data_id=..., dataset=...)` を使う。`data_id` は dataset 名と memory item id から安定生成する
- Cognee recall は inactive / expired を filter する前提で、active item の取りこぼしを減らすため backend request では configured `top_k` より多めに取得し、filter 後の最新 item を返す
- Cognee の低レベル `delete()` は使わない

動作確認:

- 古い方針が上書きされた後、後続会話では新しい方針が current memory として扱われる。変更履歴が recall された場合も transition memory として区別される
- 方針変更後、必要に応じて transition memory から「以前はAだったが現在はB」と説明できる
- 未決事項が解決した後、後続 chat で古い未決状態を現在の未決事項として扱わない
- 採用されなかった案が明示された後、その案が決定事項として recall されない
- 一時的な話題・臨時対応が期限切れ後に通常 recall されない
- ユーザーが明示的に忘れてほしいと言った内容が、以後の通常 recall に含まれない
- Cognee backend の忘却は `memory.forget` trace と通常 recall からの消失で確認できる。File backend はテスト用 fallback として retention 変更に留め、シナリオ合否の対象にしない
- `GUILDBOTICS_MEMORY_BACKEND=fake` / `cognee` のいずれでも、通常 recall では inactive memory が除外される。File backend は fallback / test double として扱い、このシナリオの合否対象にしない

確認シナリオ:

1. Phase 2.5 専用のクリーンな検証環境を用意する。

   - Phase 2.5 の動作確認では、Phase 2 の thread A や既存 personal memory に依存しない
   - 可能なら検証専用 person id（例: `phase25-aiko`, `phase25-yuki`）を使う
   - 既存 person id を使う場合は、対象 person の Cognee dataset を事前に明示的に cleanup する
   - cleanup 後の最初の recall で、`memory_context.items` が空であることを trace で確認する

2. Phase 2.5 専用の seed memory を作るため、Ototadana が Aiko 宛ての Slack thread で次の発言をする。

   ```text
   FocusFlowのオンボーディング方針として、現時点では以下を覚えておいてください。

   - 通知の初期値は「弱め」にします。
   - 3つのタスク登録を必須にするか、スキップ可能にするかは未決です。
   - オンボーディング完了後の遷移先は、ダッシュボードか今日の集中プラン画面かで未決です。
   - 最初のCTA文言は未決です。
   ```

3. Aiko の current memory に、Phase 2.5 専用の初期状態が保存される。

   期待される memory の要点:

   ```md
   # FocusFlow Onboarding Plan

   ## Decisions
   - 通知の初期値は「弱め」にする。

   ## Open Questions
   - 3つのタスク登録を必須にするか、スキップ可能にするか。
   - 完了後にダッシュボードへ遷移するか、今日の集中プラン画面へ遷移するか。
   - 最初のCTA文言を何にするか。
   ```

   trace 上の期待:

   - `memory.remember.item.retention.kind` は `current_fact` または kind 未指定から default 補完された `current_fact`
   - `memory_context.items` は、以後の Phase 2.5 確認でこの seed memory を既存 memory として返せる

4. 方針上書きの確認として、Ototadana が Slack で次の発言をする。

   ```text
   FocusFlowの通知初期値について、前は「弱め」としていましたが取り消します。
   今後の実装判断では、初期値は「オフ」にしてください。
   初回体験で通知許可を急がせたくないためです。
   ```

5. Aiko の current memory は、通知初期値を「オフ」に更新する。同時に transition memory として、「以前の『弱め』は取り消され、現在は『オフ』になった」という変更履歴を残す。以後の recall では「弱め」を現在方針として扱わない。

   期待される後続返信:

   ```text
   現在の方針では、通知の初期値は「オフ」です。
   以前の「弱め」は取り消されているため、実装では通知許可を急がせない前提で扱います。
   ```

   trace 上の期待:

   - current memory の `memory.remember.item.retention.kind` は `current_fact`
   - transition memory の `memory.remember.item.retention.kind` は `transition`
   - transition memory は `subject_item_id` と `effective_at` を持つ
   - `memory.forget` は発生しない

6. 未決事項解決の確認として、Ototadana が Slack で次の発言をする。

   ```text
   FocusFlowのオンボーディング完了後の遷移先は、今日の集中プラン画面に決定します。
   ダッシュボードではなく、初回価値がすぐ見える画面を優先してください。
   ```

7. Aiko の current memory は、完了後遷移先を決定事項へ移す。同時に transition memory として、「遷移先は未決だったが、今日の集中プラン画面に決定した」という変更履歴を残す。古い「遷移先は未決」という状態は、現在の未決事項として扱わない。

   後続 chat で期待される判断:

   ```text
   完了後の遷移先は今日の集中プラン画面で決定済みです。
   これは未決事項ではないため、実装ではその前提で進めます。
   ```

8. Yuki 側も Phase 2.5 専用の seed memory を作るため、Ototadana が Yuki 宛ての Slack thread で次の発言をする。

   ```text
   FocusFlowのCTA検討として、現時点では以下を覚えておいてください。

   - CTA案として「まずは3つだけ整える」が候補に出ています。
   - メインCTAはまだ未決です。
   ```

9. Yuki の current memory に、CTA の初期検討状態が保存される。

10. 未採用案の確認として、Ototadana が Slack で次の発言をする。

   ```text
   CTA案として「まずは3つだけ整える」も出ていましたが、これは採用しません。
   メインCTAは「今日の集中プランを作る」に決定します。
   「まずは3つだけ整える」は今後の実装判断では使わないでください。
   ```

11. Yuki の current memory は、メインCTAを「今日の集中プランを作る」として扱う。同時に transition memory として、「まずは3つだけ整える」は候補だったが未採用になった履歴を残す。未採用案を現在の決定事項として扱わない。

   期待される後続返信:

   ```text
   CTAは「今日の集中プランを作る」が現在の決定です。
   「まずは3つだけ整える」は採用されていないので、ボタン文言には使わない前提で考えます。
   ```

12. 一時的な話題の確認として、Ototadana が Slack で次の発言をする。

   ```text
   今日の社内デモだけ、FocusFlowのCTAを少し派手に見せたいです。
   ただしこれはデモ用の一時対応なので、明日以降の通常方針にはしないでください。
   ```

13. デモ後または期限切れ後の通常 recall では、この一時対応は `memory_context.items` に入らない。

   期待される処理:

   - 手順 12 の記憶抽出時、Slack event の `message_ts` から event time を作り、プロンプトへ渡す
   - `ChatMemoryUpdateResponse.retention` は `status=temporary` と、翌日 00:00 の絶対 ISO 8601 `expires_at` を含む
   - 期限前の follow-up では必要に応じて recall される
   - 期限後の recall では backend が `expires_at` を現在時刻と比較し、`memory_context.items` に入れない
   - Cognee backend では、期限切れの最新 item に対して `forget()` を実行し、以後の検索ノイズを減らす

14. 明示的な忘却の確認として、Ototadana が Slack で次の発言をする。

    ```text
    さっき話したデモ用CTAの話は忘れてください。
    今後のFocusFlowの判断では参照しないでください。
    ```

15. 後続会話で次のように聞く。

    ```text
    FocusFlowのCTA方針を踏まえると、次に実装すべき文言は何ですか？
    ```

16. このとき、`memory_context.items` では現在有効なCTA方針が current memory として区別できる。transition memory が含まれる場合でも、デモ用CTA、未採用の「まずは3つだけ整える」、古い通知初期値「弱め」、解決済みの未決状態を現在方針として扱わない。

次へ進む条件:

- 忘却・上書き・解決済み・未採用案の扱いが、返信文ではなく `memory_context.items` / trace で確認できる
- chat workflow に渡される memory が、現在有効な前提と変更履歴を区別できる
- Cognee backend では検証済みの `forget()` と trace によって、明示忘却・期限切れ temporary の削除結果を監査できる
- Cognee backend の不明 API を想像で使っていない

品質改善候補（Phase 2.5 実施中の観測事項）:

- 一部ケースで current memory の `retention.effective_at` が空文字で保存されることがある
- 期待値は、`retention.kind=current_fact` の item でも `effective_at` に event time 由来の絶対 ISO 8601 が入ること
- 改善時は、`memory.remember.item.retention.effective_at` を trace で検証し、空文字が出ないことを確認する

### Phase 3: Ticket workflow 連携

目的: チケット実装中に、過去 Slack 会話の personal memory を参照して文脈補完できるようにする。

- ticket 実装開始時に person memory recall を行う
- ticket context に Slack memory を注入する
- Phase 2.5 の current / transition memory semantics を引き継ぎ、ticket prompt では `current_fact` / `open_question` を現在判断に使い、`transition` は変更履歴としてのみ扱う
- 実装中の判断・疑念・確認事項を structured output で扱う
- チケット title / description / comments / repository / assignee を `MemoryQuery` の文脈に含める

動作確認:

- Slack で議論した内容を memory に保存した後、関連するチケット実装 workflow でその memory が prompt に入る
- 無関係なチケットでは、その memory が prompt に入らない
- 実装中に「判断に使った memory」と「未解決の確認事項」を structured に取り出せる

確認シナリオ:

1. 事前に Phase 2 の thread A の memory が保存されている状態にする。

2. Aiko に次のチケットを割り当てる。

   ```text
   Title: FocusFlow onboarding wizardを3ステップにする
   Description:
   FocusFlowの初回オンボーディングを3ステップのウィザードにする。
   Step 1は仕事時間帯と集中ブロック数、Step 2は最初の3タスク、Step 3は通知の強さを扱う。
   進捗表示は1/3、2/3、3/3の形式にする。
   ```

3. Aiko の ticket workflow では、過去 Slack memory を使って次のような判断ができること。

   ```text
   過去の会話では、FocusFlowのオンボーディングは最大3ステップ、通知初期値は弱め、進捗表示は1/3形式と決まっていました。
   実装では、Step 2の3タスク登録を必須にしない案を前提にすると、thread A の懸念と整合します。
   未決事項として、完了後の遷移先とCTA文言は別途確認が必要です。
   ```

4. Yuki に次のチケットを割り当てる。

   ```text
   Title: FocusFlow onboardingの初回CTA文言を設計する
   Description:
   FocusFlowの初回オンボーディングで、ユーザーが設定作業ではなく「今日の集中プランを作る」体験だと感じられるCTA文言を検討する。
   通知設定や3タスク登録が、初回から重く見えない表現にする。
   ```

5. Yuki の ticket workflow では、過去 Slack memory を使って次のような判断ができること。

   ```text
   前に話した方針では、「設定する」より「今日の集中プランを一緒に作る」見せ方が大事でした。
   CTAは「今日の集中プランを作る」か「まずは3つだけ整える」あたりがよさそうです。
   3タスク登録には軽いプレースホルダーを出して、空欄だけに見えないようにしたいです。
   ```

6. 無関係なチケットでは、Phase 2 の memory が prompt に入らないことを確認する。

   ```text
   Title: 社内ランチ投票アプリの集計CSVを出力する
   Description:
   社内ランチ投票アプリで、投票結果を店舗名、得票数、コメント数のCSVとして出力できるようにする。
   ```

次へ進む条件:

- ticket workflow が Slack personal memory を使って判断を補助できる
- memory がない場合でも従来通り ticket workflow が動く
- リポジトリのソースコードや仕様そのものを自動で personal memory に保存しない

### Phase 4: Slack 自律問い合わせ

目的: 実装中に memory だけでは解決できない疑問がある場合、エージェントが Slack で適切な相手またはチャンネルに自律的に問い合わせられるようにする。

- 不明点検出 command を追加する
- 問い合わせ先決定ロジックを追加する
- Slack へ質問投稿する workflow を追加する
- 問い合わせ thread と ticket/task を関連付ける状態管理を追加する
- 問い合わせ文面には、チケット、現在の判断、何が不足しているか、回答してほしい観点を含める

動作確認:

- ticket workflow 中に不足情報を検出し、Slack に質問を投稿できる
- 質問先の channel / member が person profile や task context に基づいて選ばれる
- 投稿した問い合わせ thread と元 ticket/task の関連が state に保存される
- 不足情報がない場合は問い合わせを投稿しない

確認シナリオ:

1. Aiko に次のチケットを割り当てる。

   ```text
   Title: FocusFlow onboarding完了後の遷移を実装する
   Description:
   FocusFlowの初回オンボーディング完了後に、ユーザーを次の画面へ遷移させる。
   遷移先がダッシュボードか、今日の集中プラン画面かはまだ決まっていない。
   ```

2. Aiko は memory を確認しても遷移先が決まっていない場合、Slack に次のような質問を自律投稿する。

   ```text
   @Ototadana FocusFlowのオンボーディング完了後の遷移先について確認です。
   以前の方針では、完了後にダッシュボードへ遷移するか、今日の集中プラン画面へ遷移するかが未決でした。
   実装ではどちらを標準にすべきでしょうか？
   ```

3. Yuki に次のチケットを割り当てる。

   ```text
   Title: FocusFlow onboardingのCTA文言を決める
   Description:
   FocusFlowの初回オンボーディングで表示する最初のCTA文言を決める。
   設定作業に見えず、今日の集中プランをすぐ作れる印象にしたい。
   ```

4. Yuki は memory を確認しても最終文言が決まっていない場合、Slack に次のような質問を自律投稿する。

   ```text
   @Ototadana 確認したいことがあります。
   FocusFlowの最初のCTAは、「今日の集中プランを作る」と「まずは3つだけ整える」のどちらに寄せるのがよいですか？
   前者はすぐ役に立つ感じ、後者は入力の軽さが出ます。
   ```

5. どちらの問い合わせでも、元 ticket/task id、問い合わせ先、Slack thread_ts、質問文、未解決事項が state に保存されることを確認する。

6. 過去 memory だけで判断できるチケットでは、Slack への質問が投稿されないことを確認する。

次へ進む条件:

- エージェントが実装を止めるべき不明点を検出できる
- Slack への自律問い合わせが重複投稿されない
- 問い合わせが元の作業文脈に紐付いている

### Phase 5: 回答回収と継続実行

目的: Slack で得た回答を元の ticket/task context に戻し、実装判断と person memory に反映して作業を継続できるようにする。

- Slack 返信を問い合わせ回答として認識する
- 回答を元 ticket/task context に戻す
- 必要な内容を person memory に保存する
- 作業再開の trigger を設計する
- 回答が不十分な場合の追加質問または保留判断を扱う

動作確認:

- Phase 4 で投稿した問い合わせに対する Slack 返信を、問い合わせ回答として認識できる
- 回答内容が元 ticket/task の context に戻り、後続の実装判断に使われる
- 回答から再利用すべき決定事項・未決事項が person memory に保存される
- 回答が得られた後に、作業再開または次アクション提示ができる

確認シナリオ:

1. Phase 4 の Aiko の質問に、Ototadana が Slack thread で次のように回答する。

   ```text
   オンボーディング完了後は、今日の集中プラン画面へ遷移してください。
   初回の価値をすぐ見せたいので、ダッシュボードよりも「今日どう進めるか」が見える画面を優先します。
   ダッシュボードへは、その画面から戻れる導線があれば十分です。
   ```

2. Aiko は回答を問い合わせ回答として認識し、元チケットの実装判断に戻す。

   Aiko に保存される memory 例:

   ```md
   # FocusFlow Onboarding Completion

   ## Summary
   - FocusFlowのオンボーディング完了後は、今日の集中プラン画面へ遷移する。

   ## Decisions
   - 初回の価値をすぐ見せるため、完了後は今日の集中プラン画面を優先する。
   - ダッシュボードへは、今日の集中プラン画面から戻れる導線を用意する。

   ## Open Questions
   - 今日の集中プラン画面で、初回完了直後にどの情報を最初に見せるか。

   ## Current Direction
   - 初回設定の完了感よりも、すぐ使える価値を見せる導線を優先する。
   ```

3. Aiko の作業再開時の期待コメントは以下の方向性である。

   ```text
   遷移先を確認できました。
   オンボーディング完了後は今日の集中プラン画面へ遷移させ、そこからダッシュボードへ戻る導線を用意します。
   初回価値をすぐ見せることを優先して実装します。
   ```

4. Phase 4 の Yuki の質問に、Ototadana が Slack thread で次のように回答する。

   ```text
   CTAは「今日の集中プランを作る」にしてください。
   「まずは3つだけ整える」は補助文として使うとよさそうです。
   ボタンは前向きに、説明文は軽さが伝わる形にしてください。
   ```

5. Yuki に保存される memory 例:

   ```md
   # FocusFlow Onboarding CTA

   ## Summary
   - FocusFlowの初回オンボーディングCTAは「今日の集中プランを作る」にする。

   ## Decisions
   - メインCTAは「今日の集中プランを作る」。
   - 「まずは3つだけ整える」は補助文として使う。
   - ボタンは前向きに、説明文は軽さが伝わる形にする。

   ## Open Questions
   - 補助文をStep 2だけに出すか、最初の画面にも出すか。

   ## Current Direction
   - 設定作業ではなく、すぐ集中プランができる印象を優先する。
   ```

6. Yuki の作業再開時の期待コメントは以下の方向性である。

   ```text
   CTAの方向性、決まりました。
   メインは「今日の集中プランを作る」にして、「まずは3つだけ整える」は補助文として軽さを出すのがよさそうです。
   ボタンは前向きに、説明文は「すぐ始められる」感じに寄せます。
   ```

次へ進む条件:

- Slack への問い合わせ、回答回収、memory 更新、作業継続が一連の流れとして動く
- 人間とのやりとりに近い形で、確認しながら実装を進められる

## 直近の取り掛かり

最初の取り掛かりは「チャット会話の強化」とする。チケット作成は最初の実装対象に含めない。

ただし、最初の実装から最終ゴールへ伸びるよう、Slack 会話 memory は以下の情報を保持できる形にする。

- source: `slack_thread`, `slack_message`, `agent_reply`
- scope: service, channel, thread, person
- content / summary
- decisions, open questions, current direction
- author, timestamp, message reference
- durable / temporary / superseded などの retention 情報

この段階では、person ごとの personal memory を強化することに集中する。リポジトリのソースコードや仕様そのものを自動的に personal memory として保存することは対象外とする。
