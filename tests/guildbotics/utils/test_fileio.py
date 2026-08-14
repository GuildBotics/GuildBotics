from pathlib import Path

import pytest

from guildbotics.utils.fileio import (
    GUILDBOTICS_WORKSPACE_ROOT,
    WorkspaceNotConfiguredError,
    _clean_data,
    apply_workspace_root,
    find_package_subdir,
    get_config_path,
    get_machine_state_path,
    get_machine_state_root,
    get_member_clone_path,
    get_primary_config_path,
    get_workspace_config_dir,
    get_workspace_local_path,
    get_workspace_root,
    get_workspace_state_path,
    get_workspace_work_path,
    load_markdown_with_frontmatter,
    load_person_slot_mapping,
    load_yaml_file,
    save_yaml_file,
)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_load_markdown_with_frontmatter_handles_newlines(tmp_path, newline):
    """Front matter parses correctly when files use LF or CRLF newlines."""
    content = ("---\nbrain: agent\n---\nBody text\n").replace("\n", newline)

    path = tmp_path / "prompt.md"
    path.write_text(content, encoding="utf-8")

    metadata = load_markdown_with_frontmatter(path)
    assert metadata["brain"] == "agent"
    assert metadata["body"] == "Body text"


def test_find_package_subdir_templates_exists():
    """find_package_subdir returns an existing 'templates' directory from package root."""
    p = find_package_subdir(Path("templates"))
    assert p.name == "templates"
    assert p.exists() and p.is_dir()


def test_get_config_path_prefers_env_over_template(tmp_path, monkeypatch):
    """When env config contains the file, it takes precedence over templates."""
    env_dir = tmp_path / "envcfg"
    env_dir.mkdir()

    env_file = env_dir / "foo.yaml"
    env_file.write_text("a: 1\n", encoding="utf-8")

    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(env_dir))

    resolved = get_config_path("foo.yaml")
    assert resolved == env_file
    assert load_yaml_file(resolved) == {"a": 1}


def test_get_config_path_uses_template_when_env_missing_file(tmp_path, monkeypatch):
    """If the workspace config lacks the file, falls back to package templates."""
    env_dir = tmp_path / "envcfg"
    env_dir.mkdir()

    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(env_dir))

    resolved = get_config_path("team/defaults.yml")
    assert "templates" in resolved.parts
    assert resolved.name == "defaults.yml"


def _write_mapping(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_load_person_slot_mapping_merges_member_over_team(tmp_path, monkeypatch):
    """A member's partial mapping overrides matching slots and inherits the rest."""
    config_dir = tmp_path / "config"
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(config_dir))

    rel = "intelligences/model_mapping.yml"
    _write_mapping(
        config_dir / rel,
        "default: models/openai/default.yml\ntranslation: models/gemini/translation.yml\n",
    )
    _write_mapping(
        config_dir / "team/members/yuki" / rel,
        "default: models/anthropic/default.yml\n",
    )

    merged = load_person_slot_mapping("yuki", rel)

    assert merged == {
        "default": "models/anthropic/default.yml",
        "translation": "models/gemini/translation.yml",
    }


def test_load_person_slot_mapping_uses_team_when_member_absent(tmp_path, monkeypatch):
    """With no member mapping file, the team mapping is returned unchanged."""
    config_dir = tmp_path / "config"
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(config_dir))

    rel = "intelligences/model_mapping.yml"
    _write_mapping(
        config_dir / rel,
        "default: models/openai/default.yml\ntranslation: models/gemini/translation.yml\n",
    )

    merged = load_person_slot_mapping("aiko", rel)

    assert merged == {
        "default": "models/openai/default.yml",
        "translation": "models/gemini/translation.yml",
    }


def test_get_config_path_language_specific_and_fallback(tmp_path, monkeypatch):
    """Language-specific file resolves first; otherwise falls back to '.en'."""
    env_dir = tmp_path / "envcfg"
    env_dir.mkdir()
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(env_dir))

    ja_file = env_dir / "prompt.ja.yaml"
    en_file = env_dir / "prompt.en.yaml"
    ja_file.write_text("msg: ja\n", encoding="utf-8")
    en_file.write_text("msg: en\n", encoding="utf-8")

    resolved_ja = get_config_path("prompt.yaml", language_code="ja")
    assert resolved_ja == ja_file

    ja_file.unlink()
    resolved_fallback = get_config_path("prompt.yaml", language_code="ja")
    assert resolved_fallback == en_file


def test_get_primary_config_path_uses_resolved_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)

    assert get_primary_config_path(Path("team/project.yml")) == (
        tmp_path / ".guildbotics/config/team/project.yml"
    )


def test_get_machine_state_root_is_independent_of_workspace(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    assert get_machine_state_root() == home / ".guildbotics" / "data"
    assert get_machine_state_path("run", "service.lock") == (
        home / ".guildbotics" / "data" / "run" / "service.lock"
    )


def test_workspace_state_and_local_paths_do_not_overlap(tmp_path, monkeypatch):
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))

    state = get_workspace_state_path("documents")
    local = get_workspace_local_path("run")
    clone = get_member_clone_path("aiko")
    work = get_workspace_work_path("command-authoring")

    assert state == tmp_path / ".guildbotics" / "state" / "documents"
    assert local == tmp_path / ".guildbotics" / "local" / "run"
    assert clone == tmp_path / ".guildbotics" / "local" / "clones" / "aiko"
    assert work == (tmp_path / ".guildbotics" / "local" / "work" / "command-authoring")
    assert state.is_relative_to(tmp_path / ".guildbotics" / "state")
    assert local.is_relative_to(tmp_path / ".guildbotics" / "local")
    assert not state.is_relative_to(tmp_path / ".guildbotics" / "local")
    assert not clone.is_relative_to(tmp_path / ".guildbotics" / "state")


def test_get_workspace_root_does_not_use_cwd(tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    monkeypatch.delenv(GUILDBOTICS_WORKSPACE_ROOT, raising=False)
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)

    with pytest.raises(WorkspaceNotConfiguredError):
        get_workspace_root()


def test_apply_workspace_root_publishes_config_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    applied = apply_workspace_root(workspace)

    assert applied == workspace.resolve()
    assert get_workspace_root() == workspace.resolve()
    assert get_workspace_config_dir() == workspace.resolve() / ".guildbotics" / "config"


def test_clean_data_removes_none_and_empty_keys():
    """_clean_data drops None/'' keys in dicts, preserves list items."""
    raw = {
        "a": 1,
        "b": None,
        "c": "",
        "d": {"e": None, "f": "", "g": 2},
        "h": [
            {"i": None, "j": ""},
            5,
            None,
            "",
        ],
    }
    cleaned = _clean_data(raw)
    assert cleaned == {
        "a": 1,
        "d": {"g": 2},
        "h": [{}, 5, None, ""],
    }


def test_save_yaml_file_roundtrip_cleans(tmp_path):
    """save_yaml_file writes cleaned YAML; loading reproduces cleaned structure."""
    raw = {
        "title": "example",
        "unused": None,
        "nested": {"x": 1, "drop": ""},
        "items": [
            {"keep": 1, "omit": None},
            {"omit": ""},
            None,
            "",
        ],
    }
    expected = {
        "title": "example",
        "nested": {"x": 1},
        "items": [
            {"keep": 1},
            {},
            None,
            "",
        ],
    }

    out = tmp_path / "out.yaml"
    save_yaml_file(out, raw)
    loaded = load_yaml_file(out)
    assert loaded == expected
