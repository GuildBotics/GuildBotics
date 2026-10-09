---
brain: agent
description: 指定ファイルの最初のセクションを、指定言語で1行に要約します。
args:
  file:
    required: true
  language:
    default: 日本語
inputs:
  message: hidden
---
${file}の最初のセクションを読み、その内容を${language}を用いて、1行で要約してください
