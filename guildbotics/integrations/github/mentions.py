"""Mentions resolved by GitHub, plus GuildBotics' explicit agent tokens."""

import re
from collections.abc import Iterable
from html.parser import HTMLParser


class _Mentions(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.names: set[str] = set()
        self.tokens: set[str] = set()
        self._code = 0
        self._anchor: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"code", "pre"}:
            self._code += 1
        if not self._code and tag == "a":
            classes = (dict(attrs).get("class") or "").split()
            if {"user-mention", "team-mention"}.intersection(classes):
                self._anchor = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"code", "pre"}:
            self._code -= 1
        if tag == "a" and self._anchor is not None:
            self.names.add("".join(self._anchor).strip().casefold())
            self._anchor = None

    def handle_data(self, data: str) -> None:
        if not self._code:
            # Tokens are literal identifiers, never assembled across elements.
            self.tokens.update(re.findall(r"⚙[\w-]+", data.casefold()))
            if self._anchor is not None:
                self._anchor.append(data)


def mentions(html: str, agent_tokens: Iterable[str]) -> set[str]:
    """Read GitHub's mention links and configured tokens outside code.

    GitHub owns Markdown and username resolution. A raw ``@name`` is never
    inferred to be a mention here. ``⚙person_id`` is GuildBotics' own protocol,
    retained for members assigned through the board's Agent field.
    """
    parser = _Mentions()
    parser.feed(html)
    parser.close()
    return parser.names | parser.tokens.intersection(
        token.casefold() for token in agent_tokens
    )
