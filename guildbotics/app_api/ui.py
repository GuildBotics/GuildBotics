"""The screens the server hands to a browser and to the Desktop windows.

They are the Vite build of ``desktop/``, which the packaged programs carry at
the same place relative to the package (``desktop/sidecar/guildbotics.spec``).
The pages hold no data: what they show, they read from the API with the
session the entry link gave the browser.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

import guildbotics
from guildbotics.utils.i18n_tool import t

UI_DIRECTORY = Path(guildbotics.__file__).parent.parent / "desktop" / "dist"
#: The main screen's paths: its own routes are in the fragment, and the entry
#: and consent pages are told apart by their path.
_MAIN_PATHS = ("/", "/index.html", "/-/enter", "/-/authorize")


def serve(app: FastAPI, directory: Path = UI_DIRECTORY) -> None:
    def page(name: str) -> Response:
        path = directory / name
        if not path.is_file():
            return PlainTextResponse(
                t("app_api.ui.not_built", path=path), status_code=503
            )
        # A new build replaces the pages' asset names, so the page itself is
        # always asked for again; the assets never change under one name.
        return FileResponse(path, headers={"Cache-Control": "no-cache"})

    for path in _MAIN_PATHS:
        app.add_api_route(
            path, lambda: page("index.html"), methods=["GET"], include_in_schema=False
        )
    app.add_api_route(
        "/quick.html",
        lambda: page("quick.html"),
        methods=["GET"],
        include_in_schema=False,
    )
    app.mount(
        "/assets",
        StaticFiles(directory=directory / "assets", check_dir=False),
        name="assets",
    )
