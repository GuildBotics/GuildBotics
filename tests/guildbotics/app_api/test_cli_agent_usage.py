import asyncio
from types import SimpleNamespace

import pytest

from guildbotics.app_api import cli_agent_usage as module
from guildbotics.app_api.cli_agent_usage import CliAgentUsageCache
from guildbotics.app_api.errors import AppApiError
from guildbotics.intelligences.agent_runtime import usage

pytestmark = pytest.mark.asyncio


class Readers:
    """Probes the test releases one by one, counting how many run at once."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.gates = {name: asyncio.Event() for name in usage.CLI_AGENT_USAGE_READERS}
        self.calls: list[str] = []
        self.running = 0
        self.peak = 0
        self.cancelled: list[str] = []
        self.fail: set[str] = set()
        self.percent = 10.0
        for name in usage.CLI_AGENT_USAGE_READERS:
            monkeypatch.setitem(
                usage.CLI_AGENT_USAGE_READERS, name, lambda name=name: self.read(name)
            )
        monkeypatch.setattr(module, "has_credentials", lambda _: True)

    async def read(self, name: str) -> usage.CliAgentUsageSnapshot:
        self.calls.append(name)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await self.gates[name].wait()
        except asyncio.CancelledError:
            self.cancelled.append(name)
            raise
        finally:
            self.running -= 1
        self.gates[name].clear()
        if name in self.fail:
            raise usage.CliAgentUsageError("offline")
        return usage.CliAgentUsageSnapshot(
            agent=name,
            windows=[usage.CliAgentUsageWindow("primary", self.percent)],
            checked_at=f"{self.percent}",
        )


@pytest.fixture
def readers(monkeypatch):
    return Readers(monkeypatch)


@pytest.fixture
def cache():
    return CliAgentUsageCache(lambda: "trace-1")


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_a_slow_tool_does_not_hold_back_another(readers, cache):
    slow = asyncio.create_task(cache.read("codex"))
    fast = asyncio.create_task(cache.read("claude"))
    await settle()
    readers.gates["claude"].set()

    response = await fast

    assert not slow.done()
    assert response.usage is not None
    assert response.usage.windows[0].used_percent == 10.0
    assert response.check is not None and response.check.status == "succeeded"
    assert response.check.trace_id == "trace-1"
    assert not response.refreshing
    readers.gates["codex"].set()
    await slow


async def test_concurrent_reads_of_one_tool_share_a_probe(readers, cache):
    reads = [asyncio.create_task(cache.read("codex")) for _ in range(3)]
    await settle()
    readers.gates["codex"].set()

    responses = await asyncio.gather(*reads)

    assert readers.calls == ["codex"]
    assert all(response == responses[0] for response in responses)
    assert (await cache.read("codex")) == responses[0]
    assert readers.calls == ["codex"]


async def test_a_reader_that_leaves_does_not_cancel_the_shared_probe(readers, cache):
    leaving = asyncio.create_task(cache.read("codex"))
    staying = asyncio.create_task(cache.read("codex"))
    await settle()
    leaving.cancel()
    await settle()
    readers.gates["codex"].set()

    response = await staying

    assert readers.cancelled == []
    assert response.usage is not None


async def test_a_due_read_answers_with_the_last_reading_while_refreshing(
    readers, cache, monkeypatch
):
    readers.gates["codex"].set()
    await cache.read("codex")
    monkeypatch.setattr(module, "USAGE_TTL_SECONDS", 0.0)
    readers.percent = 55.0

    stale = await cache.read("codex")

    assert stale.refreshing
    assert stale.usage is not None and stale.usage.windows[0].used_percent == 10.0
    readers.gates["codex"].set()
    await settle()
    monkeypatch.setattr(module, "USAGE_TTL_SECONDS", 300.0)
    fresh = await cache.read("codex")
    assert not fresh.refreshing
    assert fresh.usage is not None and fresh.usage.windows[0].used_percent == 55.0


async def test_a_failed_refresh_keeps_the_previous_reading_apart(readers, cache):
    readers.gates["codex"].set()
    first = await cache.read("codex")
    readers.fail.add("codex")
    readers.gates["codex"].set()

    failed = await cache.read("codex", refresh=True)

    assert failed.usage == first.usage
    assert failed.check is not None and failed.check.status == "failed"
    assert cache.checks()["codex"].status == "failed"


async def test_a_failure_is_retried_sooner_than_a_success(readers, cache, monkeypatch):
    now = 1000.0
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now))
    readers.gates["codex"].set()
    await cache.read("codex")
    readers.fail.add("codex")
    readers.gates["codex"].set()
    await cache.read("codex", refresh=True)

    now += module.USAGE_RETRY_SECONDS
    retrying = await cache.read("codex")

    assert retrying.refreshing
    await settle()
    assert readers.calls == ["codex", "codex", "codex"]
    readers.fail.clear()
    readers.gates["codex"].set()
    await settle()
    assert cache.checks()["codex"].status == "succeeded"
    now += module.USAGE_RETRY_SECONDS
    await cache.read("codex")
    assert readers.calls == ["codex", "codex", "codex"]


async def test_a_failed_first_probe_shows_no_reading(readers, cache):
    readers.fail.add("codex")
    readers.gates["codex"].set()

    response = await cache.read("codex")

    assert response.usage is None
    assert response.check is not None and response.check.status == "failed"


async def test_probes_run_at_most_the_concurrency_limit(readers, cache):
    reads = [
        asyncio.create_task(cache.read(name)) for name in usage.CLI_AGENT_USAGE_READERS
    ]
    await settle()

    assert readers.running == module.MAX_CONCURRENT_PROBES
    for gate in readers.gates.values():
        gate.set()
    await asyncio.gather(*reads)
    assert readers.peak == module.MAX_CONCURRENT_PROBES
    assert sorted(readers.calls) == sorted(usage.CLI_AGENT_USAGE_READERS)


async def test_close_cancels_running_probes(readers, cache):
    read = asyncio.create_task(cache.read("codex"))
    await settle()

    await cache.aclose()

    assert readers.cancelled == ["codex"]
    with pytest.raises(asyncio.CancelledError):
        await read


async def test_tools_without_usage_or_login(readers, cache, monkeypatch):
    with pytest.raises(AppApiError):
        await cache.read("unknown")
    readers.gates["codex"].set()
    await cache.read("codex")
    monkeypatch.setattr(module, "has_credentials", lambda _: False)

    response = await cache.read("codex")

    assert response.usage is None and response.check is None
    assert cache.checks() == {}
    assert readers.calls == ["codex"]


async def test_a_logout_while_probing_does_not_start_a_second_probe(
    readers, cache, monkeypatch
):
    read = asyncio.create_task(cache.read("codex"))
    await settle()
    monkeypatch.setattr(module, "has_credentials", lambda _: False)

    logged_out = await cache.read("codex")

    assert logged_out.usage is None and logged_out.check is None
    monkeypatch.setattr(module, "has_credentials", lambda _: True)
    back = asyncio.create_task(cache.read("codex"))
    await settle()

    assert readers.calls == ["codex"]
    assert back.done()
    assert back.result().refreshing and back.result().usage is None
    read.cancel()


async def test_shutdown_releases_a_probe_whose_tool_logged_out(
    readers, cache, monkeypatch
):
    read = asyncio.create_task(cache.read("codex"))
    await settle()
    monkeypatch.setattr(module, "has_credentials", lambda _: False)
    await cache.read("codex")

    await cache.aclose()

    assert readers.cancelled == ["codex"]
    with pytest.raises(asyncio.CancelledError):
        await read


async def test_a_reading_taken_before_a_logout_is_not_published(
    readers, cache, monkeypatch
):
    read = asyncio.create_task(cache.read("codex"))
    await settle()
    monkeypatch.setattr(module, "has_credentials", lambda _: False)
    await cache.read("codex")
    readers.gates["codex"].set()
    await read

    assert cache.checks() == {}

    monkeypatch.setattr(module, "has_credentials", lambda _: True)
    readers.percent = 55.0
    fresh = asyncio.create_task(cache.read("codex"))
    await settle()
    assert readers.calls == ["codex", "codex"]
    readers.gates["codex"].set()

    response = await fresh

    assert response.usage is not None
    assert response.usage.windows[0].used_percent == 55.0
