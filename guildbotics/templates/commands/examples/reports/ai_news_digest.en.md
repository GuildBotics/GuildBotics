---
template_engine: jinja2
description: Turn fetched AI news items into a Slack-ready English digest (LLM sample)
commands:
  - name: ai_news
    path: tools/fetch_ai_news.py
---
You are preparing a short Slack digest of recent AI-related news for a technical team.
Read the fetched items below and produce a concise English digest that can be posted directly.

## Input Data (from the previous command)
{{ ai_news }}

## Output Rules
- Return only the Slack message body (no explanation, no code fence)
- Write in English
- Select up to 3 items (prioritize practical impact / relevance)
- Summarize each item in 1-2 sentences
- Include links when possible
- Keep it factual and concise
- Target length: about 8-16 lines
- End with 1-2 lines of "why this matters" for the team
