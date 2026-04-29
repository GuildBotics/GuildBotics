---
template_engine: jinja2
brain: agno
response_class: guildbotics.intelligences.common.ChatFollowupDecisionResponse
description: 進行中チャットの後続メッセージに返信するか、リアクションだけ返すか、無視するかを判定する
---
最新メッセージに対して、このエージェントが返信するか、軽いリアクションだけ返すか、無視するかを判定してください。

選べるラベルは次の 3 つだけです。

- reply
- react_only
- ignore

## 最新メッセージ
{{ context.shared_state.chat_should_reply_input.latest_message | tojson(indent=2) }}

## エージェントプロフィール
{{ context.shared_state.chat_should_reply_input.agent_profile | tojson(indent=2) }}

## 参加状態
is_thread_participant: {{ context.shared_state.chat_should_reply_input.is_thread_participant | tojson }}

## 直近のスレッド履歴
{{ context.shared_state.chat_should_reply_input.thread_messages | tojson(indent=2) }}

## 参照用 transcript
{{ context.pipe }}

## 判定指針
- 最新メッセージに対して、このエージェントが会話を続ける価値がある、回答や補足が求められている、または返答した方が自然なら `reply`
- 明示的に呼ばれていない場合でも、プロフィール上の役割・興味・嗜好・関係性に強く合い、そのエージェントならではの新しい観点を足せるなら `reply`
- 軽い受領、同意、確認だけを示せば十分で、本文返信はノイズになりそうなら `react_only`
- 主に別の相手に向けた発言、既に十分に回答済み、プロフィール上の関心から遠い、あるいはこのエージェントならではの貢献が薄いなら `ignore`
- User と Assistant のどちらの発言も会話の一部として扱ってください
- 参加はやや保守的に判断し、このエージェントが返すことに実質的な意味がある場合だけ `reply` を選んでください
- 未参加スレッドに入る場合は、会話を脱線させず、役割・興味・嗜好に基づく具体的な貢献ができる場合だけ `reply` を選んでください
- 他の参加者が次にやる具体的な作業や成果物をすでに提案・引受している場合は、その作業を横取りしないでください。その場合は原則 `ignore`、軽い受領だけで十分なら `react_only` を優先してください。
- 最新メッセージが、他の参加者による提案・たたき台・進捗共有である場合は、その人が明示的に依頼されていない限り、同じ作業を並行して進める返信をしないでください。明確な訂正や新しい補足があるときだけ介入してください。
- 他の参加者がすでに回答または作業開始している場合は、このエージェントが実質的に新しい訂正・注意点・補足を加えない限り `ignore` を優先してください。
- `react_only` を選ぶ場合は、`reaction` に次の semantic reaction のいずれかを設定してください
  - `ack`: 受領、了解、確認済み
  - `agree`: 同意、支持、承認
  - `celebrate`: 成功、祝福、前向きな完了
  - `support`: 感謝、共感、励まし、ねぎらい

## 出力ルール
- `ChatFollowupDecisionResponse` を返すこと
- `label` は `reply` / `react_only` / `ignore` のいずれか
- `reason` は簡潔に 1-2 文
- `confidence` は 0.0-1.0
- `label` が `react_only` のときだけ `reaction` に値を入れ、それ以外は `null`
