from pathlib import Path

from guildbotics.entities.team import Person, Project, Role, Team
from guildbotics.utils.memory_repo import MemoryRepository


def _make_repo(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    person = Person(
        person_id="alice",
        name="Alice",
        roles={"dev": Role(id="dev", summary="Developer", description="Writes code")},
        relationships="Works well with Bob.",
        speaking_style="Direct and concise.",
        account_info={"git_user": "Alice", "git_email": "alice@example.com"},
    )
    team = Team(project=Project(name="GuildBotics", language="ja"), members=[person])
    return MemoryRepository(person, team)


def test_memory_repo_initializes_instruction_files(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)

    repo_path = repo.get_repo_path()

    assert (repo_path / ".git").exists()
    assert (repo_path / "AGENTS.md").exists()
    assert (repo_path / "CLAUDE.md").exists()
    assert (repo_path / "GEMINI.md").exists()
    assert "Speaking style: Direct and concise." in (repo_path / "AGENTS.md").read_text(
        encoding="utf-8"
    )


def test_memory_repo_commit_if_changed_is_noop_without_changes(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    repo.get_repo_path()

    assert repo.commit_if_changed("Update memory") is None


def test_memory_repo_commit_if_changed_commits_changes(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, monkeypatch)
    repo_path = repo.get_repo_path()
    agents_path = repo_path / "AGENTS.md"
    agents_path.write_text(
        agents_path.read_text(encoding="utf-8") + "\n- Learns quickly.\n",
        encoding="utf-8",
    )

    commit_sha = repo.commit_if_changed("Update memory")

    assert commit_sha is not None
