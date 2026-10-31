"""The screens the server hands to a browser."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from guildbotics.app_api import ui
from tests.guildbotics.app_api.owner import ORIGIN


def _served(tmp_path: Path) -> TestClient:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("main")
    (tmp_path / "quick.html").write_text("quick")
    (tmp_path / "bootstrap.html").write_text("bootstrap")
    (tmp_path / "assets" / "app.js").write_text("js")
    app = FastAPI()
    ui.serve(app, tmp_path)
    return TestClient(app, base_url=ORIGIN)


def test_the_main_page_answers_its_own_paths(tmp_path):
    client = _served(tmp_path)
    for path in ("/", "/index.html", "/-/enter", "/-/authorize"):
        answer = client.get(path)
        assert answer.text == "main" and answer.headers["cache-control"] == "no-cache"
    assert client.get("/quick.html").text == "quick"
    assert client.get("/assets/app.js").text == "js"


def test_only_the_pages_are_served(tmp_path):
    client = _served(tmp_path)
    for path in ("/bootstrap.html", "/service", "/assets/../index.html.bak"):
        assert client.get(path).status_code == 404


def test_unbuilt_screens_say_so(tmp_path):
    app = FastAPI()
    ui.serve(app, tmp_path / "missing")
    answer = TestClient(app, base_url=ORIGIN).get("/")
    assert answer.status_code == 503 and "not built" in answer.text
