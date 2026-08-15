from __future__ import annotations

from datetime import UTC, datetime


def utc_now_iso() -> str:
    """Return the current time in the one format every record is written in.

    Devices compare and sort these strings across machines, so they are all
    produced here rather than formatted again at each call site.
    """
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_iso_datetime(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
