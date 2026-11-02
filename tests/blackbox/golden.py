"""Copy a production workspace for golden reads, without its keychain values.

    python -m tests.blackbox.golden <workspace_root> <identity_root> <destination>

The copy holds what the server reads, never the secrets in the OS keychain,
and stays out of the repository because it holds production content.
"""

from __future__ import annotations

import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

# Relative to each copied ``.guildbotics``. The keychain namespace of a store
# (``local/secrets.json``) would let any server holding the OS keychain read
# production's values; without it, the server starts an empty one. The rest is
# state no read serves, large or holding the owner's own files.
EXCLUDED = {
    "local/secrets.json",
    "data",
    "local/clones",
    "local/member_git",
    "local/agent_environment",
    "local/work",
    "local/run/command-inputs",
}


def copy(workspace_root: Path, identity_root: Path, destination: Path) -> None:
    for root, name in ((workspace_root, "workspace"), (identity_root, "identity")):
        source = root / ".guildbotics"

        def ignore(directory: str, names: list[str], source=source) -> set[str]:
            return {
                name
                for name in names
                if (Path(directory) / name).relative_to(source).as_posix() in EXCLUDED
            }

        # Links are copied as what they point to: the copy never reads production.
        shutil.copytree(source, destination / name / ".guildbotics", ignore=ignore)
    workspace = destination / "workspace/.guildbotics"
    # Services never start against the copy: no patrol, chat, or upkeep.
    _update(workspace / "local/service.yml", start_with_server=False)
    # Nothing expires: a server prunes transcripts older than the retention.
    _update(workspace / "config/transcripts.yml", retention_days=36500)
    # Branvil is unreachable whether or not a server honors the dead proxy.
    _update(
        destination / "identity/.guildbotics/config/connections/branvil.yml",
        base_url="http://127.0.0.1:1",
    )
    (destination / "copied_at").write_text(
        datetime.now(UTC).isoformat(timespec="seconds"), encoding="utf-8"
    )


def _update(path: Path, **values) -> None:
    try:
        current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        current = {}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(current | values), encoding="utf-8")


if __name__ == "__main__":
    copy(*(Path(argument).expanduser().resolve() for argument in sys.argv[1:4]))
