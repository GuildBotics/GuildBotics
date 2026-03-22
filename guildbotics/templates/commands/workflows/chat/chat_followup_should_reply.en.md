---
template_engine: jinja2
brain: agno
response_class: guildbotics.intelligences.common.ChatFollowupDecisionResponse
description: Decide whether to reply, react only, or ignore a thread follow-up message
---
Your task is to decide whether this agent should reply, add only a lightweight reaction, or ignore the latest message in a chat thread that it is already participating in.

Choose exactly one label:

- reply
- react_only
- ignore

## Latest message
{{ context.shared_state.chat_should_reply_input.latest_message | tojson(indent=2) }}

## Recent thread history
{{ context.shared_state.chat_should_reply_input.thread_messages | tojson(indent=2) }}

## Reference transcript
{{ context.pipe }}

## Decision guidance
- Choose `reply` when the latest message appears to invite, require, or benefit from this agent continuing the conversation.
- Choose `react_only` when a lightweight acknowledgement is appropriate but a full text reply would be unnecessary or noisy.
- Choose `ignore` when the latest message is mainly directed to someone else, is already sufficiently answered, or this agent adding any response would be low value.
- Treat both user and bot messages as valid conversation turns.
- Prefer conservative participation: only choose `reply` when another response from this agent is actually useful.
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
