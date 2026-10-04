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
2. Work in the current working directory: the requester's working tree, including its uncommitted changes. Do not clone, run `member git prepare`, or change branches. Unless a grant opens it for writing, it is a copy: its `.git`, when there is one, is read-only, so git shows status and diffs but cannot commit, and the changes to regular files are written back to the requester's working tree when the task ends successfully. Links you create are not written back.
3. Perform only the requested work: a review is read-only; a request to fix permits the requested edits and their verification. PR creation and external comments require inclusion in the request; do not expand the task using the standard work procedure.
4. Do not commit or push: the requester publishes the changes. List the files you changed in your result.
5. Return the result, verification, and any blocker as text on stdout for the requesting member to relay. Do not call workflow `complete` or `noop` commands or start another delegation.
</instructions>
