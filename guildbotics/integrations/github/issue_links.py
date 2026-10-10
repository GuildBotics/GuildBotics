"""The issue links of a pull request body, as GitHub reads them.

A standalone ``Closes #<n>`` / ``Fixes`` / ``Resolves`` / ``Refs #<n>`` line in
a paragraph or a list item links the pull request to an issue; GitHub closes a
closing-keyword issue when the pull request merges into the default branch.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from markdown_it import MarkdownIt

from guildbotics.runtime.integration_factory import MemberCapabilityError

_ISSUE_CLOSING_KEYWORD = r"(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)"
_ISSUE_REFS_KEYWORD = r"refs?"
_ISSUE_LINK = re.compile(
    rf"((?:{_ISSUE_CLOSING_KEYWORD}|{_ISSUE_REFS_KEYWORD})[ \t]+#(\d+))",
    re.I,
)
_ISSUE_SOURCE_LINK = re.compile(
    rf"(?:[ \t]*(?:[-+*]|\d+[.)])[ \t]+)*[ \t]*{_ISSUE_LINK.pattern}[ \t]*", re.I
)


@dataclass(frozen=True)
class _IssueLink:
    start: int
    end: int
    trailer: str
    number: str


def _issue_links(body: str) -> Iterator[_IssueLink]:
    """Select standalone paragraph or list links, excluding quoted examples."""
    source = body.splitlines(keepends=True)
    offsets = [0]
    for line in source:
        offsets.append(offsets[-1] + len(line))
    tokens = MarkdownIt("commonmark").parse(body)
    quoted = 0
    for index, token in enumerate(tokens):
        if token.type == "blockquote_open":
            quoted += 1
        elif token.type == "blockquote_close":
            quoted -= 1
        if (
            token.type != "inline"
            or tokens[index - 1].type != "paragraph_open"
            or token.map is None
            or quoted
        ):
            continue
        code_rows: set[int] = set()
        code_contents = {
            child.content
            for child in token.children or []
            if child.type == "code_inline"
        }
        for code in re.finditer(
            r"(?=(?<![\\`])(?:\\\\)*(`+)(?!`)(.*?)(?<!`)\1(?!`))", token.content, re.S
        ):
            content = code.group(2).replace("\n", " ")
            if content.startswith(" ") and content.endswith(" ") and content.strip():
                content = content[1:-1]
            if content not in code_contents:
                continue
            code_rows.update(
                range(
                    token.content.count("\n", 0, code.start()),
                    token.content.count("\n", 0, code.end(2)) + 1,
                )
            )
        for row, line in enumerate(token.content.splitlines()):
            candidate = _ISSUE_LINK.fullmatch(line.strip())
            if candidate is None or row in code_rows:
                continue
            source_row = token.map[0] + row
            match = _ISSUE_SOURCE_LINK.fullmatch(source[source_row].rstrip("\r\n"))
            if match is not None and match.group(1) == candidate.group(1):
                offset = offsets[source_row]
                yield _IssueLink(
                    offset + match.start(1),
                    offset + match.end(1),
                    match.group(1),
                    match.group(2),
                )


def _append_trailer(body: str, trailer: str) -> str:
    result = f"{body.rstrip()}\n\n{trailer}" if body.strip() else trailer
    if not any(
        link.trailer == trailer and link.start == len(result) - len(trailer)
        for link in _issue_links(result)
    ):
        raise MemberCapabilityError(
            "Cannot append an issue link outside Markdown code or HTML. "
            "Close the open code or HTML block in the body first."
        )
    return result


def preserve_issue_links(body: str, previous_body: str) -> str:
    """Append missing issue links while keeping the replacement body's wording."""
    mentioned = {match.number for match in _issue_links(body)}
    inherited: set[str] = set()
    for match in _issue_links(previous_body):
        number = match.number
        trailer = match.trailer
        if number not in mentioned and trailer.casefold() not in inherited:
            body = _append_trailer(body, trailer)
            inherited.add(trailer.casefold())
    return body


def append_issue_link(body: str, issue_url: str, *, closes: bool) -> str:
    if not issue_url:
        return body
    match = re.search(r"/issues/(\d+)", issue_url)
    if not match:
        return body
    issue_number = match.group(1)
    links = [link for link in _issue_links(body) if link.number == issue_number]
    refs = [link for link in links if re.match(_ISSUE_REFS_KEYWORD, link.trailer, re.I)]
    if len(links) > len(refs):
        return body
    if refs:
        if not closes:
            return body
        for link in reversed(refs):
            body = f"{body[: link.start]}Closes #{issue_number}{body[link.end :]}"
        return body
    trailer = f"Closes #{issue_number}" if closes else f"Refs #{issue_number}"
    return _append_trailer(body, trailer)
