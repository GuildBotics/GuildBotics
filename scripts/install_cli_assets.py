#!/usr/bin/env python3
"""Install the GuildBotics CLI and the agent skill for this user, from this checkout.

``~/.guildbotics/bin/guildbotics`` runs the checkout's CLI: instructions, the
skill, and the git credential helper of human members name that path
(``guildbotics/utils/managed_cli.py``). The skill ``skills/guildbotics/SKILL.md``
is copied to where each AI CLI of the catalog installed here reads user skills
(``CliAgentProvision.user_skills``). A copy the user
edited is left alone: next to each copy a metadata file records the hash of
what was written, and a copy whose content no longer matches it is the user's.

Run it from the checkout's environment, as ``scripts/hub-server.sh`` does:
``.venv/bin/python scripts/install_cli_assets.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

from guildbotics.intelligences.cli_agents import CLI_AGENTS, CliAgentProvision

REPOSITORY = Path(__file__).resolve().parent.parent
SKILL = REPOSITORY / "skills" / "guildbotics" / "SKILL.md"
MANAGED_MARKER = "Managed by GuildBotics"
MANAGED_SKILL_METADATA = ".guildbotics-managed.json"


def content_hash(content: bytes) -> str:
    """FNV-1a 64 of the content, as the metadata file records it."""
    value = 0xCBF29CE484222325
    for byte in content:
        value = ((value ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016x}"


def _write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".pending")
    pending.write_text(content, encoding="utf-8")
    pending.chmod(mode)
    pending.replace(path)


def _shim(target: str) -> str:
    return f'#!/bin/sh\n# {MANAGED_MARKER}.\nexec {shlex.quote(target)} "$@"\n'


def install_cli(home: Path, cli: Path) -> None:
    """Point ``~/.guildbotics/bin/guildbotics`` (and ``~/.local/bin``) at ``cli``."""
    bin_dir = home / ".guildbotics" / "bin"
    if bin_dir.is_symlink():
        # A link to a directory of programs, never a directory of ours.
        bin_dir.unlink()
    managed = bin_dir / "guildbotics"
    _write(managed, _shim(str(cli)), 0o755)
    on_path = home / ".local" / "bin" / "guildbotics"
    if not on_path.exists() or MANAGED_MARKER in on_path.read_text(
        encoding="utf-8", errors="replace"
    ):
        _write(on_path, _shim(str(managed)), 0o755)


def _recorded_hash(skill_dir: Path) -> str | None:
    try:
        metadata = json.loads(
            (skill_dir / MANAGED_SKILL_METADATA).read_text(encoding="utf-8")
        )
    except OSError, ValueError:
        return None
    recorded = metadata.get("content_hash") if isinstance(metadata, dict) else None
    return recorded if isinstance(recorded, str) else None


def _is_ours(skill_dir: Path) -> bool:
    """Whether the copy in ``skill_dir`` is unedited since this script wrote it."""
    recorded = _recorded_hash(skill_dir)
    return recorded is not None and recorded == content_hash(
        (skill_dir / "SKILL.md").read_bytes()
    )


def install_skill(skill_dir: Path, content: str) -> bool:
    """Write the skill unless the user owns the copy there; say whether it did."""
    if (skill_dir / "SKILL.md").exists() and not _is_ours(skill_dir):
        return False
    _write(skill_dir / "SKILL.md", content)
    metadata = {
        "manager": "GuildBotics",
        "skill": "guildbotics",
        "content_hash": content_hash(content.encode("utf-8")),
    }
    _write(skill_dir / MANAGED_SKILL_METADATA, json.dumps(metadata, indent=2) + "\n")
    return True


def _skills_dir(home: Path, provision: CliAgentProvision) -> Path | None:
    """Where the tool reads user skills, or None when it is not installed here."""
    if not provision.user_skills:
        return None
    root = Path(os.environ.get(provision.state_root_env) or home / provision.state_root)
    if not root.is_dir():
        return None
    under_root = provision.user_skills.removeprefix(provision.state_root + "/")
    if under_root != provision.user_skills:
        return root / under_root
    return home / provision.user_skills


def install_skills(home: Path, content: str) -> list[str]:
    """Install the skill for every AI CLI found; describe each outcome."""
    report = []
    for agent in CLI_AGENTS:
        skills = _skills_dir(home, agent.provision)
        if skills is None:
            report.append(f"{agent.name}: not installed, skipped")
            continue
        skill = skills / "guildbotics" / "SKILL.md"
        if install_skill(skill.parent, content):
            report.append(f"{agent.name}: {skill}")
        else:
            report.append(f"{agent.name}: {skill} was edited, kept")
    return report


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    cli = REPOSITORY / ".venv" / "bin" / "guildbotics"
    if not cli.exists():
        print(
            f"{cli} is missing. Run `uv sync` in {REPOSITORY} first.", file=sys.stderr
        )
        return 1
    home = Path.home()
    install_cli(home, cli)
    print(f"CLI: {home / '.guildbotics' / 'bin' / 'guildbotics'} -> {cli}")
    for line in install_skills(home, SKILL.read_text(encoding="utf-8")):
        print(f"Skill for {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
