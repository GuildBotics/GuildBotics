"""The base image as this device holds it, and the words for when it does not.

A workspace that names its own base image (:class:`.toolchain.BaseImage`)
shares the image's name and its identity per CPU architecture; the image
itself is loaded on each device by hand, from an archive built for that
device's architecture (``guildbotics environment image load``). Which of the
images this device holds may be named at all (:func:`candidate_images`), and
whether it holds the one declared for its architecture, are read here, once,
for the status card, the CLI, the declaration's picker, the build, and the
service's upkeep -- so every place lists and refuses the same images.
"""

from __future__ import annotations

import platform
import shlex
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

from guildbotics.intelligences.agent_environment import runtime
from guildbotics.intelligences.agent_environment.runtime import ImageInfo
from guildbotics.intelligences.agent_environment.toolchain import (
    ToolchainDeclaration,
)
from guildbotics.utils.i18n_tool import t

#: The base image unless the declaration names one: Debian with Node.js,
#: npm, and git, the tools the provider CLIs are installed and run with.
#: Pinned to an exact tag so two devices building the same declaration get
#: the same environment. An image the declaration names must give the recipe
#: the same: Debian's apt, Node.js with npm, curl and tar.
IMAGE = "node:22.23.2-bookworm"
#: How much of a digest a person is shown: enough to tell two apart.
_SHORT_DIGEST = len("sha256:") + 12
#: The runtime's own name for what it pulled, beside the tag it was asked
#: for; not a name the user gave an image they loaded.
_DIGEST_REFERENCE = "@sha256:"
#: What this device's CPU is called by OCI images, from what Python calls it.
_ARCHITECTURES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


def device_architecture() -> str:
    """This device's CPU architecture as OCI names it (``amd64``, ``arm64``).

    The environment runs on this CPU without emulation, so only an image
    built for it can be its base.
    """
    machine = platform.machine().lower()
    return _ARCHITECTURES.get(machine, machine)


@dataclass(frozen=True, slots=True)
class ImageStatus:
    """The declared base image against this device.

    ``reference`` is empty when the declaration names no image and the
    recipe's own is used, which every device can pull. ``digest`` is what the
    declaration names for this device's ``architecture`` (empty when it
    names nothing for it) and ``digests`` is everything it names; ``held`` is
    the digest this device holds under the reference, empty when it holds
    none, and ``present`` says the two agree. ``error`` is why the device's
    images could not be read at all, in which case nothing else is known.

    A device runs what it holds: nothing held is a :attr:`refusal`, while
    holding the reference at another digest than declared, or with nothing
    declared for this architecture, is a :attr:`warning` -- the turn runs
    on the image loaded here and the device is told how to fall in line.
    """

    reference: str = ""
    architecture: str = ""
    digest: str = ""
    present: bool = True
    held: str = ""
    error: str = ""
    digests: dict[str, str] = field(default_factory=dict)

    @property
    def declared(self) -> bool:
        return bool(self.reference)

    @property
    def refusal(self) -> str:
        """Why no turn can start over this image, or "" when one can."""
        if self.error:
            return self.error
        if self.declared and not self.held:
            return t(
                "intelligences.agent_environment.image.missing",
                reference=self.reference,
                architecture=self.architecture,
                command=image_load_command(),
            )
        return ""

    @property
    def warning(self) -> str:
        """How the image a turn runs on differs from the declaration, or ""."""
        if self.refusal or not self.declared or self.present:
            return ""
        if not self.digest:
            return t(
                "intelligences.agent_environment.image.undeclared",
                reference=self.reference,
                architecture=self.architecture,
                declared=", ".join(sorted(self.digests)),
                held=short_digest(self.held),
            )
        return t(
            "intelligences.agent_environment.image.mismatch",
            reference=self.reference,
            architecture=self.architecture,
            held=short_digest(self.held),
            digest=short_digest(self.digest),
            command=image_load_command(),
        )


def candidate_images() -> tuple[ImageInfo, ...]:
    """The images this device holds that its declaration may name as the base.

    Built for this device's architecture (the environment runs no other),
    named by the user rather than by the pull that fetched them (a
    ``@sha256:`` alias), and not the recipe's own image, which a declaration
    names by naming none. The CLI's ``image list``, the Desktop's picker, and
    the status all read this one list, so what can be picked is what is
    listed and what is looked for.

    Raises:
        AgentEnvironmentError: When the runtime cannot enumerate its store.
    """
    architecture = device_architecture()
    return tuple(
        image
        for image in runtime.list_images()
        if image.architecture == architecture
        and image.reference != IMAGE
        and _DIGEST_REFERENCE not in image.reference
    )


def image_status(
    declaration: ToolchainDeclaration, *, lookup: bool = True
) -> ImageStatus:
    """Compare the declared image with what this device holds.

    ``lookup=False`` reports what is declared for this device without asking
    the runtime, for a device that has no runtime to ask.
    """
    image = declaration.image
    if image is None:
        return ImageStatus()
    architecture = device_architecture()
    declared = ImageStatus(
        reference=image.reference,
        architecture=architecture,
        digest=image.digest_for(architecture),
        present=False,
        digests=dict(image.digests),
    )
    if not lookup:
        return declared
    try:
        held = next(
            (i for i in candidate_images() if i.reference == image.reference), None
        )
    except runtime.AgentEnvironmentError as exc:
        return replace(declared, error=str(exc))
    return replace(
        declared,
        present=held is not None and held.digest == declared.digest,
        held=held.digest if held else "",
    )


async def load_image(
    archive: Path, *, tag: str | None = None
) -> tuple[runtime.ImageInfo, ...]:
    """Load an image archive here, refusing one built for another CPU.

    The runtime records a loaded archive as this device's architecture
    whatever it was built for, so the archive is the only place to read it;
    an image that cannot run here is not let into the store at all.

    Raises:
        AgentEnvironmentError: When the archive is built for another
            architecture, cannot be read, or holds no image.
    """
    architecture = runtime.archive_architecture(archive)
    if architecture and architecture != device_architecture():
        raise runtime.AgentEnvironmentError(
            t(
                "intelligences.agent_environment.image.wrong_architecture",
                path=archive,
                architecture=architecture,
                device=device_architecture(),
            )
        )
    return await runtime.load_image(archive, tag=tag)


def short_digest(digest: str) -> str:
    return digest[:_SHORT_DIGEST]


def image_load_command(*, platform: str | None = None) -> str:
    """The terminal instruction that loads an image archive on this device.

    Spelled like the login command: Windows installers put the CLI on PATH,
    Unix Desktop installs it under home.
    """
    if (platform or sys.platform) == "win32":
        return "guildbotics environment image load <archive.tar>"
    return (
        shlex.join(
            [
                str(Path.home() / ".guildbotics/bin/guildbotics"),
                "environment",
                "image",
                "load",
            ]
        )
        + " <archive.tar>"
    )
