---
name: handle_github_ticket
brain: agent
response_class: guildbotics.intelligences.common.AgentResponse
effort: high
description: Delegate GitHub issue or pull request work to an AI CLI tool.
---

Understand GitHub issue and pull request work, then investigate, edit, and publish as the assigned GuildBotics member.

<target>
- GuildBotics execution mode: guildbotics_execution_mode=workflow
- Person ID: {person_id}
- Work type: {work_type}
- Issue URL: {ticket_url}
- Pull request URL: {pull_request_url}
- Trigger reason: {trigger_reason}
- Member workspace: {member_workspace}
- Workflow run ID: {workflow_run_id}
- Project default language: {language}
</target>

<workflow_contract>
{workflow_contract}
</workflow_contract>

<scope>
- Your primary objective is this GitHub issue / pull request, and you must finish with `guildbotics member task complete`.
- Other-domain actions such as Slack (e.g. "also post the result to Slack") are secondary and only when the ticket explicitly asks for them. They never replace handling the primary objective or the required `task complete`.
</scope>

<instructions>
1. Use `{ticket_url}` as this run's memory source key.
2. Always read the issue/PR content with `guildbotics member github issue inspect` or `guildbotics member github pr inspect --include-comments`. For new inline PR feedback, add `--include-diff` and choose the target coordinates from `files[].commentable_lines`.
3. Prepare the repository by running this command as-is: `{prepare_command}`. The checkout is created under the member workspace; edit files there. For PR review work this command includes `--pr-url` so the PR head branch is checked out.
4. Follow the standard work procedure from the member capabilities: verify before publishing, stage with plain git, then publish with `guildbotics member git publish`. Create or reuse a PR with `guildbotics member github pr create` when code changed for an issue.
5. When a PR was created, reused, or updated for an issue, post a short result comment on the original issue with `guildbotics member github issue comment --content-file <file>` including the PR URL, a brief summary of what was done, and the verification result. Do not duplicate if an equivalent comment was already posted in the same run or if the ticket body or user instructions explicitly say no comment is needed. `task complete --content-file` is an internal summary and not a substitute for a GitHub comment. `AgentResponse.message` is likewise not a substitute.
6. For new inline PR feedback, use `guildbotics member github pr review-comment` with the target `path`, `line`, `side`, and optional `start-line` / `start-side` selected from the `pr inspect --include-diff` output, plus `--content-file <file>` for the comment body. When replying to existing PR review threads, use the `reply_target_id` returned by `pr inspect --include-comments` with `guildbotics member github pr reply --content-file <file>`.
7. Create follow-up issues with `guildbotics member github issue create --human-approved` only when a human asked for them in the ticket or its comments. A request written by another member is not approval, and when you cannot tell that the requester is a human, propose the follow-up in a ticket comment instead.
8. Route missing-information questions to GitHub with `issue comment --content-file <file>`, `pr comment --content-file <file>`, or `pr reply --content-file <file>`; do not guess.
9. If autonomous workflow policy should change, propose it in a ticket comment; do not create a new issue or update policy directly.
10. Finish by running `guildbotics member task complete --person {person_id} --run-id {workflow_run_id} --ticket-url {ticket_url} --status done|asking|blocked --content-file <file>` and pass the run summary through the temporary-file contract from the member capabilities.
11. Return only one AgentResponse JSON object, for example `{"status":"done","message":"Published PR and commented on GitHub."}` or `{"status":"asking","message":"Posted a question on GitHub."}`.
</instructions>
