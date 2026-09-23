"""The ``guildbotics environment`` commands: this device's isolated agent environment.

Building the snapshot the shared declaration asks for, loading the base image
it names, logging in to an AI CLI tool inside it, showing what this device
holds, and removing it. A device without a Desktop -- a headless Linux box
that joined the workspace -- has only these; the Desktop shows the same state.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import click

from guildbotics.cli._options import (
    apply_workspace_option,
    format_option,
    workspace_option,
)
from guildbotics.intelligences.agent_environment import image as image_module
from guildbotics.intelligences.agent_environment import (
    provider_state,
    runtime,
    snapshot,
)
from guildbotics.intelligences.agent_environment.image import (
    IMAGE,
    candidate_images,
    device_architecture,
    image_status,
    short_digest,
)
from guildbotics.intelligences.agent_environment.runtime import (
    AgentEnvironmentError,
    ImageInfo,
)
from guildbotics.intelligences.agent_environment.status import device_status
from guildbotics.intelligences.agent_environment.toolchain import (
    TOOLCHAIN_PATH,
    BaseImage,
    ToolchainDeclaration,
    ToolchainError,
    load_toolchain,
)
from guildbotics.intelligences.cli_agents import CLI_AGENTS, cli_agent_info
from guildbotics.utils.fileio import (
    get_primary_config_dir,
    save_yaml_file,
)
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.shared_write_lock import shared_write_lock

_PROVISIONED = [agent.name for agent in CLI_AGENTS if agent.provision.provisioned]


@click.group()
@workspace_option
def environment(workspace_dir: Path | None) -> None:
    """Build and log in to the isolated agent environment on this device."""
    apply_workspace_option(workspace_dir)


@environment.group(name="image")
def image_group() -> None:
    """The base images this device holds for the environment.

    A workspace may name an image it built itself as the base of the
    environment; the image is not shared, so each device loads it from an
    archive (`docker save`) built for its CPU architecture, and the
    declaration names the image's digest per architecture.
    """


@image_group.command(name="load")
@click.argument("archive", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--tag",
    help="A reference to add to the loaded image, beyond the archive's own tags.",
)
def image_load_command(archive: Path, tag: str | None) -> None:
    """Load an image archive into this device's runtime.

    The archive must be built for this device's CPU architecture. Its own
    tags are kept. What was loaded is printed by reference, with the digest
    the declaration names it by.
    """
    _require_runtime()
    try:
        loaded = asyncio.run(image_module.load_image(archive, tag=tag))
    except AgentEnvironmentError as exc:
        raise click.ClickException(str(exc)) from exc
    for image in loaded:
        click.echo(f"loaded {_image_line(image)}")


@image_group.command(name="list")
@format_option("markdown")
def image_list_command(output_format: str) -> None:
    """List the images this device's declaration may name as its base.

    Those loaded here, by the name they were loaded under, and whether the
    declaration names each for this device's architecture; the same list the
    Desktop's declaration picks from.
    """
    _require_runtime()
    declaration = _declaration()
    image = image_status(declaration, lookup=False)
    try:
        images = candidate_images()
    except AgentEnvironmentError as exc:
        raise click.ClickException(str(exc)) from exc
    rows = [
        {
            "reference": i.reference,
            "digest": i.digest,
            "size_bytes": i.size_bytes,
            "declared": i.reference == image.reference and i.digest == image.digest,
        }
        for i in images
    ]
    architecture = device_architecture()
    if output_format == "json":
        click.echo(
            json.dumps(
                {"architecture": architecture, "images": rows},
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    click.echo(f"architecture: {architecture}")
    if not images:
        click.echo("This device holds no image.")
    for row in rows:
        note = (
            "declared"
            if row["declared"]
            else f"declared at {short_digest(image.digest)}"
            if row["reference"] == image.reference and image.digest
            else ""
        )
        click.echo(f"{row['reference']} {row['digest']} {note}".rstrip())


@image_group.command(name="declare")
@click.argument("reference", required=False)
@click.option(
    "--digest",
    "digests",
    multiple=True,
    metavar="ARCH=DIGEST",
    help=(
        "Name the image's digest for an architecture (e.g. amd64=sha256:...). "
        "Repeatable. Without it, the digest of the image loaded here under "
        "REFERENCE is declared for this device's architecture."
    ),
)
@click.option(
    "--default",
    "use_default",
    is_flag=True,
    help="Declare no image: the environment builds from GuildBotics' own.",
)
def image_declare_command(
    reference: str | None, digests: tuple[str, ...], use_default: bool
) -> None:
    """Name the base image in the workspace's shared declaration.

    The declaration is shared with every device of the workspace. Naming the
    same reference again merges the digests, so a device of another
    architecture adds its own; naming another reference replaces them.
    """
    if use_default == bool(reference):
        raise click.UsageError("Give REFERENCE, or --default.")
    config_dir = get_primary_config_dir()
    if config_dir is None:
        raise click.ClickException("No workspace is selected.")
    with shared_write_lock():
        declaration = _declaration()
        if use_default:
            image = None
        else:
            assert reference is not None
            try:
                named = dict(_parse_digests(digests))
                if not named:
                    _require_runtime()
                    named = {device_architecture(): _held_digest(reference)}
                kept = (
                    declaration.image.digests
                    if declaration.image and declaration.image.reference == reference
                    else {}
                )
                image = BaseImage.model_validate(
                    {"reference": reference, "digests": {**kept, **named}}
                )
            except (ValueError, AgentEnvironmentError) as exc:
                raise click.ClickException(str(exc)) from exc
        updated = declaration.model_copy(update={"image": image})
        save_yaml_file(
            config_dir / TOOLCHAIN_PATH,
            updated.model_dump(mode="json", exclude_none=True),
        )
    if image is None:
        click.echo(f"image: {IMAGE} (GuildBotics default)")
        return
    for architecture, digest in sorted(image.digests.items()):
        click.echo(f"{image.reference} {architecture} {digest}")


def _parse_digests(entries: tuple[str, ...]) -> list[tuple[str, str]]:
    parsed = []
    for entry in entries:
        architecture, separator, digest = entry.partition("=")
        if not separator:
            raise click.BadParameter(
                f"'{entry}' is not ARCH=DIGEST", param_hint="--digest"
            )
        parsed.append((architecture, digest))
    return parsed


def _held_digest(reference: str) -> str:
    """The digest of the image loaded here under ``reference``."""
    for image in candidate_images():
        if image.reference == reference:
            return image.digest
    raise AgentEnvironmentError(
        t(
            "intelligences.agent_environment.image.missing",
            reference=reference,
            architecture=device_architecture(),
            command=image_module.image_load_command(),
        )
    )


def _image_line(image: ImageInfo) -> str:
    return f"{image.reference} {image.digest}"


@environment.command(name="build")
@click.option(
    "--force",
    is_flag=True,
    help="Rebuild even when the snapshot already matches the declaration.",
)
def build_command(force: bool) -> None:
    """Build the environment the shared declaration asks for.

    The build installs provider CLIs and nothing else, so it needs no input. A
    build that fails is remembered until the declaration changes or this
    command runs again.
    """
    _require_runtime()
    declaration = _declaration()
    status = snapshot.snapshot_status(image_status(declaration))
    if status.state == "ready" and not force:
        click.echo(f"The environment {status.name} is already up to date.")
        return
    if status.state == "building":
        raise click.ClickException("A build of this environment is already running.")
    try:
        built = asyncio.run(snapshot.build_snapshot(declaration, on_line=click.echo))
    except (AgentEnvironmentError, ToolchainError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"The environment {built.name} is ready.")


@environment.command(name="login")
@click.argument("tool", type=click.Choice(_PROVISIONED))
def login_command(tool: str) -> None:
    """Log in to an AI CLI tool inside the environment.

    The tool's own login command runs in the environment and talks you
    through it here; what it stores stays on this device, outside the
    snapshot, and every member uses it.
    """
    _require_runtime()
    info = cli_agent_info(tool)
    declaration = _declaration()
    status = snapshot.snapshot_status(image_status(declaration))
    if status.state != "ready":
        raise click.ClickException(
            f"The environment is {status.state}; build it first with "
            "`guildbotics environment build`."
        )
    try:
        code = asyncio.run(
            provider_state.login(
                info,
                declaration,
                snapshot=status.path,
                read_line=_read_stdin_line,
                write=_write_stdout,
            )
        )
    except (AgentEnvironmentError, ToolchainError) as exc:
        raise click.ClickException(str(exc)) from exc
    if code != 0:
        raise click.ClickException(f"{info.label} login exited with code {code}.")
    click.echo(
        t("intelligences.agent_environment.tool.credentials_saved", tool=info.label)
    )


def _read_stdin_line() -> str | None:
    return sys.stdin.readline() or None


def _write_stdout(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


@environment.command(name="status")
@format_option("markdown")
def status_command(output_format: str) -> None:
    """Show the environment's runtime, snapshot, and logins on this device."""
    payload = _status_payload()
    if output_format == "json":
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if payload["refusal"]:
        click.echo(f"{payload['setting']}: {payload['refusal']}")
    if payload["warning"]:
        click.echo(f"warning: {payload['warning']}")
    health = payload["runtime"]
    click.echo(
        f"runtime: {'available ' + health['version'] if health['available'] else 'unavailable: ' + health['reason']}"
    )
    if health["home"]:
        click.echo(f"runtime home: {health['home']}")
    resources = payload["resources"]
    if resources is None:
        click.echo("resources: unavailable")
    else:
        click.echo(
            f"resources: {resources['memory_mib']} MiB, {resources['cpus']} vCPU"
        )
    image = payload["image"]
    if image["reference"]:
        held = (
            "not loaded"
            if not image["held"]
            else "loaded, declared"
            if image["present"]
            else f"loaded at {short_digest(image['held'])}, not the declared one"
            if image["digest"]
            else f"loaded at {short_digest(image['held'])}, not declared for {image['architecture']}"
        )
        declared = " ".join(
            f"{arch}={short_digest(digest)}"
            for arch, digest in image["digests"].items()
        )
        click.echo(
            f"image: {image['reference']} [{declared}] {image['architecture']}: {held}"
        )
    else:
        click.echo(f"image: {IMAGE} (GuildBotics default)")
    state = payload["snapshot"]
    # The reason is the device's; what to do about it is this command's.
    hint = state["detail"] or (
        "run `guildbotics environment build`"
        if state["state"] in ("missing", "stale")
        else ""
    )
    detail = f" ({hint})" if hint else ""
    click.echo(f"snapshot: {state['state']} {state['name']}{detail}")
    click.echo(f"location: {state['path']}")
    network = payload["network"]
    if network is None:
        click.echo("network: unavailable")
    else:
        network_details = ", ".join(network["allowed_domains"])
        if network["allow_local_network"]:
            network_details = ", ".join(
                filter(None, (network_details, "localhost and LAN"))
            )
        click.echo(
            f"network: {network['mode']}"
            + (f" ({network_details})" if network_details else "")
        )
    dns = payload["dns"]
    click.echo(
        f"dns: {dns['declared']} -> {', '.join(dns['nameservers']) or dns['problem']}"
    )
    for tool in payload["tools"]:
        click.echo(
            f"{tool['name']}: {tool['problem'] or t('intelligences.agent_environment.tool.credentials_saved', tool=tool['label'])}"
        )


def _status_payload() -> dict[str, Any]:
    status = device_status()
    state = status.snapshot
    return {
        "refusal": status.refusal,
        "setting": status.setting,
        "warning": status.warning,
        "runtime": {
            "available": status.runtime.available,
            "reason": status.runtime.reason,
            "version": status.runtime.runtime_version,
            "home": status.runtime.home,
        },
        "resources": (
            status.declaration.resources.model_dump(mode="json")
            if status.declaration
            else None
        ),
        "snapshot": {
            "state": state.state if state else "missing",
            "name": state.name if state else "",
            "path": str(state.path) if state else "",
            "detail": state.detail if state else status.declaration_problem,
        },
        "image": {
            "reference": status.image.reference,
            "architecture": status.image.architecture or device_architecture(),
            "digest": status.image.digest,
            "digests": dict(status.image.digests),
            "present": status.image.present,
            "held": status.image.held,
            "problem": status.image.refusal,
            "warning": status.image.warning,
        },
        "network": status.network.model_dump(mode="json") if status.network else None,
        "dns": {
            "declared": status.dns.declared,
            "nameservers": list(status.dns.nameservers),
            "problem": status.dns.problem,
        },
        "tools": [
            {
                "name": tool.name,
                "label": tool.label,
                "provisioned": tool.provisioned,
                "credentials_saved": tool.credentials_saved,
                "authentication_failed": tool.authentication_failed,
                "problem": tool.problem,
            }
            for tool in status.tools
        ],
    }


@environment.command(name="remove")
def remove_command() -> None:
    """Remove the workspace's snapshots from this device (logins are kept)."""
    _require_runtime()
    try:
        removed = asyncio.run(snapshot.remove_snapshots())
    except AgentEnvironmentError as exc:
        raise click.ClickException(str(exc)) from exc
    if not removed:
        click.echo("This device holds no snapshot of the workspace.")
        return
    for name in removed:
        click.echo(f"removed {name}")


def _require_runtime() -> None:
    health = runtime.doctor()
    if not health.available:
        raise click.ClickException(health.reason)


def _declaration() -> ToolchainDeclaration:
    try:
        return load_toolchain()
    except ToolchainError as exc:
        raise click.ClickException(str(exc)) from exc
