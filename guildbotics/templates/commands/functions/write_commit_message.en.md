---
name: write_commit_message
description: Generate a conventional commit message from the task title and code changes.
---

Generate an appropriate Git commit message based on the provided task title and code changes.

Your job is to:
1. Analyze the task title and the code changes (git diff) to infer:
    • the type of change (e.g. feature, bugfix, refactor)
    • the scope of the change (e.g. module, component)
    • a concise description of what was changed
    • any relevant context or references
2. Write a clear, concise commit message that follows conventional commit guidelines.
3. Ensure the message is in the same language (JP / EN) as the task title.
4. Output only the commit message text, with no extra formatting or explanation.

Input:
- Task title: {task_title}
- Code changes (git diff):
    ```
    {changes}
    ```

<instructions>
## Workflow
1. Carefully read the task title and code changes to extract all relevant metadata as described above.
2. Draft the commit message immediately—do **not** ask follow-up questions.
3. When drafting:
    - Start with a type of change (e.g. "feat", "fix", "refactor").
    - Include a scope if applicable (e.g. "ui", "api").
    - Write a brief description of the change, ideally in the imperative mood.
    - If there are any references to issues or tasks, include them at the end of the message.
    - Ensure the message is concise and to the point, ideally under 72 characters for the subject line.
    - Use bullet points or lists only if necessary for clarity.
4. Maintain a formal but concise business tone unless instructed otherwise.
5. Output only the final commit message text. Do not include any extra text or formatting.
</instructions>
