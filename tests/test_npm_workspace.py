"""Keep both screens on one dependency graph and the image's browser."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_screens_share_dependency_ranges_and_one_lock():
    manifest = json.loads((ROOT / "package.json").read_text())
    ranges = {}
    lock = json.loads((ROOT / "package-lock.json").read_text())
    for workspace in manifest["workspaces"]:
        assert not (ROOT / workspace / "package-lock.json").exists()
        package = json.loads((ROOT / workspace / "package.json").read_text())
        assert lock["packages"][workspace]["name"] == package["name"]
        for section in ("dependencies", "devDependencies"):
            for name, version in package[section].items():
                assert version.startswith("^")
                assert ranges.setdefault(name, version) == version


def test_every_locked_playwright_uses_the_images_chromium():
    dockerfile = (ROOT / "docker/agent-environment/Dockerfile").read_text()
    version = re.search(r"ARG PLAYWRIGHT_VERSION=(\S+)", dockerfile)[1]
    lock = json.loads((ROOT / "package-lock.json").read_text())
    browsers = [
        package["version"]
        for path, package in lock["packages"].items()
        if path.endswith(("/@playwright/test", "/playwright", "/playwright-core"))
    ]
    assert browsers
    assert set(browsers) == {version}


def test_react_has_one_shared_installation():
    lock = json.loads((ROOT / "package-lock.json").read_text())
    for name in ("react", "react-dom"):
        installations = [
            path
            for path in lock["packages"]
            if path.endswith(f"/node_modules/{name}") or path == f"node_modules/{name}"
        ]
        assert installations == [f"node_modules/{name}"]
