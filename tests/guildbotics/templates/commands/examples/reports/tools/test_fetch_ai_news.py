from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from guildbotics.templates.commands.examples.reports.tools import fetch_ai_news


def test_build_google_news_rss_url_contains_query_and_locale():
    url = fetch_ai_news.build_google_news_rss_url("OpenAI", language="ja", country="JP")
    assert "news.google.com/rss/search" in url
    assert "q=OpenAI" in url
    assert "hl=ja" in url
    assert "gl=JP" in url
    assert "ceid=JP:ja" in url


def test_parse_google_news_rss_filters_old_items_and_sorts_newest():
    now = datetime.now(timezone.utc)
    recent = format_datetime(now - timedelta(hours=2))
    older = format_datetime(now - timedelta(hours=50))
    newer = format_datetime(now - timedelta(minutes=20))
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test</title>
    <item>
      <title>Old item</title>
      <link>https://example.com/old</link>
      <source>Source A</source>
      <pubDate>{older}</pubDate>
    </item>
    <item>
      <title>Recent item</title>
      <link>https://example.com/recent</link>
      <source>Source B</source>
      <pubDate>{recent}</pubDate>
    </item>
    <item>
      <title>Newest item</title>
      <link>https://example.com/newest</link>
      <source>Source C</source>
      <pubDate>{newer}</pubDate>
    </item>
  </channel>
</rss>
"""
    items = fetch_ai_news.parse_google_news_rss(xml, max_age_hours=36)
    assert [item["title"] for item in items] == ["Newest item", "Recent item"]
    assert items[0]["source"] == "Source C"
    assert items[0]["link"] == "https://example.com/newest"


def test_parse_google_news_rss_handles_missing_channel_or_fields():
    assert fetch_ai_news.parse_google_news_rss("<rss></rss>") == []

    xml = """<rss><channel><item><title>Missing link</title></item></channel></rss>"""
    assert fetch_ai_news.parse_google_news_rss(xml, max_age_hours=1000) == []
