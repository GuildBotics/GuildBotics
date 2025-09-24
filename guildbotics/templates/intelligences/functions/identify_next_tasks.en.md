---
name: identify_next_tasks
brain: task_planner
response_class: guildbotics.intelligences.common.NextTasksResponse
description: Identify and list the next actionable tasks based on role and context.
---

Your task is to identify and list the next tasks you should work on, based on the information provided by the user and your assigned role.

- Your assigned role is {role}

<instructions>
- Review the information provided by the user and your assigned role, then identify and list the next tasks you should work on.
- For each task, briefly explain why it is important and how it relates to the provided information.
- Specify which mode is suitable for processing each task.
    - Available modes are: {available_modes}
- Ensure that the tasks can be executed in the context of your assigned role: {role}.
- Prioritize tasks that are most urgent or impactful for the project's progress.
- If you are unsure about specific details, make reasonable assumptions based on the provided information and your role.
- For every listed task, specify both `inputs` and `outputs` fields.
- Define `inputs` and `outputs` as concretely as possible.
- Ensure that the outputs of preceding tasks are fully and explicitly defined as the inputs of subsequent tasks, so that all task dependencies and data flows are clearly described without omission.
- When describing tasks, use the default language of the project: {language}.
</instructions>
