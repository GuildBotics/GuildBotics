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
- Keep decisions separate from open questions.
- If existing memory is present, merge without duplication.
- Do not store agent personality, preferences, or relationships here.

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
- When updating existing memory, set `topic_id` to that existing topic id; when creating a new topic, use a short ASCII lowercase kebab-case id
- `title` should be a short topic name
- `summary` should be a one-sentence summary for the memory index
- `confidence` must be in 0.0-1.0
