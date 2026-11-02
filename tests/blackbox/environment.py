"""Lend a throwaway stack the device's ready runtime and snapshots, as desktop E2E does."""

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EnvironmentSource:
    workspace: Path
    home: Path

    def adopt(self, workspace: Path, home: Path):
        declaration = Path(".guildbotics/config/intelligences/agent_environment.yml")
        destination = workspace / declaration
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.workspace / declaration, destination)
        # No service upkeep or build is started. Linking the directory lets
        # the public status command choose the snapshot matching the declaration.
        snapshots = Path(".guildbotics/local/agent_environment/snapshots")
        (workspace / snapshots).parent.mkdir(parents=True, exist_ok=True)
        (workspace / snapshots).symlink_to(
            self.workspace / snapshots, target_is_directory=True
        )
        runtime = home / ".guildbotics/data/msb"
        runtime.unlink()  # Remove the ordinary stack's fail-closed tripwire.
        runtime.symlink_to(
            self.home / ".guildbotics/data/msb", target_is_directory=True
        )
