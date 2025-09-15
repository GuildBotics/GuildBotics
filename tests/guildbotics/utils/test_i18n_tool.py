"""Unit tests for guildbotics.utils.i18n_tool.

Validates set/get language behavior and English fallback configuration.
Each test isolates the global i18n state to avoid cross-test interference.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Generator

import pytest


@pytest.fixture(autouse=True)
def isolate_i18n_state() -> Generator[None, None, None]:
    """Isolate global i18n state per test.

    The i18n library keeps global configuration and load paths. This fixture
    snapshots those structures and restores them after each test so that
    importing and using ``i18n_tool`` in one test does not leak into others.
    """
    import i18n  # Local import to avoid module-level side effects

    settings_backup = deepcopy(getattr(i18n, "config").settings)
    load_path_backup = list(i18n.load_path)

    try:
        yield
    finally:
        # Restore load path and all config settings
        i18n.load_path[:] = load_path_backup
        i18n.config.settings.clear()
        i18n.config.settings.update(settings_backup)


def test_set_and_get_language_roundtrip() -> None:
    """set_language updates locale and get_language returns it."""
    # Import inside test after the isolation fixture has started
    from guildbotics.utils import i18n_tool

    # Set to a locale (may or may not exist in available_locales)
    i18n_tool.set_language("ja")
    assert i18n_tool.get_language() == "ja"

    # Change to another locale and verify roundtrip
    i18n_tool.set_language("fr")
    assert i18n_tool.get_language() == "fr"


def test_sets_fallback_to_english() -> None:
    """set_language always configures English ('en') as fallback."""
    import i18n

    from guildbotics.utils import i18n_tool

    i18n_tool.set_language("xx")  # arbitrary/unknown locale is fine
    assert i18n.get("fallback") == "en"
