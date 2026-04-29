from pathlib import Path

from guildbotics.entities.team import Person, Project, Team
from guildbotics.utils.memory_backend import (
    FileMemoryBackend,
    MemoryQuery,
    MemoryUpdate,
)


def _backend(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    person = Person(person_id="alice", name="Alice")
    team = Team(project=Project(name="GuildBotics", language="ja"), members=[person])
    return FileMemoryBackend(person, team)


def test_file_memory_backend_remembers_and_recalls_topic(tmp_path, monkeypatch):
    backend = _backend(tmp_path, monkeypatch)

    result = backend.remember(
        MemoryUpdate(
            should_update=True,
            topic_id="onboarding",
            title="Onboarding",
            summary="Initial onboarding flow decisions.",
            memory="# Onboarding\n\n## Decisions\n- Keep the first step short.",
        )
    )

    context = backend.recall(
        MemoryQuery(
            person_id="alice",
            thread_topic="Initial onboarding flow",
            latest_focus="",
            transcript="",
        )
    )

    assert result.changed is True
    assert result.backend == "file"
    assert result.reference
    assert context.backend == "file"
    assert context.items[0].path == "topics/onboarding/memory.md"
    assert "Keep the first step short." in context.items[0].content
