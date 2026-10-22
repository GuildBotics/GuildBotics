"""The owner's own git: what the human member's checkouts authenticate to
Branvil with on this machine.

Branvil's Smart HTTP takes a member's token as the password of the member's
name, and GuildBotics keeps no token of the owner, who is the human member.
So each machine issues the owner a token of its own with GuildBotics'
connection, labelled with the machine, and keeps it in its OS keychain: never
in the identities' store, whose Secrets the Hub hands to every machine. A
token revoked under Branvil's Settings → Members → owner → Tokens is that
machine's only, and the machine issues itself another on the next request
while the connection still has the owner's rights.
"""

from __future__ import annotations

from contextlib import suppress

from keyring.errors import KeyringError

from guildbotics.integrations.branvil.api import (
    Admin,
    Unauthorized,
    connection_url,
    segment,
)
from guildbotics.runtime.code_hosting_service import GitCredential
from guildbotics.runtime.port import ServiceError, ServiceNotFound
from guildbotics.utils.advisory_lock import LockTimeoutError, held_lock
from guildbotics.utils.fileio import get_machine_state_path
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.keychain import Keychain, SecretStoreError, system_keychain
from guildbotics.utils.managed_cli import managed_cli_command
from guildbotics.workspace.identity import ensure_device_identity

#: Where the keychain keeps this machine's token, by Branvil's base URL.
_KEYCHAIN_SERVICE = "GuildBotics/branvil-git"
#: Long enough for the git that issues a token to make its requests.
_ISSUE_LOCK_SECONDS = 60.0


async def owner_git_credential(origin: str) -> GitCredential | None:
    """The owner's name and this machine's token, for a remote at ``origin``;
    ``None`` unless ``origin`` is the Branvil GuildBotics is connected to.

    Raises:
        ServiceError: If Branvil cannot be asked, GuildBotics is not
            connected on this machine, or the keychain cannot be used.
    """
    url = connection_url()
    if not url or origin.casefold() != url.casefold():
        return None
    try:
        keychain = system_keychain()
        if credential := await _held(url, keychain):
            return credential
        with held_lock(
            get_machine_state_path("run", "branvil-git.lock"),
            timeout=_ISSUE_LOCK_SECONDS,
        ):
            # Another git may have issued one while this one waited.
            return await _held(url, keychain) or await _issue(url, keychain)
    except (KeyringError, SecretStoreError, LockTimeoutError) as exc:
        raise ServiceError(
            t("integrations.branvil.owner_git.unavailable", reason=str(exc))
        ) from exc


async def _held(url: str, keychain: Keychain) -> GitCredential | None:
    """The token this machine holds, while Branvil takes it for the owner's."""
    token = keychain.get_password(_KEYCHAIN_SERVICE, url)
    if not token:
        return None
    async with Admin(url, token) as held:
        try:
            return GitCredential(username=await held.owner(), password=token)
        except Unauthorized:
            return None


async def _issue(url: str, keychain: Keychain) -> GitCredential:
    """Issue this machine's token in place of every one Branvil issued it
    before, whatever name the machine had then: the label ends with the
    machine's ID."""
    device = ensure_device_identity()
    machine = f"({device.device_id[-8:]})"
    async with Admin(url) as admin:
        owner = await admin.owner()
        tokens = f"/members/{segment(owner)}/tokens"
        for held in await admin.items(tokens):
            label = str(held.get("label") or "")
            if label.startswith("git:") and label.endswith(machine):
                with suppress(ServiceNotFound):
                    await admin.call("DELETE", f"{tokens}/{segment(str(held['id']))}")
        issued = await admin.call(
            "POST", tokens, {"label": f"git:{device.display_name} {machine}"}
        )
    token = str(issued["token"])
    keychain.set_password(_KEYCHAIN_SERVICE, url, token)
    return GitCredential(username=owner, password=token)


def git_config(url: str) -> str:
    """What the owner adds at the end of their global git configuration, for
    their own git to authenticate to the Branvil at ``url`` with
    :func:`owner_git_credential`: no helper configured before it is asked."""
    helper = "!" + managed_cli_command("git-credential")
    value = helper.replace("\\", "\\\\").replace('"', '\\"')
    return f'[credential "{url}"]\n\thelper =\n\thelper = "{value}"\n'
