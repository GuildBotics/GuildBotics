# Contributing Guidelines

This document covers the **human contribution process**. For repository structure, environment setup, the CI-equivalent build/test/lint commands, coding standards, and the testing strategy, treat the following as the **single source of truth** — they are kept current with CI, so this file does not duplicate (and drift from) them:

- **`AGENTS.md`** — repository working guide: module layout, config resolution, CLI, the CI-equivalent commands, and the testing strategy.
- **`desktop/README.md`** — desktop app build, development, and tests (Vitest unit/component + Playwright E2E).
- **`.github/workflows/ci.yml`** — the exact checks CI enforces.

Tests live under `tests/guildbotics/` mirroring the package layout (there is no separate `tests/it/` tree); the CLI entry point is `guildbotics` (`guildbotics.cli:main`), not a `main.py`.

## Before opening a PR — run the CI-equivalent checks

The authoritative list lives in `AGENTS.md` ("開発時の基本コマンド (CI 準拠)"). Quick reference:

```bash
# Python (repo root)
uv sync --extra test --extra dev
uv run --no-sync ruff format --check guildbotics
uv run --no-sync ruff check guildbotics
uv run --no-sync mypy guildbotics
uv run --no-sync pylint guildbotics
uv run --no-sync python -m pytest tests/ --cov=guildbotics --cov-report=xml

# Markdown links (repo root; requires lychee v0.24.2)
lychee --no-progress --scheme file --include-fragments \
  --exclude-path 'desktop/node_modules' \
  './*.md' './docs/**/*.md' './desktop/**/*.md' './skills/**/*.md'

# Desktop frontend (desktop/) — when touching desktop/
cd desktop && npm ci && npm run quality
# Desktop E2E (real browser + Local API backend; NOT in `npm run quality` or push CI):
#   npm run e2e:install   # first run only
#   npm run e2e
```

`uv sync` installs the pinned dependencies from `pyproject.toml`.

## Coding Style & Naming Conventions
- Python 3.12+ (`requires-python = ">=3.12,<3.14"`); 4-space indent; prefer full type hints.
- Format and lint with Ruff — the source of truth. CI runs `ruff format --check` and `ruff check`; apply locally with `uv run --no-sync ruff format guildbotics`.
- Imports: stdlib, third-party, local (grouped and sorted; handled by Ruff).
- Naming: modules/funcs/vars `snake_case`, classes `PascalCase`, constants `UPPER_SNAKE_CASE`.
- Keep logs structured: `%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s`.
- Write comments in the source code in English. Use Google-style docstrings.

## Core Engineering Principles
- Scope discipline: keep every change tightly focused; do not broaden scope without explicit agreement.
- Simplicity first: apply KISS; avoid speculative abstraction (YAGNI).
- Pragmatic SOLID: especially Single Responsibility—avoid bloated functions/modules.
- DRY: no copy-paste duplication; factor shared logic into `utils/` or suitable shared modules.
- One-way dependencies: prevent cyclic imports/architectural cycles; lower-level modules (`entities/`, `utils/`) must not depend on higher orchestration layers (`templates/`, `commands/`, `drivers/`).
- Respect existing architecture: review `docs/ARCHITECTURE.md` before altering boundaries.
- Performance mindset: do not prematurely optimize, but fix evident inefficiencies (N+1 calls, needless I/O, excessive complexity) when discovered.

## Testing Guidelines
The full testing strategy (which layer to cover, the test pyramid, and the lean-but-real desktop E2E policy) is canonical in `AGENTS.md` → "テスト実装の考え方". Process essentials:

- Framework: pytest. Place tests under `tests/guildbotics/` mirroring the package layout, with files named `test_*.py`. Integration tests (FastAPI `TestClient`, temp-workspace) live alongside, e.g. `tests/guildbotics/app_api/`. There is no separate `tests/it/` tree.
- Desktop frontend: Vitest + React Testing Library for unit/component, and a small set of real-browser Playwright journeys under `desktop/e2e/` (see `desktop/README.md`).
- Use `monkeypatch`/`tmp_path` for time, randomness, env, cwd, HOME, and I/O; keep tests deterministic and hermetic (never touch the real home dir or external services).
- Maintain or improve coverage; verify `coverage.xml` updates locally.
- Report results honestly; never state success when failures occurred.
- Disclose environment limitations early (missing creds, disabled services) instead of silently skipping critical logic.
- Design for testability: small pure functions, clear side-effect boundaries, explicit dependency injection where helpful.
- Desktop frontend follows the same pyramid: Vitest + React Testing Library for unit/component tests (assert role/text/value/payload, not implementation detail), plus a small set of lean-but-real Playwright journeys under `desktop/e2e/` for cross-boundary critical paths exercised against a real browser and a real Local API backend. Keep exhaustive branch coverage in unit/component; reserve E2E for what only a real browser + real wire can verify. The canonical strategy lives in `AGENTS.md` → "テスト実装の考え方".

## Commit & Pull Request Guidelines
- Use Conventional Commits: `feat:`, `fix:`, `chore:`, `refactor:`, etc. Short, imperative subject; details in body. English or Japanese is fine.
- PRs: clear description, linked issues (`#123`), screenshots/logs when relevant, reproduction and test steps, and note any env/config changes.
- Ensure the CI-equivalent checks (see "Before opening a PR") pass before requesting review: `ruff format --check`, `ruff check`, `mypy`, `pylint`, and `pytest` for Python; `npm run quality` under `desktop/` when touching the frontend (plus `npm run e2e` when changing a cross-boundary journey).

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
- Validate external input where applicable; fail fast with clear errors.
- Use least-privilege credentials; rotate secrets upon suspicion or exposure.
