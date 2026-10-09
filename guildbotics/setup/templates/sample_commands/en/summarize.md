---
brain: agent
description: Read the first section of a file and summarize it in one line using the specified language.
args:
  file:
    required: true
  language:
    default: English
inputs:
  message: hidden
---
Read the first section of ${file} and summarize it in one line using ${language}.
