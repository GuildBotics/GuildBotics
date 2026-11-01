from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from guildbotics.drivers.service_settings import (
    ServiceSettings,
    ServiceSettingsError,
    read_service_settings,
    service_settings_path,
    write_service_settings,
)


def test_the_defaults_run_everything_every_ten_minutes() -> None:
    settings = read_service_settings()

    assert settings == ServiceSettings()
    assert settings.model_dump() == {
        # The server starts alone, as Desktop did, until this is turned on.
        "start_with_server": False,
        "keep_awake": False,
        "scheduled": True,
        "routine": True,
        "event_queue": True,
        "routine_interval_minutes": 10,
        "max_consecutive_errors": 3,
    }
    assert not service_settings_path().exists()


def test_saved_settings_are_read_back_from_the_device_file(tmp_path: Path) -> None:
    saved = ServiceSettings(
        keep_awake=True,
        scheduled=False,
        routine=True,
        event_queue=False,
        routine_interval_minutes=3,
        max_consecutive_errors=7,
    )

    write_service_settings(saved)

    assert service_settings_path() == tmp_path / ".guildbotics/local/service.yml"
    assert read_service_settings() == saved
    assert read_service_settings(tmp_path) == saved


def test_a_file_written_before_the_other_settings_keeps_its_keep_awake() -> None:
    path = service_settings_path()
    path.parent.mkdir(parents=True)
    path.write_text("keep_awake: true\n", encoding="utf-8")

    assert read_service_settings() == ServiceSettings(keep_awake=True)


def test_nothing_to_run_cannot_be_saved() -> None:
    with pytest.raises(ValidationError):
        ServiceSettings(scheduled=False, routine=False, event_queue=False)


@pytest.mark.parametrize(
    ("field", "accepted", "refused"),
    [
        ("routine_interval_minutes", (1, 1440), (0, 1441)),
        ("max_consecutive_errors", (1, 20), (0, 21)),
    ],
)
def test_numbers_stay_within_what_the_screen_offers(
    field: str, accepted: tuple[int, int], refused: tuple[int, int]
) -> None:
    for value in accepted:
        assert getattr(ServiceSettings(**{field: value}), field) == value
    for value in refused:
        with pytest.raises(ValidationError):
            ServiceSettings(**{field: value})


@pytest.mark.parametrize(
    "content",
    [
        b"keep_awake: [",
        b"",
        b"- a list\n",
        b"routine_interval_minutes: 0\n",
        b"scheduled: false\nroutine: false\nevent_queue: false\n",
        b"unknown: 1\n",
        b"\xff\xfe",
    ],
)
def test_a_file_that_is_not_settings_is_refused_by_name(content: bytes) -> None:
    path = service_settings_path()
    path.parent.mkdir(parents=True)
    path.write_bytes(content)

    with pytest.raises(ServiceSettingsError) as caught:
        read_service_settings()

    assert caught.value.path == path
    assert path.read_bytes() == content
