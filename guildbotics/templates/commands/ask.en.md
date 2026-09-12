---
name: ask
brain: agent
template_engine: jinja2
description: Ask a member to perform a one-off task in the current working tree.
inputs:
  message: required
---

This is a delegated one-off task (guildbotics_execution_mode=delegated). Follow this envelope for the request supplied as input; the interactive skill's Definition of Done and the workflow completion contract do not apply.

<instructions>
1. Read `guildbotics member context --person {{ context.person.person_id }}` first. Its capabilities are authoritative for member operations.
2. Use the current working directory, including its uncommitted changes. Do not clone, run `member git prepare`, or change branches.
3. Perform only the requested work: a review is read-only; a request to fix permits the requested edits and their verification. Commit, push, PR creation, and external comments require inclusion in the request; do not expand the task using the standard work procedure.
4. If git publishing is requested, use the member git commands with `--workspace-mode current`.
5. Return the result, verification, and any blocker as text on stdout for the requesting member to relay. Do not call workflow `complete` or `noop` commands or start another delegation.
</instructions>
