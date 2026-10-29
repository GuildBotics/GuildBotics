"""Search failures must never masquerade as an empty tracker."""

import json

import httpx
import pytest

from guildbotics.runtime.port import ServiceError
from guildbotics.utils.i18n_tool import t
from tests.guildbotics.integrations.github_services import (
    REPO,
    ScriptedGitHub,
    ticket_service,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"items": [], "total_count": 0, "incomplete_results": True},
        {"items": [], "total_count": 1001, "incomplete_results": False},
    ],
)
async def test_github_search_refuses_partial_answers(monkeypatch, response):
    github = ScriptedGitHub().on("GET", "/search/issues", response)
    service = ticket_service(github, monkeypatch)
    try:
        with pytest.raises(
            ServiceError, match=t("integrations.github.response.search_incomplete")
        ):
            await service.read("tracker_tickets", REPO)
    finally:
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404, 422, 500])
async def test_github_search_propagates_failed_reads(monkeypatch, status):
    github = ScriptedGitHub().on(
        "GET",
        "/search/issues",
        httpx.Response(status, json={"message": "Search unavailable"}),
    )
    service = ticket_service(github, monkeypatch)
    try:
        with pytest.raises(ServiceError):
            await service.read("tracker_tickets", REPO)
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_github_search_quotes_literals_and_uses_fixed_scope(monkeypatch):
    github = ScriptedGitHub().on(
        "GET",
        "/search/issues",
        {"items": [], "total_count": 0, "incomplete_results": False},
    )
    service = ticket_service(github, monkeypatch)
    text = 'repo:other/repo OR "title" back\\slash'
    label = 'label with "quotes"'
    try:
        await service.read(
            "tracker_tickets",
            REPO,
            parameters={"text": text, "state": "all", "label": label, "page_size": 7},
        )
        assert len(github.requests) == 1
        params = github.requests[0].url.params
        assert params["q"] == " ".join(
            [
                f"repo:{REPO}",
                "is:issue",
                "in:title,body",
                *[json.dumps(term) for term in text.split()],
                "label:" + json.dumps(label),
            ]
        )
        assert params["per_page"] == "7" and params["page"] == "1"
        assert params["sort"] == "created" and params["order"] == "asc"
    finally:
        await service.aclose()
