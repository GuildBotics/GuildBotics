"""Shared fixtures for workflow presentation tests."""

from datetime import datetime
from zoneinfo import ZoneInfo

import i18n  # type: ignore
import pytest

from guildbotics.utils import rate_limit_display
from guildbotics.utils.i18n_tool import set_language


@pytest.fixture
def local_rate_limit_timezone(monkeypatch, request):
    """Fix the host timezone without changing process-global OS state."""
    zone = ZoneInfo(getattr(request, "param", "Asia/Tokyo"))

    class LocalDatetime(datetime):
        def astimezone(self, tz=None):
            return super().astimezone(zone if tz is None else tz)

    monkeypatch.setattr(rate_limit_display, "datetime", LocalDatetime)
    return zone


@pytest.fixture
def notice_language(request):
    previous_locale = i18n.get("locale")
    previous_fallback = i18n.get("fallback")
    set_language(request.param)
    yield request.param
    i18n.set("locale", previous_locale)
    i18n.set("fallback", previous_fallback)
