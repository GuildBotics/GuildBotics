"""The microVMs AI CLI turns run in, driven through the microsandbox SDK.

This is the only module that talks to the runtime. A :class:`AgentEnvironment` is
created from a snapshot -- for the turns of one command, or for a login or
probe of its own -- with the mounts and network policy a
:class:`AgentEnvironmentSpec` states, runs the provider CLI inside with its
stdio bridged to the host, and is discarded when what it was booted for ends.
Stopping it ends every process inside, so no process survives it.

The SDK is imported when an environment is needed rather than when this module
is: a device without a wheel for its platform must still start GuildBotics
and be told, through :func:`doctor`, why no agent can run there.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import (
    Awaitable,
    Callable,
    Coroutine,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path, PurePosixPath
from typing import Any, cast

from guildbotics.intelligences.agent_environment.spec import (
    AgentEnvironmentSpec,
    EnvironmentNetwork,
)
from guildbotics.utils.fileio import get_machine_state_path
from guildbotics.utils.i18n_tool import t

#: How a sandbox GuildBotics created is named, so a stale one is recognisable.
_NAME_PREFIX = "guildbotics-"
#: The one sandbox a snapshot is built in; a device builds one at a time.
_BUILD_NAME = _NAME_PREFIX + "build"
#: Size of an empty directory of the microVM's own: a login's state, a
#: read-only turn's working directory, a cover over a denied directory; never
#: a workspace.
_SCRATCH_MIB = 64
#: The exit code reported when the guest process ended without one: the
#: runtime killed it, or its exec session broke.
_KILLED = -1
_STOP_TIMEOUT = 5.0
#: Longest output line a build step may print before its reader gives up.
_BUILD_LINE_LIMIT = 1 << 20
#: The environment's network is IPv4: the declaration names IPv4 resolvers,
#: the policy is written for IPv4, and the gateway forwards over IPv4. The
#: guest nevertheless boots with an IPv6 address and an IPv6 gateway resolver
#: in ``/etc/resolv.conf``, and a stub resolver that consults both (Codex's)
#: never answers; so IPv6 is switched off before anything else runs, in a
#: turn's environment and in the build's alike.
_IPV4_ONLY = "echo 1 > /proc/sys/net/ipv6/conf/all/disable_ipv6"
#: The SDK reads where the runtime and its state live from these variables.
RUNTIME_HOME_ENV = "MSB_HOME"
RUNTIME_BINARY_ENV = "MSB_PATH"
#: Written beside the placed runtime, so a newer SDK replaces an older copy.
_RUNTIME_VERSION_FILE = "version"
#: The Windows Defender Firewall rule for the runtime, created once for its
#: fixed path (the runtime binds a listening socket, which Windows asks about
#: per program path; a path that changed on every launch asked every time).
FIREWALL_RULE_NAME = "GuildBotics agent environment (msb)"


class AgentEnvironmentError(RuntimeError):
    """The environment could not be created or the runtime refused a step."""


def _start_failure(*, build: bool, error: Exception, memory_mib: int, cpus: int) -> str:
    """Describe a failed boot with facts GuildBotics knows about the request."""
    values = {"error": error, "memory_mib": memory_mib, "cpus": cpus}
    if build:
        return t("intelligences.agent_environment.runtime.build_start_failed", **values)
    return t("intelligences.agent_environment.runtime.start_failed", **values)


@dataclass(frozen=True, slots=True)
class AgentEnvironmentHealth:
    """Whether this device can create an environment, and if not, why."""

    available: bool
    reason: str = ""
    runtime_version: str = ""
    #: Where the runtime and its state (images, sandboxes) live on this device.
    home: str = ""


@dataclass(frozen=True, slots=True)
class ImageInfo:
    """An image this device holds, as the runtime names and identifies it.

    ``digest`` is the digest of the image's configuration: the identity that
    survives ``docker save`` and ``msb image load``, unlike the
    ``manifest_digest``, which an archive re-encodes and which is how the
    runtime itself names what a sandbox was created from. ``architecture``
    is the CPU architecture the image was built for, as OCI names it.
    """

    reference: str
    digest: str
    architecture: str = ""
    size_bytes: int | None = None
    manifest_digest: str = ""


def runtime_home() -> Path:
    """The fixed directory the runtime lives in on this device.

    GuildBotics places the runtime here itself, from the copy the SDK wheel
    carries, so nothing outside GuildBotics decides which runtime runs and
    the path is the same on every launch. The path is kept short: the
    runtime opens unix sockets under it, and those have a length limit.
    """
    return get_machine_state_path("msb")


def runtime_binary(home: Path) -> Path:
    """The ``msb`` binary under a runtime home."""
    return home / "bin" / ("msb.exe" if os.name == "nt" else "msb")


def doctor() -> AgentEnvironmentHealth:
    """Check that the SDK and its runtime are present on this device.

    The runtime is placed under :func:`runtime_home` when it is missing or
    older than the SDK, and the SDK is pointed at it; nothing is fetched from
    the network. Absent the SDK, or a runtime the device cannot hold, no
    agent may start here (fail-closed), and the reason is what the Desktop
    shows beside that refusal.
    """
    home = runtime_home()
    os.environ[RUNTIME_HOME_ENV] = str(home)
    os.environ.setdefault(RUNTIME_BINARY_ENV, str(runtime_binary(home)))
    try:
        import microsandbox
    except ImportError:
        return AgentEnvironmentHealth(
            False, t("intelligences.agent_environment.runtime.sdk_missing")
        )
    version = microsandbox.version()
    try:
        _place_runtime(home, version)
    except OSError as exc:
        return AgentEnvironmentHealth(
            False,
            t(
                "intelligences.agent_environment.runtime.not_placed",
                home=home,
                error=exc,
            ),
            home=str(home),
        )
    if not microsandbox.is_installed():
        return AgentEnvironmentHealth(
            False,
            t("intelligences.agent_environment.runtime.not_installed"),
            home=str(home),
        )
    return AgentEnvironmentHealth(True, runtime_version=version, home=str(home))


def _place_runtime(home: Path, version: str) -> None:
    """Copy the SDK's bundled ``msb`` and ``libkrunfw`` under ``home`` once per version.

    The wheel carries both; a packaged GuildBotics unpacks them to a fresh
    temporary directory on every launch, so they are copied to the fixed
    home the SDK is pointed at. On Windows the firewall rule for that fixed
    path is created at the same time.
    """
    marker = home / _RUNTIME_VERSION_FILE
    if runtime_binary(home).is_file() and _read(marker) == version:
        return
    bundled = files("microsandbox._bundled")
    for part in ("bin", "lib"):
        target_dir = home / part
        target_dir.mkdir(parents=True, exist_ok=True)
        for entry in bundled.joinpath(part).iterdir():
            if not entry.is_file():
                continue
            target = target_dir / entry.name
            with as_file(entry) as source:
                shutil.copyfile(source, target)
            if part == "bin":
                target.chmod(0o755)
    marker.write_text(version, encoding="utf-8")
    _ensure_firewall_rule(runtime_binary(home))


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _ensure_firewall_rule(binary: Path) -> None:
    """Create the Windows Defender Firewall rule for the runtime, once, elevated.

    Best effort: a declined elevation leaves Windows to ask on the runtime's
    first listen, which for a fixed path it does once.
    """
    if sys.platform != "win32":
        return
    # Only the exit status says whether the rule exists; the text netsh
    # prints is in the console code page and is not read at all (decoding it
    # as text has failed on a Japanese Windows).
    shown = subprocess.run(
        [
            "netsh",
            "advfirewall",
            "firewall",
            "show",
            "rule",
            f"name={FIREWALL_RULE_NAME}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if shown.returncode == 0:
        return
    arguments = (
        f'advfirewall firewall add rule name="{FIREWALL_RULE_NAME}" dir=in '
        f'action=allow program="{binary}" enable=yes profile=any'
    )
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"Start-Process -FilePath netsh -Verb RunAs -Wait -ArgumentList '{arguments}'",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


class EnvironmentStdin:
    """The guest process' stdin, with the surface of an asyncio stream writer."""

    def __init__(self, sink: Any) -> None:
        self._sink = sink
        self._buffer = bytearray()
        self._closed = False

    def write(self, data: bytes) -> None:
        self._buffer += data

    async def drain(self) -> None:
        data = bytes(self._buffer)
        self._buffer.clear()
        if not data:
            return
        try:
            await self._sink.write(data)
        except Exception as exc:
            raise ConnectionError(f"environment stdin closed: {exc}") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        asyncio.get_running_loop().create_task(self._aclose())

    def is_closing(self) -> bool:
        return self._closed

    async def _aclose(self) -> None:
        with suppress(Exception):
            await self._sink.close()


class EnvironmentProcess:
    """A process inside the environment, with the surface of an asyncio subprocess.

    ``stdout`` and ``stderr`` are stream readers fed from the runtime's event
    stream, so the line-oriented transports the adapters already use read
    them unchanged. ``returncode`` is set when the process ends; ``kill``
    ends it now.
    """

    def __init__(self, handle: Any, *, limit: int) -> None:
        self._handle = handle
        self.stdin = EnvironmentStdin(handle.take_stdin())
        self.stdout = asyncio.StreamReader(limit=limit)
        self.stderr = asyncio.StreamReader(limit=limit)
        self.pid: int | None = None
        self.returncode: int | None = None
        self._exited = asyncio.Event()
        self._pump = asyncio.create_task(self._pump_events())

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode

    async def kill(self) -> None:
        if self.returncode is None:
            with suppress(Exception):
                await self._handle.kill()
        await self.wait()

    async def communicate(self) -> tuple[bytes, bytes]:
        """Close stdin, read both streams to their end, and wait; as asyncio does."""
        self.stdin.close()
        stdout, stderr = await asyncio.gather(self.stdout.read(), self.stderr.read())
        await self.wait()
        return stdout, stderr

    async def _pump_events(self) -> None:
        try:
            async for event in self._handle:
                self._apply(event)
        except Exception as exc:  # the exec session itself broke
            if self.returncode is None:
                self.stderr.feed_data(f"{exc}\n".encode())
        finally:
            if self.returncode is None:
                self.returncode = _KILLED
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self._exited.set()

    def _apply(self, event: Any) -> None:
        kind = str(event.event_type)
        if kind == "started":
            self.pid = event.pid
        elif kind == "stdout":
            self.stdout.feed_data(event.data or b"")
        elif kind == "stderr":
            self.stderr.feed_data(event.data or b"")
        elif kind == "exited":
            self.returncode = _KILLED if event.code is None else int(event.code)
        elif kind == "failed":
            # The runtime could not spawn the command; its reason is the
            # process' only output.
            self.stderr.feed_data((event.data or b"") + b"\n")
            self.returncode = int(event.code or 1)


class AgentEnvironment:
    """One microVM: a command's turns', or a single turn's."""

    def __init__(
        self,
        sandbox: Any,
        spec: AgentEnvironmentSpec,
        on_close: Callable[[], None] | None = None,
        before_stop: Callable[[AgentEnvironment], Awaitable[None]] | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._closed = False
        self._on_close = on_close
        self._before_stop = before_stop
        self.spec = spec

    @classmethod
    async def start(
        cls,
        spec: AgentEnvironmentSpec,
        *,
        snapshot: str,
        memory_mib: int,
        cpus: int,
        on_close: Callable[[], None] | None = None,
        before_stop: Callable[[AgentEnvironment], Awaitable[None]] | None = None,
    ) -> AgentEnvironment:
        """Boot a microVM from ``snapshot`` shaped by ``spec``.

        The sandbox is ephemeral: stopping it removes it, so nothing of the
        turns it ran outlives it. ``on_close`` is what the caller has to do
        once the microVM is gone -- take the provider's persisted state out
        of the turn's directory -- and it runs exactly once, whether the boot
        failed, the turn ended, or the turn was cancelled. ``before_stop`` is
        what it has to take out of the microVM's memory while it still runs
        (a refreshed login), after its processes have ended; it runs at
        most once, and not when the boot failed.
        """
        import microsandbox

        name = _NAME_PREFIX + secrets.token_hex(6)
        try:
            sandbox = await microsandbox.Sandbox.create(
                name,
                from_snapshot=snapshot,
                ephemeral=True,
                memory=memory_mib,
                cpus=cpus,
                workdir=spec.cwd,
                volumes=_volumes(spec),
                network=_network(spec),
            )
        except BaseException as exc:
            # Cancellation is not an ``Exception``: a turn the service
            # cancelled while its microVM was starting must hand its state
            # back too, and it is not a boot failure to report.
            if on_close is not None:
                on_close()
            if not isinstance(exc, Exception):
                raise
            raise AgentEnvironmentError(
                _start_failure(
                    build=False,
                    error=exc,
                    memory_mib=memory_mib,
                    cpus=cpus,
                )
            ) from exc
        environment = cls(sandbox, spec, on_close, before_stop)
        try:
            await _ipv4_only(sandbox)
        except BaseException:
            await environment.close()
            raise
        return environment

    async def run(
        self,
        command: str,
        *args: str,
        limit: int,
        tty: bool = False,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> EnvironmentProcess:
        """Start ``command`` inside the environment with its stdio bridged.

        Args:
            command: The program, resolved on the guest's PATH.
            args: Its arguments.
            limit: Buffer limit of the stdout / stderr readers; a transport
                that reads long single lines sets it high enough for them.
            tty: Give the command a terminal, for a dialogue that only asks
                its questions on one (a login's confirmation prompt).
            cwd: The guest directory it runs in; the environment's own by
                default. The turns of one command share the microVM, each in
                its own working directory.
            env: What it starts with; the environment's own by default.
        """
        from microsandbox import Stdin

        try:
            handle = await self._sandbox.exec_stream(
                command,
                list(args),
                stdin=Stdin.pipe(),
                cwd=cwd or self.spec.cwd,
                env={"HOME": self.spec.home, **(self.spec.env if env is None else env)},
                tty=tty,
            )
        except Exception as exc:
            raise AgentEnvironmentError(
                t(
                    "intelligences.agent_environment.runtime.exec_failed",
                    command=command,
                    error=exc,
                )
            ) from exc
        return EnvironmentProcess(handle, limit=limit)

    async def write_file(self, path: str, data: bytes) -> None:
        """Write ``data`` at the guest ``path`` through the runtime, never
        through a file of the host's, making the directories it is in."""
        try:
            for parent in reversed(PurePosixPath(path).parents):
                if not await self._sandbox.fs.exists(str(parent)):
                    await self._sandbox.fs.mkdir(str(parent))
            await self._sandbox.fs.write(path, data)
        except Exception as exc:
            raise AgentEnvironmentError(
                t(
                    "intelligences.agent_environment.runtime.file_failed",
                    path=path,
                    error=exc,
                )
            ) from exc

    async def read_file(self, path: str) -> bytes | None:
        """The guest file at ``path``, or None when there is none."""
        try:
            if not await self._sandbox.fs.exists(path):
                return None
            return bytes(await self._sandbox.fs.read(path))
        except Exception as exc:
            raise AgentEnvironmentError(
                t(
                    "intelligences.agent_environment.runtime.file_failed",
                    path=path,
                    error=exc,
                )
            ) from exc

    async def close(self) -> None:
        """Stop the microVM; every process inside it ends with it.

        What the caller takes out of the microVM runs first, while it still
        runs; the microVM is stopped whether that succeeds or not, and its
        error is raised once it is gone. What the caller has to do once the
        microVM is gone runs even when the stop itself is cancelled, because
        the turn's state is on this device either way.
        """
        if self._closed:
            return
        self._closed = True
        try:
            if self._before_stop is not None:
                await self._before_stop(self)
        finally:
            try:
                await self._sandbox.stop(timeout=_STOP_TIMEOUT)
            except Exception:
                with suppress(Exception):
                    await self._sandbox.destroy(force=True)
            finally:
                if self._on_close is not None:
                    self._on_close()


@dataclass(frozen=True, slots=True)
class BuildStep:
    """One shell script of a snapshot build, named for the build log."""

    label: str
    script: str


async def build_snapshot(
    name: Callable[[str], str],
    *,
    dest_dir: Path,
    image: str,
    pull: bool,
    home: str,
    steps: Sequence[BuildStep],
    nameservers: Iterable[str],
    memory_mib: int,
    cpus: int,
    on_line: Callable[[str], None],
) -> Path:
    """Run ``steps`` on ``image`` and keep the result as a snapshot.

    The build sandbox has all egress open: it fetches packages from wherever
    they live, and nothing of the user's is mounted into it. Every line the
    steps print goes to ``on_line``. The sandbox is removed whether the
    build succeeds or not; only the snapshot under ``dest_dir`` remains.

    Args:
        name: Names the snapshot from the config digest of the image the
            sandbox was actually created from ("" when the runtime holds
            no such image, as for one it just pulled and cannot describe).
            A reference is read once, here, by the runtime; naming from the
            same reading keeps the name true to the content when the
            reference is re-tagged by a concurrent ``image load``.
        pull: Fetch ``image`` from its registry when this device lacks it.
            False for an image the user loaded here: a local reference
            names nothing anywhere else, and asking a registry about it
            would fail slowly instead of not at all.

    Raises:
        AgentEnvironmentError: When the sandbox cannot start, a step exits
            non-zero, or the snapshot cannot be written.
    """
    from microsandbox import PullPolicy, Sandbox, Snapshot

    network = EnvironmentNetwork(
        unrestricted=True,
        domains=(),
        host_ports=(),
        local_network=False,
        nameservers=tuple(nameservers),
    )
    try:
        with _anonymous_registry():
            sandbox = await Sandbox.create(
                _BUILD_NAME,
                image=image,
                pull_policy=PullPolicy.IF_MISSING if pull else PullPolicy.NEVER,
                replace=True,
                memory=memory_mib,
                cpus=cpus,
                workdir="/",
                network=_network_of(network),
            )
    except Exception as exc:
        raise AgentEnvironmentError(
            _start_failure(
                build=True,
                error=exc,
                memory_mib=memory_mib,
                cpus=cpus,
            )
        ) from exc
    try:
        snapshot_name = name(await _built_from(Sandbox))
        await _ipv4_only(sandbox)
        for step in steps:
            on_line(f"[{step.label}]")
            handle = await sandbox.exec_stream(
                "sh",
                ["-ec", step.script],
                env={"HOME": home, "DEBIAN_FRONTEND": "noninteractive"},
            )
            process = EnvironmentProcess(handle, limit=_BUILD_LINE_LIMIT)
            await _relay_lines(process, on_line)
            code = await process.wait()
            if code != 0:
                raise AgentEnvironmentError(
                    t(
                        "intelligences.agent_environment.runtime.build_step_failed",
                        step=step.label,
                        code=code,
                    )
                )
        await sandbox.stop(timeout=_STOP_TIMEOUT)
        snapshot = await Snapshot.create(
            snapshot_name,
            from_sandbox=_BUILD_NAME,
            dest_dir=str(dest_dir),
            force=True,
        )
        return Path(snapshot.path)
    except AgentEnvironmentError:
        raise
    except Exception as exc:
        raise AgentEnvironmentError(
            t("intelligences.agent_environment.runtime.build_failed", error=exc)
        ) from exc
    finally:
        with suppress(Exception):
            await (await Sandbox.get(_BUILD_NAME)).destroy(force=True)


async def _built_from(sandbox_api: Any) -> str:
    """The config digest of the image the build sandbox was created from.

    The runtime records the manifest it resolved the reference to; the
    config digest is read from the image that manifest belongs to.
    """
    config = json.loads((await sandbox_api.get(_BUILD_NAME)).config_json)
    manifest = str(config.get("manifest_digest") or "")
    for image in await _images():
        if manifest and image.manifest_digest == manifest:
            return image.digest
    return ""


@contextmanager
def _anonymous_registry() -> Iterator[None]:
    """Pull the base image without the Docker client's configuration.

    The runtime reads ``~/.docker/config.json`` the way the Docker client
    does and runs the credential helper it names. Docker Desktop's helper
    blocks for as long as Desktop is unwell, and a build with it, silently.
    The base image needs no credentials, and Docker Desktop's state is no
    setting of GuildBotics', so the pull sees an empty configuration
    directory instead of the user's.
    """
    previous = os.environ.get("DOCKER_CONFIG")
    with tempfile.TemporaryDirectory(prefix="guildbotics-registry-") as empty:
        os.environ["DOCKER_CONFIG"] = empty
        try:
            yield
        finally:
            if previous is None:
                del os.environ["DOCKER_CONFIG"]
            else:
                os.environ["DOCKER_CONFIG"] = previous


async def _images() -> tuple[ImageInfo, ...]:
    from microsandbox import Image

    images = []
    # The stub spells the return type as the method that shadows it.
    for handle in cast("list[Any]", await Image.list()):
        detail = await handle.inspect()
        if detail.config is None:
            continue
        images.append(
            ImageInfo(
                handle.reference,
                detail.config.digest,
                handle.architecture or "",
                handle.size_bytes,
                handle.manifest_digest or "",
            )
        )
    return tuple(images)


def list_images() -> tuple[ImageInfo, ...]:
    """Every image this device's runtime holds, by reference.

    Synchronous because the device's status is read synchronously wherever
    it is asked for (the CLI, an API thread, a turn about to start), and
    what is read is local metadata.

    Raises:
        AgentEnvironmentError: When the runtime cannot enumerate its store.
    """
    try:
        return _run_sync(_images())
    except Exception as exc:
        raise AgentEnvironmentError(
            t("intelligences.agent_environment.runtime.images_failed", error=exc)
        ) from exc


def archive_architecture(archive: Path) -> str:
    """The CPU architecture an image archive was built for, or "" if unsaid.

    Read from the archive itself -- the image configuration of a ``docker
    save`` archive (``manifest.json``) or of an OCI layout (``index.json``,
    whose first manifest is the one taken) -- because the runtime records a
    loaded archive as this device's architecture whatever it holds.

    Raises:
        AgentEnvironmentError: When the archive cannot be read.
    """
    try:
        with tarfile.open(archive) as tar:

            def read(name: str) -> Any:
                member = tar.extractfile(name)
                if member is None:
                    raise KeyError(name)
                with member:
                    return json.load(member)

            def blob(digest: str) -> str:
                algorithm, _, hexdigest = digest.partition(":")
                return f"blobs/{algorithm}/{hexdigest}"

            try:
                manifest = read("manifest.json")
            except KeyError:
                manifest = None
            if manifest:
                return str(read(manifest[0]["Config"]).get("architecture", ""))
            descriptor = read("index.json")
            for _ in range(2):  # an index may point at a per-platform index
                manifests = descriptor.get("manifests") or []
                if not manifests:
                    return ""
                platform = manifests[0].get("platform") or {}
                if platform.get("architecture"):
                    return str(platform["architecture"])
                descriptor = read(blob(manifests[0]["digest"]))
                if "config" in descriptor:
                    config = read(blob(descriptor["config"]["digest"]))
                    return str(config.get("architecture", ""))
            return ""
    except (OSError, tarfile.TarError, KeyError, ValueError, TypeError) as exc:
        raise AgentEnvironmentError(
            t(
                "intelligences.agent_environment.runtime.load_failed",
                path=archive,
                error=exc,
            )
        ) from exc


async def load_image(archive: Path, *, tag: str | None = None) -> tuple[ImageInfo, ...]:
    """Load a ``docker save`` / OCI archive into this device's runtime.

    The archive's own tags are kept and ``tag`` is added to the first image;
    what was loaded is returned by reference.

    Raises:
        AgentEnvironmentError: When the archive cannot be read, or holds
            no image.
    """
    from microsandbox import Image

    try:
        handles = cast("list[Any]", await Image.load(str(archive), tag=tag))
    except Exception as exc:
        raise AgentEnvironmentError(
            t(
                "intelligences.agent_environment.runtime.load_failed",
                path=archive,
                error=exc,
            )
        ) from exc
    if not handles:
        raise AgentEnvironmentError(
            t("intelligences.agent_environment.runtime.nothing_loaded", path=archive)
        )
    references = {handle.reference for handle in handles}
    return tuple(image for image in list_images() if image.reference in references)


def _run_sync[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Run ``coroutine`` to completion from synchronous code.

    From a thread that already runs an event loop (a turn starting inside
    the service), the coroutine runs on a loop of its own in another
    thread; ``asyncio.run`` refuses to nest.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


async def remove_snapshot(path: Path) -> None:
    """Delete the snapshot at ``path`` and forget it."""
    from microsandbox import Snapshot

    try:
        await Snapshot.remove(str(path), force=True)
    except Exception as exc:
        raise AgentEnvironmentError(
            t(
                "intelligences.agent_environment.runtime.remove_failed",
                path=path,
                error=exc,
            )
        ) from exc


async def _ipv4_only(sandbox: Any) -> None:
    """Switch off IPv6 in a sandbox that just booted (see ``_IPV4_ONLY``)."""
    try:
        handle = await sandbox.exec_stream("sh", ["-ec", _IPV4_ONLY])
        code = await EnvironmentProcess(handle, limit=_BUILD_LINE_LIMIT).wait()
    except Exception as exc:
        raise AgentEnvironmentError(
            t("intelligences.agent_environment.runtime.ipv4_only_failed", error=exc)
        ) from exc
    if code != 0:
        raise AgentEnvironmentError(
            t(
                "intelligences.agent_environment.runtime.ipv4_only_failed",
                error=t("intelligences.agent_environment.runtime.exit_code", code=code),
            )
        )


async def _relay_lines(
    process: EnvironmentProcess, on_line: Callable[[str], None]
) -> None:
    async def pump(reader: asyncio.StreamReader) -> None:
        while line := await reader.readline():
            on_line(line.decode(errors="replace").rstrip("\r\n"))

    await asyncio.gather(pump(process.stdout), pump(process.stderr))


def _volumes(spec: AgentEnvironmentSpec) -> dict[str, Any]:
    """The SDK volumes for the spec's mounts.

    The host side is bound by its resolved path: the runtime cannot bind
    through a symlinked component (macOS spells its temporary directories
    under `/var`, a link to `/private/var`). The guest side keeps the spelling
    the spec gave it, so what the agent is told is where it is.
    """
    from microsandbox import Volume

    return {
        mount.guest: (
            Volume.tmpfs(size_mib=_SCRATCH_MIB, readonly=mount.readonly)
            if mount.host is None
            else Volume.bind(str(mount.host.resolve()), readonly=mount.readonly)
        )
        for mount in spec.mounts
    }


def _network(spec: AgentEnvironmentSpec) -> Any:
    return _network_of(spec.network)


def _network_of(network: EnvironmentNetwork) -> Any:
    from microsandbox import (
        Action,
        DestGroup,
        Destination,
        Network,
        NetworkPolicy,
        Protocol,
        Rule,
    )
    from microsandbox.types import DnsConfig

    rules: list[Any] = []
    if not network.unrestricted:
        rules.extend(Rule.allow_dns())
        rules.extend(
            Rule.allow(
                destination=Destination.group(DestGroup.HOST),
                protocol=Protocol.TCP,
                port=port,
            )
            for port in network.host_ports
        )
        rules.extend(
            Rule.allow(
                destination=(
                    Destination.domain_suffix(domain[2:])
                    if domain.startswith("*.")
                    else Destination.domain(domain)
                )
            )
            for domain in network.domains
        )
        if network.local_network:
            rules.extend(
                Rule.allow(destination=Destination.group(group))
                for group in (DestGroup.HOST, DestGroup.PRIVATE)
            )
    return Network(
        policy=NetworkPolicy(
            default_egress=Action.ALLOW if network.unrestricted else Action.DENY,
            default_ingress=Action.DENY,
            rules=tuple(rules),
        ),
        dns=DnsConfig(nameservers=network.nameservers),
    )
