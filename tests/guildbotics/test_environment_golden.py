"""What a command's isolated environment is told of its host, and how big it
boots, as the Python version decides them and the Rust version too
(``records/golden/environment.json``).

The fixture's inputs are written by hand and its answers by this module:
``uv run --no-sync python -m tests.guildbotics.test_environment_golden``
writes them again, after the Python version changed. It writes the codes
langcodes replaces too (``crates/gb-environment/src/language_aliases.json``),
which the Rust version declines to name rather than replace otherwise.
"""

from __future__ import annotations

import itertools
import json
import string
import subprocess
from pathlib import Path
from unittest import mock

import pytest
import yaml
from langcodes import Language
from langcodes.data_dicts import LANGUAGE_REPLACEMENTS

from guildbotics.environment import spec
from guildbotics.environment.toolchain import ToolchainError, parse_toolchain
from guildbotics.utils import os_language

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "records" / "golden" / "environment.json"
FIXTURE = json.loads(GOLDEN.read_text(encoding="utf-8"))
ALIASES = ROOT / "crates" / "gb-environment" / "src" / "language_aliases.json"


def aliases() -> dict[str, list[str]]:
    """The language and region codes langcodes reads as other ones, and the
    whole tags it replaces (``sgn-br``)."""

    def replaced(codes, tag, read):
        return sorted(c for c in codes if read(Language.get(tag(c))) != c)

    letters = string.ascii_lowercase
    languages = (
        "".join(t) for n in (2, 3) for t in itertools.product(letters, repeat=n)
    )
    regions = itertools.chain(
        ("".join(t) for t in itertools.product(string.ascii_uppercase, repeat=2)),
        (f"{n:03}" for n in range(1000)),
    )
    return {
        "languages": replaced(languages, str, str),
        "regions": replaced(regions, lambda r: f"en-{r}", lambda t: t.territory),
        "tags": sorted(tag for tag in LANGUAGE_REPLACEMENTS if "-" in tag),
    }


def language_of_tag(tag: str) -> str | None:
    """``LANGUAGE`` as the environment is told it, for the OS's ``tag``."""
    with mock.patch.object(
        spec, "os_ui_language", lambda: os_language.ui_language(tag)
    ):
        return spec._ui_language().get("LANGUAGE")


def macos_tag(stdout: str) -> str | None:
    """The tag macOS's ``defaults read -g AppleLanguages`` printing ``stdout`` names."""
    completed = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
    with mock.patch.object(os_language.subprocess, "run", return_value=completed):
        return os_language._macos_ui_language_tag()


def posix_tag(environ: dict[str, str]) -> str | None:
    """The tag the gettext variables ``environ`` name."""
    with mock.patch.dict(os_language.os.environ, environ, clear=True):
        return os_language._posix_ui_language_tag()


def resources(declaration: str) -> dict | None:
    """The resources the declaration boots with; None when it is refused."""
    try:
        declared = parse_toolchain(yaml.safe_load(declaration), where="test")
    except ToolchainError:
        return None
    return declared.resources.model_dump()


def test_the_alias_tables_are_langcodes_own():
    assert json.loads(ALIASES.read_text(encoding="utf-8")) == aliases()


@pytest.mark.parametrize("case", FIXTURE["tags"], ids=lambda c: repr(c["tag"]))
def test_a_tag_is_the_language_the_fixture_has(case):
    assert language_of_tag(case["tag"]) == case["expected"]


@pytest.mark.parametrize("case", FIXTURE["macos"], ids=lambda c: repr(c["stdout"]))
def test_macos_names_the_tag_the_fixture_has(case):
    assert macos_tag(case["stdout"]) == case["expected"]


@pytest.mark.parametrize("case", FIXTURE["posix"], ids=lambda c: repr(c["environ"]))
def test_the_variables_name_the_tag_the_fixture_has(case):
    assert posix_tag(case["environ"]) == case["expected"]


@pytest.mark.parametrize("case", FIXTURE["resources"], ids=lambda c: c["id"])
def test_a_declaration_boots_as_the_fixture_has_it(case):
    assert resources(case["declaration"]) == case["expected"]


def main() -> None:
    """Write the fixture's answers again from its inputs."""
    fixture = dict(FIXTURE)
    for case in fixture["tags"]:
        case["expected"] = language_of_tag(case["tag"])
    for case in fixture["macos"]:
        case["expected"] = macos_tag(case["stdout"])
    for case in fixture["posix"]:
        case["expected"] = posix_tag(case["environ"])
    for case in fixture["resources"]:
        case["expected"] = resources(case["declaration"])
    GOLDEN.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    ALIASES.write_text(json.dumps(aliases(), indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
