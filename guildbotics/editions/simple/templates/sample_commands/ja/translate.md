---
description: 入力文をOSのUI言語と英語の間で相互翻訳します。OSのUI言語が英語の場合は日本語を使用します。
template_engine: jinja2
inputs:
  message: required
commands:
  - name: os_ui_language
    command: functions/get_os_ui_language
---
入力メッセージは構造化データです。
{% if os_ui_language.language_code == "en" %}
`input`フィールドのテキストが日本語であれば英語に、英語であれば日本語に翻訳してください。
{% else %}
`input`フィールドのテキストが{{ os_ui_language.language_name }}であれば英語に、英語であれば{{ os_ui_language.language_name }}に翻訳してください。
{% endif %}
翻訳結果だけを返してください。
