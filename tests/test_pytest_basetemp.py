from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def root_conftest(pytestconfig: pytest.Config):
    plugin = pytestconfig.pluginmanager.get_plugin(
        str(Path(__file__).with_name("conftest.py"))
    )
    assert plugin is not None
    return plugin


def _config(basetemp: str | None = None, *, worker: bool = False) -> SimpleNamespace:
    config = SimpleNamespace(
        option=SimpleNamespace(basetemp=basetemp),
        stash=pytest.Stash(),
        getoption=lambda _: None,
    )
    if worker:
        config.workerinput = {}
    return config


def test_windows_default_basetemp_is_short_and_removed(
    root_conftest, fake_platform, monkeypatch, tmp_path: Path
) -> None:
    fake_platform(root_conftest, "win32")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    config = _config()

    root_conftest.pytest_configure(config)
    basetemp = Path(config.option.basetemp)
    try:
        assert basetemp.parent == tmp_path / "tmp"
        assert basetemp.name.startswith("gb-")
        (basetemp / "worker-file").write_text("worker output")
    finally:
        root_conftest.pytest_unconfigure(config)
    assert not basetemp.exists()


@pytest.mark.parametrize("worker", [False, True])
def test_explicit_basetemp_is_preserved(
    root_conftest, fake_platform, tmp_path: Path, worker: bool
) -> None:
    fake_platform(root_conftest, "win32")
    basetemp = tmp_path / "chosen"
    basetemp.mkdir()
    config = _config(str(basetemp), worker=worker)

    root_conftest.pytest_configure(config)
    root_conftest.pytest_unconfigure(config)

    assert config.option.basetemp == str(basetemp)
    assert basetemp.exists()


def test_worker_does_not_create_a_second_basetemp(
    root_conftest, fake_platform, monkeypatch, tmp_path: Path
) -> None:
    fake_platform(root_conftest, "win32")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    config = _config(worker=True)

    root_conftest.pytest_configure(config)
    root_conftest.pytest_unconfigure(config)

    assert config.option.basetemp is None
    assert not (tmp_path / "tmp").exists()
