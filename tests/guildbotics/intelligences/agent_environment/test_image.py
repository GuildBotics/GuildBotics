"""The declared base image against this device, in one set of words."""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

import pytest

from guildbotics.intelligences.agent_environment import image as module
from guildbotics.intelligences.agent_environment.image import (
    IMAGE,
    ImageStatus,
    candidate_images,
    device_architecture,
    image_load_command,
    image_status,
    load_image,
)
from guildbotics.intelligences.agent_environment.runtime import (
    AgentEnvironmentError,
    ImageInfo,
)
from guildbotics.intelligences.agent_environment.toolchain import parse_toolchain
from guildbotics.utils.i18n_tool import t

_DIGEST = "sha256:" + "c" * 64
_OTHER = "sha256:" + "d" * 64
_DNS = {"nameservers": ["1.1.1.1"]}


def _declared(digest: str = _DIGEST, architecture: str | None = None):
    architecture = architecture or device_architecture()
    return parse_toolchain(
        {
            "image": {"reference": "local/agent:1", "digests": {architecture: digest}},
            "dns": _DNS,
        },
        where="t",
    )


@pytest.fixture
def arm64(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(module.platform, "machine", lambda: "arm64")
    return "arm64"


def _store(monkeypatch: pytest.MonkeyPatch, *images: ImageInfo) -> None:
    monkeypatch.setattr(module.runtime, "list_images", lambda: images)


def test_the_candidates_are_what_the_user_loaded_for_this_architecture(
    monkeypatch: pytest.MonkeyPatch, arm64: str
) -> None:
    """The recipe's own image and the pull's digest alias are the runtime's
    business; an image of another architecture cannot run here."""
    mine = ImageInfo("local/agent:1", _DIGEST, "arm64", 900)
    _store(
        monkeypatch,
        ImageInfo(IMAGE, "sha256:" + "a" * 64, "arm64", 381),
        ImageInfo(
            "docker.io/library/node@sha256:" + "a" * 64, "sha256:" + "a" * 64, "arm64"
        ),
        mine,
        ImageInfo("local/agent:1", _OTHER, "amd64", 900),
    )

    assert candidate_images() == (mine,)


@pytest.mark.parametrize(
    ("machine", "architecture"),
    [("x86_64", "amd64"), ("AMD64", "amd64"), ("aarch64", "arm64"), ("arm64", "arm64")],
)
def test_the_devices_cpu_is_named_the_way_images_name_it(
    monkeypatch: pytest.MonkeyPatch, machine: str, architecture: str
) -> None:
    monkeypatch.setattr(module.platform, "machine", lambda: machine)

    assert device_architecture() == architecture


def test_the_recipes_own_image_is_always_present() -> None:
    status = image_status(parse_toolchain({"dns": _DNS}, where="t"))

    assert status == ImageStatus()
    assert (status.declared, status.present) == (False, True)
    assert (status.refusal, status.warning) == ("", "")


def test_the_device_runs_what_it_holds_and_is_told_how_it_differs(
    monkeypatch: pytest.MonkeyPatch, arm64: str
) -> None:
    """Nothing loaded refuses; the declared digest is silent; another digest
    runs with a warning that names both and the way to fall in line."""
    _store(monkeypatch)
    missing = image_status(_declared())
    assert (missing.held, missing.present) == ("", False)
    assert missing.refusal == t(
        "intelligences.agent_environment.image.missing",
        reference="local/agent:1",
        architecture="arm64",
        command=image_load_command(),
    )
    assert missing.warning == ""

    _store(monkeypatch, ImageInfo("local/agent:1", _DIGEST, "arm64", 9))
    declared = image_status(_declared())
    assert (declared.held, declared.present) == (_DIGEST, True)
    assert (declared.refusal, declared.warning) == ("", "")

    _store(monkeypatch, ImageInfo("local/agent:1", _OTHER, "arm64", 9))
    differs = image_status(_declared())
    assert (differs.held, differs.present, differs.refusal) == (_OTHER, False, "")
    assert differs.warning == t(
        "intelligences.agent_environment.image.mismatch",
        reference="local/agent:1",
        architecture="arm64",
        held=_OTHER[:19],
        digest=_DIGEST[:19],
        command=image_load_command(),
    )


def test_an_image_declared_only_for_other_architectures_runs_if_held(
    monkeypatch: pytest.MonkeyPatch, arm64: str
) -> None:
    """The declaration says nothing about this device; what it loaded runs,
    and it is told to declare it. Nothing loaded is still nothing to run."""
    _store(monkeypatch, ImageInfo("local/agent:1", _OTHER, "arm64", 9))

    status = image_status(_declared(architecture="amd64"))

    assert status == ImageStatus(
        "local/agent:1", "arm64", "", False, _OTHER, digests={"amd64": _DIGEST}
    )
    assert status.refusal == ""
    assert status.warning == t(
        "intelligences.agent_environment.image.undeclared",
        reference="local/agent:1",
        architecture="arm64",
        declared="amd64",
        held=_OTHER[:19],
    )

    _store(monkeypatch)
    assert image_status(_declared(architecture="amd64")).refusal == t(
        "intelligences.agent_environment.image.missing",
        reference="local/agent:1",
        architecture="arm64",
        command=image_load_command(),
    )


def test_without_a_lookup_the_declaration_is_reported_as_is(
    monkeypatch: pytest.MonkeyPatch, arm64: str
) -> None:
    monkeypatch.setattr(module.runtime, "list_images", lambda: pytest.fail("looked"))

    assert image_status(_declared(), lookup=False) == ImageStatus(
        "local/agent:1", "arm64", _DIGEST, present=False, digests={"arm64": _DIGEST}
    )


def test_a_store_that_cannot_be_read_is_the_problem_itself(
    monkeypatch: pytest.MonkeyPatch, arm64: str
) -> None:
    def fail() -> tuple[ImageInfo, ...]:
        raise AgentEnvironmentError("store locked")

    monkeypatch.setattr(module.runtime, "list_images", fail)

    status = image_status(_declared())

    assert status == ImageStatus(
        "local/agent:1",
        "arm64",
        _DIGEST,
        present=False,
        error="store locked",
        digests={"arm64": _DIGEST},
    )
    assert (status.refusal, status.warning) == ("store locked", "")


def test_an_archive_built_for_another_architecture_is_not_loaded(
    monkeypatch: pytest.MonkeyPatch, arm64: str, tmp_path: Path
) -> None:
    archive = tmp_path / "agent.tar"
    monkeypatch.setattr(module.runtime, "archive_architecture", lambda path: "amd64")
    loads: list[Path] = []

    async def load(path: Path, *, tag: str | None = None) -> tuple[ImageInfo, ...]:
        loads.append(path)
        return ()

    monkeypatch.setattr(module.runtime, "load_image", load)

    with pytest.raises(AgentEnvironmentError) as exc_info:
        asyncio.run(load_image(archive))

    assert str(exc_info.value) == t(
        "intelligences.agent_environment.image.wrong_architecture",
        path=archive,
        architecture="amd64",
        device="arm64",
    )
    assert loads == []

    monkeypatch.setattr(module.runtime, "archive_architecture", lambda path: "arm64")
    asyncio.run(load_image(archive, tag="local/agent:1"))
    monkeypatch.setattr(module.runtime, "archive_architecture", lambda path: "")
    asyncio.run(load_image(archive))
    assert loads == [archive, archive]


def test_load_guidance_quotes_unix_paths_and_uses_windows_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "my home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    unix = image_load_command(platform="darwin")
    assert unix.endswith(" <archive.tar>")
    assert shlex.split(unix[: -len(" <archive.tar>")]) == [
        str(home / ".guildbotics/bin/guildbotics"),
        "environment",
        "image",
        "load",
    ]
    assert (
        image_load_command(platform="win32")
        == "guildbotics environment image load <archive.tar>"
    )
