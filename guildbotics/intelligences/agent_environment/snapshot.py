"""The snapshot every turn boots from: built from the declaration, named after it.

A device holds one snapshot per workspace, under the workspace's device-local
directory. Its name is a digest of everything that went into it -- the base
image, the provider CLIs at the versions GuildBotics pins, the declared
packages, and the version of this recipe -- so a snapshot built from an
older declaration, or by an older GuildBotics, is recognised by its name
alone and rebuilt. What the name cannot tell is the content of an entry the
declaration did not pin; that is why the declaration asks for pins.

The build is not interactive: it installs packages and nothing more, so the
CLI, the Desktop, and the background service all run the same one. Logging
in to a provider is a separate, interactive step (:mod:`.provider_state`)
whose result lives outside the snapshot.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import threading
from collections.abc import Callable
from dataclasses import dataclass
from logging import Logger
from pathlib import Path
from typing import Literal

from guildbotics.intelligences.agent_environment import runtime
from guildbotics.intelligences.agent_environment.runtime import (
    AgentEnvironmentError,
    BuildStep,
)
from guildbotics.intelligences.agent_environment.spec import guest_home
from guildbotics.intelligences.agent_environment.toolchain import (
    ToolchainDeclaration,
    ToolchainError,
    load_toolchain,
    upstream_nameservers,
)
from guildbotics.intelligences.cli_agents import CLI_AGENTS
from guildbotics.utils.advisory_lock import (
    LockTimeoutError,
    held_lock,
    lock_file_nonblocking,
    open_lock_file,
    unlock_file,
)
from guildbotics.utils.fileio import get_workspace_local_path
from guildbotics.utils.i18n_tool import t

#: The base image: Debian with Node.js, npm, and git, the tools the provider
#: CLIs are installed and run with. Pinned to an exact tag so two devices
#: building the same declaration get the same environment.
IMAGE = "node:22.23.2-bookworm"
#: uv, for the Python tools a declaration asks for; installed from its
#: release archive because the image has no Python of its own.
UV_VERSION = "0.12.10"
#: Bumped when the build steps change in a way the data above does not show.
RECIPE_VERSION = 1

SNAPSHOT_PREFIX = "guildbotics-"
_LOCK_FILE = "build.lock"
_FAILED_SUFFIX = ".failed"
#: How often the background service compares the snapshot to the declaration.
UPKEEP_INTERVAL_SECONDS = 30.0
#: A build that has not finished by then has stalled -- on a pull, a fetch
#: -- and is failed rather than left holding the lock for good.
BUILD_TIMEOUT_SECONDS = 30 * 60.0

SnapshotState = Literal["missing", "stale", "building", "failed", "ready"]


@dataclass(frozen=True, slots=True)
class SnapshotStatus:
    """Whether the snapshot the declaration asks for exists on this device.

    ``ready``: it does. ``stale``: only one built from another declaration
    does. ``building``: a build holds the lock. ``failed``: the last build of
    this very declaration failed, with ``detail`` saying how; nothing
    rebuilds it until the declaration changes or someone builds by hand.
    ``missing``: there is nothing.
    """

    state: SnapshotState
    name: str
    path: Path
    detail: str = ""


def snapshots_dir(workspace_root: Path | None = None) -> Path:
    """Where this device keeps the workspace's snapshots."""
    return get_workspace_local_path(
        "agent_environment", "snapshots", workspace_root=workspace_root
    )


def provisioned_packages() -> dict[str, str]:
    """The provider CLIs the snapshot installs, by tool name."""
    return {
        agent.name: agent.provision.package
        for agent in CLI_AGENTS
        if agent.provision.package
    }


def snapshot_name(declaration: ToolchainDeclaration) -> str:
    """The name of the snapshot ``declaration`` asks for on any device."""
    recipe = {
        "recipe": RECIPE_VERSION,
        "image": IMAGE,
        "uv": UV_VERSION,
        "providers": provisioned_packages(),
        "packages": declaration.packages.model_dump(),
    }
    encoded = json.dumps(recipe, sort_keys=True, separators=(",", ":")).encode()
    return SNAPSHOT_PREFIX + hashlib.sha256(encoded).hexdigest()[:16]


def build_steps(declaration: ToolchainDeclaration) -> tuple[BuildStep, ...]:
    """The scripts that turn the base image into the declared environment."""
    packages = declaration.packages
    steps = [BuildStep("home", 'install -d -m 0700 "$HOME"')]
    if packages.apt:
        steps.append(
            BuildStep(
                "apt",
                "apt-get update\n"
                f"apt-get install -y --no-install-recommends {_args(packages.apt)}\n"
                "apt-get clean\n"
                "rm -rf /var/lib/apt/lists/*",
            )
        )
    archive = "uv-${arch}-unknown-linux-gnu"
    steps.append(
        BuildStep(
            "uv",
            'arch="$(uname -m)"\n'
            "curl -fsSL "
            f'"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/{archive}.tar.gz"'
            " | tar -xz -C /usr/local/bin --strip-components=1"
            f' "{archive}/uv" "{archive}/uvx"\n'
            "uv --version",
        )
    )
    steps.append(
        BuildStep(
            "npm",
            f"npm install -g {_args([*provisioned_packages().values(), *packages.npm])}\n"
            "npm cache clean --force",
        )
    )
    if packages.uv:
        steps.append(
            BuildStep(
                "uv-tools",
                "\n".join(
                    f"UV_TOOL_BIN_DIR=/usr/local/bin uv tool install {shlex.quote(p)}"
                    for p in packages.uv
                ),
            )
        )
    return tuple(steps)


def _args(specs: list[str]) -> str:
    return " ".join(shlex.quote(spec) for spec in specs)


def snapshot_status(
    declaration: ToolchainDeclaration, workspace_root: Path | None = None
) -> SnapshotStatus:
    """Compare what the declaration asks for with what this device holds."""
    name = snapshot_name(declaration)
    directory = snapshots_dir(workspace_root)
    path = directory / name
    if _building(directory):
        return SnapshotStatus("building", name, path)
    failed = directory / (name + _FAILED_SUFFIX)
    if failed.is_file():
        return SnapshotStatus("failed", name, path, failed.read_text().strip())
    if path.is_dir():
        return SnapshotStatus("ready", name, path)
    if _snapshots_in(directory):
        return SnapshotStatus("stale", name, path)
    return SnapshotStatus("missing", name, path)


def _building(directory: Path) -> bool:
    """Whether a build holds the directory's lock, in this process or another."""
    lock = directory / _LOCK_FILE
    if not lock.exists():
        return False
    handle = open_lock_file(lock)
    try:
        try:
            lock_file_nonblocking(handle)
        except BlockingIOError:
            return True
        unlock_file(handle)
        return False
    finally:
        handle.close()


def _snapshots_in(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob(f"{SNAPSHOT_PREFIX}*") if p.is_dir())


async def build_snapshot(
    declaration: ToolchainDeclaration,
    *,
    on_line: Callable[[str], None],
    workspace_root: Path | None = None,
    home: Path | None = None,
) -> SnapshotStatus:
    """Build the snapshot the declaration asks for and drop the ones it does not.

    Raises:
        AgentEnvironmentError: When another build of this workspace holds
            the lock, or the build itself fails. A failed build leaves its
            reason beside the snapshots, where :func:`snapshot_status`
            reports it until the declaration changes.
        ToolchainError: When the declaration's resolvers cannot be read
            on this device.
    """
    nameservers = upstream_nameservers(declaration.dns)
    name = snapshot_name(declaration)
    directory = snapshots_dir(workspace_root)
    directory.mkdir(parents=True, exist_ok=True)
    failed = directory / (name + _FAILED_SUFFIX)
    try:
        with held_lock(directory / _LOCK_FILE, timeout=0):
            failed.unlink(missing_ok=True)
            try:
                path = await asyncio.wait_for(
                    runtime.build_snapshot(
                        name,
                        dest_dir=directory,
                        image=IMAGE,
                        home=guest_home(home),
                        steps=build_steps(declaration),
                        nameservers=nameservers,
                        on_line=on_line,
                    ),
                    BUILD_TIMEOUT_SECONDS,
                )
            except (AgentEnvironmentError, TimeoutError) as exc:
                reason = (
                    str(exc)
                    if isinstance(exc, AgentEnvironmentError)
                    else t(
                        "intelligences.agent_environment.snapshot.build_timeout",
                        minutes=int(BUILD_TIMEOUT_SECONDS // 60),
                    )
                )
                failed.write_text(f"{reason}\n")
                raise AgentEnvironmentError(reason) from exc
            for other in _snapshots_in(directory):
                if other.name != name:
                    await runtime.remove_snapshot(other)
            for marker in directory.glob(f"{SNAPSHOT_PREFIX}*{_FAILED_SUFFIX}"):
                marker.unlink()
            return SnapshotStatus("ready", name, path)
    except LockTimeoutError as exc:
        raise AgentEnvironmentError(
            t("intelligences.agent_environment.snapshot.build_running")
        ) from exc


async def remove_snapshots(workspace_root: Path | None = None) -> list[str]:
    """Delete every snapshot of the workspace on this device; returns their names."""
    directory = snapshots_dir(workspace_root)
    removed = []
    for path in _snapshots_in(directory):
        await runtime.remove_snapshot(path)
        removed.append(path.name)
    for marker in directory.glob(f"{SNAPSHOT_PREFIX}*{_FAILED_SUFFIX}"):
        marker.unlink()
    return removed


class SnapshotUpkeep(threading.Thread):
    """Keep this device's snapshot matching the declaration while the service runs.

    A declaration edited here, or arriving from another device through
    synchronization, is rebuilt without anyone asking, so the next turn boots
    from it. A build already running elsewhere, and one that failed for this
    same declaration, are left alone: the failure stays on the device's
    status until the declaration changes or someone builds by hand. The
    thread is a daemon and is never joined, so a build in progress does not
    hold up the service's shutdown; the runtime discards the half-built
    sandbox when the process ends.
    """

    def __init__(
        self,
        stop: threading.Event,
        log: Logger,
        *,
        interval: float = UPKEEP_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(name="agent-environment-upkeep", daemon=True)
        self._stop = stop
        self._log = log
        self._interval = interval
        self._reported = ""

    def run(self) -> None:
        while not self._stop.is_set():
            self.once()
            self._stop.wait(self._interval)

    def once(self) -> None:
        """Build the snapshot if the declaration asks for one this device lacks."""
        health = runtime.doctor()
        if not health.available:
            self._report(f"The agent environment cannot be built here: {health.reason}")
            return
        try:
            declaration = load_toolchain()
        except (ToolchainError, OSError) as exc:
            self._report(f"The agent environment declaration cannot be read: {exc}")
            return
        status = snapshot_status(declaration)
        if status.state not in ("missing", "stale"):
            return
        self._log.info("Building the agent environment %s...", status.name)
        try:
            asyncio.run(build_snapshot(declaration, on_line=self._log.info))
        except AgentEnvironmentError as exc:
            self._log.error("The agent environment build failed: %s", exc)
        else:
            self._log.info("The agent environment %s is ready.", status.name)

    def _report(self, message: str) -> None:
        """Warn once per distinct reason, not once per interval."""
        if message != self._reported:
            self._reported = message
            self._log.warning(message)
