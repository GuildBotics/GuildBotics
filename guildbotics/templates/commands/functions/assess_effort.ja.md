---
name: assess_effort
brain: default
effort: low
response_class: guildbotics.intelligences.common.EffortAssessmentResponse
description: 受信したチャットメッセージに必要なモデルエフォートを判定します。
---

担当メンバーが、この受信メッセージにどれだけのモデルエフォートを費やすべきかを判定してください。

`high` と答えるのは、対応にメンバーのワークスペース上のローカルファイル作業が必要な場合です。たとえばコードの作成・変更、文書や設定の編集、リポジトリ横断の調査、コミットや公開が必要な成果物の作成などが該当します。

`default` と答えるのは、通常の会話応答で済む場合です。知識やスレッドの内容から質問に答える、了解を返す、確認する、既出の内容を要約する、ファイル作業を伴わない判断を示す、といった場合が該当します。

判定は使われている語彙ではなく依頼の意図に基づいて行ってください。ファイル名・リポジトリ名・技術用語が出てくるだけで、求められているのが説明だけであれば `default` です。

<latest_message>
{latest_message}
</latest_message>

<previous_thread_context>
{previous_thread_context}
</previous_thread_context>
