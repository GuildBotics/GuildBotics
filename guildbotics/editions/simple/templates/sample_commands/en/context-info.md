---
brain: none
template_engine: jinja2
description: Display the current member and team context.
---
Language code: {{ context.language_code }}
Language name: {{ context.language_name }}

ID: {{ context.person.person_id }}
Name: {{ context.person.name }}
Speaking style: {{ context.person.speaking_style }}

Team members:
{% for member in context.team.members %}
- {{ member.person_id }}: {{ member.name }}
{% endfor %}
