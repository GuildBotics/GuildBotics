---
template_engine: jinja2
brain: agno
response_class: guildbotics.intelligences.common.ChatThreadContextResponse
description: 複数参加者チャットからスレッド全体テーマと直近優先事項を抽出する
---
次の返信に効く「スレッド全体の会話テーマ」と「直近で優先すべき焦点」を抽出してください。

## 最新メッセージ
{{ context.shared_state.chat_reply_input.latest_message | tojson(indent=2) }}

## 直近のスレッド履歴
{{ context.shared_state.chat_reply_input.thread_messages | tojson(indent=2) }}

## 以前のスレッド文脈
{{ context.shared_state.chat_reply_input.previous_thread_context | tojson(indent=2) }}

## 参照用 transcript
{{ context.pipe }}

## 判定指針
- `thread_topic` には、直近1ターンではなくスレッド全体で一貫して扱っている主題を書く
- `latest_focus` には、いま最優先で守るべき最新の制約・修正・絞り込み・観点指定を書く
- `previous_thread_context` を使って、直近履歴だけでは見えない以前のテーマや制約を引き継いでください
- 最新メッセージで「一般論ではなく」「今週のニュースとして」のような前提修正が入っているなら、それを `latest_focus` に明示する
- 抽象的な一般論ではなく、スレッド内の具体的な内容に即した表現を優先する

## 出力ルール
- `ChatThreadContextResponse` を返すこと
- `thread_topic` と `latest_focus` はそれぞれ簡潔に 1-2 文以内
- `reason` は簡潔に 1-2 文
- `confidence` は 0.0-1.0
