"""Process environment and termination policy shared by native adapters."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from guildbotics.capabilities.task_runs import RUN_ENV, TASK_RUN_ENV
from guildbotics.intelligences.agent_runtime.models import AgentExecutionContext
from guildbotics.intelligences.agent_runtime.windows_job import (
    WindowsJob,
    creation_flags,
    register_process_job,
    terminate_process_job,
)
from guildbotics.intelligences.cli_agents import get_cli_agent_search_path
from guildbotics.runtime.person_lease import (
    DELEGATION_ID_ENV,
    LEASE_ID_ENV,
    LEASE_PERSON_ENV,
    LEASE_RUN_ENV,
)
from guildbotics.utils.env_loader import GUILDBOTICS_ENV_FILE
from guildbotics.utils.fileio import GUILDBOTICS_DATA_DIR
from guildbotics.utils.processes import terminate_posix_process_group
from guildbotics.utils.workspace_state import GUILDBOTICS_CONFIG_DIR

CHAT_PARTICIPANT_LABELS_ENV = "GUILDBOTICS_CHAT_PARTICIPANT_LABELS"
_WINDOWS = os.name == "nt"

#: Ambient parent values an AI CLI process must never inherit: the direct
#: write credentials, and the execution metadata that authorises a nested
#: member CLI call. A live lease is a usable grant, so inheriting it lets the
#: provider process bypass the boundary its own transport was given. Paths that
#: legitimately carry that metadata re-inject it from the execution context
#: right after this builder runs.
_STRIPPED_PARENT_ENV = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GH_CONFIG_DIR",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "SSH_AUTH_SOCK",
    RUN_ENV,
    TASK_RUN_ENV,
    LEASE_ID_ENV,
    DELEGATION_ID_ENV,
    LEASE_PERSON_ENV,
    LEASE_RUN_ENV,
)

# asyncio's default 64 KiB StreamReader limit aborts readline() on single-line
# JSON payloads such as replayed tool results or aggregated command output.
STREAM_READ_LIMIT = 10 * 1024 * 1024


def isolated_agent_environment(cwd: Path) -> tuple[dict[str, str], str]:
    """Return an AI CLI environment with inherited credentials and grants removed."""
    env = os.environ.copy()
    env["PATH"] = get_cli_agent_search_path(env.get("PATH"))
    if not env.get(GUILDBOTICS_CONFIG_DIR, "").strip():
        config_dir = cwd / ".guildbotics" / "config"
        if config_dir.exists():
            env[GUILDBOTICS_CONFIG_DIR] = str(config_dir.resolve())
    if not env.get(GUILDBOTICS_ENV_FILE, "").strip():
        env_file = cwd / ".env"
        if env_file.is_file():
            env[GUILDBOTICS_ENV_FILE] = str(env_file.resolve())
    gh_config_dir = tempfile.mkdtemp(prefix="guildbotics-gh-config-")
    for key in _STRIPPED_PARENT_ENV:
        env.pop(key, None)
    env["GH_CONFIG_DIR"] = gh_config_dir
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_SSH_COMMAND"] = (
        "ssh -F /dev/null -o BatchMode=yes "
        "-o IdentitiesOnly=yes -o IdentityFile=/dev/null"
    )
    return env, gh_config_dir


def member_command_environment(context: AgentExecutionContext) -> dict[str, str]:
    """Build the minimal verified-execution metadata inherited by child member CLI."""
    run_key = RUN_ENV if context.conversation_key.work_kind == "chat" else TASK_RUN_ENV
    env = {
        GUILDBOTICS_DATA_DIR: str(context.workspace_data_root),
        run_key: context.run_id,
    }
    if context.participant_labels:
        env[CHAT_PARTICIPANT_LABELS_ENV] = context.participant_labels
    return env


async def terminate_process_tree(
    process: asyncio.subprocess.Process, *, grace_seconds: float = 2.0
) -> None:
    """Terminate the process group and reap the owned subprocess."""
    pid = getattr(process, "pid", None)
    if _WINDOWS:
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=grace_seconds)
                return
            except TimeoutError:
                pass
        terminated = terminate_process_job(process)
        if not terminated and process.returncode is None:
            raise RuntimeError("Agent subprocess has no Windows Job Object.")
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.shield(process.wait())
        return

    if process.returncode is not None:
        # The direct child may have exited while background descendants still
        # hold inherited pipes or continue working in its process group.
        if os.name == "posix" and pid:
            with suppress(ProcessLookupError):
                terminate_posix_process_group(pid)
        await process.wait()
        return
    if os.name == "posix" and pid:
        with suppress(ProcessLookupError):
            terminate_posix_process_group(pid)
    else:
        with suppress(ProcessLookupError):
            process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        return
    except TimeoutError:
        pass
    if os.name == "posix" and pid:
        with suppress(ProcessLookupError):
            terminate_posix_process_group(pid, force=True)
    else:
        with suppress(ProcessLookupError):
            process.kill()
    with suppress(asyncio.CancelledError, Exception):
        await asyncio.shield(process.wait())


async def create_agent_subprocess(
    *program: str,
    **kwargs: Any,
) -> asyncio.subprocess.Process:
    """Create an agent subprocess under the platform's process-tree policy."""
    if not _WINDOWS:
        return await asyncio.create_subprocess_exec(*program, **kwargs)

    job = WindowsJob.create()
    process: asyncio.subprocess.Process | None = None
    existing_flags = int(kwargs.pop("creationflags", 0))
    try:
        process = await asyncio.create_subprocess_exec(
            *program,
            creationflags=existing_flags | creation_flags(),
            **kwargs,
        )
        job.assign_and_resume(process.pid)
    except Exception:
        if process is not None:
            with suppress(Exception):
                job.terminate()
            with suppress(Exception):
                process.kill()
            with suppress(Exception):
                await process.wait()
        job.close()
        raise
    register_process_job(process, job)
    return process


def remove_isolated_config(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
