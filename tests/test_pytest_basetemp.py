from pathlib import Path
from types import SimpleNamespace

import pytest

import conftest


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
    fake_platform, monkeypatch, tmp_path: Path
) -> None:
    fake_platform(conftest, "win32")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    config = _config()

    conftest.pytest_configure(config)
    basetemp = Path(config.option.basetemp)
    try:
        assert basetemp.parent == tmp_path / "tmp"
        assert basetemp.name.startswith("gb-")
        (basetemp / "worker-file").write_text("worker output")
    finally:
        conftest.pytest_unconfigure(config)
    assert not basetemp.exists()


@pytest.mark.parametrize("worker", [False, True])
def test_explicit_basetemp_is_preserved(
    fake_platform, tmp_path: Path, worker: bool
) -> None:
    fake_platform(conftest, "win32")
    basetemp = tmp_path / "chosen"
    basetemp.mkdir()
    config = _config(str(basetemp), worker=worker)

    conftest.pytest_configure(config)
    conftest.pytest_unconfigure(config)

    assert config.option.basetemp == str(basetemp)
    assert basetemp.exists()


def test_worker_does_not_create_a_second_basetemp(
    fake_platform, monkeypatch, tmp_path: Path
) -> None:
    fake_platform(conftest, "win32")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    config = _config(worker=True)

    conftest.pytest_configure(config)
    conftest.pytest_unconfigure(config)

    assert config.option.basetemp is None
    assert not (tmp_path / "tmp").exists()
