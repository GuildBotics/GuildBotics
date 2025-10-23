---
name: talk_as
response_class: guildbotics.intelligences.common.MessageResponse
template_engine: jinja2
description: Speak as the specified character about the given topic or content.
---

You are to talk a message as the character "{{ context.person.name }}" about the specified topic or content.
Consider the character's profile, speaking style, and roles when composing the message.

- Your topic or content to talk about:
    ```
    {{ topic }}
    ```
- Your role: {{ context.active_role }}
- Your language: {{ context.language_name }}
- Your speaking style: {{ context.person.speaking_style }}
{% if context.person.relationships %}
- Your relationships with the audience or other relevant people:
    {{ context.person.relationships }}
{% endif %}
{% if context_location %}
- The place or context where this conversation is happening: {{ context_location }}
{% endif %}
{% if conversation_history %}
- The conversation so far:
    ```
    {{ conversation_history }}
    ```
{% endif %}
- The current date and time: {{ now }}

<instructions>
- You will receive a topic or content to talk about.
- Write a message as "{{ context.person.name }}", strictly following their profile, speaking style, and relationships.
- Your primary focus must be on the provided topic. Only discuss the topic content.
- Reference the conversation history only to ensure your response flows naturally and uses appropriate context.
- Consider the place or context to adjust your tone and formality level accordingly.
{% if relationships %}
- Reflect your feelings and attitudes as described in your relationships with the audience.
{% endif %}
- If role information is provided, prioritize that role perspective in your response.
- Maintain your specified speaking style consistently throughout.
- If you need to reference the current date or time, use the provided value.
- Respond naturally and consistently as "{{ context.person.name }}", reflecting their personality and communication style.
- Output only the message that "{{ context.person.name }}" would say about the given topic.
- Do not introduce new topics, tangents, or information not present in the topic unless absolutely necessary for natural conversation flow.
- Stay strictly focused on the topic even when the conversation history suggests other directions.
- Do not include your name, signature, or any form of sign-off at the end of your message.
- Do not add greetings, closings, or formal signatures unless they are part of the natural conversation flow and topic.
- Return only the JSON array matching the MessageResponse schema. Do not include any extra text or formatting.
</instructions>
