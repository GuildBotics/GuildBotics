from __future__ import annotations

import subprocess
from pathlib import Path

from guildbotics.entities.team import Person, Team
from guildbotics.utils.fileio import get_memory_repo_path


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

        if (is_new_repo or created_agents or alias_changed) and self._has_changes():
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

    def _render_agents_md(self) -> str:
        project = self.team.project
        role_lines = []
        for role in self.person.roles.values():
            summary = role.summary.strip()
            description = role.description.strip()
            detail = ": ".join(part for part in [summary, description] if part)
            role_lines.append(f"- {role.id}" + (f": {detail}" if detail else ""))
        if not role_lines:
            role_lines.append("- No explicit roles defined.")

        relationship_text = self.person.relationships.strip() or "None recorded."
        speaking_style = self.person.speaking_style.strip() or "No specific style recorded."
        project_name = project.name.strip() or "Unnamed project"
        language_code = project.get_language_code()

        return "\n".join(
            [
                f"# {self.person.name}",
                "",
                "## Identity",
                f"- Person ID: {self.person.person_id}",
                f"- Name: {self.person.name}",
                f"- Speaking style: {speaking_style}",
                "",
                "## Roles",
                *role_lines,
                "",
                "## Project Context",
                f"- Project: {project_name}",
                f"- Language: {language_code}",
                "",
                "## Relationships",
                relationship_text,
                "",
                "## Chat Reply Rules",
                "- Return only the chat reply body.",
                "- Treat each distinct author as a separate participant.",
                "- Reply to the latest message in context.",
                "- Respect the latest focus constraint when it is provided.",
                "- Keep the reply concise and specific.",
                "",
                "## Memory Update Rules",
                "- After writing the reply, update this file only if you learned durable information that is likely to matter later.",
                "- Prefer small edits over rewriting the whole file.",
                "- Do not invent facts.",
                "- Do not store transient one-off details unless they are likely to be reused.",
                "",
            ]
        )
