from guildbotics.app_api.activity_links import links_from_attributes


def test_github_link_kind_comes_from_provider_attributes_not_url_path() -> None:
    links = links_from_attributes(
        {
            "github.kind": "issue",
            "github.number": "8",
            "github.url": "https://github.com/owner/repo/pull/8",
        }
    )

    assert [(link.kind, link.label, link.url) for link in links] == [
        ("issue", "Issue #8", "https://github.com/owner/repo/pull/8")
    ]


def test_unknown_github_link_kind_is_external() -> None:
    links = links_from_attributes(
        {
            "github.number": "8",
            "github.url": "https://github.com/owner/repo/pull/8",
        }
    )

    assert [(link.kind, link.label, link.url) for link in links] == [
        ("external", "GitHub #8", "https://github.com/owner/repo/pull/8")
    ]
