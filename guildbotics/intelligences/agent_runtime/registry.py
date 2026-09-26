"""The native adapters of the running command.

A command's turns speak to their provider through native adapters, and the
adapters belong to that command: they live in its environment and end with
it. A command never closes another's, so two commands of one member -- a
read-only one beside one that writes -- run side by side.
"""

from __future__ import annotations

from guildbotics.intelligences.agent_runtime.environment import running_command
from guildbotics.intelligences.agent_runtime.factory import create_native_adapter
from guildbotics.intelligences.agent_runtime.models import AgentAdapter


async def get_native_adapter(
    person_id: str, adapter_name: str, execution_id: str
) -> AgentAdapter:
    """Return the running command's adapter for one execution of a member.

    Within a command a member keeps one native process: an adapter for
    another execution of the same member replaces the one it had.

    Raises:
        AgentRuntimeError: ``configuration`` when no command is running.
    """
    command = running_command()
    async with command.adapters_lock:
        key = (person_id, f"{adapter_name}:{execution_id}")
        adapter = command.adapters.get(key)
        if adapter is None:
            stale = [
                existing
                for existing in command.adapters
                if existing[0] == person_id and existing != key
            ]
            for existing in stale:
                await command.adapters.pop(existing).close()
            adapter = create_native_adapter(adapter_name)
            command.adapters[key] = adapter
        return adapter
