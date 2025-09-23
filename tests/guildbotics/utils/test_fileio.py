from pathlib import Path

import pytest

from guildbotics.utils.fileio import (
    _clean_data,
    find_package_subdir,
    get_config_path,
    load_markdown_with_frontmatter,
    load_yaml_file,
    save_yaml_file,
)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_load_markdown_with_frontmatter_handles_newlines(tmp_path, newline):
    """Front matter parses correctly when files use LF or CRLF newlines."""
    content = ("---\n" "brain: cli\n" "---\n" "Body text\n").replace("\n", newline)

    path = tmp_path / "prompt.md"
    path.write_text(content, encoding="utf-8")

    metadata = load_markdown_with_frontmatter(path)
    assert metadata["brain"] == "cli"
    assert metadata["body"] == "Body text"


def test_find_package_subdir_templates_exists():
    """find_package_subdir returns an existing 'templates' directory from package root."""
    p = find_package_subdir(Path("templates"))
    assert p.name == "templates"
    assert p.exists() and p.is_dir()


def test_get_config_path_prefers_env_over_home(tmp_path, monkeypatch):
    """When both env and HOME contain the file, env dir takes precedence."""
    env_dir = tmp_path / "envcfg"
    env_dir.mkdir()
    home_dir = tmp_path / "home"
    (home_dir / ".guildbotics" / "config").mkdir(parents=True)

    env_file = env_dir / "foo.yaml"
    home_file = home_dir / ".guildbotics" / "config" / "foo.yaml"
    env_file.write_text("a: 1\n", encoding="utf-8")
    home_file.write_text("a: 2\n", encoding="utf-8")

    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(env_dir))
    monkeypatch.setenv("HOME", str(home_dir))

    resolved = get_config_path("foo.yaml")
    assert resolved == env_file
    assert load_yaml_file(resolved) == {"a": 1}


def test_get_config_path_uses_home_when_env_missing_file(tmp_path, monkeypatch):
    """If env dir lacks the file, falls back to $HOME/.guildbotics/config."""
    env_dir = tmp_path / "envcfg"
    env_dir.mkdir()
    home_dir = tmp_path / "home"
    (home_dir / ".guildbotics" / "config").mkdir(parents=True)

    home_file = home_dir / ".guildbotics" / "config" / "bar.yaml"
    home_file.write_text("k: v\n", encoding="utf-8")

    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(env_dir))
    monkeypatch.setenv("HOME", str(home_dir))

    resolved = get_config_path("bar.yaml")
    assert resolved == home_file


def test_get_config_path_language_specific_and_fallback(tmp_path, monkeypatch):
    """Language-specific file resolves first; otherwise falls back to '.en'."""
    env_dir = tmp_path / "envcfg"
    env_dir.mkdir()
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(env_dir))

    ja_file = env_dir / "prompt.ja.yaml"
    en_file = env_dir / "prompt.en.yaml"
    ja_file.write_text("msg: ja\n", encoding="utf-8")
    en_file.write_text("msg: en\n", encoding="utf-8")

    # Prefers language-specific
    resolved_ja = get_config_path("prompt.yaml", language_code="ja")
    assert resolved_ja == ja_file

    # Remove ja to force fallback to en
    ja_file.unlink()
    resolved_fallback = get_config_path("prompt.yaml", language_code="ja")
    assert resolved_fallback == en_file


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
