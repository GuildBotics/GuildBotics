"""Keep the retired Branvil environment names and CI directory out of the repository."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT_NAME = re.compile(r"(?<![A-Z0-9_])BRANVIL_[A-Z0-9_]+")
RETIRED_DIRECTORY = ".branvil/"
# Names of stored secrets, which the data conversions read. They are not
# environment variables.
STORED_SECRETS = {"BRANVIL_ADMIN_TOKEN", "BRANVIL_TOKEN", "BRANVIL_TOKEN_ID"}
STORED_SECRET_FILES = {
    "crates/gb-server/src/hub_data.rs": STORED_SECRETS,
    "crates/gb-server/tests/support/commands-frozen-map.md": STORED_SECRETS,
    "docs/hub-server.md": STORED_SECRETS,
}
GIT_TOKEN_FILES = {"docs/secret_migration.md", "records/golden/secret_migration.json"}


def tracked_files() -> list[str]:
    output = subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "-z"])
    return [name for name in output.decode().split("\0") if name]


def retired_names(name: str, text: str) -> set[str]:
    found = set(ENVIRONMENT_NAME.findall(text)) - STORED_SECRET_FILES.get(name, set())
    if name in GIT_TOKEN_FILES:
        found = {item for item in found if not item.startswith("BRANVIL_GIT_TOKEN_")}
    if RETIRED_DIRECTORY in text:
        found.add(RETIRED_DIRECTORY)
    return found


def test_a_retired_name_is_found() -> None:
    assert retired_names("a.sh", "echo $BRANVIL_SHA") == {"BRANVIL_SHA"}
    assert retired_names("a.md", "see `.branvil/ci.toml`") == {RETIRED_DIRECTORY}
    assert retired_names("a.rs", '"BRANVIL_TOKEN"') == {"BRANVIL_TOKEN"}
    assert retired_names("docs/secret_migration.md", "BRANVIL_DATA_DIR") == {
        "BRANVIL_DATA_DIR"
    }
    assert not retired_names("docs/hub-server.md", "`BRANVIL_TOKEN_ID`")
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


def test_the_ci_reads_the_current_configuration_file() -> None:
    assert ".guildbotics-ci.toml" in tracked_files()
    pipeline = (ROOT / "crates/branvil/src/ci/pipeline.rs").read_text("utf-8")
    assert 'pub const PIPELINE_PATH: &str = ".guildbotics-ci.toml";' in pipeline
