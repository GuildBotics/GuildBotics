"""Select reset information for rate-limit notices and trace presentations."""

from guildbotics.utils.timestamps import parse_iso_datetime


def rate_limit_reset_display(
    retry_after_at: str, retry_after_text: str
) -> tuple[str, str]:
    """Return the message-key suffix and its reset value.

    Preserve the timestamp's offset so a saved notice remains unambiguous.
    Provider wording is only a hint when no reset timestamp can be read.
    """
    reset = parse_iso_datetime(retry_after_at)
    if reset is not None:
        return "_with_reset", reset.isoformat(sep=" ", timespec="seconds")
    if retry_after_text:
        return "_with_hint", retry_after_text
    return "", ""
