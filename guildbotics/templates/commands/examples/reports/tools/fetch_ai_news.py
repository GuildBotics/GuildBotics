from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

import requests

DEFAULT_QUERY = "AI OR OpenAI OR Anthropic OR Google AI"


def main(
    context,  # noqa: ANN001 - runtime injects Context
    query: str = DEFAULT_QUERY,
    language: str = "ja",
    country: str = "JP",
    limit: str | int = 8,
    max_age_hours: str | int = 36,
) -> dict[str, Any]:
    """Fetch recent AI news headlines from Google News RSS and return structured data."""
    _ = context  # unused in MVP sample but accepted for consistency with PythonCommand
    limit_n = _to_int(limit, 8, minimum=1, maximum=20)
    max_age_n = _to_int(max_age_hours, 36, minimum=1, maximum=24 * 14)
    feed_url = build_google_news_rss_url(query, language=language, country=country)
    response = requests.get(feed_url, timeout=10)
    response.raise_for_status()
    items = parse_google_news_rss(response.text, max_age_hours=max_age_n)
    return {
        "query": query,
        "language": language,
        "country": country,
        "feed_url": feed_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "item_count": len(items[:limit_n]),
        "items": items[:limit_n],
    }


def build_google_news_rss_url(query: str, *, language: str = "ja", country: str = "JP") -> str:
    lang = (language or "ja").lower()
    ctry = (country or "JP").upper()
    return (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(query)}&hl={lang}&gl={ctry}&ceid={ctry}:{lang}"
    )


def parse_google_news_rss(xml_text: str, *, max_age_hours: int = 36) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    channel = root.find("./channel")
    if channel is None:
        return []

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max(1, int(max_age_hours)))
    items: list[NewsItem] = []
    for item in channel.findall("./item"):
        parsed = _parse_item(item)
        if parsed is None:
            continue
        if parsed.published_at and parsed.published_at < cutoff:
            continue
        items.append(parsed)

    # Newest first when publish time is available; otherwise preserve relative order at end.
    items.sort(key=lambda x: x.sort_key(), reverse=True)
    return [i.to_dict() for i in items]


@dataclass(slots=True)
class NewsItem:
    title: str
    link: str
    source: str = ""
    published_at: datetime | None = None

    def sort_key(self) -> datetime:
        return self.published_at or datetime.fromtimestamp(0, tz=timezone.utc)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"title": self.title, "link": self.link}
        if self.source:
            out["source"] = self.source
        if self.published_at:
            out["published_at"] = self.published_at.isoformat()
        return out


def _parse_item(item: ET.Element) -> NewsItem | None:
    title = _text(item, "title")
    link = _text(item, "link")
    if not title or not link:
        return None

    source = _text(item, "source")
    pub_date = _text(item, "pubDate")
    published_at = None
    if pub_date:
        try:
            dt = parsedate_to_datetime(pub_date)
            published_at = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            published_at = None

    return NewsItem(title=title, link=link, source=source, published_at=published_at)


def _text(parent: ET.Element, tag: str) -> str:
    el = parent.find(tag)
    if el is None or el.text is None:
        return ""
    return el.text.strip()


def _to_int(value: str | int, default: int, *, minimum: int, maximum: int) -> int:
    try:
        n = int(value)
    except Exception:
        return default
    return max(minimum, min(maximum, n))
