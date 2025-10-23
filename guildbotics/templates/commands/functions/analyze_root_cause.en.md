---
name: analyze_root_cause
response_class: guildbotics.intelligences.common.RootCauseAnalysis
description: Perform root-cause analysis of a {subject_type} using evaluation and feedback to identify problems and causes.
---

Your task is to perform a root-cause analysis on a previously evaluated {subject_type}.
Use the following two inputs:
  - The ArtifactProcessEvaluation result (score, reason, context)
  - The original feedback/review comments
From these, identify what problems occurred and their underlying causes.
Conduct the analysis from multiple perspectives:
  - Agent’s behavior: How the AI agent acted (commits/iterations, replies, timing, content quality)
  - User’s behavior: How reviewers and authors interacted (clarity of requests, feedback loops)
  - System’s design: How the workflow, tools, or prompt definitions may have contributed
Feel free to suggest additional perspectives if they surface important insights.

<instructions>
- Read the evaluation result section to understand the performance summary.
- Read the original feedback section to see the raw reviewer feedback.
- For each identified problem (e.g., slow turnaround, incomplete fixes, misinterpretation of requests):
    1. Describe the symptom (what went wrong).
    2. Analyze its root cause, citing evidence from the inputs.
    3. Assign the cause to one or more perspectives (agent, user, system, or others).
- Organize your findings under headings for each perspective, listing problems and causes.
- If you discover additional useful perspectives (e.g., repository conventions, CI tooling), include them.
- Present your analysis as a structured JSON report using the RootCauseAnalysis schema.
- Use the project’s default language for all descriptions: {language}.
</instructions>
