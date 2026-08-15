"""The hub: the one machine every device synchronizes its workspaces through.

A hub is machine-wide rather than workspace-wide. Everything it owns lives
under ``~/.guildbotics/hub/`` and never inside a workspace, so one machine can
host several workspaces and a workspace can be moved without telling the hub.

Two views of the same thing live here. :mod:`guildbotics.hub.host` is the
machine that hosts a hub, where the bare repositories are created;
:mod:`guildbotics.hub.connection` is a device reaching a hub, whether that hub
is this same machine or another one over OpenSSH.

The hub knows nothing about what the repositories contain. Shared state and its
rules belong to :mod:`guildbotics.workspace`, and turning local writes into
commits belongs to :mod:`guildbotics.sync`.
"""

from __future__ import annotations

from guildbotics.hub.connection import (
    HubEndpoint,
    HubHostKey,
    HubLocation,
    HubSshKey,
    HubUnreachableError,
    InvalidHubEndpointError,
    create_hub_workspace,
    ensure_ssh_key,
    hub_remote_url,
    list_hub_workspaces,
    parse_hub_endpoint,
    probe_host_key,
    read_ssh_key,
    trust_host_key,
)
from guildbotics.hub.host import (
    HubSettings,
    create_hub,
    hub_root,
    list_workspace_ids,
    read_hub,
    workspace_repository_path,
)
from guildbotics.hub.host import (
    create_workspace_repository as create_local_workspace_repository,
)

__all__ = [
    "HubEndpoint",
    "HubHostKey",
    "HubLocation",
    "HubSettings",
    "HubSshKey",
    "HubUnreachableError",
    "InvalidHubEndpointError",
    "create_hub",
    "create_hub_workspace",
    "create_local_workspace_repository",
    "ensure_ssh_key",
    "hub_remote_url",
    "hub_root",
    "list_hub_workspaces",
    "list_workspace_ids",
    "parse_hub_endpoint",
    "probe_host_key",
    "read_hub",
    "read_ssh_key",
    "trust_host_key",
    "workspace_repository_path",
]
