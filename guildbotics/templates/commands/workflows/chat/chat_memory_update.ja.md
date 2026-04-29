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
- 決定事項と未決事項を混ぜない
- 既存メモリーがある場合は、重複を避けて統合する
- agent の人格・嗜好・関係性は保存しない

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
- `topic_id` は既存メモリーを更新する場合はその既存 topic_id、新規作成なら英数字ハイフンの短い識別子。日本語は使わない
- `title` は短い topic 名
- `summary` は memory index 用の1文
- `confidence` は 0.0-1.0
