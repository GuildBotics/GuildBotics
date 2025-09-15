# Contributing Guidelines

## Project Structure & Module Organization
- `guildbotics/`: Core package. Key modules include `drivers/` (schedulers), `workflows/` (task orchestration), `modes/` (behavior per task type), `intelligences/` (brains), `integrations/`, `loader/`, `runtime/`, `entities/`, `utils/`, and `templates/`.
- `tests/`: Pytest suite. Unit tests mirror package paths under `tests/guildbotics/`; integration tests live in `tests/it/` with sample configs in `tests/it/config/`.
- `docs/`: Architecture and design (`docs/ARCHITECTURE.*.md`).
- `main.py`: Entry point for running the scheduler.
- `.env`, `.env.example`: Local configuration.

## Build, Test, and Development Commands
- Sync dependencies: `uv sync --extra test`
- Run tests and create coverage report: `uv run --no-sync python -m pytest tests/ --cov=guildbotics --cov-report=xml` (output: `coverage.xml`).

Note: This repository provides a fixed dependency file `pyproject.toml`. Use `uv sync` to install the required libraries.

## Coding Style & Naming Conventions
- Python 3.11+; 4-space indent; prefer full type hints.
- Format with Black (88 cols). Example: `python -m black .`
- Imports: stdlib, third-party, local (grouped and sorted).
- Naming: modules/funcs/vars `snake_case`, classes `PascalCase`, constants `UPPER_SNAKE_CASE`.
- Keep logs structured: `%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s`.
- Write comments in the source code in English. Use Google-style docstrings.

## Core Engineering Principles
- Scope discipline: keep every change tightly focused; do not broaden scope without explicit agreement.
- Simplicity first: apply KISS; avoid speculative abstraction (YAGNI).
- Pragmatic SOLID: especially Single Responsibility—avoid bloated functions/modules.
- DRY: no copy-paste duplication; factor shared logic into `utils/` or suitable shared modules.
- One-way dependencies: prevent cyclic imports/architectural cycles; lower-level modules (`entities/`, `utils/`) must not depend on higher orchestration layers (`workflows/`, `drivers/`).
- Respect existing architecture: review `docs/ARCHITECTURE.*.md` before altering boundaries.
- Performance mindset: do not prematurely optimize, but fix evident inefficiencies (N+1 calls, needless I/O, excessive complexity) when discovered.

## Testing Guidelines
- Framework: pytest. Place tests under `tests/` with files named `test_*.py`.
- Mirror package structure for unit tests; use `tests/it/` for workflow/integration scenarios.
- Use `monkeypatch` for time, randomness, and I/O; keep tests deterministic.
- Maintain or improve coverage; verify `coverage.xml` updates locally.
- Report results honestly; never state success when failures occurred.
- Disclose environment limitations early (missing creds, disabled services) instead of silently skipping critical logic.
- Design for testability: small pure functions, clear side-effect boundaries, explicit dependency injection where helpful.

## Commit & Pull Request Guidelines
- Use Conventional Commits: `feat:`, `fix:`, `chore:`, `refactor:`, etc. Short, imperative subject; details in body. English or Japanese is fine.
- PRs: clear description, linked issues (`#123`), screenshots/logs when relevant, reproduction and test steps, and note any env/config changes.
- Ensure `pytest` passes before requesting review.

## Code Review Etiquette
- Address all feedback (implement or clarify); do not ignore comments.
- Keep PR scope tight; open follow-up issues/PRs for out-of-scope refactors.
- Provide concise rationale when choosing alternative solutions.
- Keep diffs cohesive and reasonably small; split large refactors.
- Avoid unrelated style or rename churn unless essential to the change.

## Documentation & Markdown Guidelines
- Tone: concise, formal technical business writing unless another style is explicitly requested.
- Headings: structure content with a clear hierarchy (H1–H3 preferred). Avoid skipping levels.
- Audience adaptation: tailor vocabulary, emphasis, and depth to target roles (e.g., PM, architect, UX) when stated.
- Metadata: early section should surface document type, purpose, intended audience, key requirements, and any open questions.
- Lists & tables: prefer bullet lists or tables for structured data instead of prose paragraphs.
- Diagrams: use Mermaid fenced code blocks when a diagram clarifies flows or relationships; validate with a Mermaid tool before committing.
- Mermaid conventions: quote labels with special characters, declare nodes before references, close subgraphs/directions, keep diagrams minimal and readable.
- Pending items: mark unresolved or unknown points with the token "🔶 Pending" for easy triage.
- Language: default to the user's prompt language (Japanese or English) unless the repository standard dictates otherwise.
- Slides: for presentation-style artifacts, use Marp Markdown (https://marp.app/) rather than plain Markdown.
- Output discipline: produce a single self-contained document without extraneous commentary or tool noise.
- Reuse: do not duplicate existing docs—link to canonical sources (e.g., architecture docs) instead of restating them.

## Security & Configuration Tips
- Never commit secrets.
- Review `docs/ARCHITECTURE.en.md` for how drivers, workflows, and modes interact; some runtime data may store under `~/.guildbotics/data/`.
- Validate external input where applicable; fail fast with clear errors.
- Use least-privilege credentials; rotate secrets upon suspicion or exposure.

