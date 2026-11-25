"""Keep the retired Branvil names out of the repository and of the text people read."""

import json
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_policy

import yaml

ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT_NAME = re.compile(r"(?<![A-Z0-9_])BRANVIL_[A-Z0-9_]+")
RETIRED_DIRECTORY = ".branvil/"
# Names of stored secrets, which the data conversions read. They are not
# environment variables.
STORED_SECRETS = {"BRANVIL_ADMIN_TOKEN", "BRANVIL_TOKEN", "BRANVIL_TOKEN_ID"}
STORED_SECRET_FILES = {
    "crates/gb-server/src/hub_data.rs": STORED_SECRETS,
    "docs/hub-server.md": STORED_SECRETS,
}
GIT_TOKEN_FILES = {"docs/secret_migration.md", "records/golden/secret_migration.json"}
# The prefix of the notices the code host and its runners add to job logs.
RETIRED_LOG_PREFIX = "[branvil]"
# Measurements recorded with the logs of their time.
LOG_RECORD_FILES = {"crates/branvil/docs/performance.md"}
# The code host's former product name. Code keeps it; the docs, the screens and
# the messages call the code host by GuildBotics and its features.
PRODUCT_NAME = "Branvil"
OUTWARD_TEXT = re.compile(
    r"README(\.ja)?\.md|docs/.+\.md|desktop/README\.md|skills/.+\.md"
    r"|crates/gb-member/README\.md|guildbotics/templates/(member|commands)/.+\.md"
    r"|guildbotics/templates/locales/.+\.yml"
    r"|desktop/src/(repository/i18n/(en|ja)|setup/providers/[^/]+/i18n)\.ts"
)


def tracked_files() -> list[str]:
    output = subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "-z"])
    return [name for name in output.decode().split("\0") if name]


def retired_names(name: str, text: str) -> set[str]:
    found = set(ENVIRONMENT_NAME.findall(text)) - STORED_SECRET_FILES.get(name, set())
    if name in GIT_TOKEN_FILES:
        found = {item for item in found if not item.startswith("BRANVIL_GIT_TOKEN_")}
    if RETIRED_DIRECTORY in text:
        found.add(RETIRED_DIRECTORY)
    if RETIRED_LOG_PREFIX in text and name not in LOG_RECORD_FILES:
        found.add(RETIRED_LOG_PREFIX)
    return found


def strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def names_the_product(name: str, text: str) -> bool:
    if not OUTWARD_TEXT.fullmatch(name):
        return False
    if name.endswith(".yml"):
        # Locale keys name the code; their values are what people read.
        values = strings(yaml.safe_load(text))
        return any(PRODUCT_NAME.lower() in value.lower() for value in values)
    return PRODUCT_NAME in text


def test_a_retired_name_is_found() -> None:
    assert retired_names("a.sh", "echo $BRANVIL_SHA") == {"BRANVIL_SHA"}
    assert retired_names("a.md", "see `.branvil/ci.toml`") == {RETIRED_DIRECTORY}
    assert retired_names("a.rs", '"BRANVIL_TOKEN"') == {"BRANVIL_TOKEN"}
    assert retired_names("docs/secret_migration.md", "BRANVIL_DATA_DIR") == {
        "BRANVIL_DATA_DIR"
    }
    assert not retired_names("docs/hub-server.md", "`BRANVIL_TOKEN_ID`")
    assert retired_names("a.rs", 'format!("[branvil] {text}")') == {RETIRED_LOG_PREFIX}
    assert not retired_names("crates/branvil/docs/performance.md", "[branvil] sandbox")
    assert not retired_names("a.rs", "[guildbotics] sandbox started")
    assert not retired_names("a.rs", "ALICE_BRANVIL_TOKEN BRANVIL:KEEP /.branvil-log")


def test_no_tracked_file_uses_a_retired_name() -> None:
    this_file = Path(__file__).relative_to(ROOT).as_posix()
    found: dict[str, set[str]] = {}
    for name in tracked_files():
        if name.startswith(RETIRED_DIRECTORY):
            found[name] = {RETIRED_DIRECTORY}
        path = ROOT / name
        if name == this_file or path.is_symlink() or not path.is_file():
            continue
        try:
            names = retired_names(name, path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            continue
        if names:
            found[name] = names
    assert not found


def test_the_product_name_is_found_only_in_outward_text() -> None:
    assert names_the_product("README.md", "Run Branvil")
    assert names_the_product("docs/code_host.ja.md", "Branvil の画面")
    assert names_the_product(
        "desktop/src/repository/i18n/ja.ts", '"Branvil に届きません"'
    )
    assert names_the_product("desktop/src/setup/providers/slack/i18n.ts", "Branvil")
    locale = "guildbotics/templates/locales/integrations/branvil/api.en.yml"
    assert names_the_product(locale, "api:\n  hint: see docs/branvil_integration.md\n")
    assert not names_the_product(
        locale, "branvil:\n  api:\n    stopped: The code host stopped.\n"
    )
    assert not names_the_product("docs/code_host.en.md", "the crate `crates/branvil`")
    assert not names_the_product("AGENTS.md", "Branvil")
    assert not names_the_product("crates/branvil/docs/api.md", "Branvil")
    assert not names_the_product("desktop/src/App.tsx", "Branvil")


def test_no_outward_text_names_the_product() -> None:
    found = [
        name
        for name in tracked_files()
        if OUTWARD_TEXT.fullmatch(name)
        and names_the_product(name, (ROOT / name).read_text(encoding="utf-8"))
    ]
    assert not found


def test_the_ci_reads_the_current_configuration_file() -> None:
    assert ".guildbotics-ci.toml" in tracked_files()
    pipeline = (ROOT / "crates/branvil/src/ci/pipeline.rs").read_text("utf-8")
    assert 'pub const PIPELINE_PATH: &str = ".guildbotics-ci.toml";' in pipeline


def test_guest_entry_instructions_use_the_snapshot_interpreter():
    catalog = json.loads(
        (ROOT / "guildbotics/templates/member/catalog.json").read_text(encoding="utf-8")
    )
    interpreter = f"{catalog['guest_venv']}/bin/python"
    required = {
        "guildbotics/templates/member/reference.md",
        "guildbotics/templates/commands/functions/handle_ticket.en.md",
        "guildbotics/templates/commands/functions/handle_ticket.ja.md",
    }
    pattern = re.compile(r"([^\s`\"']+)[`\"']?\s+-m\s+guildbotics\.guest\.entry\b")
    documented = set()
    for name in tracked_files():
        if name.startswith("tests/") or Path(name).suffix not in {
            ".md",
            ".py",
            ".yaml",
            ".yml",
        }:
            continue
        paths = pattern.findall((ROOT / name).read_text(encoding="utf-8"))
        if paths:
            documented.add(name)
            assert all(path == interpreter for path in paths), (name, paths)
    assert required <= documented
