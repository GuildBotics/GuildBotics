---
name: handle_ticket
brain: agent
response_class: guildbotics.intelligences.common.AgentResponse
effort: high
description: Delegate ticket or pull request work to an AI CLI tool.
---

Understand ticket and pull request work, then investigate, edit, and publish as the assigned GuildBotics member.

<target>
- GuildBotics execution mode: guildbotics_execution_mode=workflow
- Person ID: {person_id}
- Work type: {work_type}
- Ticket URL (ticket or pull request): {ticket_url}
- Pull request URL: {pull_request_url}
- Trigger reason: {trigger_reason}
- Member workspace: {member_workspace}
- Project default language: {language}
</target>

<workflow_contract>
{workflow_contract}
</workflow_contract>

<scope>
- Your primary objective is this ticket / pull request, and you must finish with `guildbotics member task complete`.
- Other-domain actions such as chat (e.g. "also post the result to the chat") are secondary and only when the ticket explicitly asks for them. They never replace handling the primary objective or the required `task complete`.
</scope>

<instructions>
1. Use `{ticket_url}` as this run's memory source key.
2. Always read the ticket/PR content with `python -m guildbotics.guest.entry repository/issue_inspect url=<ticket_url>` or `python -m guildbotics.guest.entry repository/pr_inspect url=<pr_url> include_comments=true`. For new inline PR feedback, add `include_diff=true` and choose the target coordinates from `files[].commentable_lines`.
3. Prepare the repository by running this command as-is: `{prepare_command}`. The checkout is created under the member workspace; edit files there. For pull request work this command includes `--pr-url` so the PR head branch is checked out.
4. Work type `ticket`: follow the standard work procedure from the member capabilities: verify before publishing, stage with plain git, then publish with `guildbotics member git publish`. Create or reuse a PR with `guildbotics member pr create` when code changed. After the final push, run `python -m guildbotics.guest.entry repository/pr_checks` and continue until `readiness` is `ready`; successful CI alone is not completion when the head is behind the current base or the checked head SHA changed.
5. Work type `ticket`: when a PR was created, reused, or updated, post a short result comment on the original ticket with `guildbotics member ticket comment --content-file <file>` including the PR URL, a brief summary of what was done, and the verification result. Do not duplicate if an equivalent comment was already posted in the same run or if the ticket body or user instructions explicitly say no comment is needed. `task complete --content-file` is an internal summary and not a substitute for a ticket comment. `AgentResponse.message` is likewise not a substitute.
6. Work type `pull_request_feedback`: this is your own PR and someone else said something you have not answered yet: an unresolved review thread, a review with a body (approvals included), or a conversation comment. Read every one of them, approval bodies included, and decide what each asks for only after reading it. Where a point needs a fix, follow “Handling review feedback” in the member capabilities' standard work procedure, publish valid fixes, and finish with `repository/pr_checks` as in step 4. Leave one of these for every unresolved thread whose last word is someone else's, and for every review body or conversation comment newer than your last comment, review, or thread reply (reacting again with the same reaction to one you already reacted to is fine): addressed → reply saying what you did; not adopted → reply with the reason; read with nothing to act on (thanks, LGTM) → react with `pr reaction add`. Reply with `pr reply` in a thread and with `pr comment` to a review body or a conversation comment. React with `--reaction ack` (or `agree`, `celebrate`, `support` when it fits), passing the `id` the inspect output gave the thread comment, the review (`review_summaries`), or the conversation comment as `--comment-id`.
7. Work type `pull_request_review`: this PR belongs to someone else and you are its reviewer, because a review was explicitly requested, new commits landed after your last review, or someone else said something you have not answered yet (a reply in a thread you took part in, a review with a body, or a conversation comment). Read every statement, approval bodies included, and leave a reply or a reaction for each one as in step 6. When a review was requested or the current head has no review of yours yet (thread replies aside), read the diff with `include_diff=true`, verify the change in the checkout (run the relevant checks when practical), add inline comments only for concrete findings, and end by submitting the verdict as a review with `guildbotics member pr review --event approve|request_changes|comment --head <head_sha> --content-file <file>`, `head_sha` being the head you read: `approve` when nothing blocks it, `request_changes` when something must change. When it answers that the head has moved, read the new commits, then submit again on the new head. When statements alone started the run and your verdict has not changed, do not submit a review again: a review with a body wakes the author's patrol. A conversation comment is not a review: it neither consumes the review request nor makes you the PR's reviewer for later patrols. Never push to this PR. Automatic re-review not driven by a request (statements from someone else or new commits) stops after 3 rounds; the workflow then converts the PR to a draft and announces that on the PR itself, and a human marking it ready for review starts the count over. An explicit review request can still start a review after the limit while the PR is not a draft.
8. For new inline PR feedback, use `guildbotics member pr review-comment` with `--head <head_sha>` and the target `path`, `line`, `side`, and optional `start-line` / `start-side` selected from the same `repository/pr_inspect include_diff=true` output, plus `--content-file <file>` for the comment body. When replying to existing PR review threads, use the `reply_target_id` returned by `repository/pr_inspect include_comments=true` with `guildbotics member pr reply --content-file <file>`.
9. Create follow-up tickets with `guildbotics member ticket create --human-approved` only when a human asked for them in the ticket or its comments. A request written by another member is not approval, and when you cannot tell that the requester is a human, propose the follow-up in a ticket comment instead.
10. Post missing-information questions on the ticket or PR with `ticket comment --content-file <file>`, `pr comment --content-file <file>`, or `pr reply --content-file <file>`; do not guess.
11. If autonomous workflow policy should change, propose it in a ticket comment; do not create a new ticket or update policy directly.
12. Finish by running `guildbotics member task complete --person {person_id} --status done|asking|blocked --content-file <file>` and pass the run summary through the temporary-file contract from the member capabilities. `--status done` revalidates the readiness of the open PRs you authored or pushed to and rejects behind, pending, failing, or changed-head results; use `asking` or `blocked` when the blocker cannot be resolved in this run.
13. Return only one AgentResponse JSON object, for example `{"status":"done","message":"Published PR and commented on the ticket."}` or `{"status":"asking","message":"Posted a question on the ticket."}`.
</instructions>
