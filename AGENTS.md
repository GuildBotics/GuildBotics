# AGENTS.md — Agent Operation Policy

This document defines the operational rules, workflows, output contracts, and verification policies for agents (LLM/automation assistants) active in this repository. When creating deliverables (code/documentation), always refer to this document along with the canonical documents.

## Canonical Documents (Required Reading)
- `CONTRIBUTING.md`: Code conventions, testing, documentation, and security quality standards.
- `README.md`: Project overview, setup, and execution methods.
- `docs/ARCHITECTURE.*.md`: Architecture, layer boundaries, and dependency direction principles.

## Execution Constraints and Approval Flow
- Sandbox: Writing is limited to within the workspace. Network access may be restricted.
- Approval Mode: For important changes or commands that fail due to restrictions, obtain approval and re-execute.
- Destructive Operations: Do not execute `rm`, history alterations, or external writes without explicit permission.

## Workflow (Mandatory)
- Preamble: Before executing commands, declare what will be done in 1–2 sentences.
- Plan Management: For non-trivial tasks, create/update TODOs with `update_plan`. Always have only 1 item `in_progress`.
- Minimal Changes: Strictly adhere to scope, avoid unnecessary renames or large-scale modifications.
- Verification Priority: After changes, run tests or builds as much as possible and report results honestly.

## Output Contract (Format)
- Sections: Use short headings (bold) only when necessary.
- Bullet Points: Summarize important matters concisely with `-`. Integrate related matters.
- Monospace: Enclose commands, file paths, and identifiers in backticks (e.g., `src/app.py:42`).
- File References: Present as clickable single paths. Line numbers in `path:line` format (no range specification).
- Tone: Concise, fact-based, collaborative. Avoid redundant explanations.

## Tool Usage Policy
- Shell: Prioritize `rg` for searches. Read files in units of up to 250 lines. Split processes that produce long outputs.
- `apply_patch`: Always use this for file changes. Do not mix unrelated fixes.
- Testing/Formatting: Use only if existing scripts or settings are present. New introductions are out of scope.

## Verification Policy
- Local Verification: Start from the smallest test closest to the change location and expand as needed.
- Tests are executed with the following command:
  ```bash
  uv run --no-sync python -m pytest tests/ --cov=guildbotics --cov-report=xml
  ```
- Coverage: In possible environments, confirm updates to `coverage.xml`.
- Environment Constraints: Clearly indicate credential shortages or network unavailability as "🔶 Pending".

## Security and Confidentiality
- Secret Information: Never commit `.env` values. Refer to `.env.example` for samples.
- Input Validation: Validate external inputs and provide clear errors on failure.
- Least Privilege: Use only the minimum necessary permissions/keys, and recommend rotation if suspicious.

## Change Completion Criteria (Done Definition)
- It is the minimal difference that meets the requirements within the scope.
- Style/naming/structure complies with `CONTRIBUTING.md`.
- Validity is confirmed through testing or execution, and results are reported.
- Documentation or comments (when necessary) are updated.

---

Note: General development rules for the repository are consolidated in `CONTRIBUTING.md`. Agents must follow this policy while strictly adhering to the conventions in `CONTRIBUTING.md` when creating deliverables.

