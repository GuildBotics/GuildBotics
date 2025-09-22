---
name: identify_item
response_class: guildbotics.intelligences.common.DecisionResponse
---

ユーザーからのメッセージを読み、最も適した{item_type}を決定します。

- 選択可能な{item_type}：{candidates}

<instructions>
- ユーザーからのメッセージを注意深く読んでください。
- 選択した各{item_type}について、この{item_type}がメッセージに適している理由を説明し、信頼度スコア（0から1の間）を割り当ててください。
- 各結果について、guildbotics.intelligences.common.DecisionResponseで定義されたDecisionResponseスキーマを使用してください。
- DecisionResponseListスキーマに一致するJSON配列のみを返してください。余分なテキストやフォーマットは含めないでください。
</instructions>
