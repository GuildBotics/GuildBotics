"""The access contract: what one AI CLI turn may reach.

Every turn an AI CLI tool runs for GuildBotics is confined the same way,
whatever the turn is for: the agent may read and write its working directory
and the directories the user granted under their home, and it reaches the
network only as the selected tool definition allows, whether through a
command it runs or the provider's built-in web tools. The agent's tools are
the environment's own, so nothing of this device's PATH is part of it. The
contract is what GuildBotics asks for, as data; the isolated agent
environment (:mod:`.spec`, :mod:`.runtime`) is what enforces it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from guildbotics.utils.fileio import (
    get_config_path,
    get_workspace_local_path,
    load_yaml_file,
)
from guildbotics.utils.i18n_tool import t

NetworkMode = Literal["deny", "allowlist", "unrestricted"]
NETWORK_MODES: tuple[NetworkMode, ...] = ("deny", "allowlist", "unrestricted")
GrantAccess = Literal["read", "read_write"]
#: The workspace-shared file: the directories under the home directory every
#: agent may use for documents.
FILESYSTEM_GRANTS_PATH = "intelligences/cli_agent_filesystem_grants.yml"
#: The exchange directory, relative to the home: where what the user hands an
#: agent from the Desktop is placed, and where an agent leaves what it makes
#: for the user. Granted read-write to every workspace until its grants file
#: says otherwise, so the two sides of a hand-over have one place to look.
EXCHANGE_DIRECTORY = "Documents/GuildBotics"
#: Under the exchange directory: what GuildBotics itself placed there for one
#: App API session -- pasted images, copies of files an agent could not
#: otherwise reach -- emptied when that session ends. What is made for the
#: user goes beside it, never inside.
EXCHANGE_TMP_DIRECTORY = "tmp"
#: The device-local file (under ``<workspace>/.guildbotics/local``): extra
#: paths this machine alone opens or closes, never synchronized.
LOCAL_GRANTS_FILENAME = "cli_agent_filesystem_grants.yml"
_HOME_TOKEN = "$HOME"
_WORKSPACE_TOKEN = "<workspace>"


class AccessContractError(ValueError):
    """Raised when a sandbox setting is malformed or cannot be honoured."""


class NetworkPolicy(BaseModel):
    """The ``network`` block of an AI CLI tool definition: what a turn may reach.

    One rule covers the provider's own web tools and every command it runs,
    because the environment that enforces it cannot tell the two apart. ``mode``
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

    def check(self) -> None:
        if self.mode == "allowlist" and not self.allowed_domains:
            raise ValueError(
                t("intelligences.agent_environment.grants.allowlist_needs_domain")
            )
        if self.mode != "allowlist" and self.allowed_domains:
            raise ValueError(
                t("intelligences.agent_environment.grants.domains_need_allowlist")
            )


def parse_network_policy(raw: Any, *, where: str) -> NetworkPolicy:
    """Validate a definition's ``network`` block; absent means closed.

    A definition that states a block states all of it: there is no per-field
    merge with the tool's default, so a partial block is a mistake to report,
    not a shorthand to complete.
    """
    if raw is None:
        return NetworkPolicy()
    if not isinstance(raw, dict):
        raise AccessContractError(
            t(
                "intelligences.agent_environment.grants.network_not_a_mapping",
                where=where,
            )
        )
    try:
        policy = NetworkPolicy.model_validate(raw)
        policy.check()
    except (ValidationError, ValueError) as exc:
        raise AccessContractError(
            t(
                "intelligences.agent_environment.grants.network_invalid",
                where=where,
                error=exc,
            )
        ) from exc
    return policy


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


def exchange_tmp_dir(home: Path | None = None) -> Path:
    """Where GuildBotics places what it hands over for one App API session."""
    return exchange_dir(home) / EXCHANGE_TMP_DIRECTORY


class LocalGrants(BaseModel):
    """The device-local part of what agents may reach, and may not.

    ``deny`` closes directories this device would otherwise open: a whole
    tree the PATH derived, or a corner of one. Absolute or relative to the
    home directory; it need not exist, since closing what is absent costs
    nothing and keeps the file true when the directory appears.
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


def load_shared_grants() -> SharedGrants:
    """The workspace's shared grants; no file means none beyond the built-in."""
    path = get_config_path(FILESYSTEM_GRANTS_PATH)
    if not path.exists():
        return SharedGrants()
    return parse_shared_grants(load_yaml_file(path), where=FILESYSTEM_GRANTS_PATH)


def load_local_grants() -> LocalGrants:
    """This device's own grants; no file means none."""
    path = get_workspace_local_path(LOCAL_GRANTS_FILENAME)
    if not path.exists():
        return LocalGrants()
    return parse_local_grants(
        load_yaml_file(path), where=f"local/{LOCAL_GRANTS_FILENAME}"
    )


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
    builtin: bool


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
        target = path.resolve()
        opened = [g.path for g in (*self.documents, *self.paths) if g.present]
        if cwd is not None:
            opened.append(cwd.resolve())
        return any(target.is_relative_to(root) for root in opened) and not any(
            target.is_relative_to(denied.path) for denied in self.denied
        )


def resolve_access(
    shared: SharedGrants,
    local: LocalGrants,
    home: Path | None = None,
    *,
    create: bool = True,
) -> ResolvedAccess:
    """Resolve the shared and local grants against this device.

    A document directory that does not exist yet is created before a turn
    starts: the grant is shared by every device using the workspace, and an
    empty directory under the home directory is a harmless thing to make. A
    preview passes ``create=False`` and sees it as absent instead. A local
    path must exist here, because this file describes this machine: a turn
    refuses to start over one that does not, and a preview reports it absent
    so the row can be pointed at. The exchange directory comes first, built
    in, ahead of whatever the grants file adds.
    """
    home_root = (home or Path.home()).resolve()
    documents = (
        _resolve_document(EXCHANGE_GRANT, home_root, create, builtin=True),
        *(
            _resolve_document(grant, home_root, create)
            for grant in shared.documents
            if grant.path != EXCHANGE_GRANT.path
        ),
    )
    paths = tuple(_resolve_local(grant, home_root, create) for grant in local.paths)
    denied = (
        *(DeniedPath(path, builtin=True) for path in builtin_denied_paths(home_root)),
        *(
            DeniedPath(_resolve_deny(path, home_root), builtin=False)
            for path in local.deny
        ),
    )
    return ResolvedAccess(documents=documents, paths=paths, denied=denied)


def _resolve_document(
    grant: DocumentGrant, home_root: Path, create: bool, *, builtin: bool = False
) -> ResolvedGrant:
    target = home_root / grant.path
    if create:
        try:
            target.mkdir(parents=True, exist_ok=True)
        except FileExistsError as exc:
            raise AccessContractError(
                t(
                    "intelligences.agent_environment.grants.document_not_a_directory",
                    path=grant.path,
                )
            ) from exc
    elif not target.exists():
        return ResolvedGrant(
            target, grant.access, grant.path, present=False, builtin=builtin
        )
    real = target.resolve()
    if not real.is_dir():
        raise AccessContractError(
            t(
                "intelligences.agent_environment.grants.document_not_a_directory",
                path=grant.path,
            )
        )
    if real == home_root or not real.is_relative_to(home_root):
        raise AccessContractError(
            t(
                "intelligences.agent_environment.grants.document_outside_home",
                path=grant.path,
            )
        )
    return ResolvedGrant(real, grant.access, grant.path, builtin=builtin)


def local_path_missing(path: str) -> str:
    """Why a local path grant cannot be honoured: the same words for the turn
    that refuses to start and the preview that points at the row."""
    return t("intelligences.agent_environment.grants.local_path_missing", path=path)


def _resolve_local(
    grant: LocalPathGrant, home_root: Path, strict: bool
) -> ResolvedGrant:
    target = Path(grant.path) if grant.absolute else home_root / grant.path
    real = target.resolve()
    if not real.is_dir():
        if strict:
            raise AccessContractError(local_path_missing(grant.path))
        return ResolvedGrant(real, grant.access, grant.path, present=False)
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
    target = (Path(path) if absolute else home_root / path).resolve()
    if target == home_root or target == Path(target.anchor):
        raise AccessContractError(
            t("intelligences.agent_environment.grants.deny_too_broad", path=path)
        )
    return target


@dataclass(frozen=True, slots=True)
class AccessContract:
    """What GuildBotics asks a provider to enforce for one turn.

    The working directory is not part of the contract because it is the
    turn's own ``cwd``: always readable and writable, whatever else is granted.
    """

    network: NetworkPolicy = field(default_factory=NetworkPolicy)
    access: ResolvedAccess = field(default_factory=ResolvedAccess)

    def requested_policy(
        self, cwd: Path, *, home: Path | None = None, workspace_root: Path | None = None
    ) -> dict[str, Any]:
        """The contract as recorded in diagnostics, with device paths masked."""

        def mask(path: Path) -> str:
            return redact_path(path, home, workspace_root)

        return {
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
            "network": self.network.model_dump(mode="json"),
        }


#: Home directories that hold credentials or the provider's own state. Two
#: things follow from the one list: a path the user grants under one of these
#: is warned about, and every one that exists is closed with a deny on top of
#: whatever is open, so a grant that merely *contains* one (`~/.local` around
#: `~/.local/share/keyrings`) can still be read around it.
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


def builtin_denied_paths(home: Path | None = None) -> tuple[Path, ...]:
    """The sensitive directories that exist on this device, to be closed."""
    home_root = (home or Path.home()).resolve()
    return tuple(
        path
        for name in SENSITIVE_HOME_DIRECTORIES
        if (path := (home_root / name).resolve()).is_dir()
    )


def sensitive_grant_reason(path: str, home: Path | None = None) -> str:
    """Why a grant would expose credentials or provider state, or ""."""
    home_root = (home or Path.home()).resolve()
    if PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute():
        target = Path(path).resolve()
    else:
        target = (home_root / path).resolve()
    if target == home_root or home_root.is_relative_to(target):
        return t("intelligences.agent_environment.grants.sensitive_home")
    for name in SENSITIVE_HOME_DIRECTORIES:
        sensitive = (home_root / name).resolve()
        if target.is_relative_to(sensitive) or sensitive.is_relative_to(target):
            return f"~/{name}"
    return ""


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
    path: Path, home: Path | None = None, workspace_root: Path | None = None
) -> str:
    """Spell a device path with ``$HOME`` / ``<workspace>`` in place of its root."""
    text = str(path)
    for root, token in (
        (workspace_root, _WORKSPACE_TOKEN),
        (home or Path.home(), _HOME_TOKEN),
    ):
        if root is None:
            continue
        prefix = str(root)
        if text == prefix:
            return token
        if text.startswith(prefix + os.sep):
            return token + text[len(prefix) :]
    return text
