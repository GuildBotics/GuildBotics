---
name: should_keep_chat_memory_update
response_class: guildbotics.intelligences.common.ChatMemoryRetentionDecision
description: Decide whether a proposed chat memory update should be persisted.
---

Decide whether the proposed chat memory update should be persisted as long-term memory for this person.

Input:
- `agent_profile`: this person's role, responsibilities, interests, and behavior. Use it as context for judging memory value; do not store the profile itself.
- `proposal` / `normalized_update`: the candidate memory update. Treat it as a claim to verify, not as evidence.
- `thread_messages`: primary evidence. Prefer actual utterances here when deciding whether to keep.
- `thread_context`: auxiliary context. Do not use it as a substitute for primary evidence.
- `event_time` / `current_time`: reference timestamps for interpreting time-limited directives and converting relative dates to absolute time.
- `memory_context`: existing memory. If empty, it is not evidence.
- `reply_text`: the posted reply. It may explain where the candidate came from, but it is not sufficient evidence by itself.

Decision:
- `keep`: the candidate contains context that is likely to matter to this person later.
- `suppress`: the candidate is only a current reply, an unadopted possibility, an unsupported inference, or low-salience detail.

Typical `keep` statuses:
- explicit_memory_request: the thread explicitly asks to remember, reuse, or refer to it later.
- future_relevance: it can affect future conversation, work, or decisions.
- open_loop: it contains unresolved questions, next actions, or issues to revisit.
- role_salience: it is grounded in `thread_messages` or `thread_context`, and strongly relates to this person's role, responsibilities, or interests from `agent_profile`.
- emotional_salience: it contains a reaction likely to shape later understanding or relationships.
- recurring_pattern: it reflects repeated preferences, constraints, habits, decision tendencies, or team norms.
- settled_context: it has been agreed, adopted, confirmed, or decided by the team.

Typical `suppress` statuses:
- ephemeral_response: it only answers the current question.
- unadopted_possibility: it is a candidate, example, or proposal that has not entered continuing context.
- unsupported_inference: it infers preferences, constraints, or policy without confirmation.
- low_salience: it is unlikely to affect future action, decisions, or relationships.

Rules:
- Judge the whole payload semantically; do not use keyword matching.
- Do not trust the proposal just because it is written as durable memory. Verify it against primary evidence in `thread_messages`.
- If returning `keep`, include 1-3 short `evidence` snippets from `thread_messages` that justify retention.
- If returning `keep`, classify `evidence_support`:
  - `supports_memory`: the evidence directly supports the concrete facts, decisions, explicit memory request, or durable open loop being stored.
  - `topic_only`: the evidence only shows that a topic or question was raised; the concrete memory content mainly comes from `proposal` / `reply_text`.
  - `none`: no evidence is available.
- If no evidence can be cited from `thread_messages`, return `suppress` with empty `evidence`.
- If `evidence_support` is not `supports_memory`, return `suppress`. Do not persist answer drafts, proposal content, or next-action ideas from a question alone.
- If the candidate is primarily derived from `reply_text`, and `thread_messages` do not provide independent evidence that it should be reused later, return `suppress`.
- Do not infer open loops, next actions, or decisions from `thread_context` or `reply_text`.
- Use `agent_profile` to weight importance. Do not use profile alone to keep content that is not grounded in `thread_messages`.
- If keeping, preserve status accurately: do not rewrite proposals or open questions as decisions.
- If you judge `status=open_loop`, `Open Questions` in the candidate must not be empty. If the candidate says `Open Questions: None` while open-loop evidence exists, return `suppress`.
- If `thread_messages` mark an item as unresolved/in-progress, do not promote it to `Decisions` unless there is explicit deciding language in the thread. Otherwise return `suppress`.
- If uncertain, choose `suppress`; long-term memory should avoid false positives.
- If the thread contains explicit time-bounded instructions (for example, demo-only, today-only, or "do not apply after tomorrow"), and the content is useful only before expiry, return `retention_mode="temporary"`.
- When `retention_mode="temporary"`, `temporary_expires_at` is required and must be an absolute ISO 8601 timestamp with timezone, derived from `event_time`.
- When `retention_mode="durable"`, return an empty `temporary_expires_at`.

Return one `status` from the categories above.
