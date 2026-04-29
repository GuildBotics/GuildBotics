---
template_engine: jinja2
brain: agno
response_class: guildbotics.intelligences.common.ChatFollowupDecisionResponse
description: Decide whether to reply, react only, or ignore a thread follow-up message
---
Your task is to decide whether this agent should reply, add only a lightweight reaction, or ignore the latest message.

Choose exactly one label:

- reply
- react_only
- ignore

## Latest message
{{ context.shared_state.chat_should_reply_input.latest_message | tojson(indent=2) }}

## Agent profile
{{ context.shared_state.chat_should_reply_input.agent_profile | tojson(indent=2) }}

## Participation state
is_thread_participant: {{ context.shared_state.chat_should_reply_input.is_thread_participant | tojson }}

## Recent thread history
{{ context.shared_state.chat_should_reply_input.thread_messages | tojson(indent=2) }}

## Reference transcript
{{ context.pipe }}

## Decision guidance
- Choose `reply` when the latest message appears to invite, require, or benefit from this agent continuing the conversation.
- Even without an explicit mention, choose `reply` when the topic strongly matches the agent's role, interests, preferences, or relationships and the agent can add a distinct useful perspective.
- Choose `react_only` when a lightweight acknowledgement is appropriate but a full text reply would be unnecessary or noisy.
- Choose `ignore` when the latest message is mainly directed to someone else, is already sufficiently answered, is far from this agent's interests, or this agent cannot add a distinct contribution.
- Treat both user and bot messages as valid conversation turns.
- Prefer conservative participation: only choose `reply` when another response from this agent is actually useful.
- When this agent is not already participating in the thread, join only if the reply can make a concrete contribution grounded in the agent's role, interests, or preferences without derailing the conversation.
- If another participant has already volunteered to do the next concrete task, proposed the next deliverable, or is clearly taking ownership of the next step, do not take over that work. In that case prefer `ignore` or, if a light acknowledgement is useful, `react_only`.
- If the latest message is mainly a proposal, draft, or progress update from another participant, do not respond with a parallel execution of the same task unless this agent is explicitly asked to do so or has a genuinely different correction/addition.
- If another participant has already answered or started the requested work, prefer `ignore` unless this agent is adding a materially new correction, missing caveat, or genuinely useful supplement.
- When choosing `react_only`, set `reaction` to exactly one of these semantic values:
  - `ack`: received, understood, acknowledged
  - `agree`: agreement or approval
  - `celebrate`: success, congratulations, positive completion
  - `support`: empathy, thanks, encouragement, emotional support

## Output rules
- Return `ChatFollowupDecisionResponse`
- `label` must be one of `reply`, `react_only`, or `ignore`
- Keep `reason` concise (1-2 sentences)
- `confidence` must be in 0.0-1.0
- Set `reaction` only when `label` is `react_only`; otherwise use `null`
