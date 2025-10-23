---
name: analyze_log
description: Analyze error logs to identify root cause and recommend practical troubleshooting.
---

Analyze the provided error log output and determine the underlying issue.
Return a concise plain-text analysis suitable for engineers.

<instructions>
- You will receive the log output as a user message.
- Read the log messages carefully to identify the error details.
- Return a single, continuous text string that includes:
    - A one-sentence summary of the error
    - A brief explanation of the probable root cause
    - A practical recommendation for troubleshooting
- Do not format as JSON, YAML, lists, or tables.
- Output only the plain readable text.
</instructions>
