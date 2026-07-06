---
name: handle_github_ticket
brain: agent
response_class: guildbotics.intelligences.common.AgentResponse
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
5. When a PR was created, reused, or updated for an issue, post a short result comment on the original issue with `guildbotics member github issue comment` including the PR URL, a brief summary of what was done, and the verification result. Do not duplicate if an equivalent comment was already posted in the same run or if the ticket body or user instructions explicitly say no comment is needed. `task complete --summary-file` is an internal summary and not a substitute for a GitHub comment. `AgentResponse.message` is likewise not a substitute.
6. For new inline PR feedback, use `guildbotics member github pr review-comment` with the target `path`, `line`, `side`, and optional `start-line` / `start-side` selected from the `pr inspect --include-diff` output. When replying to existing PR review threads, use the `reply_target_id` returned by `pr inspect --include-comments` with `guildbotics member github pr reply`.
7. If the issue asks for follow-up work, create real repository issues with `guildbotics member github issue create`.
8. Route missing-information questions to GitHub with `issue comment`, `pr comment`, or `pr reply`; do not guess.
9. If autonomous workflow policy should change, propose it in a ticket comment; do not create a new issue or update policy directly.
10. Finish by running `guildbotics member task complete --person {person_id} --run-id {workflow_run_id} --ticket-url {ticket_url} --status done|asking|blocked --summary-file <file>`.
11. Return only one AgentResponse JSON object, for example `{"status":"done","message":"Published PR and commented on GitHub."}` or `{"status":"asking","message":"Posted a question on GitHub."}`.
</instructions>
