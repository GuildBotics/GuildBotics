"""``guildbotics git-credential``: git's credential helper protocol, as git
speaks it to the CLI."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from guildbotics.cli import git_credential as cli
from guildbotics.cli import main
from guildbotics.runtime.code_hosting_service import GitCredential
from guildbotics.integrations.branvil import owner_git
from guildbotics.runtime.port import ServiceError
from guildbotics.utils.i18n_tool import t

_ORIGIN = "http://127.0.0.1:7878"
_CREDENTIAL = GitCredential(username="otota", password="bvt_machine")


@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The origins the providers are asked for, which hand ``_CREDENTIAL``
    for ``_ORIGIN`` only."""
    origins: list[str] = []

    async def credential(origin: str) -> GitCredential | None:
        origins.append(origin)
        return _CREDENTIAL if origin == _ORIGIN else None

    monkeypatch.setattr(cli, "human_git_credential", credential)
    return origins


def _helper(action: str, request: str):
    return CliRunner().invoke(main, ["git-credential", action], input=request)


def test_get_hands_the_credential_for_the_remote_s_origin(asked: list[str]):
    result = _helper(
        "get",
        "capability[]=authtype\n"
        "capability[]=state\n"
        "protocol=http\n"
        "host=127.0.0.1:7878\n"
        "path=git/acme/demo.git\n"
        "wwwauth[]=Basic realm=branvil\n"
        "\n",
    )

    assert result.exit_code == 0
    assert result.stdout_bytes == b"username=otota\npassword=bvt_machine\n"
    assert asked == [_ORIGIN]


def test_a_request_that_ends_without_a_blank_line_is_read(asked: list[str]):
    result = _helper("get", "protocol=http\nhost=127.0.0.1:7878")

    assert result.stdout_bytes == b"username=otota\npassword=bvt_machine\n"


def test_a_remote_no_provider_serves_gets_no_answer(asked: list[str]):
    result = _helper("get", "protocol=https\nhost=github.com\n\n")

    assert result.exit_code == 0
    assert result.stdout_bytes == b""
    assert result.stderr == (
        t("cli.git_credential.not_served", origin="https://github.com") + "\n"
    )
    assert asked == ["https://github.com"]


@pytest.mark.parametrize("action", ["store", "erase", "capabilities"])
def test_actions_other_than_get_keep_and_ask_nothing(asked: list[str], action: str):
    result = _helper(
        action, "protocol=http\nhost=127.0.0.1:7878\nusername=otota\npassword=x\n\n"
    )

    assert result.exit_code == 0
    assert result.stdout_bytes == b""
    assert asked == []


def test_a_failure_is_said_and_ends_git_s_search(monkeypatch: pytest.MonkeyPatch):
    async def failing(origin: str) -> GitCredential | None:
        raise ServiceError("Branvil is not reachable.")

    monkeypatch.setattr(cli, "human_git_credential", failing)

    result = _helper("get", "protocol=http\nhost=127.0.0.1:7878\n\n")

    assert result.exit_code == 0
    assert result.stdout_bytes == b"quit=1\n"
    assert result.stderr == "Branvil is not reachable.\n"


_HELPER = """\
import sys

from guildbotics.cli.git_credential import answer
from guildbotics.runtime.code_hosting_service import GitCredential
from guildbotics.integrations.branvil import owner_git
from guildbotics.runtime.port import ServiceError
from guildbotics.utils.i18n_tool import t


async def credential(origin):
    if sys.argv[1] == "fail":
        raise ServiceError("unavailable")
    return GitCredential(username="otota", password=origin)


# git appends the action to the command.
answer(sys.argv[2], sys.stdin, sys.stdout.buffer, credential)
"""


@pytest.mark.windows
@pytest.mark.parametrize("outcome", ["hand", "fail"])
def test_git_asks_only_the_helper_for_the_remote_and_reads_its_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
):
    """The configuration Setup shows, with a stand-in for the CLI: git asks
    no helper configured before it for the remote, reads its answer, and asks
    none after it once it fails."""
    script = tmp_path / "helper.py"
    script.write_text(_HELPER, encoding="utf-8")
    monkeypatch.setattr(
        owner_git,
        "managed_cli_command",
        lambda *args: shlex.join(
            [Path(sys.executable).as_posix(), script.as_posix(), outcome]
        ),
    )
    before, after = tmp_path / "asked-before", tmp_path / "asked-after"
    config = tmp_path / "gitconfig"
    config.write_text(
        f"[credential]\n\thelper = \"!printf x > '{before.as_posix()}'\"\n"
        + owner_git.git_config(_ORIGIN)
        + f"\thelper = \"!printf x > '{after.as_posix()}'\"\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["git", "credential", "fill"],
        cwd=tmp_path,
        env={
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(config),
            "GIT_TERMINAL_PROMPT": "0",
        },
        input="protocol=http\nhost=127.0.0.1:7878\n\n",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert not before.exists()
    assert not after.exists(), (result.stdout, result.stderr)
    if outcome == "hand":
        assert result.returncode == 0, result.stderr
        assert f"username=otota\npassword={_ORIGIN}\n" in result.stdout
    else:
        assert result.returncode != 0
        assert "unavailable" in result.stderr
