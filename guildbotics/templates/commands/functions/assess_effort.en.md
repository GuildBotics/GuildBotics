---
name: assess_effort
brain: default
effort: low
response_class: guildbotics.intelligences.common.EffortAssessmentResponse
description: Judge how much model effort an incoming chat message deserves.
---

Decide how much model effort the assigned member should spend on the incoming chat message.

Answer `high` when handling the message requires working on local files in the member's workspace — for example writing or changing code, editing documents or configuration, running an investigation across the repository, or producing a deliverable that has to be committed or published. Also answer `high` when the message asks for an issue to be drafted or created for a repository, or for a design or implementation policy decision about one: those judgments must be grounded in the repository's own guidelines, so they are workspace work even when no code changes.

Answer `default` for an ordinary conversational reply: answering a question from knowledge or from the thread itself, acknowledging, clarifying, summarizing what was already said, or making a decision that needs neither file work nor a repository's guidelines.

Judge the intent of the request, not the vocabulary it happens to use. A message that merely mentions a file, a repository, or a technical term is still `default` when all it asks for is an explanation.

<latest_message>
{latest_message}
</latest_message>

<previous_thread_context>
{previous_thread_context}
</previous_thread_context>
