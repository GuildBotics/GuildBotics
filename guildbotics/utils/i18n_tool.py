import locale
from typing import Any

import i18n  # type: ignore

from guildbotics.utils.fileio import get_config_path

i18n.load_path.append(get_config_path("locales"))


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
