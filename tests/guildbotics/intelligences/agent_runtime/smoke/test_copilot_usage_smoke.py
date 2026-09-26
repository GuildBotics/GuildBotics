"""Optional smoke test of the GitHub Copilot account quota probe.

Skipped unless ``GUILDBOTICS_COPILOT_SMOKE=1``. The probe runs the Copilot CLI
pinned in this device's isolated environment with the login saved here,
exactly as the Desktop does (see ``conftest.py`` for what that takes). It
starts no turn and spends no quota, so it may run freely, but it never runs
in CI. Nothing it observes is written to a fixture.
"""

from __future__ import annotations

import os

import pytest

from guildbotics.intelligences.agent_runtime.usage import read_copilot_usage

TOOL = "copilot"

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
