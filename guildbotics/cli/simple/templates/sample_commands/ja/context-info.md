---
brain: none
template_engine: jinja2
description: 実行中のメンバーとチーム情報を表示します。
---
言語コード: {{ context.language_code }}
言語名: {{ context.language_name }}

ID: {{ context.person.person_id }}
名前: {{ context.person.name }}
話し方: {{ context.person.speaking_style }}

チームメンバー:
{% for member in context.team.members %}
- {{ member.person_id }}: {{ member.name }}
{% endfor %}
