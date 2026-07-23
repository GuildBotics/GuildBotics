---
description: Translate input text between the OS UI language and English, using Japanese when the OS UI language is English.
template_engine: jinja2
inputs:
  message: required
commands:
  - name: os_ui_language
    command: functions/get_os_ui_language
---
The input message is structured data.
{% if os_ui_language.language_code == "en" %}
If the text in the `input` field is in Japanese, translate it to English; if it is in English, translate it to Japanese.
{% else %}
If the text in the `input` field is in {{ os_ui_language.language_name }}, translate it to English; if it is in English, translate it to {{ os_ui_language.language_name }}.
{% endif %}
Return only the translated text.
