"""The one error type API endpoints answer with, and how its text is made.

An error's sentence is never written inline at the raise site. It is named by
a message key, looked up under ``app_api.errors.*`` in the locale files, and
rendered in the language the request asked for -- so the English and Japanese
sentence for one situation live next to each other and are translated as a
pair, not summarized per error code. A code is a machine-readable category
and may cover several situations; the message key names exactly one.

The exception is a *reason*: a sentence produced by a lower layer -- a
validation's own explanation, a subprocess's output, a setup service message.
Those are passed through verbatim, because a static translation of a dynamic
sentence would replace it with a different claim.
"""

from __future__ import annotations

from typing import Any

from guildbotics.utils.i18n_tool import t


def api_error_message(message_key: str, language: str, params: dict[str, Any]) -> str:
    """Render one error sentence in ``language`` from the locale files."""
    return t(f"app_api.errors.{message_key}", locale=language, **params)


class AppApiError(Exception):
    """A request that cannot be served, as the screen reports it.

    Exactly one of two things carries the sentence: ``message_key`` (a key
    into ``locales/app_api/errors.*.yml``, defaulting to ``code``) for text
    this layer writes, or ``reason`` for a sentence a lower layer already
    produced. ``message`` holds the English rendering for logs and tests; the
    response's message is rendered per request language by the handler.
    """

    def __init__(
        self,
        code: str,
        message_key: str | None = None,
        *,
        reason: str | None = None,
        params: dict[str, Any] | None = None,
        status_code: int = 400,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.reason = reason
        self.message_key = None if reason is not None else (message_key or code)
        self.params = dict(params or {})
        self.status_code = status_code
        self.context = context or {}
        self.message = self.localized("en")
        super().__init__(self.message)

    def localized(self, language: str) -> str:
        """Return this error's sentence in ``language``."""
        if self.message_key is None:
            return self.reason or ""
        return api_error_message(self.message_key, language, self.params)

    def with_status(self, status_code: int) -> AppApiError:
        """Return the same error answered with a different status code."""
        return AppApiError(
            self.code,
            self.message_key,
            reason=self.reason,
            params=self.params,
            status_code=status_code,
            context=self.context,
        )
