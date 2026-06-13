import os

from guildbotics.utils.env_loader import GUILDBOTICS_ENV_FILE, load_guildbotics_env


def test_load_guildbotics_env_prefers_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / "secrets.env"
    cwd_env = tmp_path / ".env"
    env_file.write_text("AIKO_GITHUB_ACCESS_TOKEN=from-file\n", encoding="utf-8")
    cwd_env.write_text("AIKO_GITHUB_ACCESS_TOKEN=from-cwd\n", encoding="utf-8")
    monkeypatch.setenv(GUILDBOTICS_ENV_FILE, str(env_file))

    loaded = load_guildbotics_env(tmp_path, override=True, prefer_env_file=True)

    assert loaded == env_file
    assert loaded is not None
    assert loaded.is_absolute()
    assert loaded.read_text(encoding="utf-8")
    assert loaded == env_file.resolve()
    assert os.environ["AIKO_GITHUB_ACCESS_TOKEN"] == "from-file"
    assert os.environ[GUILDBOTICS_ENV_FILE] == str(env_file.resolve())


def test_load_guildbotics_env_sets_absolute_path(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("AIKO_GITHUB_ACCESS_TOKEN=from-cwd\n", encoding="utf-8")

    loaded = load_guildbotics_env(tmp_path, override=True, prefer_env_file=False)

    assert loaded == env_file.resolve()
    assert loaded is not None
    assert loaded.is_absolute()
    assert loaded.read_text(encoding="utf-8")
    assert os.environ["AIKO_GITHUB_ACCESS_TOKEN"] == "from-cwd"
    assert os.environ[GUILDBOTICS_ENV_FILE] == str(env_file.resolve())
