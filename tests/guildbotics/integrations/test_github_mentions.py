"""GitHub's resolved links, with GuildBotics tokens outside Markdown code."""

import pytest

from guildbotics.integrations.github.mentions import mentions


@pytest.mark.parametrize(
    "html,expected",
    [
        (
            '<p><a class="user-mention notranslate" href="https://github.com/AIKO">@AIKO</a></p>',
            {"@aiko"},
        ),
        ('<a class="team-mention">@org/team</a>', {"@org/team"}),
        ('<a class="user-mention"><strong>@aiko</strong></a>', {"@aiko"}),
        ('<a class="user-mention">@aiko-app[bot]</a>', {"@aiko-app[bot]"}),
        ("<p>@unresolved and email@example.com</p>", set()),
        ('<a href="https://github.com/aiko">@aiko</a>', set()),
        ("<code>@pytest.fixture and @types/node and ⚙aiko</code>", set()),
        ("<pre><code>@aiko ⚙aiko</code></pre>", set()),
        ("<p>⚙AIKO Please help.</p>", {"⚙aiko"}),
        ("<p>⚙aiko-long and ⚙unknown</p>", set()),
        ("<!-- ⚙aiko --><p>Nothing addressed.</p>", set()),
        ("<p>⚙<code>ignored</code>aiko</p>", set()),
        ('<code><a class="user-mention">@aiko</a></code>', set()),
    ],
)
def test_only_resolved_links_and_configured_visible_tokens_count(html, expected):
    assert mentions(html, ["⚙aiko"]) == expected


@pytest.mark.parametrize(
    "html,expected",
    [
        ("<p>⚙aiko<br>please help</p>", {"⚙aiko"}),
        ("<p>⚙<br>aiko</p>", set()),
        ("<p>⚙aiko</p><p>-yuki</p>", {"⚙aiko"}),
        ("<p>⚙</p><p>aiko</p>", set()),
    ],
)
def test_html_elements_neither_join_tokens_nor_extend_them(html, expected):
    assert mentions(html, ["⚙aiko"]) == expected
