"""Event-loop-local native adapter registry and shutdown hook."""

from __future__ import annotations

import asyncio
import weakref

from guildbotics.intelligences.agent_runtime.factory import create_native_adapter
from guildbotics.intelligences.agent_runtime.models import AgentAdapter

_registries: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[tuple[str, str], AgentAdapter]
] = weakref.WeakKeyDictionary()
_registry_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


async def get_native_adapter(
    person_id: str, adapter_name: str, execution_id: str
) -> AgentAdapter:
    loop = asyncio.get_running_loop()
    lock = _registry_locks.setdefault(loop, asyncio.Lock())
    async with lock:
        registry = _registries.setdefault(loop, {})
        key = (person_id, f"{adapter_name}:{execution_id}")
        adapter = registry.get(key)
        if adapter is None:
            stale = [
                existing
                for existing in registry
                if existing[0] == person_id and existing != key
            ]
            for existing in stale:
                await registry.pop(existing).close()
            adapter = create_native_adapter(adapter_name, person_id)
            registry[key] = adapter
        return adapter


async def close_native_adapters(person_id: str | None = None) -> None:
    loop = asyncio.get_running_loop()
    lock = _registry_locks.setdefault(loop, asyncio.Lock())
    async with lock:
        registry = _registries.get(loop, {})
        keys = [key for key in registry if person_id is None or key[0] == person_id]
        for key in keys:
            adapter = registry.pop(key)
            await adapter.close()
        if not registry:
            _registries.pop(loop, None)
            _registry_locks.pop(loop, None)
