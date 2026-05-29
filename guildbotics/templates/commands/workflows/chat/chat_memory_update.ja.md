---
template_engine: jinja2
brain: agno
response_class: guildbotics.intelligences.common.ChatMemoryUpdateResponse
description: チャットスレッドから長期メモリー更新案を抽出する
---
このスレッドから、今後の会話・実装・判断に再利用されそうな topic memory の更新案を作ってください。

## エージェントプロフィール
{{ context.shared_state.chat_memory_update_input.agent_profile | tojson(indent=2) }}

## スレッド文脈
{{ context.shared_state.chat_memory_update_input.thread_context | tojson(indent=2) }}

## 時刻
イベント時刻:
{{ context.shared_state.chat_memory_update_input.event_time | tojson(indent=2) }}

現在時刻:
{{ context.shared_state.chat_memory_update_input.current_time | tojson(indent=2) }}

## 既存の関連メモリー
{{ context.shared_state.chat_memory_update_input.memory_context | tojson(indent=2) }}

## 直近のスレッド履歴
{{ context.shared_state.chat_memory_update_input.thread_messages | tojson(indent=2) }}

## 投稿済み返信
{{ context.shared_state.chat_memory_update_input.reply_text }}

## 参照用 transcript
{{ context.pipe }}

## 更新基準
- topic の今後の会話・実装・判断に再利用されそうな確定事項、実装方針、未決事項だけを保存する
- 仮案、単なる言い換え、社交的な応答、一時的な確認は保存しない
- 「今日だけ」「今回だけ」「デモ後まで」のように明確な期限付きで、期限内の後続返信に再利用する価値がある内容は、一時 memory として保存してよい
- 一時 memory を保存する場合は `retention.status="temporary"` とし、`retention.expires_at` にイベント時刻を基準にした絶対 ISO 8601 時刻を書く。例: 「今日だけ」はイベント時刻の日付の翌日 00:00
- 期限付きの内容を保存する場合、`expires_at` を決められないなら durable memory にしない
- agent の提案は、スレッド内で明確に決定として受け入れられていない限り、確定事項として扱わない
- agent 自身がその場で提示した候補、ランキング、判断基準、推定された好み、返信固有の整理観点は、ユーザーが明示的に採用・確認・記憶依頼していない限り、永続記憶にしない
- 決定事項と未決事項を混ぜない
- 既存メモリーがある場合は、現在有効な方針・決定・未決事項として全文を統合更新する
- 既存メモリーが同一テーマで recall されている場合、原則として新規 topic を作らず、既存 topic_id を更新する
- 同一テーマの部分決定（例: 未決項目のうち1件だけ決まる）では、既存 topic の `Decisions` / `Open Questions` / `Current Direction` を更新する。別 topic へ分割しない
- 既存 topic を更新する場合、`title` は原則維持する（意味が大きく変わる場合のみ更新）
- 方針変更・未決事項解決・未採用案の明示は、通常の忘却ではなく memory evolution として扱う。現在有効な memory には新しい状態を保存し、古い方針・未決状態・未採用案を現在方針として残さない
- 既存 memory の `Open Questions` は、今回の thread_messages 内で「解決・取り消し・不採用」が明示された項目だけを閉じる。今回触れていない未決事項は消さずに維持する
- `Open Questions` を `None`（空）にするのは、既存の未決事項すべてが今回の thread_messages 内で明示的に解決済みと判断できる場合に限る
- 方針変更・未決事項解決・未採用案の明示だけを理由に `forget_item_ids` を使わない。履歴は別の transition memory として保存される
- ユーザーが明示的に「忘れて」「取り消し」「今後使わない」と言った場合は、該当する既存メモリーを `forget_item_ids` に入れる
- `forget_item_ids` は既存メモリーの id だけを使う。新しい id や推測した id を作らない
- 既存メモリーを忘却する理由を `forget_reason` に短く書く
- agent の人格・嗜好・関係性は保存しない
- エージェントプロフィールは抽出観点として使う。person の役割にとって重要な再利用可能事実・未決事項は保存するが、一般的な人格特徴そのものは memory に保存しない
- Slack thread 由来の再利用文脈を優先する。後続のチャット返信や実装判断に効く決定事項、リスク、未決事項、判断理由、次アクションを保存する
- 既存メモリーを更新する場合、過去の状態を現在方針としては書かない。ただし変更理由や「以前の状態は取り消された」という現在判断に必要な注意は、簡潔に含めてよい
- durable に再利用できる文脈がなければ `should_update=false` にする

## memory 形式
更新する場合、`memory` は以下のような Markdown 全文にしてください。

```md
# <topic title>

## Summary
- ...

## Decisions
- ...

## Open Questions
- ...

## Current Direction
- ...
```

## 出力ルール
- `ChatMemoryUpdateResponse` を返すこと
- 保存すべき内容がなければ `should_update=false`、`memory` は空文字
- 既存メモリーを忘却するだけの場合は `should_update=false` にし、`forget_item_ids` と `forget_reason` を返す
- `topic_id` は既存メモリーを更新する場合はその既存 topic_id、新規作成なら英数字ハイフンの短い識別子。日本語は使わない
- `title` は短い topic 名
- `summary` は memory index 用の1文
- 通常の長期 memory は `retention` を空 `{}` または `{"status":"active"}` にする
- 一時 memory は `retention={"status":"temporary","expires_at":"<absolute ISO 8601>","reason":"<why temporary>"}` を返す
- `confidence` は 0.0-1.0
