"""The Desktop's view of hubs and workspace synchronization.

This is the one module in the application layer that knows synchronization is
Git and that a hub is reached over OpenSSH. Everywhere else -- the runtime, the
routes, the frontend -- names workspaces, hubs, and states, and this module
converts between those names and the two packages that implement them.

It is also the process entry point for the queue itself: the Desktop backend is
what the user has open when they enable synchronization, so it is what installs
the queue and takes it down again across a workspace switch.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from guildbotics.app_api.errors import AppApiError
from guildbotics.app_api.models import (
    DeviceSshKey,
    HubConnection,
    HubStatus,
    HubTarget,
    HubTrustRequest,
    UnsendableChangeModel,
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
from guildbotics.sync import (
    EnrollmentError,
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
)
from guildbotics.utils.fileio import WorkspaceNotConfiguredError, get_workspace_root
from guildbotics.utils.openssh import OpenSshNotFoundError
from guildbotics.workspace.identity import (
    ensure_device_identity,
    ensure_workspace_identity,
    read_workspace_identity,
)

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
)


class WorkspaceSyncService:
    """Hub hosting, workspace enrollment, and the running sync queue."""

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
            return _ssh_key_model(connection.ensure_ssh_key())

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
            enroll(url, _workspace_root())
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
        if (destination / ".guildbotics").exists():
            raise AppApiError(
                "workspace_already_exists",
                "That directory already holds a GuildBotics workspace. "
                "Enable synchronization on it instead of taking a copy.",
                context={"workspace_dir": str(destination)},
                status_code=409,
            )
        with _reporting("sync_clone_failed", "The workspace could not be taken."):
            url = connection.hub_remote_url(location, request.workspace_id)
            clone_workspace(url, destination)
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
            )
        return _status_model(manager.status(), repository.remote_url())

    def activate(self) -> WorkspaceSyncStatus:
        """Start the queue for the selected workspace, if it has a hub."""
        activate_workspace_sync(_workspace_root())
        return self.get_status()

    def deactivate(self) -> bool:
        """Stop the queue, which a workspace switch does before it switches.

        Returns:
            bool: Whether it stopped. A caller that was making room for another
                workspace must not proceed on False -- the old queue still
                holds its repository.
        """
        return deactivate_workspace_sync()

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
            enroll(url, _workspace_root())
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
    endpoint = target.endpoint.strip()
    if not endpoint:
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


def _status_model(status: GitSyncStatus, hub_url: str | None) -> WorkspaceSyncStatus:
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
        last_success_at=status.last_success_at,
        last_error_code=status.last_error_code,
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
