"""Select reset information for rate-limit notices and trace presentations."""

from datetime import datetime


def rate_limit_reset_display(
    retry_after_at: str, retry_after_text: str
) -> tuple[str, str]:
    """Return the message-key suffix and its reset value.

    Use the host's local time with its offset so a saved notice stays unambiguous.
    Provider wording is only a hint when no reset timestamp can be read.
    """
    try:
        reset = datetime.fromisoformat(retry_after_at.strip())
    except ValueError:
        reset = None
    if reset is not None and reset.tzinfo is not None:
        return "_with_reset", reset.astimezone().isoformat(sep=" ", timespec="seconds")
    if retry_after_text:
        return "_with_hint", retry_after_text
    return "", ""
