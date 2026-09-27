import os
import subprocess
from pathlib import Path

import git

from guildbotics.utils.git_tool import (
    build_git_auth_environment,
    create_git_askpass_script,
)


def test_auth_environment_appends_credential_reset_to_command_config(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.sslVerify")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")

    env = build_git_auth_environment(tmp_path / "askpass.sh", "member-token")

    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_1"] == "credential.helper"
    assert env["GIT_CONFIG_VALUE_1"] == ""


def test_member_auth_environment_resets_configured_credential_helpers(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    repo_path = tmp_path / "repo"
    helper_marker = tmp_path / "external-helper-ran"
    with git.Repo.init(repo_path) as repo, repo.config_writer() as writer:
        writer.set_value(
            "credential",
            "helper",
            f'!printf invoked > "{helper_marker.as_posix()}"',
        )

    askpass_path = create_git_askpass_script()
    try:
        env = os.environ.copy()
        env.update(build_git_auth_environment(askpass_path, "member-token"))
        result = subprocess.run(
            ["git", "credential", "fill"],
            cwd=repo_path,
            env=env,
            input="protocol=https\nhost=example.invalid\n\n",
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    finally:
        askpass_path.unlink(missing_ok=True)

    assert result.returncode == 0
    assert "username=x-access-token" in result.stdout
    assert "password=member-token" in result.stdout
    assert not helper_marker.exists()
