"""How long a test may run before it fails instead of stopping the suite.

Every test has the default ``timeout`` of the pytest settings in
``pyproject.toml``; the tests here name what they take beyond it.
"""

import pytest

#: Tests on this device's real agent environment boot a microVM and wait on
#: provider CLIs and AI turns. They opt out of the ordinary network guard.
REAL_DEVICE_OPT_INS = frozenset(
    {
        "GUILDBOTICS_CONTRACT_PROBE",
        "GUILDBOTICS_COPILOT_SMOKE",
        "GUILDBOTICS_GROK_SMOKE",
        "GUILDBOTICS_ANTIGRAVITY_SMOKE",
    }
)


def REAL_DEVICE(opt_in: str):
    if opt_in not in REAL_DEVICE_OPT_INS:
        raise ValueError(f"Unknown real-device opt-in: {opt_in}")
    return (pytest.mark.timeout(30 * 60), pytest.mark.real_device(opt_in))
