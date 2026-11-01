"""Tests for the installer of the managed CLI and the agent skill."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import install_cli_assets as installer
import pytest

SKILL = "# GuildBotics\nnew\n"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for agent in installer.CLI_AGENTS:
        if agent.provision.state_root_env:
            monkeypatch.delenv(agent.provision.state_root_env, raising=False)
    return tmp_path


def _metadata(skill_dir: Path) -> dict[str, str]:
    return json.loads((skill_dir / installer.MANAGED_SKILL_METADATA).read_text())


def test_the_content_hash_is_fnv_1a_64() -> None:
    # The format of the copies installed so far, which stay recognisable.
    assert installer.content_hash(b"") == "cbf29ce484222325"
    assert installer.content_hash(b"a") == "af63dc4c8601ec8c"


@pytest.mark.usefixtures("shell_commands")
def test_the_managed_cli_runs_the_checkout_cli_with_its_arguments(home: Path) -> None:
    cli = home / "checkout dir" / "guildbotics"
    cli.parent.mkdir()
    cli.write_text('#!/bin/sh\nprintf "%s|" "$@"\n')
    cli.chmod(0o755)

    installer.install_cli(home, cli)

    for path in (
        home / ".guildbotics/bin/guildbotics",
        home / ".local/bin/guildbotics",
    ):
        result = subprocess.run(
            [str(path), "a b", "c"], capture_output=True, text=True, check=True
        )
        assert result.stdout == "a b|c|"


def test_a_link_left_at_bin_becomes_a_directory_of_its_own(home: Path) -> None:
    programs = home / ".guildbotics/programs/build"
    programs.mkdir(parents=True)
    (programs / "guildbotics").write_text("program")
    (home / ".guildbotics/bin").symlink_to(programs)

    installer.install_cli(home, home / "cli")

    assert not (home / ".guildbotics/bin").is_symlink()
    assert "exec " in (home / ".guildbotics/bin/guildbotics").read_text()
    assert (programs / "guildbotics").read_text() == "program"


def test_a_cli_of_the_users_own_on_path_is_left_alone(home: Path) -> None:
    own = home / ".local/bin/guildbotics"
    own.parent.mkdir(parents=True)
    own.write_text("#!/bin/sh\nexec mine\n")

    installer.install_cli(home, home / "cli")

    assert own.read_text() == "#!/bin/sh\nexec mine\n"


def test_a_shim_an_earlier_installer_wrote_on_path_is_replaced(home: Path) -> None:
    earlier = home / ".local/bin/guildbotics"
    earlier.parent.mkdir(parents=True)
    earlier.write_text("#!/bin/sh\n# Managed by GuildBotics desktop.\nexec old\n")

    installer.install_cli(home, home / "cli")

    assert str(home / ".guildbotics/bin/guildbotics") in earlier.read_text()


def test_every_ai_cli_of_the_catalog_says_where_it_reads_user_skills() -> None:
    assert all(agent.provision.user_skills for agent in installer.CLI_AGENTS)


def test_the_skill_goes_to_each_installed_ai_cli(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (home / ".claude").mkdir()
    (home / ".codex").mkdir()
    (home / ".gemini").mkdir()
    copilot = home / "elsewhere"
    copilot.mkdir()
    # Moves the skills with the state root, as the tool itself does.
    monkeypatch.setenv("COPILOT_HOME", str(copilot))

    report = installer.install_skills(home, SKILL)

    for skill_dir in (
        home / ".claude/skills/guildbotics",
        # Codex reads user skills from a shared location.
        home / ".agents/skills/guildbotics",
        home / ".gemini/config/skills/guildbotics",
        copilot / "skills/guildbotics",
    ):
        assert (skill_dir / "SKILL.md").read_text() == SKILL
        assert _metadata(skill_dir)["content_hash"] == installer.content_hash(
            SKILL.encode()
        )
    assert not (home / ".grok").exists()
    assert "grok: not installed, skipped" in report


def test_an_unedited_copy_is_updated_and_an_edited_one_kept(home: Path) -> None:
    claude = home / ".claude/skills/guildbotics"
    grok = home / ".grok/skills/guildbotics"
    for skill_dir in (claude, grok):
        assert installer.install_skill(skill_dir, "old\n")
    (grok / "SKILL.md").write_text("edited\n")

    report = installer.install_skills(home, SKILL)

    assert (claude / "SKILL.md").read_text() == SKILL
    assert (grok / "SKILL.md").read_text() == "edited\n"
    assert f"grok: {grok / 'SKILL.md'} was edited, kept" in report


def test_a_copy_the_user_rewrote_in_another_encoding_is_kept(home: Path) -> None:
    grok = home / ".grok/skills/guildbotics"
    installer.install_skill(grok, "old\n")
    (grok / "SKILL.md").write_bytes("編集".encode("shift_jis"))

    installer.install_skills(home, SKILL)

    assert (grok / "SKILL.md").read_bytes() == "編集".encode("shift_jis")


def test_a_skill_of_the_users_own_without_metadata_is_kept(home: Path) -> None:
    claude = home / ".claude/skills/guildbotics"
    claude.mkdir(parents=True)
    (claude / "SKILL.md").write_text("mine\n")

    installer.install_skills(home, SKILL)

    assert (claude / "SKILL.md").read_text() == "mine\n"
    assert not (claude / installer.MANAGED_SKILL_METADATA).exists()
