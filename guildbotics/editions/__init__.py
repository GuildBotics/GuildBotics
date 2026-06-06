from __future__ import annotations

import os

from guildbotics.editions.edition import Edition
from guildbotics.utils.import_utils import instantiate_class


def get_edition() -> Edition:
    """Resolve the active runtime edition from ``GUILDBOTICS_EDITION``."""
    name = os.getenv("GUILDBOTICS_EDITION", "simple")
    if "." not in name:
        name = f"guildbotics.editions.{name}.{name}_edition.{name.capitalize()}Edition"
    return instantiate_class(name, expected_type=Edition)


__all__ = ["Edition", "get_edition"]
