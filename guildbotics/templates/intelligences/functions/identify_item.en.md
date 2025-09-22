---
name: identify_item
response_class: guildbotics.intelligences.common.DecisionResponse
---

Read the user's message and determine which {item_type} is most suitable.

- Available {item_type}s to choose from: {candidates}

<instructions>
- Carefully read the user's message.
- For each selected {item_type}, explain why this {item_type} is suitable for the message and assign a confidence score (between 0 and 1).
- Use the DecisionResponse schema defined in guildbotics.intelligences.common.DecisionResponse for each result.
- Return only the JSON array matching the DecisionResponseList schema. Do not include any extra text or formatting.
</instructions>
