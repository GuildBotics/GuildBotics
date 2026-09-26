"""The device the smoke tests in this directory run on.

The suite keeps every other test off the device: the adapter tests boot a fake
environment and a member broker without a socket, the keychain is in memory,
the host's time zone and UI language are fixed, and the machine home and the
workspace root are temporary directories. These fixtures undo all of that, so
a smoke test runs the tool the way the product does: the CLI pinned in this
device's snapshot, inside a real microVM, with the login saved here, for the
workspace the device has selected (or the one ``GUILDBOTICS_CONFIG_DIR``
names). No CLI on the host is needed.

Each module names the tool it drives as ``TOOL`` and skips itself unless its
own flag is set; where the environment or that tool's login is not ready,
the test is skipped with the reason the device gives. Run them without
``pytest-xdist`` (``-p no:xdist``): tests in parallel workers would each boot a
microVM and be lent the same saved login at once.
"""

from __future__ import annotations

import pytest

from guildbotics.intelligences.agent_environment.status import device_status
from guildbotics.utils.fileio import GUILDBOTICS_CONFIG_DIR, GUILDBOTICS_WORKSPACE_ROOT
from guildbotics.utils.workspace_state import (
    has_explicit_workspace_source,
    read_active_workspace,
)


@pytest.fixture
def fake_environment() -> None:
    """The real environment, not the adapter tests' fake."""


@pytest.fixture
def _adapter_member_broker_without_socket() -> None:
    """The real member broker, which the tool inside reaches from the guest."""


@pytest.fixture
def fake_keyring() -> None:
    """The real keychain, which holds the key the saved logins are sealed with."""


@pytest.fixture
def host_facts() -> None:
    """The host's own time zone and UI language, as every environment is told."""


@pytest.fixture
def _isolate_machine_home() -> None:
    """The real device: its runtime, its provider store, its saved logins."""


@pytest.fixture
def _isolate_workspace_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """The workspace the device has selected, applied as the CLI applies it,
    unless ``GUILDBOTICS_CONFIG_DIR`` or ``GUILDBOTICS_WORKSPACE_ROOT`` already
    names one. Its declaration and snapshot are what the tool runs on."""
    if has_explicit_workspace_source():
        return
    state = read_active_workspace()
    if state is None:
        pytest.skip("No GuildBotics workspace is selected on this device.")
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(state.workspace))
    monkeypatch.setenv(GUILDBOTICS_CONFIG_DIR, str(state.config_dir))


@pytest.fixture(autouse=True)
def _tool_ready(request: pytest.FixtureRequest, _isolate_workspace_data: None) -> None:
    """Skip, saying why, when the module's tool cannot run on this device."""
    refusal = device_status().turn_refusal(request.module.TOOL)
    if refusal:
        pytest.skip(refusal)
