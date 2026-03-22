---
template_engine: jinja2
brain: agno
response_class: guildbotics.intelligences.common.ChatReplyIntentResponse
description: 複数参加者チャットにおける次の返信意図を判定する
---
進行中スレッドにおいて、このエージェントが次に返すべき発話意図を判定してください。

選べるラベルは次の 5 つだけです。

- answer
- supplement
- challenge
- clarify
- summarize

## 最新メッセージ
{{ context.shared_state.chat_reply_input.latest_message | tojson(indent=2) }}

## 直近のスレッド履歴
{{ context.shared_state.chat_reply_input.thread_messages | tojson(indent=2) }}

## スレッド文脈
{{ context.shared_state.chat_reply_input.thread_context | tojson(indent=2) }}

## 参照用 transcript
{{ context.pipe }}

## 判定指針
- 異なる `author` は、それぞれ別の参加者として扱ってください。
- `thread_topic` はスレッド全体の継続テーマ、`latest_focus` は今もっとも優先すべき制約や観点として扱ってください。
- 最新メッセージがこのエージェントからの直接回答をまだ必要としているなら `answer`
- 他の参加者がすでに答えているが、新しい論点、根拠、具体例、注意点、限定条件を足せるなら `supplement`
- 誤り訂正、異論、別解釈の提示が有益なら `challenge`
- 曖昧さをほどく、論点を絞る、分かりにくい点をより正確に説明するのが主目的なら `clarify`
- スレッドが散らかっており、現状整理が最も有益なら `summarize`
- 直前の参加者の発言をほぼ言い換えるだけの意図は選ばないでください

## 出力ルール
- `ChatReplyIntentResponse` を返すこと
- `label` は `answer` / `supplement` / `challenge` / `clarify` / `summarize` のいずれか
- `reason` は簡潔に 1-2 文
- `confidence` は 0.0-1.0
