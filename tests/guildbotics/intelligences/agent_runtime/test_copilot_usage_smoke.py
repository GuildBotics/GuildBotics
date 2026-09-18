"""Optional smoke test of the GitHub Copilot account quota probe.

Skipped unless ``GUILDBOTICS_COPILOT_SMOKE=1``. Unlike the turn smoke test it
needs no Copilot CLI on the host: the probe runs the pinned CLI inside this
device's isolated environment with the login saved there, exactly as the
Desktop does. It starts no turn and spends no quota, so it may run freely, but
it needs a built environment and a saved login and therefore never runs in CI.
Nothing it observes is written to a fixture.

The suite keeps every other test off the device: the adapter tests boot a fake
environment, and the machine home and the workspace root are temporary
directories. This module overrides those fixtures, so the probe sees the real
runtime, the real provider store and the workspace the device has selected
(or the one ``GUILDBOTICS_CONFIG_DIR`` names), as the product does.
"""

from __future__ import annotations

import os

import pytest

from guildbotics.intelligences.agent_runtime.usage import read_copilot_usage
from guildbotics.utils.fileio import GUILDBOTICS_CONFIG_DIR, GUILDBOTICS_WORKSPACE_ROOT
from guildbotics.utils.workspace_state import (
    has_explicit_workspace_source,
    read_active_workspace,
)


@pytest.fixture
def fake_environment() -> None:
    """The real environment, not the adapter tests' fake."""


@pytest.fixture
def _isolate_machine_home() -> None:
    """The real device: its runtime, its provider store, its saved login."""


@pytest.fixture
def _isolate_workspace_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """The workspace the device has selected, applied as the CLI applies it,
    unless ``GUILDBOTICS_CONFIG_DIR`` or ``GUILDBOTICS_WORKSPACE_ROOT`` already
    names one. Its declaration and snapshot are what the probe runs on."""
    if has_explicit_workspace_source():
        return
    state = read_active_workspace()
    if state is None:
        pytest.skip("No GuildBotics workspace is selected on this device.")
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(state.workspace))
    monkeypatch.setenv(GUILDBOTICS_CONFIG_DIR, str(state.config_dir))


pytestmark = [
    pytest.mark.skipif(
        os.environ.get("GUILDBOTICS_COPILOT_SMOKE") != "1",
        reason="Set GUILDBOTICS_COPILOT_SMOKE=1 to run the real Copilot smoke test.",
    ),
    pytest.mark.asyncio,
]


async def test_real_copilot_usage_probe_reads_a_finite_budget() -> None:
    """`account.getQuota` on the SDK server answers with the saved login."""
    snapshot = await read_copilot_usage()

    print("windows:", [window.__dict__ for window in snapshot.windows])
    assert snapshot.agent == "copilot"
    # Every account has at least the premium-interaction budget; unlimited
    # chat / completions never become meters.
    assert snapshot.windows, "no finite quota window"
    for window in snapshot.windows:
        assert 0.0 <= window.used_percent <= 100.0, window
        assert window.label == window.window, window
