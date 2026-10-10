"""The access contract: what one AI CLI turn may reach.

Every turn an AI CLI tool runs for GuildBotics is confined the same way,
whatever provider runs it: the agent may read and write its working directory
and the directories the user granted under their home, and it reaches the
network only as the workspace's environment declaration allows, whether
through a command it runs or the provider's built-in web tools (those it
runs on its own servers excepted: they never pass the environment). A read-only
turn reads the same grants but writes nowhere and reaches no network beyond
its provider and GuildBotics itself. The agent's tools are
the environment's own, so nothing of this device's PATH is part of it. The
contract is what GuildBotics asks for, as data; the isolated agent
environment (:mod:`.spec`, :mod:`.runtime`) is what enforces it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from guildbotics.utils.fileio import (
    WorkspaceNotConfiguredError,
    get_config_path,
    get_workspace_local_path,
    get_workspace_root,
)
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.safe_paths import (
    HostPathPermissionError,
    PathFacts,
    host_path_contains,
    inspect_host_path,
    normalize_host_path,
    read_host_file,
    resolve_host_links,
)
from guildbotics.utils.safe_paths import UnsafePathError as AccessContractError
from guildbotics.utils.workspace_state import registered_workspaces

NetworkMode = Literal["deny", "allowlist", "unrestricted"]
GrantAccess = Literal["read", "read_write"]
#: The workspace-shared file: the directories under the home directory every
#: agent may use for documents.
FILESYSTEM_GRANTS_PATH = "intelligences/cli_agent_filesystem_grants.yml"
#: The exchange directory, relative to the home: where an agent leaves what it
#: makes for the user, and the default working directory. Granted read-write
#: to every workspace until its grants file says otherwise.
EXCHANGE_DIRECTORY = "Documents/GuildBotics"
#: The device-local file (under ``<workspace>/.guildbotics/local``): extra
#: paths this machine alone opens or closes, never synchronized.
LOCAL_GRANTS_FILENAME = "cli_agent_filesystem_grants.yml"
_HOME_TOKEN = "$HOME"
_WORKSPACE_TOKEN = "<workspace>"


class NetworkPolicy(BaseModel):
    """The environment declaration's ``network`` block: what a turn may reach.

    One rule covers the provider's own web tools and every command it runs,
    because the environment that enforces it cannot tell the two apart. A web
    tool the provider runs on its own servers never passes through the
    environment, so no rule here reaches it. ``mode``
    is a plain string, never a YAML boolean: ``off`` / ``on`` read as booleans
    under YAML 1.1, which is why the closed value is spelled ``deny``.
    ``allow_local_network`` opens the host and its private networks as well.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    mode: NetworkMode = "deny"
    allowed_domains: list[str] = Field(default_factory=list)
    allow_local_network: bool = False

    @field_validator("allowed_domains")
    @classmethod
    def _validate_domains(cls, domains: list[str]) -> list[str]:
        for domain in domains:
            if (
                not domain.strip()
                or any(ch.isspace() for ch in domain)
                or "/" in domain
            ):
                raise ValueError(
                    t(
                        "intelligences.agent_environment.grants.not_a_domain",
                        domain=domain,
                    )
                )
        return domains

    @model_validator(mode="after")
    def _consistent(self) -> NetworkPolicy:
        if self.mode == "allowlist" and not self.allowed_domains:
            raise ValueError(
                t("intelligences.agent_environment.grants.allowlist_needs_domain")
            )
        if self.mode != "allowlist" and self.allowed_domains:
            raise ValueError(
                t("intelligences.agent_environment.grants.domains_need_allowlist")
            )
        return self


class DocumentGrant(BaseModel):
    """A directory below the home directory that agents may use for documents.

    This is the workspace's decision -- where its work reads from and writes
    to -- so it is shared, spelled relative to the home directory and resolved
    on every device the same way.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    path: str
    access: GrantAccess

    @field_validator("path")
    @classmethod
    def _validate_path(cls, path: str) -> str:
        if PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute():
            raise ValueError(
                t("intelligences.agent_environment.grants.path_not_relative", path=path)
            )
        _require_directory_name(path)
        return path


class LocalPathGrant(BaseModel):
    """A path this device alone grants, beyond what its PATH derives.

    Machine-shaped by nature (a cache a tool writes, data a tool keeps outside
    its install tree), so it lives under ``local/`` and may be absolute. It
    must exist here: this file describes this machine, and nothing else.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    path: str
    access: GrantAccess

    @property
    def absolute(self) -> bool:
        return (
            PurePosixPath(self.path).is_absolute()
            or PureWindowsPath(self.path).is_absolute()
        )

    @field_validator("path")
    @classmethod
    def _validate_path(cls, path: str) -> str:
        _require_directory_name(path)
        return path


def _require_directory_name(path: str) -> None:
    segments = path.replace("\\", "/").strip("/").split("/")
    if not path.strip("/") or any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError(
            t(
                "intelligences.agent_environment.grants.path_not_a_directory_name",
                path=path,
            )
        )


class SharedGrants(BaseModel):
    """The workspace-shared part of what agents may reach."""

    model_config = ConfigDict(extra="forbid", strict=True)

    documents: list[DocumentGrant] = Field(default_factory=list)


#: The exchange directory as every turn is granted it: read-write and built
#: in, so what the Desktop hands over and what an agent makes for the user
#: always have a place both can reach, whatever the grants file says. A grants
#: file entry for the same path is ignored rather than merged.
EXCHANGE_GRANT = DocumentGrant(path=EXCHANGE_DIRECTORY, access="read_write")


def exchange_dir(home: Path | None = None) -> Path:
    """The exchange directory on this device."""
    return (home or Path.home()) / EXCHANGE_DIRECTORY


class LocalGrants(BaseModel):
    """The device-local part of what agents may reach, and may not.

    ``deny`` protects directories against sharing their contents or parents.
    Absolute or relative to the home directory; absent paths are protected too.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    paths: list[LocalPathGrant] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)

    @field_validator("deny")
    @classmethod
    def _validate_deny(cls, deny: list[str]) -> list[str]:
        for path in deny:
            _require_directory_name(path)
        return deny


def parse_shared_grants(raw: Any, *, where: str) -> SharedGrants:
    return _parse(SharedGrants, raw, where)


def parse_local_grants(raw: Any, *, where: str) -> LocalGrants:
    return _parse(LocalGrants, raw, where)


def _parse[T: BaseModel](model: type[T], raw: Any, where: str) -> T:
    if raw is None:
        return model()
    if not isinstance(raw, dict):
        raise AccessContractError(
            t("intelligences.agent_environment.grants.not_a_mapping", where=where)
        )
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise AccessContractError(
            t("intelligences.agent_environment.grants.invalid", where=where, error=exc)
        ) from exc


def load_shared_grants(*, workspace: Path | None = None) -> SharedGrants:
    """The workspace's shared grants; no file means none beyond the built-in."""
    path = (
        workspace / ".guildbotics/config" / FILESYSTEM_GRANTS_PATH
        if workspace is not None
        else get_config_path(FILESYSTEM_GRANTS_PATH)
    )
    return _load_grants(SharedGrants, path, FILESYSTEM_GRANTS_PATH)


def load_local_grants(*, workspace: Path | None = None) -> LocalGrants:
    """This device's own grants; no file means none."""
    path = (
        workspace / ".guildbotics/local" / LOCAL_GRANTS_FILENAME
        if workspace is not None
        else get_workspace_local_path(LOCAL_GRANTS_FILENAME)
    )
    return _load_grants(LocalGrants, path, f"local/{LOCAL_GRANTS_FILENAME}")


def _load_grants[T: BaseModel](model: type[T], path: Path, where: str) -> T:
    try:
        raw = yaml.safe_load(read_host_file(path))
    except FileNotFoundError:
        return model()
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise AccessContractError(
            t("intelligences.agent_environment.grants.invalid", where=where, error=exc)
        ) from exc
    return _parse(model, raw, where)


@dataclass(frozen=True, slots=True)
class ResolvedGrant:
    """A directory grant as it stands on this device.

    ``grant`` is the path as the grant file spells it, so a row shown for
    this device can be matched back to the entry that produced it without
    the reader re-deriving how a path is written on this OS. ``present`` is
    False for a document directory a preview found missing; a turn creates
    it instead, so what the provider receives is always there. ``builtin``
    marks the grant GuildBotics makes itself (:data:`EXCHANGE_GRANT`), which
    no grants file entry produced and none can remove.
    """

    path: Path
    access: GrantAccess
    grant: str
    present: bool = True
    builtin: bool = False


@dataclass(frozen=True, slots=True)
class DeniedPath:
    """A path closed on this device: shipped with GuildBotics, or the user's."""

    path: Path
    kind: Literal["credentials", "workspace", "local"]

    @property
    def builtin(self) -> bool:
        return self.kind != "local"

    def refusal(self, source: Path) -> str:
        params = {"path": source, "protected": self.path}
        return {
            "credentials": t("safe_paths.protected_credentials", **params),
            "workspace": t("safe_paths.protected_workspace", **params),
            "local": t("safe_paths.protected_local", **params),
        }[self.kind]

    def facts(self) -> tuple[PathFacts, ...]:
        """Close every intermediate link name, destination and absent name."""
        try:
            resolution = resolve_host_links(self.path)
            return tuple(
                inspect_host_path(
                    name, missing=True, directory=False, link_as_missing=True
                )
                for name in resolution.names
            )
        except HostPathPermissionError as exc:
            raise HostPathPermissionError(
                Path(exc.filename), protected=self.path
            ) from exc
        except AccessContractError as exc:
            raise AccessContractError(
                t("safe_paths.protected_unavailable", protected=self.path, reason=exc)
            ) from exc


@dataclass(frozen=True, slots=True)
class ResolvedAccess:
    """Everything beyond the working directory a turn may reach on this device."""

    documents: tuple[ResolvedGrant, ...] = ()
    paths: tuple[ResolvedGrant, ...] = ()
    denied: tuple[DeniedPath, ...] = ()

    def reaches(self, path: Path, cwd: Path | None = None) -> bool:
        """Whether a turn run in ``cwd`` sees ``path`` on this device.

        The same answer the mounts give: under the working directory or a
        granted directory that exists here, and not under a closed one.
        """
        target = inspect_host_path(path, missing=True, directory=False)
        opened = [g.path for g in (*self.documents, *self.paths) if g.present]
        if cwd is not None:
            try:
                opened.append(validate_mount_source(cwd, self.denied, grant=True))
            except AccessContractError:
                return False
        return any(
            inspect_host_path(root).contains(target) for root in opened
        ) and not any(
            facts.contains(target) for denied in self.denied for facts in denied.facts()
        )


def resolve_access(
    shared: SharedGrants,
    local: LocalGrants,
    home: Path | None = None,
    *,
    create: bool = True,
    workspace: Path | None = None,
) -> ResolvedAccess:
    """Resolve the shared and local grants against this device.

    A document directory that does not exist yet is created before a turn
    starts: the grant is shared by every device using the workspace, and an
    empty directory under the home directory is a harmless thing to make. A
    preview passes ``create=False`` and sees it as absent instead. A local
    path must exist here, because this file describes this machine: a turn
    refuses to start over one that does not, and a preview reports it absent
    so the row can be pointed at. The exchange directory comes first, built
    in, ahead of whatever the grants file adds. ``workspace`` is the selected
    workspace, whose own ``.guildbotics`` is closed like a credential
    directory; it is read from the selection when not given.
    """
    home_root = normalize_host_path(home or Path.home())
    workspace = workspace or _selected_workspace()
    denied = protected_paths(home_root, workspace, local=local)
    # Judge every planned grant before creating any of its directories.
    grants: list[DocumentGrant | LocalPathGrant] = [
        *shared.documents,
        *local.paths,
        EXCHANGE_GRANT,
    ]
    for grant in grants:
        target = _grant_path(grant.path, home_root)
        validate_mount_source(target, denied, grant=True, missing=True)
    documents = (
        _resolve_document(EXCHANGE_GRANT, home_root, create, builtin=True),
        *(
            _resolve_document(grant, home_root, create)
            for grant in shared.documents
            if grant.path != EXCHANGE_GRANT.path
        ),
    )
    paths = tuple(_resolve_local(grant, home_root, create) for grant in local.paths)
    return ResolvedAccess(documents=documents, paths=paths, denied=denied)


def protected_paths(
    home: Path | None = None,
    workspace: Path | None = None,
    *,
    local: LocalGrants | None = None,
) -> tuple[DeniedPath, ...]:
    """The one protected-path table, including absent paths and local denies."""
    home_root = normalize_host_path(home or Path.home())
    workspace = workspace or _selected_workspace()
    if local is None:
        local = load_local_grants() if workspace is not None else LocalGrants()
    return (
        *builtin_denied(home_root, workspace),
        *(DeniedPath(_resolve_deny(path, home_root), "local") for path in local.deny),
    )


def _resolve_document(
    grant: DocumentGrant, home_root: Path, create: bool, *, builtin: bool = False
) -> ResolvedGrant:
    facts = inspect_host_path(home_root / grant.path, create=create, missing=not create)
    return ResolvedGrant(
        facts.path, grant.access, grant.path, present=facts.present, builtin=builtin
    )


def local_path_missing(path: str) -> str:
    """Why a local path grant cannot be honoured: the same words for the turn
    that refuses to start and the preview that points at the row."""
    return t("intelligences.agent_environment.grants.local_path_missing", path=path)


def _resolve_local(
    grant: LocalPathGrant, home_root: Path, strict: bool
) -> ResolvedGrant:
    facts = inspect_host_path(_grant_path(grant.path, home_root), missing=True)
    if not facts.present:
        if strict:
            raise AccessContractError(local_path_missing(grant.path))
        return ResolvedGrant(facts.path, grant.access, grant.path, present=False)
    real = facts.path
    if real == home_root or real == Path(real.anchor):
        raise AccessContractError(
            t(
                "intelligences.agent_environment.grants.local_path_too_broad",
                path=grant.path,
            )
        )
    return ResolvedGrant(real, grant.access, grant.path)


def _resolve_deny(path: str, home_root: Path) -> Path:
    absolute = PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()
    target = normalize_host_path(Path(path) if absolute else home_root / path)
    if any(
        facts.path == home_root or facts.path == Path(facts.path.anchor)
        for facts in DeniedPath(target, "local").facts()
    ):
        raise AccessContractError(
            t("intelligences.agent_environment.grants.deny_too_broad", path=path)
        )
    return target


@dataclass(frozen=True, slots=True)
class AccessContract:
    """What GuildBotics asks a provider to enforce for one turn.

    The working directory is not part of the contract because it is the
    turn's own ``cwd``: readable and writable unless the turn is read-only.
    """

    network: NetworkPolicy = field(default_factory=NetworkPolicy)
    access: ResolvedAccess = field(default_factory=ResolvedAccess)

    #: The turn may change nothing: it still sees the grants, but read-only,
    #: its working directory is empty, and ``network`` does not apply.
    read_only: bool = False

    @property
    def reached_network(self) -> NetworkPolicy:
        """What the turn reaches beyond its provider and GuildBotics itself.

        A turn that may change nothing reaches nothing more: a request to
        another host is a write somewhere the environment cannot undo.
        """
        return NetworkPolicy() if self.read_only else self.network

    def requested_policy(
        self, cwd: Path, *, home: Path | None = None, workspace_root: Path | None = None
    ) -> dict[str, Any]:
        """The contract as recorded in diagnostics, with device paths masked."""

        def mask(path: Path) -> str:
            return redact_path(path, home, workspace_root)

        return {
            "read_only": self.read_only,
            "filesystem": {
                "working_directory": mask(cwd),
                "documents": [
                    {"path": mask(g.path), "access": g.access, "present": g.present}
                    for g in self.access.documents
                ],
                "paths": [
                    {"path": mask(g.path), "access": g.access}
                    for g in self.access.paths
                ],
                "denied": [
                    {"path": mask(d.path), "builtin": d.builtin}
                    for d in self.access.denied
                ],
            },
            "network": self.reached_network.model_dump(mode="json"),
        }


#: Credential and device-state directories. Their contents and every parent
#: containing them are refused as user grants, even when they do not exist.
SENSITIVE_HOME_DIRECTORIES: tuple[str, ...] = (
    ".ssh",
    ".gnupg",
    ".aws",
    ".kube",
    ".docker",
    ".config/gh",
    ".local/share/keyrings",
    ".guildbotics",
    ".codex",
    ".claude",
    ".grok",
    ".copilot",
    ".gemini",
    ".agents",
)


#: The workspace's own directory: its shared configuration and state, and
#: this device's clones of every member. Only explicitly selected host-owned
#: children are mounted; sharing the workspace root is refused.
WORKSPACE_STATE_DIRECTORY = ".guildbotics"


def _selected_workspace() -> Path | None:
    try:
        return get_workspace_root()
    except WorkspaceNotConfiguredError:
        return None


def builtin_denied(
    home: Path | None = None, workspace: Path | None = None
) -> tuple[DeniedPath, ...]:
    """Protected locations, including inactive workspace state and absent dirs."""
    home_root = normalize_host_path(home or Path.home())
    paths: dict[Path, Literal["credentials", "workspace", "local"]] = {
        normalize_host_path(home_root / name): "credentials"
        for name in SENSITIVE_HOME_DIRECTORIES
    }
    for root in (
        *registered_workspaces(),
        *((workspace,) if workspace is not None else ()),
    ):
        state = normalize_host_path(root / WORKSPACE_STATE_DIRECTORY)
        paths[state] = "workspace"
    return tuple(DeniedPath(path, kind) for path, kind in paths.items())


def _grant_path(path: str, home: Path) -> Path:
    target = Path(path)
    return normalize_host_path(target if target.is_absolute() else home / target)


def validate_mount_source(
    path: Path,
    denied: tuple[DeniedPath, ...],
    *,
    grant: bool = False,
    missing: bool = False,
    create: bool = False,
) -> Path:
    """Reject links and protected ancestors before reading or making a source.

    Explicit host-owned mounts may name a precise child of protected state.
    A user grant may never name that child. Neither may share the parent.
    """
    source = inspect_host_path(path, missing=missing or create, directory=False)
    for closed in denied:
        for protected in closed.facts():
            if source.contains(protected) or (grant and protected.contains(source)):
                raise AccessContractError(closed.refusal(source.path))
    return inspect_host_path(
        source.path, create=create, missing=missing, directory=False
    ).path


def validate_workspace_location(workspace: Path) -> Path:
    """Refuse workspace state inside its own grants or the exchange directory."""
    target = inspect_host_path(workspace, missing=True)
    home = normalize_host_path(Path.home())
    # Selection enables editing and synchronization to repair configuration;
    # it never authorizes execution. Runtime contract loading stays strict.
    grants: list[DocumentGrant | LocalPathGrant] = []
    for loader, field_name in (
        (load_shared_grants, "documents"),
        (load_local_grants, "paths"),
    ):
        try:
            grants.extend(getattr(loader(workspace=target.path), field_name))
        except AccessContractError:
            continue
    grants.append(EXCHANGE_GRANT)
    for grant in grants:
        opened = _grant_path(grant.path, home)
        try:
            contains = host_path_contains(opened, target)
        except AccessContractError:
            if grant is EXCHANGE_GRANT:
                raise
            continue
        if contains:
            raise AccessContractError(
                t("safe_paths.workspace_granted", path=target.path, granted=opened)
            )
    return target.path


def grant_spelling(path: PurePath, home: PurePath) -> str:
    """How a grant file names ``path``: relative to the home directory when
    under it, absolute otherwise, in this OS's own separators.

    This is the inverse of resolving a grant, spelled once here so that what a
    device reports for a grant is exactly the entry that produced it.
    """
    if path.is_relative_to(home) and path != home:
        return str(path.relative_to(home))
    return str(path)


def redact_path(
    path: PurePath, home: PurePath | None = None, workspace_root: PurePath | None = None
) -> str:
    """Spell a device path with ``$HOME`` / ``<workspace>`` in place of its root.

    The result still names a path on that device, so it keeps that device's
    separators. Joining the token through the path's own class is what makes
    that true wherever the path came from, rather than wherever this runs.
    """
    for root, token in (
        (workspace_root, _WORKSPACE_TOKEN),
        (home or Path.home(), _HOME_TOKEN),
    ):
        if root is None:
            continue
        if path == root:
            return token
        if path.is_relative_to(root):
            return str(type(path)(token, path.relative_to(root)))
    return str(path)
