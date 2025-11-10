import locale
from pathlib import Path
from typing import Any

import i18n  # type: ignore

from guildbotics.utils.fileio import (
    get_home_config_path,
    get_primary_config_path,
    get_template_path,
)

locales_path = Path("locales")
i18n.load_path.append(get_template_path() / locales_path)
home_config_locales_path = get_home_config_path(locales_path)
if home_config_locales_path.exists():
    i18n.load_path.append(home_config_locales_path)
primary_config_locales_path = get_primary_config_path(locales_path)
if primary_config_locales_path.exists():
    i18n.load_path.append(primary_config_locales_path)


def set_language(language_code: str) -> None:
    """
    Set the language for localization.
    Args:
        language_code (str): The language code to set.
    """
    i18n.set("locale", language_code)
    i18n.set("fallback", "en")


def get_language() -> str:
    """
    Get the current language code.
    Returns:
        str: The current language code.
    """
    return i18n.get("locale")


def t(key: str, **kwargs: Any) -> str:
    """
    Translate a key to the current language.
    Args:
        key (str): The key to translate.
        **kwargs: Additional keyword arguments for formatting.
    Returns:
        str: The translated string.
    """
    return i18n.t(key, **kwargs)


def get_system_default_language() -> str:
    """
    Get the system's default language code.
    Example: 'ja_JP' → 'ja', 'en_US' → 'en'. If it cannot be determined, returns 'en'.
    """
    lang, _ = locale.getdefaultlocale()
    if not lang:
        return "en"
    return lang.split("_")[0]
