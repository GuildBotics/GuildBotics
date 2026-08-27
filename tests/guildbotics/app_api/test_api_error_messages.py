"""Every API error sentence exists in both languages, and says the same thing.

The population is collected from the source: every ``AppApiError``,
``_error``, ``_reporting`` and ``api_error_message`` call in the application
layer names either a message key (translated here) or a ``reason`` (a dynamic
sentence from a lower layer, deliberately passed through verbatim). A key
without an entry in both locale files, an entry no call uses, or a pair whose
placeholders differ, all fail here -- which is what keeps a translation from
quietly becoming a different sentence with different data in it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

from guildbotics.app_api.errors import AppApiError

APP_API = Path("guildbotics/app_api")
LOCALES = Path("guildbotics/templates/locales/app_api")

#: Keys raised through a constant or composed away from the call site, which
#: the literal scan below cannot see.
KEYS_BEHIND_CONSTANTS = {
    "config_changed",  # config_revisions.CONFIG_CHANGED
}

_CALL_NAMES = {"AppApiError", "_error", "_reporting", "api_error_message"}


def _collect_used_keys() -> set[str]:
    keys: set[str] = set()
    for path in APP_API.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if name not in _CALL_NAMES or not node.args:
                continue
            if any(keyword.arg == "reason" for keyword in node.keywords):
                continue
            second = node.args[1] if len(node.args) > 1 else None
            if isinstance(second, ast.Constant) and isinstance(second.value, str):
                keys.add(second.value)
            elif isinstance(node.args[0], ast.Constant) and isinstance(
                node.args[0].value, str
            ):
                keys.add(node.args[0].value)
    return keys | KEYS_BEHIND_CONSTANTS


def _flatten(prefix: str, data: dict) -> dict[str, str]:
    flat: dict[str, str] = {}
    for name, value in data.items():
        key = f"{prefix}.{name}" if prefix else name
        if isinstance(value, dict):
            flat.update(_flatten(key, value))
        else:
            flat[key] = str(value)
    return flat


def _entries(language: str) -> dict[str, str]:
    data = yaml.safe_load((LOCALES / f"errors.{language}.yml").read_text())
    return _flatten("", data[language])


def _placeholders(template: str) -> set[str]:
    names = set()
    rest = template
    while "%{" in rest:
        rest = rest.split("%{", 1)[1]
        names.add(rest.split("}", 1)[0])
    return names


def test_every_used_key_exists_in_both_languages_and_no_entry_is_unused():
    used = _collect_used_keys()
    english = _entries("en")
    japanese = _entries("ja")

    assert sorted(used - english.keys()) == [], "missing from errors.en.yml"
    assert sorted(used - japanese.keys()) == [], "missing from errors.ja.yml"
    assert sorted(english.keys() - used) == [], "unused entries in errors.en.yml"
    assert sorted(english.keys() ^ japanese.keys()) == []


def test_the_two_languages_carry_the_same_placeholders():
    english = _entries("en")
    japanese = _entries("ja")
    for key, template in english.items():
        assert _placeholders(template) == _placeholders(japanese[key]), key


def test_an_error_renders_its_sentence_in_the_asked_language():
    error = AppApiError("hotkey_invalid", params={"accelerator": "Ctrl+X"})

    assert error.localized("en") == "'Ctrl+X' is not a usable key combination."
    assert error.localized("ja") == "'Ctrl+X' はキーの組み合わせとして使えません。"
    # The English rendering doubles as the exception's own text for logs.
    assert str(error) == error.localized("en")


def test_a_reason_passes_through_untranslated():
    error = AppApiError("device_name_invalid", reason="too long")

    assert error.localized("ja") == "too long"
    assert error.message == "too long"
