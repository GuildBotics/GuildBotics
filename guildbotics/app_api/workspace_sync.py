"""The Desktop's view of hubs and workspace synchronization.

This is the one module in the application layer that knows synchronization is
Git and that a hub is reached over OpenSSH. Everywhere else -- the runtime, the
routes, the frontend -- names workspaces, hubs, and states, and this module
converts between those names and the two packages that implement them.

It is also one process entry point for the queue itself: the Desktop backend
installs the queue and takes it down again across a workspace switch. The CLI
has separate composition-root paths for ``guildbotics start`` and its one-shot
member synchronization.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from guildbotics.app_api.errors import AppApiError
from guildbotics.app_api.models import (
    DeviceRenameRequest,
    DeviceSshKey,
    HubConnection,
    HubStatus,
    HubTarget,
    HubTrustRequest,
    RejectedChangeModel,
    UnsendableChangeModel,
    WorkspaceDevice,
    WorkspaceDevices,
    WorkspaceLiveState,
    WorkspaceLiveWork,
    WorkspaceServiceOwner,
    WorkspaceSyncCloneRequest,
    WorkspaceSyncEnableRequest,
    WorkspaceSyncPreview,
    WorkspaceSyncStatus,
)
from guildbotics.hub import (
    HostKeyChangedError,
    HubLocation,
    HubUnreachableError,
    InvalidHubEndpointError,
    connection,
    host,
)
from guildbotics.hub.relay import LIVE_EXPIRE_AFTER_SECONDS, ServiceOwner
from guildbotics.hub.relay_client import HubRelayClient, HubRelayClientError
from guildbotics.observability.activity_event_store import ActivityEventStore
from guildbotics.observability.event_types import SYNC_UPDATE_REJECTED
from guildbotics.runtime.live_state import LiveState, LiveStatePort
from guildbotics.runtime.relay_runtime import RelayRuntime
from guildbotics.runtime.service_owner import (
    ServiceOwnerError,
    create_relay_runtime,
)
from guildbotics.runtime.service_owner import (
    prepare_service_owner as prepare_relay_service_owner,
)
from guildbotics.sync import (
    EnrollmentError,
    GitSyncManager,
    GitSyncStatus,
    LocalSyncRepository,
    SyncRepositoryError,
    SyncStillStoppingError,
    UnsendableChange,
    activate_workspace_sync,
    clone_workspace,
    current_sync_manager,
    deactivate_workspace_sync,
    enroll,
    paused_workspace_sync,
    preview_enrollment,
    synchronize_once,
)
from guildbotics.utils.fileio import WorkspaceNotConfiguredError, get_workspace_root
from guildbotics.utils.openssh import OpenSshNotFoundError
from guildbotics.utils.sync_lock import SyncRepositoryBusyError
from guildbotics.workspace.identity import (
    DeviceRecord,
    ensure_device_identity,
    ensure_workspace_identity,
    list_device_records,
    publish_device_record,
    read_workspace_identity,
    set_device_display_name,
)

#: How long a refresh waits for the repository. It is a courtesy before an
#: operation, not the operation itself, so it gives up quickly rather than
#: making the user wait on whatever else is using the repository.
REFRESH_LOCK_TIMEOUT_SECONDS = 2.0

#: The failures a hub operation reports back to the user rather than crashing
#: on. They are all the boundary's own exception types: Git and OpenSSH
#: failures are converted before they get here, so this list stays a statement
#: about what the user can be told rather than a net for anything that goes
#: wrong underneath.
_HUB_FAILURES = (
    HubUnreachableError,
    InvalidHubEndpointError,
    OpenSshNotFoundError,
    host.HubError,
    EnrollmentError,
    SyncRepositoryError,
    HubRelayClientError,
)


class WorkspaceSyncService:
    """Hub hosting, workspace enrollment, and the running sync queue."""

    def __init__(
        self,
        *,
        on_live_publisher: Callable[[LiveStatePort | None], None] | None = None,
        on_owner_transfer: Callable[[str], object] | None = None,
    ) -> None:
        self._relay_runtime: RelayRuntime | None = None
        self._live_states: dict[tuple[str, str], LiveState] = {}
        self._live_error_code: str | None = None
        self._on_live_publisher = on_live_publisher
        self._on_owner_transfer = on_owner_transfer

    # -- Hub hosting --------------------------------------------------------

    def get_hub(self) -> HubStatus:
        """Report whether this machine hosts a hub."""
        settings = host.read_hub()
        if settings is None:
            return HubStatus(hosted=False, hub_root=host.hub_root())
        return HubStatus(
            hosted=True,
            hub_root=host.hub_root(),
            hub_id=settings.hub_id,
            created_at=settings.created_at,
            ssh_endpoint=settings.ssh_endpoint,
            workspace_ids=host.list_workspace_ids(),
        )

    def create_hub(self) -> HubStatus:
        """Make this machine a hub, or report the one it already hosts."""
        with _reporting("hub_create_failed", "This machine could not be made a hub."):
            host.create_hub()
        return self.get_hub()

    # -- Reaching a hub -----------------------------------------------------

    def inspect_hub(self, target: HubTarget) -> HubConnection:
        """Report what this device can see of a hub before connecting to it.

        A hub that is not yet trusted is described rather than contacted for
        its workspace list: the user confirms the host key fingerprint first.
        """
        location = _location(target)
        if location.endpoint is None:
            return HubConnection(
                is_local=True, workspace_ids=self._workspace_ids(location)
            )
        with _reporting("hub_unreachable", f"{location.label} could not be reached."):
            host_key = connection.probe_host_key(location.endpoint)
        return HubConnection(
            endpoint=location.endpoint.target,
            is_local=False,
            host_key_fingerprints=list(host_key.fingerprints),
            host_key_trusted=host_key.trusted,
            host_key_changed=host_key.changed,
            workspace_ids=(self._workspace_ids(location) if host_key.trusted else []),
        )

    def trust_hub(self, request: HubTrustRequest) -> HubConnection:
        """Record the host key the user confirmed for a hub machine."""
        target = HubTarget(endpoint=request.endpoint)
        location = _location(target)
        if location.endpoint is None:
            return self.inspect_hub(target)
        if not request.fingerprint:
            raise AppApiError(
                "host_key_not_confirmed",
                "Name the fingerprint that was confirmed before trusting a hub.",
                status_code=400,
            )
        try:
            with _reporting(
                "hub_unreachable", f"{location.label} could not be reached."
            ):
                connection.trust_host_key(location.endpoint, request.fingerprint)
        except HostKeyChangedError as exc:
            raise AppApiError(
                "host_key_changed",
                f"{location.label} offered a different host key than the one "
                "confirmed. Check the fingerprint again before trusting it.",
                context={"detail": str(exc)},
                status_code=409,
            ) from exc
        return self.inspect_hub(target)

    def get_ssh_key(self) -> DeviceSshKey:
        """Report this device's public key, without creating one."""
        return _ssh_key_model(connection.read_ssh_key())

    def create_ssh_key(self) -> DeviceSshKey:
        """Return this device's public key, creating a key pair on first use."""
        with _reporting(
            "ssh_key_failed", "An SSH key could not be created on this device."
        ):
            key = connection.ensure_ssh_key()
            root = _workspace_root()
            if root is not None:
                publish_device_record(
                    root,
                    ssh_public_key_fingerprint=key.fingerprint,
                )
            return _ssh_key_model(key)

    # -- Devices sharing the workspace --------------------------------------

    def get_devices(self) -> WorkspaceDevices:
        """List the machines this workspace has been shared with."""
        device_id = ensure_device_identity().device_id
        return WorkspaceDevices(
            device_id=device_id,
            devices=[
                _device_model(
                    record, device_id, self._live_for_device(record.device_id)
                )
                for record in _device_records()
            ],
        )

    def get_live_states(self) -> list[WorkspaceLiveState]:
        """Return Hub-relayed current work without waiting for Git history."""
        return [
            _live_model(state)
            for state in sorted(
                self._live_states.values(),
                key=lambda state: (state.device_id, state.publisher_id),
            )
        ]

    def get_service_owner(self) -> WorkspaceServiceOwner:
        """Read the persistent Hub owner, without changing it."""
        workspace_id = _workspace_id()
        if workspace_id is None:
            raise AppApiError(
                "workspace_not_configured",
                "Select a workspace before reading its service owner.",
                status_code=409,
            )
        repository = _repository()
        remote_url = repository.remote_url() if repository is not None else None
        if not remote_url:
            return WorkspaceServiceOwner(workspace_id=workspace_id)
        with _reporting("hub_unreachable", "The service owner could not be read."):
            client = self._relay_client(workspace_id, remote_url)
            try:
                owner = client.owner_get()
            finally:
                client.close()
        return _owner_model(owner, workspace_id, ensure_device_identity().device_id)

    def transfer_service_owner(self, device_id: str) -> WorkspaceServiceOwner:
        """Move ownership only through the explicit Desktop action."""
        workspace_id = _workspace_id()
        repository = _repository()
        remote_url = repository.remote_url() if repository is not None else None
        if workspace_id is None or not remote_url:
            raise AppApiError(
                "workspace_sync_disabled",
                "Enable workspace synchronization before transferring service ownership.",
                status_code=409,
            )
        current_device_id = ensure_device_identity().device_id
        if device_id != current_device_id:
            raise AppApiError(
                "service_owner_target_invalid",
                "The Desktop takeover can target only this device.",
                status_code=400,
            )
        with _reporting("hub_unreachable", "The service owner could not be changed."):
            client = self._relay_client(workspace_id, remote_url)
            try:
                previous = client.owner_get()
                owner = client.owner_transfer(device_id)
                if (
                    previous is not None
                    and previous.owner_device_id != owner.owner_device_id
                    and self._on_owner_transfer is not None
                ):
                    self._on_owner_transfer(previous.owner_device_id)
            finally:
                client.close()
        return _owner_model(owner, workspace_id, current_device_id)

    def rename_device(self, request: DeviceRenameRequest) -> WorkspaceDevices:
        """Rename this machine and publish the new name to the other devices.

        Only this machine can be renamed: the name belongs to the device, so
        every other entry is edited on the machine it describes.
        """
        try:
            set_device_display_name(request.display_name)
        except ValueError as exc:
            raise AppApiError("device_name_invalid", str(exc), status_code=400) from exc
        root = _workspace_root()
        if root is not None:
            publish_device_record(root, ssh_public_key_fingerprint=_key_fingerprint())
        return self.get_devices()

    # -- Enrolling a workspace ----------------------------------------------

    def preview(self, request: WorkspaceSyncEnableRequest) -> WorkspaceSyncPreview:
        """Report what enabling synchronization would do, changing nothing shared.

        Only joining is previewed. Registering gives the hub its first content,
        so there is nothing on the other side to show -- and the caller already
        knows which it is from the hub's own workspace list.
        """
        location = _location(request.hub)
        target = request.workspace_id or _workspace_id() or ""
        with _reporting("sync_preview_failed", "The hub could not be compared."):
            if not target or target not in self._workspace_ids(location):
                raise AppApiError(
                    "sync_preview_unavailable",
                    f"{location.label} does not hold this workspace, so enabling "
                    "synchronization would register it rather than join it.",
                    status_code=409,
                )
            url = connection.hub_remote_url(location, target)
            with self._paused():
                preview = preview_enrollment(url, _workspace_root())
        return WorkspaceSyncPreview(
            hub_workspace_id=preview.hub_workspace_id,
            workspace_id=preview.workspace_id,
            mode=preview.mode,
            hub_only=list(preview.hub_only),
            device_only=list(preview.device_only),
            differing=list(preview.differing),
            unsendable_changes=_unsendable(preview.unsendable),
        )

    def enable(self, request: WorkspaceSyncEnableRequest) -> WorkspaceSyncStatus:
        """Connect the selected workspace to a hub and start synchronizing it."""
        location = _location(request.hub)
        url = self._remote_url(location, request.workspace_id, create=True)
        with (
            _reporting("sync_enable_failed", "Synchronization could not be enabled."),
            self._paused(),
        ):
            enroll(
                url,
                _workspace_root(),
                ssh_public_key_fingerprint=_key_fingerprint(),
            )
        return self.activate()

    def clone(self, request: WorkspaceSyncCloneRequest) -> Path:
        """Create a new workspace root from a hub's content.

        Returns:
            Path: The new workspace root, for the caller to switch to. Nothing
                is started here, because the queue belongs to whichever
                workspace ends up selected.
        """
        location = _location(request.hub)
        destination = request.workspace_dir.expanduser().resolve()
        # Not "does .guildbotics exist": selecting this folder is what opened
        # it as a workspace, and that alone writes this device's diagnostics
        # under local/. Refusing on that refuses every folder the user picked.
        if LocalSyncRepository(destination).holds_shared_content:
            raise AppApiError(
                "workspace_already_exists",
                "That directory already holds a GuildBotics workspace. "
                "Enable synchronization on it instead of taking a copy.",
                context={"workspace_dir": str(destination)},
                status_code=409,
            )
        with _reporting("sync_clone_failed", "The workspace could not be taken."):
            url = connection.hub_remote_url(location, request.workspace_id)
            clone_workspace(
                url,
                destination,
                ssh_public_key_fingerprint=_key_fingerprint(),
            )
        return destination

    # -- The running queue --------------------------------------------------

    def get_status(self) -> WorkspaceSyncStatus:
        """Report the selected workspace's synchronization state."""
        manager = current_sync_manager()
        repository = _repository()
        if repository is None or not repository.has_remote():
            return WorkspaceSyncStatus(
                enabled=False,
                workspace_id=_workspace_id(),
                device_id=ensure_device_identity().device_id,
                live_error_code=self._live_error_code,
            )
        if manager is None:
            # Enabled but not running yet, which is what a process that has not
            # activated this workspace sees.
            return WorkspaceSyncStatus(
                enabled=True,
                workspace_id=_workspace_id(),
                device_id=ensure_device_identity().device_id,
                hub_url=repository.remote_url(),
                state="idle",
                rejected_changes=_rejected(repository),
                live_error_code=self._live_error_code,
            )
        return _status_model(
            manager.status(),
            repository.remote_url(),
            _rejected(repository),
            live_error_code=self._live_error_code,
        )

    def discard_rejection(self, rejection_id: str) -> WorkspaceSyncStatus:
        """Drop one displaced commit because the user said they are done with it.

        The user is never asked to do this, and nothing does it for them: the
        ref is the only copy, held only here. It exists so the warning can end
        without a second record of what the user has already looked at.
        """
        repository = _repository()
        if repository is None:
            raise AppApiError(
                "workspace_not_configured",
                "Select a workspace before discarding a rejected change.",
                status_code=409,
            )
        with _reporting(
            "sync_discard_failed", "The rejected change could not be discarded."
        ):
            repository.discard_rejected(rejection_id)
        return self.get_status()

    def activate(self) -> WorkspaceSyncStatus:
        """Start the queue for the selected workspace, if it has a hub."""
        root = _workspace_root()
        manager = activate_workspace_sync(root)
        if manager is not None:
            # Activation is also the repair point for a key created or replaced
            # outside the current Desktop process. Publishing through the normal
            # device-record writer keeps every machine's fingerprint current.
            publish_device_record(root, ssh_public_key_fingerprint=_key_fingerprint())
        self._restart_relay(manager, root)
        return self.get_status()

    def prepare_service_owner(self) -> Callable[[], bool | None] | None:
        """Settle sync and require this device to own the service.

        The relay runtime is already the process-wide live-state connection, so
        the owner command reuses its client. A missing Hub answer is a start
        failure; later owner probes return ``None`` and only block new work.
        """
        manager = current_sync_manager()
        if manager is None:
            return None
        status = manager.synchronize()
        if status.state != "idle" or status.last_error_code is not None:
            detail = status.last_error_code or status.state
            raise AppApiError(
                "service_start_sync_failed",
                "The service cannot start because workspace synchronization failed.",
                context={"detail": detail},
                status_code=409,
            )
        runtime = self._relay_runtime
        if runtime is None:
            self._restart_relay(manager, _workspace_root())
            runtime = self._relay_runtime
        if runtime is None:
            raise AppApiError(
                "hub_unreachable",
                "The service cannot start because the Hub is unavailable.",
                status_code=409,
            )
        device_id = ensure_device_identity().device_id
        try:
            prepare_relay_service_owner(runtime, device_id, start_runtime=False)
        except ServiceOwnerError as exc:
            context = (
                {"owner_device_id": exc.owner_device_id}
                if exc.owner_device_id is not None
                else None
            )
            raise AppApiError(
                exc.code,
                str(exc),
                context=context,
                status_code=409,
            ) from exc
        return runtime.check_owner

    def deactivate(self) -> bool:
        """Stop the queue, which a workspace switch does before it switches.

        Returns:
            bool: Whether it stopped. A caller that was making room for another
                workspace must not proceed on False -- the old queue still
                holds its repository.
        """
        stopped = deactivate_workspace_sync()
        if stopped:
            self._stop_relay()
        return stopped

    def retry(self) -> WorkspaceSyncStatus:
        """Try again after an unreachable hub or repaired shared data."""
        manager = current_sync_manager()
        if manager is None:
            return self.activate()
        with _reporting("sync_retry_failed", "Synchronization could not be retried."):
            manager.resume()
        return self.get_status()

    def change_hub(self, request: WorkspaceSyncEnableRequest) -> WorkspaceSyncStatus:
        """Point the selected workspace at a different hub.

        This is how a device reconnects after a hub is rebuilt elsewhere. The
        content it holds is not discarded: whatever the new hub already has
        wins, and anything only this device has is sent to it.
        """
        location = _location(request.hub)
        url = self._remote_url(location, request.workspace_id, create=True)
        with (
            _reporting("sync_enable_failed", "The hub could not be changed."),
            self._paused(),
        ):
            enroll(
                url,
                _workspace_root(),
                ssh_public_key_fingerprint=_key_fingerprint(),
            )
        return self.activate()

    # -- Internals ----------------------------------------------------------

    @contextmanager
    def _paused(self) -> Iterator[None]:
        """Hold the workspace's repository alone, reporting a busy queue.

        The exclusion itself belongs to the activation boundary, which is
        process-wide: this service is instantiated more than once, so a lock
        held here would not keep a workspace switch or another request out.

        Raises:
            AppApiError: When the queue is still finishing and cannot be
                stopped. Proceeding would put a second worker on the same
                repository.
        """
        self._stop_relay()
        try:
            with paused_workspace_sync(_workspace_root()):
                yield
        except SyncStillStoppingError as exc:
            raise AppApiError(
                "workspace_sync_busy",
                "Synchronization is still finishing its last cycle. "
                "Try again in a moment.",
                context={"detail": str(exc)},
                status_code=409,
            ) from exc
        finally:
            manager = current_sync_manager()
            self._restart_relay(manager, _workspace_root())

    def _restart_relay(
        self,
        manager: GitSyncManager | None,
        workspace_root: Path | None,
    ) -> None:
        self._stop_relay()
        if manager is None or workspace_root is None:
            return
        repository = LocalSyncRepository(workspace_root)
        remote_url = repository.remote_url()
        if not remote_url:
            return
        runtime: RelayRuntime | None = None
        try:
            status = manager.status()
            runtime = create_relay_runtime(
                remote_url,
                status.workspace_id,
                ensure_device_identity().device_id,
                on_live_state=self._receive_live_state,
                on_live_expired=self._receive_live_expired,
                on_head_updated=manager.wake,
                on_relay_error=self._receive_relay_error,
            )
            runtime.start()
        except (ServiceOwnerError, OSError, ValueError):
            if runtime is not None:
                runtime.stop()
            return
        self._relay_runtime = runtime
        if self._on_live_publisher is not None:
            self._on_live_publisher(runtime.publisher)

    def _stop_relay(self) -> None:
        runtime = self._relay_runtime
        self._relay_runtime = None
        self._live_states.clear()
        self._live_error_code = None
        if runtime is not None:
            runtime.stop()
        if self._on_live_publisher is not None:
            self._on_live_publisher(None)

    def _receive_live_state(self, state: LiveState) -> None:
        self._live_error_code = None
        self._live_states[(state.device_id, state.publisher_id)] = state

    def _receive_live_expired(
        self, device_id: str, publisher_id: str, observed_at: str
    ) -> None:
        # Expiry is a deletion event, not a new durable snapshot. Retaining an
        # empty expired state here would keep a stale offline warning in the
        # Activity/device views until the backend restarted, even though the
        # Hub has already removed the relay file.
        del observed_at
        self._live_states.pop((device_id, publisher_id), None)

    def _receive_relay_error(self, code: str) -> None:
        self._live_error_code = code

    def _live_for_device(self, device_id: str) -> LiveState | None:
        states = [
            state
            for state in self._live_states.values()
            if state.device_id == device_id
        ]
        if not states:
            return None
        return max(states, key=lambda state: state.observed_at)

    def refresh(self) -> None:
        """Bring the shared files up to date, best effort.

        Used before an operation that decides something from a shared file. A
        hub that cannot be reached, or a repository another process is holding,
        leaves the decision on what this device already has -- which is the
        state it would have been made from anyway.
        """
        with suppress(*_HUB_FAILURES, SyncRepositoryBusyError):
            synchronize_once(_workspace_root(), timeout=REFRESH_LOCK_TIMEOUT_SECONDS)

    def hub_target(self) -> tuple[HubLocation, str] | None:
        """Return where this workspace's hub is, or None when it has none.

        The synchronization remote is the one record of which machine holds
        this workspace, so everything that has to reach that machine -- Git
        aside -- asks here rather than resolving a hub of its own.
        """
        workspace_id = _workspace_id()
        repository = _repository()
        remote_url = repository.remote_url() if repository is not None else None
        if workspace_id is None or not remote_url:
            return None
        with _reporting("hub_unreachable", "The hub of this workspace is unknown."):
            return connection.location_from_remote_url(remote_url, workspace_id), (
                workspace_id
            )

    def _relay_client(self, workspace_id: str, remote_url: str) -> HubRelayClient:
        return HubRelayClient(
            connection.location_from_remote_url(remote_url, workspace_id),
            workspace_id,
            ensure_device_identity().device_id,
            str(uuid4()),
        )

    def _remote_url(
        self, location: HubLocation, workspace_id: str, *, create: bool
    ) -> str:
        """Resolve which hub repository this workspace uses, creating it if asked.

        An empty ``workspace_id`` means this workspace keeps its own identifier
        and the hub gains a repository for it. A named one means joining what
        the hub already holds, so nothing is created for it.
        """
        target = workspace_id or _own_workspace_id()
        if create and not workspace_id:
            with _reporting(
                "hub_register_failed",
                f"{location.label} did not accept the workspace.",
            ):
                connection.create_hub_workspace(location, target)
        with _reporting("hub_unreachable", f"{location.label} could not be reached."):
            return connection.hub_remote_url(location, target)

    def _workspace_ids(self, location: HubLocation) -> list[str]:
        with _reporting("hub_unreachable", f"{location.label} could not be reached."):
            return connection.list_hub_workspaces(location)


def _location(target: HubTarget) -> HubLocation:
    """Resolve which hub an operation is about.

    An empty address means the hub on this machine, so a machine that hosts
    none is refused here rather than further in. Every hub operation resolves
    its location first, and the ones that follow have side effects: registering
    mints this workspace's identifier before it discovers there is nothing to
    register with.
    """
    endpoint = target.endpoint.strip()
    if not endpoint:
        if host.read_hub() is None:
            raise AppApiError(
                "hub_not_hosted",
                "This machine hosts no hub. Give the address of the machine "
                "that does, as user@host, or make this one a hub first.",
                status_code=409,
            )
        return HubLocation()
    try:
        return HubLocation(endpoint=connection.parse_hub_endpoint(endpoint))
    except InvalidHubEndpointError as exc:
        raise AppApiError(
            "invalid_hub_endpoint",
            str(exc),
            context={"endpoint": endpoint},
            status_code=400,
        ) from exc


def _key_fingerprint() -> str | None:
    key = connection.read_ssh_key()
    return None if key is None else key.fingerprint


def _repository() -> LocalSyncRepository | None:
    try:
        repository = LocalSyncRepository(_workspace_root())
    except WorkspaceNotConfiguredError:
        return None
    return repository if repository.initialized else None


def _workspace_root() -> Path | None:
    try:
        return get_workspace_root()
    except WorkspaceNotConfiguredError:
        return None


def _workspace_id() -> str | None:
    """Return this workspace's identifier, without creating one."""
    root = _workspace_root()
    if root is None:
        return None
    identity = read_workspace_identity(root)
    return identity.workspace_id if identity is not None else None


def _own_workspace_id() -> str:
    """Return this workspace's identifier, minting it on first connection.

    A workspace is given an identifier the first time it meets a hub, and never
    again: every later machine takes the one it receives.
    """
    root = _workspace_root()
    if root is None:
        raise AppApiError(
            "workspace_not_configured",
            "Select a workspace before connecting it to a hub.",
            status_code=409,
        )
    with _reporting(
        "sync_enable_failed", "This workspace could not be given an identifier."
    ):
        return ensure_workspace_identity(root).workspace_id


def _device_records() -> list[DeviceRecord]:
    """Return the shared device records, or none when no workspace is selected."""
    root = _workspace_root()
    return list_device_records(root) if root is not None else []


def _rejected(repository: LocalSyncRepository) -> list[RejectedChangeModel]:
    """List what this device is still holding for the user to look at.

    Which refs exist is read from the repository rather than remembered, so it
    is right after a restart and after a discard. Which files each one holds
    back comes from the event recorded when it happened: it was decided against
    the hub's version at that moment, and the commit cannot say it afterwards.

    The events are only read when there is a ref to describe, which is almost
    never -- so the usual answer costs one ``for-each-ref`` and nothing else.
    """
    held = repository.list_rejected()
    if not held:
        return []
    recorded = _recorded_rejections()
    return [
        RejectedChangeModel(
            rejection_id=change.rejection_id,
            occurred_at=recorded.get(change.rejection_id, ("", []))[0]
            or change.occurred_at,
            paths=recorded.get(change.rejection_id, ("", []))[1],
        )
        for change in held
    ]


def _recorded_rejections() -> dict[str, tuple[str, list[str]]]:
    """Return ``rejection_id -> (time, paths)`` from this workspace's events.

    A ref whose event is missing still appears in the list: the identifier and
    the ref are what the recovery procedure needs, and saying nothing about a
    change that is being held would be worse than saying less about it.
    """
    recorded: dict[str, tuple[str, list[str]]] = {}
    for event in ActivityEventStore().list_between(
        datetime.min.replace(tzinfo=UTC), datetime.max.replace(tzinfo=UTC)
    ):
        if event.get("kind") != SYNC_UPDATE_REJECTED:
            continue
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        rejection_id = str(payload.get("rejection_id") or "")
        if not rejection_id:
            continue
        paths = payload.get("paths")
        recorded[rejection_id] = (
            str(event.get("occurred_at") or ""),
            [str(path) for path in paths] if isinstance(paths, list) else [],
        )
    return recorded


def _unsendable(changes: Sequence[UnsendableChange]) -> list[UnsendableChangeModel]:
    return [
        UnsendableChangeModel(path=change.path, reason=change.reason)
        for change in changes
    ]


def _ssh_key_model(key: connection.HubSshKey | None) -> DeviceSshKey:
    if key is None:
        return DeviceSshKey(exists=False)
    return DeviceSshKey(
        exists=True,
        path=key.path,
        public_key=key.public_key,
        fingerprint=key.fingerprint,
    )


LIVE_DELAY_SECONDS = 15.0


def _device_model(
    record: DeviceRecord, self_device_id: str, live: LiveState | None
) -> WorkspaceDevice:
    status = _live_status(live.observed_at) if live is not None else "unknown"
    return WorkspaceDevice(
        device_id=record.device_id,
        display_name=record.display_name,
        os=record.os,
        joined_at=record.joined_at,
        status=record.status,
        ssh_public_key_fingerprint=record.ssh_public_key_fingerprint or "",
        is_self=record.device_id == self_device_id,
        live_status=status,
        last_seen_at=live.observed_at if live is not None else None,
    )


def _live_model(state: LiveState) -> WorkspaceLiveState:
    status = _live_status(state.observed_at)
    return WorkspaceLiveState(
        schema_version=state.schema_version,
        workspace_id=state.workspace_id,
        device_id=state.device_id,
        publisher_id=state.publisher_id,
        observed_at=state.observed_at,
        status=status,
        works=[
            WorkspaceLiveWork(
                work_id=work.work_id,
                run_id=work.run_id,
                member_id=work.member_id,
                workflow_name=work.workflow_name,
                presentation=(
                    work.presentation.model_dump(mode="json")
                    if work.presentation is not None
                    else None
                ),
                retry_at=work.retry_at,
            )
            for work in state.works
        ]
        if status != "expired"
        else [],
    )


def _live_status(observed_at: str) -> Literal["online", "delayed", "expired"]:
    try:
        timestamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
    except ValueError:
        return "expired"
    age = (datetime.now(UTC) - timestamp.astimezone(UTC)).total_seconds()
    if age > LIVE_EXPIRE_AFTER_SECONDS:
        return "expired"
    if age > LIVE_DELAY_SECONDS:
        return "delayed"
    return "online"


def _owner_model(
    owner: ServiceOwner | None, workspace_id: str, self_device_id: str
) -> WorkspaceServiceOwner:
    return WorkspaceServiceOwner(
        workspace_id=workspace_id,
        owner_device_id=owner.owner_device_id if owner is not None else None,
        updated_at=owner.updated_at if owner is not None else None,
        is_self=owner is not None and owner.owner_device_id == self_device_id,
    )


def _status_model(
    status: GitSyncStatus,
    hub_url: str | None,
    rejected: list[RejectedChangeModel],
    *,
    live_error_code: str | None = None,
) -> WorkspaceSyncStatus:
    return WorkspaceSyncStatus(
        enabled=True,
        workspace_id=status.workspace_id,
        device_id=ensure_device_identity().device_id,
        hub_url=hub_url,
        state=status.state,
        local_head=status.local_head,
        remote_head=status.remote_head,
        ahead_count=status.ahead_count,
        behind_count=status.behind_count,
        unsendable_changes=_unsendable(status.invalid_paths),
        rejected_changes=rejected,
        last_success_at=status.last_success_at,
        last_error_code=status.last_error_code,
        live_error_code=live_error_code,
    )


@contextmanager
def _reporting(code: str, message: str) -> Iterator[None]:
    """Turn a hub or repository failure into an answer the Desktop can show.

    These operations fail for ordinary reasons -- the hub is off, a key is not
    registered yet, a directory is not writable -- so they belong in the
    response rather than in a stack trace.
    """
    try:
        yield
    except _HUB_FAILURES as exc:
        raise AppApiError(
            code, message, context={"detail": str(exc)}, status_code=409
        ) from exc
