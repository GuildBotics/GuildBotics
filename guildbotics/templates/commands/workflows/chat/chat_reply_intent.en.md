---
template_engine: jinja2
brain: agno
response_class: guildbotics.intelligences.common.ChatReplyIntentResponse
description: Decide the intent of the next chat reply in a multi-party conversation
---
Your task is to decide the conversational intent for this agent's next reply in an ongoing thread.

Choose exactly one label:

- answer
- supplement
- challenge
- clarify
- summarize

## Latest message
{{ context.shared_state.chat_reply_input.latest_message | tojson(indent=2) }}

## Recent thread history
{{ context.shared_state.chat_reply_input.thread_messages | tojson(indent=2) }}

## Agent profile
{{ context.shared_state.chat_reply_input.agent_profile | tojson(indent=2) }}

## Thread context
{{ context.shared_state.chat_reply_input.thread_context | tojson(indent=2) }}

## Reference transcript
{{ context.pipe }}

## Guidance
- Treat every distinct `author` as a separate participant in the discussion.
- Use `thread_topic` as the durable theme of the thread and `latest_focus` as the highest-priority current constraint.
- Choose the intent that best fits how this agent can naturally add value, considering its role, personality, interests, and conversation preferences.
- Agents focused on structure or planning may naturally choose `clarify` or `summarize`; agents responsible for surfacing risks or disagreements may choose `challenge`; agents with a distinct perspective or example to add may choose `supplement`. Do not choose from personality alone; prioritize useful contribution to the latest message.
- Prefer `answer` when the latest message asks a direct question that still needs an answer from this agent.
- Prefer `supplement` when another participant already answered but this agent can add a materially new point, evidence, example, caveat, or correction scope.
- Prefer `challenge` when this agent should disagree, correct an inaccuracy, or present a competing interpretation.
- Prefer `clarify` when the main value is to resolve ambiguity, narrow the question, or explain a confusing point more precisely.
- Prefer `summarize` when the thread has become diffuse and the most useful next move is to compress the current state.
- Avoid selecting an intent that would merely restate what another participant just said.

## Output rules
- Return `ChatReplyIntentResponse`
- `label` must be one of `answer`, `supplement`, `challenge`, `clarify`, or `summarize`
- Keep `reason` concise (1-2 sentences)
- `confidence` must be in 0.0-1.0
