"""Exercise the suite's removal of a command's variables in a separate pytest
process, with the variables an agent's command environment has."""

import os
from pathlib import Path

from tests.test_network_isolation import _run

_VARIABLES = {
    "GUILDBOTICS_HOST_URL": "http://host.test:1",
    "GUILDBOTICS_CONFIG_DIR": "/verify/.guildbotics/config",
}


def test_only_an_opted_in_real_device_test_keeps_the_commands_variables(
    tmp_path: Path,
) -> None:
    """A test is kept off them; a real-device test opted in to checks the
    workspace it was pointed at, so it keeps them."""
    test_file = tmp_path / "test_command_variables.py"
    test_file.write_text(
        f"""
import os
import pytest

VARIABLES = {_VARIABLES!r}


def test_kept_off():
    assert not set(VARIABLES) & set(os.environ)


@pytest.mark.real_device("GUILDBOTICS_CONTRACT_PROBE")
def test_real_device():
    assert {{name: os.environ.get(name) for name in VARIABLES}} == VARIABLES
""",
        encoding="utf-8",
    )
    env = {**os.environ, **_VARIABLES, "GUILDBOTICS_CONTRACT_PROBE": "1"}
    env.pop("CI", None)

    result = _run(test_file, env=env)

    assert "2 passed" in result.stdout, result.stdout + result.stderr
