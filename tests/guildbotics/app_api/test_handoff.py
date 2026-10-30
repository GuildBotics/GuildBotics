"""The exchange directory, listed and downloaded through the API."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from guildbotics.app_api import handoff
from guildbotics.app_api.api import create_app
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.environment.contract import exchange_dir
from tests.guildbotics.app_api.owner import browser_client


@pytest.fixture
def client(workspace):
    return browser_client(create_app(runtime=AppRuntime(EventBus())))


@pytest.fixture
def folder() -> Path:
    root = exchange_dir()
    (root / "reports").mkdir(parents=True)
    return root


def test_the_listing_shows_regular_files_newest_first(client, folder, symlinks):
    (folder / "old.md").write_text("old")
    os.utime(folder / "old.md", (1, 1))
    (folder / "reports" / "new.pdf").write_bytes(b"%PDF")
    (folder / ".hidden").write_text("x")
    (folder / "link.md").symlink_to(folder / "old.md")
    listing = client.get("/handoff/files").json()
    assert listing["directory"] == str(folder)
    assert [file["path"] for file in listing["files"]] == ["reports/new.pdf", "old.md"]
    assert listing["files"][0]["size"] == 4


def test_a_listed_file_downloads_as_an_attachment(client, folder):
    (folder / "reports" / "結果.md").write_text("done", encoding="utf-8")
    answer = client.get("/handoff/files/reports/結果.md")
    assert answer.status_code == 200
    assert answer.content == "done".encode()
    assert answer.headers["content-disposition"].startswith(
        "attachment; filename*=UTF-8''"
    )


@pytest.mark.parametrize(
    "path", ["../secret", "reports/../../secret", ".hidden", "missing.md", "reports"]
)
def test_only_a_file_inside_the_directory_downloads(client, folder, path):
    (folder / ".hidden").write_text("x")
    (folder.parent / "secret").write_text("x")
    assert client.get(f"/handoff/files/{path}").status_code in (400, 404)


def test_a_link_inside_the_directory_is_not_followed(client, folder, symlinks):
    (folder.parent / "secret").write_text("secret")
    (folder / "link.md").symlink_to(folder.parent / "secret")
    answer = client.get("/handoff/files/link.md")
    assert answer.status_code in (400, 404)
    assert b"secret" not in answer.content


def test_a_file_too_large_to_hand_over_is_refused(client, folder, monkeypatch):
    monkeypatch.setattr(handoff, "MAX_DOWNLOAD_BYTES", 3)
    (folder / "big.bin").write_bytes(b"1234")
    assert client.get("/handoff/files/big.bin").status_code == 413
