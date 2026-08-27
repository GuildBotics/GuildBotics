"""The Desktop's view of which devices hold which secret values.

The screen this serves is a list of key names and states and three buttons. It
never shows a value and never receives one: the transfer runs between this
machine's OS secret store and the hub machine's, and what crosses this layer is
a key name, a generation, and what happened.

The hub is asked for its side on every read. A key can be "missing here" for
two quite different reasons -- the hub has it and this machine has not fetched
it, or nobody has it at all -- and only the hub can tell them apart. When it
cannot be reached the local view is still returned, with the reason attached,
because the states this device knows on its own remain true.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from guildbotics.app_api.errors import AppApiError
from guildbotics.app_api.models import (
    SecretStoreState,
    SecretTransferRequest,
    SecretTransferResponse,
    SecretTransferResult,
    WorkspaceSecrets,
    WorkspaceSecretState,
)
from guildbotics.app_api.workspace_sync import WorkspaceSyncService
from guildbotics.hub import HubUnreachableError, host
from guildbotics.hub.secret_host import HubSecretError
from guildbotics.secrets import (
    HUB_BEHIND,
    UNCONFIRMED,
    HubSecretIndex,
    SecretPublishError,
    SecretTransfer,
    SecretTransferOutcome,
    bulk_fetch_keys,
    bulk_send_keys,
    can_fetch,
    can_send,
    hub_secret_client,
    transfer_status,
)
from guildbotics.utils.fileio import (
    WorkspaceNotConfiguredError,
    get_workspace_config_dir,
)
from guildbotics.utils.keychain import SecretStoreError
from guildbotics.utils.openssh import OpenSshNotFoundError
from guildbotics.utils.secret_store import (
    KeyringSecretStore,
    SecretKeyStatus,
    keyring_status,
)

#: The failures that describe a hub rather than break the request. They are the
#: same shape as the synchronization service's list and exist for the same
#: reason: an unreachable hub is the most ordinary thing that can happen here,
#: and the screen has somewhere to say so.
_HUB_FAILURES = (
    HubUnreachableError,
    OpenSshNotFoundError,
    HubSecretError,
    host.HubError,
)


class WorkspaceSecretService:
    """Secret states and the two explicit transfers, for one workspace."""

    def __init__(self, sync_service: WorkspaceSyncService):
        self._sync = sync_service

    def get_secrets(self) -> WorkspaceSecrets:
        """Report every key this workspace knows, on both machines."""
        store = self._store()
        if store is None:
            return WorkspaceSecrets()
        transfer = self._transfer(store)
        if transfer is None:
            return _secrets(store, None, enabled=False)
        try:
            return _secrets(store, transfer.hub_index(), enabled=True)
        except _HUB_FAILURES as exc:
            return _secrets(store, None, enabled=True, error_code=_hub_error_code(exc))

    def send(self, request: SecretTransferRequest) -> SecretTransferResponse:
        """Hand the hub this device's value for each named key.

        The shared files are refreshed first: which generation this device is
        sending from is read out of ``config/secrets.yml``, and a device that
        has been offline would otherwise send from an old one.
        """
        self._sync.refresh()
        transfer, store = self._require_transfer()
        with _reporting("secret_send_failed"):
            try:
                outcomes = (
                    transfer.send(request.keys)
                    if request.keys
                    else transfer.send_pending()
                )
            except SecretPublishError as exc:
                # The partial-success state: the hub holds the values, the
                # shared record does not name them yet, and the keys show as
                # needing a check on every machine. The message says the one
                # thing that settles it, because a bare storage error would
                # leave the user staring at that state with no way to read it.
                raise AppApiError(
                    "secret_publish_failed",
                    context={"detail": str(exc)},
                    status_code=500,
                ) from exc
        return self._respond(outcomes, store)

    def fetch(self, request: SecretTransferRequest) -> SecretTransferResponse:
        """Take the hub's values for the named keys, or for everything short.

        The shared files are refreshed first, for the same reason a send does:
        which generation is the current one is read out of
        ``config/secrets.yml``, and only that generation is ever adopted.
        """
        self._sync.refresh()
        transfer, store = self._require_transfer()
        with _reporting("secret_fetch_failed"):
            outcomes = (
                transfer.fetch(request.keys)
                if request.keys
                else transfer.fetch_missing()
            )
        return self._respond(outcomes, store)

    def _respond(
        self, outcomes: list[SecretTransferOutcome], store: KeyringSecretStore
    ) -> SecretTransferResponse:
        return SecretTransferResponse(
            results=[
                SecretTransferResult(
                    key=outcome.key,
                    status=outcome.status,
                    generation=outcome.generation,
                )
                for outcome in outcomes
            ],
            secrets=self.get_secrets(),
        )

    def _require_transfer(self) -> tuple[SecretTransfer, KeyringSecretStore]:
        """Return a transfer, refusing plainly when there is nothing to use."""
        store = self._store()
        transfer = self._transfer(store) if store is not None else None
        if store is None or transfer is None:
            raise AppApiError(
                "workspace_sync_disabled",
                "workspace_sync_disabled.secrets",
                status_code=409,
            )
        return transfer, store

    def _store(self) -> KeyringSecretStore | None:
        try:
            return KeyringSecretStore(get_workspace_config_dir())
        except WorkspaceNotConfiguredError:
            return None

    def _transfer(self, store: KeyringSecretStore) -> SecretTransfer | None:
        target = self._sync.hub_target()
        if target is None:
            return None
        location, workspace_id = target
        return SecretTransfer(store, hub_secret_client(location, workspace_id))


def _secrets(
    store: KeyringSecretStore,
    index: HubSecretIndex | None,
    *,
    enabled: bool,
    error_code: str = "",
) -> WorkspaceSecrets:
    """Build the screen's view from the local states and the hub's answer."""
    held = index.generations if index is not None else {}
    try:
        states = store.key_states()
    except SecretStoreError:
        states = []
    statuses = {
        state.key: transfer_status(
            state, held.get(state.key), hub_answered=index is not None
        )
        for state in states
    }
    keys = [
        WorkspaceSecretState(
            key=state.key,
            status=statuses[state.key],
            shared_generation=state.shared_generation,
            local_generation=state.local_generation,
            hub_generation=held.get(state.key),
            updated_at=state.updated_at,
            can_send=can_send(state),
            can_fetch=can_fetch(state, held.get(state.key)),
        )
        for state in states
    ]
    counts = dict.fromkeys(SecretKeyStatus, 0)
    for state in states:
        counts[state.status] += 1
    # What the user has to act on before the hub can hand this value to any
    # other machine: a value entered here, a key changed on two machines, a
    # send that reached the hub without its generation being recorded, and a
    # recorded generation the hub holds no copy of.
    pending = sum(
        1
        for status in statuses.values()
        if status
        in {
            SecretKeyStatus.PENDING_SEND,
            SecretKeyStatus.CONFLICT,
            UNCONFIRMED,
            HUB_BEHIND,
        }
    )
    local = keyring_status()
    return WorkspaceSecrets(
        enabled=enabled,
        hub_reachable=index is not None,
        hub_error_code=error_code,
        secret_store=SecretStoreState(
            available=bool(local["available"]), locked=bool(local["locked"])
        ),
        hub_secret_store=(
            None
            if index is None
            else SecretStoreState(available=index.available, locked=index.locked)
        ),
        keys=keys,
        # The two bulk actions name their keys here rather than being worked
        # out again on the screen: what a transfer takes is the transfer's to
        # say, and a second answer would eventually be a different one.
        sendable_keys=bulk_send_keys(states, index),
        fetchable_keys=bulk_fetch_keys(states, index),
        missing_count=counts[SecretKeyStatus.MISSING],
        outdated_count=counts[SecretKeyStatus.OUTDATED],
        pending_count=pending,
        attention_count=(
            counts[SecretKeyStatus.MISSING] + counts[SecretKeyStatus.OUTDATED] + pending
        ),
    )


@contextmanager
def _reporting(message_key: str) -> Iterator[None]:
    """Report a hub that did not answer, rather than crashing on it.

    A hub that cannot be reached is the most ordinary thing that goes wrong in
    a transfer, so it arrives at the screen as a message about the hub. The
    detail is the boundary's own exception text, which names the machine and
    the failure and never a value.
    """
    try:
        yield
    except _HUB_FAILURES as exc:
        raise AppApiError(
            _hub_error_code(exc),
            message_key,
            context={"detail": str(exc)},
            status_code=409,
        ) from exc


def _hub_error_code(exc: Exception) -> str:
    """Name why the hub did not answer, without quoting anything it sent."""
    if isinstance(exc, OpenSshNotFoundError):
        return "openssh_missing"
    return "hub_unreachable"
