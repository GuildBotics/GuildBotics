from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from guildbotics.entities.team import Person, Team
from guildbotics.utils.fileio import get_memory_repo_path
from guildbotics.utils.person_profile import build_agent_profile


class MemoryRepository:
    """Manage a local git-backed memory repository for one person."""

    def __init__(self, person: Person, team: Team) -> None:
        self.person = person
        self.team = team
        self.repo_path = get_memory_repo_path(person.person_id)

    def get_repo_path(self) -> Path:
        self.ensure_initialized()
        return self.repo_path

    def ensure_initialized(self) -> None:
        self.repo_path.mkdir(parents=True, exist_ok=True)
        is_new_repo = not (self.repo_path / ".git").exists()
        if is_new_repo:
            self._git("init")
            self._git("config", "user.name", self._git_user_name())
            self._git("config", "user.email", self._git_user_email())

        agents_text = self._render_agents_md()
        agents_path = self.repo_path / "AGENTS.md"
        created_agents = False
        if not agents_path.exists():
            agents_path.write_text(agents_text, encoding="utf-8")
            created_agents = True

        alias_changed = False
        alias_changed |= self._sync_instruction_alias("CLAUDE.md", agents_text)
        alias_changed |= self._sync_instruction_alias("GEMINI.md", agents_text)
        index_created = self._ensure_memory_index()

        if (
            is_new_repo or created_agents or alias_changed or index_created
        ) and self._has_changes():
            self._git("add", "-A")
            self._git("commit", "-m", "Initialize agent memory")

    def commit_if_changed(self, message: str) -> str | None:
        self.ensure_initialized()
        if not self._has_changes():
            return None

        self._git("add", "-A")
        self._git("commit", "-m", message)
        return self._git("rev-parse", "HEAD")

    def _sync_instruction_alias(self, file_name: str, content: str) -> bool:
        path = self.repo_path / file_name
        if path.is_symlink():
            return False
        if path.exists():
            if path.read_text(encoding="utf-8") != content:
                path.write_text(content, encoding="utf-8")
                return True
            return False
        try:
            path.symlink_to("AGENTS.md")
            return True
        except OSError:
            path.write_text(content, encoding="utf-8")
            return True

    def _has_changes(self) -> bool:
        return bool(self._git("status", "--porcelain"))

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo_path,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def _git_user_name(self) -> str:
        git_user = str(self.person.account_info.get("git_user", "")).strip()
        return git_user or self.person.name or self.person.person_id

    def _git_user_email(self) -> str:
        git_email = str(self.person.account_info.get("git_email", "")).strip()
        if git_email:
            return git_email
        return f"{self.person.person_id}@guildbotics.local"

    def _ensure_memory_index(self) -> bool:
        index_path = self.repo_path / "memory_index.yml"
        if index_path.exists():
            return False
        index_path.write_text(
            yaml.safe_dump(
                {
                    "topics": {},
                    "global": {
                        "note": (
                            "Use AGENTS.md for agent identity and durable behavior. "
                            "Use topics/<topic_id>/memory.md for topic-scoped context."
                        )
                    },
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return True

    def _render_agents_md(self) -> str:
        project = self.team.project
        profile_text = yaml.safe_dump(
            build_agent_profile(self.person),
            allow_unicode=True,
            sort_keys=False,
        ).strip()
        project_name = project.name.strip() or "Unnamed project"
        project_description = project.description.strip()
        language_code = project.get_language_code()
        project_lines = [f"- Project: {project_name}"]
        if project_description:
            project_lines.append(f"- Description: {project_description}")
        project_lines.append(f"- Language: {language_code}")

        return "\n".join(
            [
                f"# {self.person.name}",
                "",
                "## Agent Profile",
                "```yaml",
                profile_text,
                "```",
                "",
                "## Project Context",
                *project_lines,
                "",
                "## Chat Reply Rules",
                "- Return only the chat reply body.",
                "- Treat each distinct author as a separate participant.",
                "- Reply to the latest message in context.",
                "- Respect the latest focus constraint when it is provided.",
                "- Keep the reply concise and specific.",
                "",
                "## Memory Update Rules",
                "- Durable chat memory is updated after replies by a separate memory backend step.",
                "- Do not describe memory checks or memory updates in chat replies.",
                "- Do not edit `AGENTS.md` while writing a chat reply unless the user's durable preference or agent behavior rule clearly changed.",
                "- Do not invent facts.",
                "- Do not store transient one-off details unless they are likely to be reused.",
                "",
                "## Memory Navigation Rules",
                "- Use topic memory from `memory_index.yml` and `topics/<topic_id>/memory.md` when it is provided in the prompt.",
                "- Keep each topic focused; do not mix unrelated topics into one memory file.",
                "- Store project or conversation decisions in topic memory, not in the agent profile above.",
                "- Topic memory should usually contain `Summary`, `Decisions`, `Open Questions`, and `Current Direction` sections.",
                "",
            ]
        )
