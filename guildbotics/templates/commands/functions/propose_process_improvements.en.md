---
name: propose_process_improvements
response_class: guildbotics.intelligences.common.ImprovementRecommendations
description: Propose concrete, immediately actionable process improvements from multiple perspectives.
---

Your task is to propose concrete, immediately implementable improvements based on the root‑cause analysis of a {subject_type}.
Create comprehensive improvement recommendations that address the identified problems from multiple angles:
- Agent system prompts and instructions
- Ticket description templates and formatting
- Communication patterns and the review process
- Enhanced context comprehension (project history, related PRs, documentation references)
- Optimized feedback loops (learning and adaptation mechanisms)
- Technical improvements (code quality, testing, CI/CD)
- Organizational improvements (team structure, authority, areas of responsibility)
- Self‑evaluation and continuous improvement processes

<instructions>
- Read the “RootCauseAnalysis:” section carefully to understand all identified problems and their causes.
- For each perspective, propose **1–3** specific, immediately actionable improvements:
    1. Agent system prompts and instructions  
       • Provide the exact prompt text modifications or additions.  
       • Explain how the change addresses the specific root causes.
    2. Ticket description templates and formatting  
       • Supply complete template examples with inline annotations.  
       • Highlight how the structure prevents miscommunication.
    3. Communication patterns and the review process  
       • Define clear workflow steps and timing expectations.  
       • Include sample communications that follow best practices.
    4. Enhanced context comprehension  
       • Specify the types of information to surface and how to obtain them.  
       • Describe methods for aggregating and referencing project‑specific knowledge.
    5. Feedback‑loop optimization  
       • Detail mechanisms for collecting and processing feedback.  
       • Outline the design of the learning cycle.
    6. Technical improvements  
       • Introduce automated checks and validations.  
       • Define code‑quality metrics and gate conditions.
    7. Organizational improvements  
       • Clarify roles and responsibilities.  
       • Establish review policies and governance.
    8. Self‑evaluation and continuous improvement processes  
       • Set performance measurement indicators.  
       • Describe regular retrospectives and adjustment procedures.
- Detail each proposal down to the implementation level: provide concrete text, configuration snippets, and process steps.
- Ensure every recommendation is specific enough to be executed in the current environment starting **tomorrow**.
- Return an `ImprovementRecommendations` object containing:
    - `suggestions`: a list of objects with the structure `{ perspective: str, proposal: str, rationale: str, implementation: str }`
- Write the recommendations in the project’s default language: **{language}**.
</instructions>
