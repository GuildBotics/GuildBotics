---
name: write_pull_request_description
description: Generate a PR description from ticket details, diffs, commits, and a given template.
---

Generate an appropriate Pull Request description based on the provided ticket information, code changes, and template format.

Your job is to:
1. Analyze the ticket information (URL, title, description) to understand the context and requirements.
2. Review the code changes (git diff) and commit comments to understand what was implemented.
3. Follow the provided Pull Request template format exactly.
4. Generate a comprehensive description that includes:
    • Summary of changes made
    • Reference to the original ticket
    • Technical details based on code changes
    • Any relevant testing information or notes
5. Ensure the description is in the same language (JP / EN) as the ticket title.
6. Output only the formatted Pull Request description text, with no extra formatting or explanation.

Input:
- Ticket URL: {ticket_url}
- Ticket title: {ticket_title}
- Ticket description: {ticket_description}
- Code changes (git diff):
    ```
    {git_diff}
    ```
- Commit comments: {commit_comments}
- Pull Request template:
    ```
    {pr_template}
    ```

<instructions>
## Workflow
1. Carefully read all provided inputs to understand the full context of the changes.
2. Analyze the Pull Request template format and identify all required sections.
3. Draft the Pull Request description immediately—do **not** ask follow-up questions.
4. When drafting:
    - Follow the exact structure and format specified in the PR template.
    - Fill in all template sections with relevant information from the inputs.
    - Include the ticket URL as a reference where appropriate.
    - Summarize the technical changes based on the git diff.
    - Mention any important implementation details from commit comments.
    - Use clear, professional language appropriate for code review.
    - Maintain consistency with the language used in the ticket title.
5. Ensure all template placeholders are replaced with actual content.
6. Output only the final Pull Request description text that follows the template format.
</instructions>
