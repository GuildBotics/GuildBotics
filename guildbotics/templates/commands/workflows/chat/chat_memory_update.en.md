---
template_engine: jinja2
brain: agno
response_class: guildbotics.intelligences.common.ChatMemoryUpdateResponse
description: Extract a durable memory update from a chat thread
---
Create a topic memory update proposal from this thread when it contains durable context that should help future conversations, implementation, or decisions.

## Agent profile
{{ context.shared_state.chat_memory_update_input.agent_profile | tojson(indent=2) }}

## Thread context
{{ context.shared_state.chat_memory_update_input.thread_context | tojson(indent=2) }}

## Time
Event time:
{{ context.shared_state.chat_memory_update_input.event_time | tojson(indent=2) }}

Current time:
{{ context.shared_state.chat_memory_update_input.current_time | tojson(indent=2) }}

## Existing relevant memory
{{ context.shared_state.chat_memory_update_input.memory_context | tojson(indent=2) }}

## Recent thread history
{{ context.shared_state.chat_memory_update_input.thread_messages | tojson(indent=2) }}

## Posted reply
{{ context.shared_state.chat_memory_update_input.reply_text }}

## Reference transcript
{{ context.pipe }}

## Update criteria
- Store only confirmed decisions, implementation direction, and open questions that are likely to matter for future conversations, implementation, or decisions in this topic.
- Do not store tentative ideas, mere rephrasing, social acknowledgements, or transient checks.
- If the thread contains a clearly time-limited item such as "today only", "this run only", or "until after the demo", and it is useful for follow-up replies before it expires, you may save it as temporary memory.
- For temporary memory, set `retention.status="temporary"` and put an absolute ISO 8601 timestamp in `retention.expires_at`, computed from the event time. For "today only", use 00:00 on the day after the event date.
- If a time-limited item should be saved but you cannot determine `expires_at`, do not save it as durable memory.
- Do not turn an agent suggestion into a confirmed decision unless the thread clearly accepted it as a decision.
- Do not create durable memory from the agent's own suggestions, rankings, decision criteria, inferred preferences, or response-specific framing unless the user explicitly accepts, confirms, or asks to remember them.
- Keep decisions separate from open questions.
- If existing memory is present, rewrite the complete memory as the currently valid policy, decision, and open-question state.
- If an existing memory for the same theme is recalled, update that existing `topic_id` by default instead of creating a new topic.
- For partial decisions on the same theme (for example, resolving only one open question), update the same topic's `Decisions` / `Open Questions` / `Current Direction` sections. Do not split into a separate topic.
- When updating an existing topic, keep `title` stable by default. Change it only when the topic meaning itself has materially changed.
- Treat policy changes, resolved questions, and rejected options as memory evolution, not ordinary forgetting. Save the new current state and do not leave old policy, unresolved state, or rejected options as current policy.
- For `Open Questions` in existing memory, close only the items that are explicitly resolved, cancelled, or rejected in this thread_messages set. Keep unrelated unresolved items unchanged.
- Set `Open Questions` to `None` (empty) only when every existing open question is explicitly resolved in this thread_messages set.
- Do not use `forget_item_ids` only because a policy changed, a question was resolved, or an option was rejected. The workflow stores that history as a separate transition memory.
- If the user explicitly says to forget, cancel, or stop using something, put the corresponding existing memory id in `forget_item_ids`.
- `forget_item_ids` must only contain ids from existing memory. Do not invent new ids.
- Briefly explain why existing memory should be forgotten in `forget_reason`.
- Do not store agent personality, preferences, or relationships here.
- Use the agent profile as an extraction lens: keep the durable facts and open questions that matter to this person's role, but do not save generic profile traits as memory.
- Prefer reusable Slack thread context: decisions, risks, unresolved questions, rationale, and next actions that could affect later chat replies or implementation work.
- When updating existing memory, do not write past states as current policy. You may briefly include the fact that the previous state was cancelled when that caveat matters for future judgement.
- If the thread does not contain durable reusable context, set `should_update=false`.

## Memory format
When updating, `memory` must be the complete Markdown content, like this:

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

## Output rules
- Return `ChatMemoryUpdateResponse`
- If there is nothing worth saving, set `should_update=false` and `memory` to an empty string
- If the only required action is forgetting existing memory, set `should_update=false` and return `forget_item_ids` with `forget_reason`
- When updating existing memory, set `topic_id` to that existing topic id; when creating a new topic, use a short ASCII lowercase kebab-case id
- `title` should be a short topic name
- `summary` should be a one-sentence summary for the memory index
- Normal long-term memory should use empty `retention` `{}` or `{"status":"active"}`
- Temporary memory should return `retention={"status":"temporary","expires_at":"<absolute ISO 8601>","reason":"<why temporary>"}`
- `confidence` must be in 0.0-1.0
