"""The contract → boundary translation is pure and provider-neutral."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from guildbotics.intelligences.boundary.spec import (
    DEFAULT_NAMESERVERS,
    GUEST_HOME,
    BoundaryMount,
    BoundarySpecError,
    build_boundary_spec,
    guest_path,
)
from guildbotics.intelligences.sandbox import (
    CommandNetworkRoute,
    DeniedPath,
    DerivedTree,
    DocumentGrant,
    LocalGrants,
    LocalPathGrant,
    NetworkPolicy,
    ResolvedAccess,
    ResolvedGrant,
    SandboxContract,
    SharedGrants,
    WebNetworkRoute,
    resolve_access,
)


def _contract(
    access: ResolvedAccess | None = None, **network: object
) -> SandboxContract:
    return SandboxContract(
        network=NetworkPolicy(**network) if network else NetworkPolicy(),
        access=access or ResolvedAccess(),
    )


# --- filesystem -----------------------------------------------------------------


def test_the_working_directory_is_the_only_mount_of_an_empty_contract(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()

    spec = build_boundary_spec(_contract(), cwd, home=tmp_path)

    assert spec.cwd == cwd.as_posix()
    assert spec.mounts == (BoundaryMount(cwd.as_posix(), cwd, readonly=False),)
    assert spec.env == {}


def test_documents_mount_below_the_guest_home_and_local_paths_at_their_own(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    cwd = home / "repo"
    cache = tmp_path / "cache"
    for directory in (home / "Documents" / "notes", home / "out", cwd, cache):
        directory.mkdir(parents=True)
    access = resolve_access(
        SharedGrants(
            documents=[
                DocumentGrant(path="Documents/notes", access="read"),
                DocumentGrant(path="out", access="read_write"),
            ]
        ),
        LocalGrants(paths=[LocalPathGrant(path=str(cache), access="read")]),
        search_path="",
        home=home,
    )

    spec = build_boundary_spec(_contract(access), cwd, home=home)

    assert set(spec.mounts) == {
        BoundaryMount(cwd.as_posix(), cwd, readonly=False),
        BoundaryMount(f"{GUEST_HOME}/out", home / "out", readonly=False),
        BoundaryMount(
            f"{GUEST_HOME}/Documents/notes", home / "Documents" / "notes", readonly=True
        ),
        BoundaryMount(cache.as_posix(), cache, readonly=True),
    }


def test_mounts_are_ordered_outermost_first(tmp_path: Path) -> None:
    """A nested mount needs its parent mounted before it."""
    home = tmp_path / "home"
    cwd = home / "Projects" / "repo"
    cwd.mkdir(parents=True)
    access = resolve_access(
        SharedGrants(documents=[DocumentGrant(path="Projects", access="read_write")]),
        LocalGrants(paths=[LocalPathGrant(path=str(cwd), access="read")]),
        search_path="",
        home=home,
    )

    spec = build_boundary_spec(_contract(access), cwd, home=home)

    depths = [len(PurePosixPath(m.guest).parts) for m in spec.mounts]
    assert depths == sorted(depths)
    # The working directory is read-write even where a grant names it read.
    assert [m for m in spec.mounts if m.guest == cwd.as_posix()] == [
        BoundaryMount(cwd.as_posix(), cwd, readonly=False)
    ]


def test_a_deny_inside_an_opened_tree_is_covered_wherever_that_tree_appears(
    tmp_path: Path,
) -> None:
    """The same host directory can be reachable at two guest paths."""
    home = tmp_path / "home"
    cwd = home / "Documents" / "repo"
    secret = cwd / "private"
    secret.mkdir(parents=True)
    access = resolve_access(
        SharedGrants(documents=[DocumentGrant(path="Documents", access="read")]),
        LocalGrants(deny=[str(secret)]),
        search_path="",
        home=home,
    )

    spec = build_boundary_spec(_contract(access), cwd, home=home)

    covers = {m for m in spec.mounts if m.host is None}
    assert covers == {
        BoundaryMount(f"{cwd.as_posix()}/private", None, readonly=True),
        BoundaryMount(f"{GUEST_HOME}/Documents/repo/private", None, readonly=True),
    }
    guests = [m.guest for m in spec.mounts]
    assert guests.index(f"{cwd.as_posix()}/private") > guests.index(cwd.as_posix())


def test_a_deny_outside_every_mount_or_absent_on_disk_covers_nothing(
    tmp_path: Path,
) -> None:
    """Nothing of the host is mounted unless granted, so there is nothing to
    close; and an empty mount needs a directory on the host to sit on."""
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (tmp_path / "elsewhere").mkdir()
    access = ResolvedAccess(
        denied=(
            DeniedPath(tmp_path / "elsewhere", builtin=False),
            DeniedPath(cwd / "never-created", builtin=False),
        )
    )

    spec = build_boundary_spec(_contract(access), cwd, home=tmp_path)

    assert spec.mounts == (BoundaryMount(cwd.as_posix(), cwd, readonly=False),)


def test_a_grant_that_is_denied_or_absent_is_not_mounted(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = home / "repo"
    closed = home / "closed"
    missing = home / "missing"
    for directory in (cwd, closed):
        directory.mkdir(parents=True)
    access = ResolvedAccess(
        documents=(
            ResolvedGrant(closed, "read", "closed"),
            ResolvedGrant(missing, "read", "missing", present=False),
        ),
        denied=(DeniedPath(closed, builtin=False),),
    )

    spec = build_boundary_spec(_contract(access), cwd, home=home)

    assert spec.mounts == (BoundaryMount(cwd.as_posix(), cwd, readonly=False),)


def test_the_trees_a_path_derives_are_not_mounted(tmp_path: Path) -> None:
    """The agent's tools live inside the boundary, not on the host."""
    cwd = tmp_path / "repo"
    cwd.mkdir()
    access = ResolvedAccess(
        trees=(DerivedTree(Path("/opt/homebrew"), (Path("/opt/homebrew/bin"),), "x"),)
    )

    spec = build_boundary_spec(_contract(access), cwd, home=tmp_path)

    assert spec.mounts == (BoundaryMount(cwd.as_posix(), cwd, readonly=False),)


# --- guest paths ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (PurePosixPath("/Users/x/repo"), "/Users/x/repo"),
        (PureWindowsPath("C:\\tmp\\gb\\repo"), "/c/tmp/gb/repo"),
        (PureWindowsPath("d:\\"), "/d"),
    ],
)
def test_guest_paths_keep_posix_spelling_and_map_windows_drives(
    path: PurePosixPath | PureWindowsPath, expected: str
) -> None:
    assert guest_path(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        PurePosixPath("relative"),
        PureWindowsPath("C:relative"),
        PureWindowsPath("\\\\srv\\share\\x"),
    ],
)
def test_relative_and_network_paths_have_no_guest_path(
    path: PurePosixPath | PureWindowsPath,
) -> None:
    with pytest.raises(BoundarySpecError):
        guest_path(path)


# --- network --------------------------------------------------------------------


def test_a_closed_contract_still_reaches_dns_the_provider_and_the_host_ports(
    tmp_path: Path,
) -> None:
    spec = build_boundary_spec(
        _contract(),
        tmp_path,
        host_ports=[43123, 43123],
        provider_domains=["api.openai.com", "*.openai.com"],
        home=tmp_path,
    )

    network = spec.network
    assert not network.unrestricted
    assert network.domains == ("api.openai.com", "*.openai.com")
    assert network.host_ports == (43123,)
    assert not network.local_network
    assert network.nameservers == DEFAULT_NAMESERVERS


def test_both_routes_allowlists_combine_because_the_gateway_sees_one_flow(
    tmp_path: Path,
) -> None:
    spec = build_boundary_spec(
        _contract(
            command=CommandNetworkRoute(
                mode="allowlist",
                allowed_domains=["pypi.org", "files.pythonhosted.org"],
                allow_local_network=True,
            ),
            web=WebNetworkRoute(
                mode="allowlist", allowed_domains=["pypi.org", "docs.python.org"]
            ),
        ),
        tmp_path,
        provider_domains=["api.openai.com"],
        home=tmp_path,
        nameservers=["10.0.0.53"],
    )

    network = spec.network
    assert network.domains == (
        "api.openai.com",
        "pypi.org",
        "files.pythonhosted.org",
        "docs.python.org",
    )
    assert network.local_network
    assert network.nameservers == ("10.0.0.53",)


@pytest.mark.parametrize(
    "network",
    [
        {"command": CommandNetworkRoute(mode="unrestricted")},
        {"web": WebNetworkRoute(mode="unrestricted")},
    ],
)
def test_an_unrestricted_route_on_either_side_opens_all_egress(
    tmp_path: Path, network: dict[str, object]
) -> None:
    spec = build_boundary_spec(
        _contract(**network),
        tmp_path,
        provider_domains=["api.openai.com"],
        home=tmp_path,
    )

    assert spec.network.unrestricted
    assert spec.network.domains == ()


def test_the_environment_is_exactly_what_the_caller_states(
    tmp_path: Path, monkeypatch
) -> None:
    """The boundary is another machine: the host environment is not inherited."""
    monkeypatch.setenv("OPENAI_API_KEY", "not-for-the-guest")

    spec = build_boundary_spec(
        _contract(),
        tmp_path,
        env={"GUILDBOTICS_MEMBER_BROKER_TOKEN": "t"},
        home=tmp_path,
    )

    assert spec.env == {"GUILDBOTICS_MEMBER_BROKER_TOKEN": "t"}
