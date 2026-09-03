"""Provider-neutral sandbox contract for native AI CLI turns.

Every turn an AI CLI tool runs for GuildBotics is confined the same way,
whatever the turn is for: the agent may read and write its working directory,
read the trees the commands on this device's PATH live in, plus the
directories the user granted under their home; the commands it runs and the
provider's built-in web tools reach the network only as the selected tool
definition allows. The contract is what GuildBotics asks for; each adapter
translates it into its provider's own sandbox settings, and refuses to start
when the provider cannot enforce it on the current OS.
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

NetworkMode = Literal["deny", "allowlist", "unrestricted"]
NETWORK_MODES: tuple[NetworkMode, ...] = ("deny", "allowlist", "unrestricted")
GrantAccess = Literal["read", "read_write"]
#: The workspace-shared file: the directories under the home directory every
#: agent may use for documents.
FILESYSTEM_GRANTS_PATH = "intelligences/cli_agent_filesystem_grants.yml"
#: The device-local file (under ``<workspace>/.guildbotics/local``): extra
#: paths this machine alone opens or closes, never synchronized.
LOCAL_GRANTS_FILENAME = "cli_agent_filesystem_grants.yml"
#: How far below the root a tree must sit to stand in for the roots inside
#: it: `/opt/homebrew`, never `/opt`.
_MIN_TREE_DEPTH = 2
#: Directory names under which a package keeps the executables it exposes;
#: the package is their parent. Version managers (rbenv, pyenv) expose
#: `shims` beside the `versions` the shims dispatch into.
_PACKAGE_BIN_NAMES = frozenset({"bin", "shims"})
_HOME_TOKEN = "$HOME"
_WORKSPACE_TOKEN = "<workspace>"


class SandboxContractError(ValueError):
    """Raised when a sandbox setting is malformed or cannot be honoured."""


class _NetworkRoute(BaseModel):
    """One network path: what may connect, and to where.

    ``mode`` is a plain string, never a YAML boolean: ``off`` / ``on`` read as
    booleans under YAML 1.1, which is why the closed value is spelled ``deny``.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    mode: NetworkMode = "deny"
    allowed_domains: list[str] = Field(default_factory=list)

    @field_validator("allowed_domains")
    @classmethod
    def _validate_domains(cls, domains: list[str]) -> list[str]:
        for domain in domains:
            if (
                not domain.strip()
                or any(ch.isspace() for ch in domain)
                or "/" in domain
            ):
                raise ValueError(f"'{domain}' is not a domain name")
        return domains

    def check(self) -> None:
        if self.mode == "allowlist" and not self.allowed_domains:
            raise ValueError("mode 'allowlist' needs at least one allowed domain")
        if self.mode != "allowlist" and self.allowed_domains:
            raise ValueError("allowed_domains is only used with mode 'allowlist'")


class CommandNetworkRoute(_NetworkRoute):
    """Shell commands and their child processes."""

    allow_local_network: bool = False


class WebNetworkRoute(_NetworkRoute):
    """The provider's in-process web tools (search, fetch, URL reads)."""


class NetworkPolicy(BaseModel):
    """The ``network`` block of an AI CLI tool definition."""

    model_config = ConfigDict(extra="forbid", strict=True)

    command: CommandNetworkRoute = Field(default_factory=CommandNetworkRoute)
    web: WebNetworkRoute = Field(default_factory=WebNetworkRoute)


def parse_network_policy(raw: Any, *, where: str) -> NetworkPolicy:
    """Validate a definition's ``network`` block; absent means closed.

    A definition that states a block states all of it: there is no per-field
    merge with the tool's default, so a partial block is a mistake to report,
    not a shorthand to complete.
    """
    if raw is None:
        return NetworkPolicy()
    if not isinstance(raw, dict):
        raise SandboxContractError(f"{where}: 'network' must be a mapping")
    if set(raw) != {"command", "web"}:
        raise SandboxContractError(
            f"{where}: 'network' must state both 'command' and 'web'"
        )
    try:
        policy = NetworkPolicy.model_validate(raw)
        policy.command.check()
        policy.web.check()
    except (ValidationError, ValueError) as exc:
        raise SandboxContractError(f"{where}: invalid 'network': {exc}") from exc
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
            raise ValueError(f"'{path}' must be relative to the home directory")
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
        raise ValueError(f"'{path}' must name a directory")


class SharedGrants(BaseModel):
    """The workspace-shared part of what agents may reach."""

    model_config = ConfigDict(extra="forbid", strict=True)

    documents: list[DocumentGrant] = Field(default_factory=list)


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
        raise SandboxContractError(f"{where}: must be a mapping")
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise SandboxContractError(f"{where}: invalid grants: {exc}") from exc


def load_shared_grants() -> SharedGrants:
    """The workspace's shared grants; no file means none."""
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
    it instead, so what the provider receives is always there.
    """

    path: Path
    access: GrantAccess
    grant: str
    present: bool = True


@dataclass(frozen=True, slots=True)
class DerivedTree:
    """A tree this device reads because commands on its PATH live there.

    ``sources`` are the PATH directories that led here: `/opt/homebrew` for
    `/opt/homebrew/bin` and `/opt/homebrew/sbin` alike. ``grant`` is how the
    local grant file would name the tree to close it (see ``grant_spelling``).
    """

    path: Path
    sources: tuple[Path, ...]
    grant: str


@dataclass(frozen=True, slots=True)
class ExcludedTree:
    """A tree the PATH led to that is not opened, and why."""

    path: Path
    source: Path
    reason: str


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
    trees: tuple[DerivedTree, ...] = ()
    excluded: tuple[ExcludedTree, ...] = ()
    denied: tuple[DeniedPath, ...] = ()

    def entries(self) -> tuple[ResolvedGrant, ...]:
        """The directory grants an adapter translates, in a stable order.

        A user's grant on a tree the PATH also derives keeps its access; a
        user's deny is not a grant and is listed apart, because an adapter
        spells it differently.
        """
        seen: dict[Path, ResolvedGrant] = {}
        for grant in (*self.documents, *self.paths):
            if grant.present:
                seen[grant.path] = grant
        for tree in self.trees:
            seen.setdefault(tree.path, ResolvedGrant(tree.path, "read", tree.grant))
        return tuple(
            grant
            for grant in seen.values()
            if not any(grant.path.is_relative_to(d.path) for d in self.denied)
        )


def resolve_access(
    shared: SharedGrants,
    local: LocalGrants,
    search_path: str,
    home: Path | None = None,
    *,
    create: bool = True,
) -> ResolvedAccess:
    """Resolve the shared and local grants against this device.

    A document directory that does not exist yet is created before a turn
    starts: the grant is shared by every device using the workspace, and an
    empty directory under the home directory is a harmless thing to make. A
    preview passes ``create=False`` and sees it as absent instead. The trees
    the commands on ``search_path`` live in are derived here and read; the
    user closes what they do not want open with ``deny``. A local path must
    exist here, because this file describes this machine: a turn refuses to
    start over one that does not, and a preview reports it absent so the row
    can be pointed at.
    """
    home_root = (home or Path.home()).resolve()
    documents = tuple(
        _resolve_document(grant, home_root, create) for grant in shared.documents
    )
    paths = tuple(_resolve_local(grant, home_root, create) for grant in local.paths)
    trees, excluded = path_read_trees(search_path, home_root)
    denied = (
        *(DeniedPath(path, builtin=True) for path in builtin_denied_paths(home_root)),
        *(
            DeniedPath(_resolve_deny(path, home_root), builtin=False)
            for path in local.deny
        ),
    )
    return ResolvedAccess(
        documents=documents,
        paths=paths,
        trees=trees,
        excluded=excluded,
        denied=denied,
    )


def _resolve_document(
    grant: DocumentGrant, home_root: Path, create: bool
) -> ResolvedGrant:
    target = home_root / grant.path
    if create:
        try:
            target.mkdir(parents=True, exist_ok=True)
        except FileExistsError as exc:
            raise SandboxContractError(
                f"document grant '{grant.path}' exists but is not a directory"
            ) from exc
    elif not target.exists():
        return ResolvedGrant(target, grant.access, grant.path, present=False)
    real = target.resolve()
    if not real.is_dir():
        raise SandboxContractError(
            f"document grant '{grant.path}' exists but is not a directory"
        )
    if real == home_root or not real.is_relative_to(home_root):
        raise SandboxContractError(
            f"document grant '{grant.path}' must stay below the home directory"
        )
    return ResolvedGrant(real, grant.access, grant.path)


def local_path_missing(path: str) -> str:
    """Why a local path grant cannot be honoured: the same words for the turn
    that refuses to start and the preview that points at the row."""
    return f"local path '{path}' does not exist on this device"


def _resolve_local(
    grant: LocalPathGrant, home_root: Path, strict: bool
) -> ResolvedGrant:
    target = Path(grant.path) if grant.absolute else home_root / grant.path
    real = target.resolve()
    if not real.is_dir():
        if strict:
            raise SandboxContractError(local_path_missing(grant.path))
        return ResolvedGrant(real, grant.access, grant.path, present=False)
    if real == home_root or real == Path(real.anchor):
        raise SandboxContractError(
            f"local path '{grant.path}' would grant the whole home directory or root"
        )
    return ResolvedGrant(real, grant.access, grant.path)


def _resolve_deny(path: str, home_root: Path) -> Path:
    absolute = PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()
    target = (Path(path) if absolute else home_root / path).resolve()
    if target == home_root or target == Path(target.anchor):
        raise SandboxContractError(
            f"deny '{path}' would close the whole home directory or root"
        )
    return target


def path_read_trees(
    search_path: str, home: Path | None = None
) -> tuple[tuple[DerivedTree, ...], tuple[ExcludedTree, ...]]:
    """The trees the commands on ``search_path`` live in, and what was left out.

    What the user can run, the agent can run: each PATH directory opens, for
    reading, the smallest tree holding it and the packages its executables
    link into (`/opt/homebrew` for `/opt/homebrew/bin`, `~/.local` for
    `~/.local/bin`). Platform directories the provider reads anyway are
    skipped. A root inside a directory holding credentials or provider state
    is left out with its reason: a Codex install linked from `~/.local/bin`
    into `~/.codex/packages` opens `~/.local` but not `~/.codex`.
    """
    home_root = (home or Path.home()).resolve()
    platform = _platform_directories()
    trees: dict[Path, list[Path]] = {}
    excluded: list[ExcludedTree] = []
    seen: set[Path] = set()
    for entry in search_path.split(os.pathsep):
        if not entry:
            continue
        source = Path(entry)
        try:
            resolved = source.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        if any(resolved.is_relative_to(root) for root in platform):
            continue
        roots: list[Path] = []
        _collect(resolved, home_root, roots)
        for executable in _executables_in(resolved):
            _collect_read_roots(executable, home_root, roots)
        for root in _collapse(roots, home_root):
            reason = _inside_sensitive(root, home_root)
            if reason:
                excluded.append(ExcludedTree(root, source, f"it is under {reason}"))
            else:
                trees.setdefault(root, []).append(source)
    return (
        tuple(
            DerivedTree(path, tuple(sources), grant_spelling(path, home_root))
            for path, sources in trees.items()
        ),
        tuple(excluded),
    )


def _executables_in(directory: Path) -> list[Path]:
    try:
        candidates = sorted(directory.iterdir())
    except OSError:
        return []
    found: list[Path] = []
    for candidate in candidates:
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                found.append(candidate)
        except OSError:
            continue
    return found


def _platform_directories() -> tuple[Path, ...]:
    """Directories every provider's baseline reads; the PATH entries there
    are not worth deriving or showing."""
    if os.name == "nt":
        names = ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
        return tuple(Path(os.environ[n]) for n in names if os.environ.get(n))
    return tuple(
        Path(p)
        for p in (
            "/usr/bin",
            "/usr/sbin",
            "/usr/lib",
            "/usr/libexec",
            "/bin",
            "/sbin",
            "/lib",
            "/lib64",
            "/System",
        )
    )


def _inside_sensitive(path: Path, home_root: Path) -> str:
    """The credential directory ``path`` sits in, or "" (containing one is
    fine: the built-in deny closes that corner)."""
    for name in SENSITIVE_HOME_DIRECTORIES:
        if path.is_relative_to((home_root / name).resolve()):
            return f"~/{name}"
    return ""


@dataclass(frozen=True, slots=True)
class SandboxContract:
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
                "trees": [
                    {"path": mask(t.path), "sources": [mask(s) for s in t.sources]}
                    for t in self.access.trees
                ],
                "excluded": [
                    {"path": mask(t.path), "source": mask(t.source), "reason": t.reason}
                    for t in self.access.excluded
                ],
                "denied": [
                    {"path": mask(d.path), "builtin": d.builtin}
                    for d in self.access.denied
                ],
            },
            "network": self.network.model_dump(mode="json"),
        }


#: Home directories that hold credentials or the provider's own state. Three
#: things follow from the one list: a path the user grants under one of these
#: is warned about; a tree the PATH derives inside one is left out, because
#: nobody reviews a derived grant; and every one that exists is closed with
#: a deny on top of whatever is open, so a tree that merely *contains* one
#: (`~/.local` around `~/.local/share/keyrings`) can still be read around it.
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
        return "the home directory"
    for name in SENSITIVE_HOME_DIRECTORIES:
        sensitive = (home_root / name).resolve()
        if target.is_relative_to(sensitive) or sensitive.is_relative_to(target):
            return f"~/{name}"
    return ""


def executable_read_roots(
    executable: Path, home: Path | None = None
) -> tuple[Path, ...]:
    """The directories an executable must be readable through to be run.

    A program is never one file: the directory holding each symlink on the
    way to the real file (a link file such as ``~/.local/bin/codex``, a link
    directory such as ``packages/standalone/current``) must be readable to
    follow it, and the real binary belongs to a package (the parent of its
    ``bin``) whose libraries and resources it loads. A package manager keeps
    the libraries a binary links *beside* its package -- Homebrew's
    ``/opt/homebrew/opt/libuv`` for ``Cellar/node@24`` -- so what is granted
    is the smallest directory holding both the invoked link and the package:
    ``/opt/homebrew``, ``~/.local``. That ancestor is never the home directory,
    the root, or anything as shallow as ``/opt``; when it would be, the link
    directories and the package are granted separately, as for a Codex
    install linked from ``~/.local/bin`` into ``~/.codex/packages``.
    """
    home_root = (home or Path.home()).resolve()
    roots: list[Path] = []
    _collect_read_roots(executable, home_root, roots)
    return _collapse(roots, home_root)


def _collect_read_roots(executable: Path, home_root: Path, roots: list[Path]) -> None:
    current = Path(executable)
    seen: set[Path] = set()
    while current not in seen:
        seen.add(current)
        link = _first_symlink_component(current)
        if link is None:
            break
        _collect(link.parent, home_root, roots)
        try:
            target = Path(os.readlink(link))
        except OSError:
            return
        if not target.is_absolute():
            target = link.parent / target
        current = target / current.relative_to(link)
    real = Path(executable).resolve()
    _collect(
        real.parent.parent if real.parent.name in _PACKAGE_BIN_NAMES else real.parent,
        home_root,
        roots,
    )


def _collect(directory: Path, home_root: Path, roots: list[Path]) -> None:
    if directory in (home_root, Path(directory.anchor)) or directory in roots:
        return
    roots.append(directory)


def _collapse(roots: list[Path], home_root: Path) -> tuple[Path, ...]:
    """One tree when the roots share a grantable ancestor, else each outermost root."""
    ancestor = _common_ancestor(roots)
    if ancestor is not None and _is_grantable_tree(ancestor, home_root):
        return (ancestor,)
    return tuple(
        root
        for root in roots
        if not any(other != root and root.is_relative_to(other) for other in roots)
    )


def _common_ancestor(paths: list[Path]) -> Path | None:
    if not paths:
        return None
    common = paths[0]
    for path in paths[1:]:
        while not path.is_relative_to(common):
            common = common.parent
    return common


def _is_grantable_tree(directory: Path, home_root: Path) -> bool:
    """Whether a whole tree may stand in for the roots inside it.

    Below the home directory anything but the home itself will do
    (``~/.local``); elsewhere the tree must be at least two levels below the
    root (``/opt/homebrew``, ``/usr/local``), so that ``/opt`` or ``/`` is
    never granted for one command.
    """
    if directory == home_root:
        return False
    if directory.is_relative_to(home_root):
        return True
    return len(directory.relative_to(directory.anchor).parts) >= _MIN_TREE_DEPTH


def _first_symlink_component(path: Path) -> Path | None:
    """The outermost path component that is a symlink, or None."""
    for parent in reversed(path.parents):
        if parent != Path(parent.anchor) and parent.is_symlink():
            return parent
    return path if path.is_symlink() else None


def grant_spelling(path: PurePath, home: PurePath) -> str:
    """How a grant file names ``path``: relative to the home directory when
    under it, absolute otherwise, in this OS's own separators.

    This is the inverse of resolving a grant, spelled once here so that what a
    device reports for a derived tree is exactly what closing that tree with
    ``deny`` would be written as.
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
