"""The runtime reads and loads this device's images through the SDK."""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import microsandbox
import pytest

from guildbotics.intelligences.agent_environment.runtime import (
    AgentEnvironmentError,
    ImageInfo,
    archive_architecture,
    list_images,
    load_image,
)
from guildbotics.utils.i18n_tool import t


def _handle(
    reference: str,
    digest: str | None,
    size: int | None = None,
    architecture: str = "arm64",
) -> Any:
    config = None if digest is None else SimpleNamespace(digest=digest)

    async def inspect() -> Any:
        return SimpleNamespace(config=config)

    return SimpleNamespace(
        reference=reference,
        size_bytes=size,
        architecture=architecture,
        manifest_digest="",
        inspect=inspect,
    )


class _Image:
    handles: list[Any] = []
    loaded: dict[str, Any] = {}
    list_error: Exception | None = None
    load_error: Exception | None = None

    @staticmethod
    async def list() -> list[Any]:
        if _Image.list_error is not None:
            raise _Image.list_error
        return list(_Image.handles)

    @staticmethod
    async def load(input_path: str, *, tag: str | None = None) -> list[Any]:
        if _Image.load_error is not None:
            raise _Image.load_error
        _Image.loaded = {"path": input_path, "tag": tag}
        if input_path.endswith("empty.tar"):
            return []
        loaded = [_handle("archive/agent:1", "sha256:" + "b" * 64, 7)]
        if tag:
            loaded.append(_handle(tag, "sha256:" + "b" * 64, 7))
        _Image.handles.extend(loaded)
        return loaded


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> type[_Image]:
    _Image.handles = [
        _handle("node:22.23.2-bookworm", "sha256:" + "a" * 64, 381),
        _handle("local/agent:1", "sha256:" + "c" * 64, 900, architecture="amd64"),
        _handle("broken:latest", None),
    ]
    _Image.loaded = {}
    _Image.list_error = _Image.load_error = None
    monkeypatch.setattr(microsandbox, "Image", _Image)
    return _Image


def test_images_are_listed_by_reference_with_their_config_digest(sdk) -> None:
    """The config digest is the identity that survives save and load; an
    image the runtime cannot describe is left out rather than misnamed."""
    assert list_images() == (
        ImageInfo("node:22.23.2-bookworm", "sha256:" + "a" * 64, "arm64", 381),
        ImageInfo("local/agent:1", "sha256:" + "c" * 64, "amd64", 900),
    )


def test_listing_works_from_inside_a_running_event_loop(sdk) -> None:
    """A turn about to start reads the device from within the service's loop."""

    async def inside() -> tuple[ImageInfo, ...]:
        return list_images()

    assert asyncio.run(inside())[1] == ImageInfo(
        "local/agent:1", "sha256:" + "c" * 64, "amd64", 900
    )


def test_a_store_the_runtime_cannot_read_is_a_boundary_error(sdk) -> None:
    sdk.list_error = microsandbox.MicrosandboxError("store locked")

    with pytest.raises(AgentEnvironmentError) as exc_info:
        list_images()

    assert str(exc_info.value) == t(
        "intelligences.agent_environment.runtime.images_failed", error="store locked"
    )


def test_loading_an_archive_returns_what_it_added_under_the_given_tag(
    sdk, tmp_path: Path
) -> None:
    archive = tmp_path / "agent.tar"

    loaded = asyncio.run(load_image(archive, tag="local/agent:2"))

    assert sdk.loaded == {"path": str(archive), "tag": "local/agent:2"}
    assert loaded == (
        ImageInfo("archive/agent:1", "sha256:" + "b" * 64, "arm64", 7),
        ImageInfo("local/agent:2", "sha256:" + "b" * 64, "arm64", 7),
    )


def test_an_archive_without_an_image_and_an_unreadable_one_are_reported(
    sdk, tmp_path: Path
) -> None:
    with pytest.raises(AgentEnvironmentError) as empty:
        asyncio.run(load_image(tmp_path / "empty.tar"))
    assert str(empty.value) == t(
        "intelligences.agent_environment.runtime.nothing_loaded",
        path=tmp_path / "empty.tar",
    )

    sdk.load_error = microsandbox.MicrosandboxError("not a tar")
    with pytest.raises(AgentEnvironmentError) as broken:
        asyncio.run(load_image(tmp_path / "x.tar"))
    assert str(broken.value) == t(
        "intelligences.agent_environment.runtime.load_failed",
        path=tmp_path / "x.tar",
        error="not a tar",
    )


def _tar(path: Path, members: dict[str, Any]) -> Path:
    with tarfile.open(path, "w") as tar:
        for name, payload in members.items():
            data = json.dumps(payload).encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def test_the_architecture_of_a_docker_archive_is_read_from_its_config(
    tmp_path: Path,
) -> None:
    archive = _tar(
        tmp_path / "docker.tar",
        {
            "blobs/sha256/aaa": {"architecture": "amd64", "os": "linux"},
            "manifest.json": [{"Config": "blobs/sha256/aaa", "RepoTags": ["x:1"]}],
        },
    )

    assert archive_architecture(archive) == "amd64"


def test_the_architecture_of_an_oci_layout_follows_its_first_manifest(
    tmp_path: Path,
) -> None:
    """A platform on the index descriptor is enough; without one, the
    manifest's config is read, through a nested per-platform index."""
    platform = _tar(
        tmp_path / "platform.tar",
        {
            "index.json": {
                "manifests": [
                    {"digest": "sha256:m", "platform": {"architecture": "arm64"}}
                ]
            }
        },
    )
    assert archive_architecture(platform) == "arm64"

    nested = _tar(
        tmp_path / "nested.tar",
        {
            "index.json": {"manifests": [{"digest": "sha256:idx"}]},
            "blobs/sha256/idx": {"manifests": [{"digest": "sha256:m"}]},
            "blobs/sha256/m": {"config": {"digest": "sha256:c"}},
            "blobs/sha256/c": {"architecture": "amd64"},
        },
    )
    assert archive_architecture(nested) == "amd64"

    empty = _tar(tmp_path / "empty.tar", {"index.json": {"manifests": []}})
    assert archive_architecture(empty) == ""


def test_an_archive_that_is_not_an_image_is_reported(tmp_path: Path) -> None:
    broken = tmp_path / "broken.tar"
    broken.write_bytes(b"not a tar")

    with pytest.raises(AgentEnvironmentError) as exc_info:
        archive_architecture(broken)

    assert str(exc_info.value).startswith(
        t("intelligences.agent_environment.runtime.load_failed", path=broken, error="")
    )
    _tar(tmp_path / "no-manifest.tar", {"other.json": {}})
    with pytest.raises(AgentEnvironmentError):
        archive_architecture(tmp_path / "no-manifest.tar")
