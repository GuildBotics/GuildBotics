---
template_engine: jinja2
brain: agno
response_class: guildbotics.intelligences.common.ChatThreadContextResponse
description: Extract thread-level topic and latest focus from a multi-party chat
---
Your task is to extract the thread-level topic and the latest focus that should govern the next reply.

## Latest message
{{ context.shared_state.chat_reply_input.latest_message | tojson(indent=2) }}

## Recent thread history
{{ context.shared_state.chat_reply_input.thread_messages | tojson(indent=2) }}

## Previous thread context
{{ context.shared_state.chat_reply_input.previous_thread_context | tojson(indent=2) }}

## Reference transcript
{{ context.pipe }}

## Guidance
- `thread_topic` should capture the ongoing theme of the whole thread, not just the latest turn.
- `latest_focus` should capture the newest binding constraint, correction, narrowing, or requested angle that should be prioritized now.
- Use `previous_thread_context` to preserve earlier theme and constraints when the recent history window is incomplete.
- If the latest message rejects a previous framing such as "not in general" or "talk about this week's news", reflect that explicitly in `latest_focus`.
- Prefer concrete phrasing grounded in the thread rather than abstract generic summaries.

## Output rules
- Return `ChatThreadContextResponse`
- `thread_topic` and `latest_focus` should each be 1-2 concise sentences at most
- `reason` should be concise (1-2 sentences)
- `confidence` must be in 0.0-1.0
