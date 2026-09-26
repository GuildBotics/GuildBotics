"""The contract → boundary translation is pure and provider-neutral."""

from __future__ import annotations

import locale
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from zoneinfo import ZoneInfoNotFoundError

import pytest
import tzlocal

from guildbotics.intelligences.agent_environment.contract import (
    AccessContract,
    DeniedPath,
    DocumentGrant,
    LocalGrants,
    LocalPathGrant,
    NetworkPolicy,
    ResolvedAccess,
    ResolvedGrant,
    SharedGrants,
    resolve_access,
)
from guildbotics.intelligences.agent_environment import spec as spec_module
from guildbotics.intelligences.agent_environment.spec import (
    AgentEnvironmentSpecError,
    EnvironmentMount,
    guest_path,
    host_environment,
)
from guildbotics.intelligences.agent_environment.spec import (
    build_environment_spec as _build_environment_spec,
)
from guildbotics.utils import os_language

_NAMESERVERS = ("10.0.0.53",)


def build_environment_spec(*args: object, **kwargs: object) -> object:
    """The translation with the declaration's resolvers already supplied."""
    kwargs.setdefault("nameservers", _NAMESERVERS)
    return _build_environment_spec(*args, **kwargs)


def _contract(
    access: ResolvedAccess | None = None,
    *,
    read_only: bool = False,
    **network: object,
) -> AccessContract:
    return AccessContract(
        network=NetworkPolicy(**network) if network else NetworkPolicy(),
        access=access or ResolvedAccess(),
        read_only=read_only,
    )


# --- filesystem -----------------------------------------------------------------


def test_the_working_directory_is_the_only_mount_of_an_empty_contract(
    tmp_path: Path, host_facts: dict[str, str]
) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()

    spec = build_environment_spec(_contract(), cwd, home=tmp_path)

    assert spec.cwd == guest_path(cwd)
    assert spec.home == guest_path(tmp_path.resolve())
    assert spec.mounts == (EnvironmentMount(guest_path(cwd), cwd, readonly=False),)
    assert spec.env == host_facts


def test_every_grant_mounts_at_its_host_path(tmp_path: Path) -> None:
    """Inside and outside agree on what a path means: the guest's home is
    the host's, so a document grant sits where the user's own `~` has it."""
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
        home=home,
    )

    spec = build_environment_spec(_contract(access), cwd, home=home)

    assert spec.home == guest_path(home)
    exchange = home / "Documents" / "GuildBotics"
    assert set(spec.mounts) == {
        EnvironmentMount(guest_path(cwd), cwd, readonly=False),
        EnvironmentMount(guest_path(exchange), exchange, readonly=False),
        EnvironmentMount(guest_path(home / "out"), home / "out", readonly=False),
        EnvironmentMount(
            guest_path(home / "Documents" / "notes"),
            home / "Documents" / "notes",
            readonly=True,
        ),
        EnvironmentMount(guest_path(cache), cache, readonly=True),
    }


def test_mounts_are_ordered_outermost_first(tmp_path: Path) -> None:
    """A nested mount needs its parent mounted before it."""
    home = tmp_path / "home"
    cwd = home / "Projects" / "repo"
    cwd.mkdir(parents=True)
    access = resolve_access(
        SharedGrants(documents=[DocumentGrant(path="Projects", access="read_write")]),
        LocalGrants(paths=[LocalPathGrant(path=str(cwd), access="read")]),
        home=home,
    )

    spec = build_environment_spec(_contract(access), cwd, home=home)

    depths = [len(PurePosixPath(m.guest).parts) for m in spec.mounts]
    assert depths == sorted(depths)
    # The working directory is read-write even where a grant names it read.
    assert [m for m in spec.mounts if m.guest == guest_path(cwd)] == [
        EnvironmentMount(guest_path(cwd), cwd, readonly=False)
    ]


def test_a_deny_inside_an_opened_tree_is_covered_once_at_its_path(
    tmp_path: Path,
) -> None:
    """Nested grants share one guest path per host path, so one cover."""
    home = tmp_path / "home"
    cwd = home / "Documents" / "repo"
    secret = cwd / "private"
    secret.mkdir(parents=True)
    access = resolve_access(
        SharedGrants(documents=[DocumentGrant(path="Documents", access="read")]),
        LocalGrants(deny=[str(secret)]),
        home=home,
    )

    spec = build_environment_spec(_contract(access), cwd, home=home)

    covers = [m for m in spec.mounts if m.host is None]
    assert covers == [
        EnvironmentMount(f"{guest_path(cwd)}/private", None, readonly=False)
    ]
    guests = [m.guest for m in spec.mounts]
    assert guests.index(f"{guest_path(cwd)}/private") > guests.index(guest_path(cwd))


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

    spec = build_environment_spec(_contract(access), cwd, home=tmp_path)

    assert spec.mounts == (EnvironmentMount(guest_path(cwd), cwd, readonly=False),)


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

    spec = build_environment_spec(_contract(access), cwd, home=home)

    assert spec.mounts == (EnvironmentMount(guest_path(cwd), cwd, readonly=False),)


def test_a_read_only_contract_mounts_every_grant_read_only_over_an_empty_cwd(
    tmp_path: Path,
) -> None:
    """Whatever provider runs a read-only turn, nothing it sees of the host
    can be changed: the exchange directory and a `read_write` grant
    included. Its working directory has nothing to show, so no host
    directory backs it: it is the microVM's own; a deny inside a grant is
    still covered."""
    home = tmp_path / "home"
    cwd = home / "work"
    secret = home / "out" / "private"
    for directory in (cwd, secret, home / "Documents" / "notes"):
        directory.mkdir(parents=True)
    access = resolve_access(
        SharedGrants(
            documents=[
                DocumentGrant(path="Documents/notes", access="read"),
                DocumentGrant(path="out", access="read_write"),
            ]
        ),
        LocalGrants(deny=[str(secret)]),
        home=home,
    )

    spec = build_environment_spec(_contract(access, read_only=True), cwd, home=home)

    exchange = home / "Documents" / "GuildBotics"
    assert set(spec.mounts) == {
        EnvironmentMount(guest_path(cwd), None, readonly=False),
        EnvironmentMount(guest_path(exchange), exchange, readonly=True),
        EnvironmentMount(guest_path(home / "out"), home / "out", readonly=True),
        EnvironmentMount(
            guest_path(home / "Documents" / "notes"),
            home / "Documents" / "notes",
            readonly=True,
        ),
        EnvironmentMount(guest_path(secret), None, readonly=False),
    }


@pytest.mark.parametrize("read_only", [False, True])
def test_nothing_of_the_microvms_own_is_read_only(
    tmp_path: Path, read_only: bool
) -> None:
    """A mount nested under an empty directory of the microVM's own needs a
    mount point made there, which a read-only one refuses: the microVM does
    not start. Such a directory holds nothing of the host and is discarded
    with the microVM, so it is writable, whatever the turn is; what is bound
    under it keeps its own access."""
    home = tmp_path / "home"
    cwd = home
    granted = home / "Documents" / "notes"
    secret = home / "Documents" / "notes" / "private"
    for directory in (secret, home / ".ssh" / "keys"):
        directory.mkdir(parents=True)
    access = resolve_access(
        SharedGrants(
            documents=[DocumentGrant(path="Documents/notes", access="read_write")]
        ),
        LocalGrants(deny=[str(secret), str(home / ".ssh")]),
        home=home,
    )

    spec = build_environment_spec(
        _contract(access, read_only=read_only), cwd, home=home
    )

    own = [mount for mount in spec.mounts if mount.host is None]
    assert own
    assert not [mount for mount in own if mount.readonly]
    assert (
        EnvironmentMount(guest_path(granted), granted, readonly=read_only)
        in spec.mounts
    )


def test_what_guildbotics_binds_itself_keeps_its_access_on_a_read_only_turn(
    tmp_path: Path,
) -> None:
    """The provider's sessions are resumed, so they stay writable."""
    sessions = EnvironmentMount("/home/x/.codex/sessions", tmp_path, readonly=False)

    spec = build_environment_spec(
        _contract(read_only=True), tmp_path, home=tmp_path, mounts=[sessions]
    )

    assert sessions in spec.mounts


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
    with pytest.raises(AgentEnvironmentSpecError):
        guest_path(path)


# --- network --------------------------------------------------------------------


def test_a_closed_contract_still_reaches_dns_the_provider_and_the_host_ports(
    tmp_path: Path,
) -> None:
    spec = build_environment_spec(
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
    assert network.nameservers == _NAMESERVERS


def test_an_allowlist_adds_its_domains_after_the_providers(tmp_path: Path) -> None:
    spec = build_environment_spec(
        _contract(
            mode="allowlist",
            allowed_domains=["pypi.org", "files.pythonhosted.org", "api.openai.com"],
            allow_local_network=True,
        ),
        tmp_path,
        provider_domains=["api.openai.com"],
        home=tmp_path,
        nameservers=["10.0.0.53"],
    )

    network = spec.network
    assert network.domains == ("api.openai.com", "pypi.org", "files.pythonhosted.org")
    assert network.local_network
    assert network.nameservers == ("10.0.0.53",)


def test_unrestricted_opens_all_egress(tmp_path: Path) -> None:
    spec = build_environment_spec(
        _contract(mode="unrestricted"),
        tmp_path,
        provider_domains=["api.openai.com"],
        home=tmp_path,
    )

    assert spec.network.unrestricted
    assert spec.network.domains == ()


@pytest.mark.parametrize(
    "network",
    [
        {
            "mode": "allowlist",
            "allowed_domains": ["github.com"],
            "allow_local_network": True,
        },
        {"mode": "unrestricted"},
    ],
)
def test_a_read_only_contract_reaches_only_the_provider_and_the_host_ports(
    tmp_path: Path, network: dict[str, object]
) -> None:
    """The workspace's declaration is for turns that work; one that may
    change nothing sends nothing anywhere else."""
    spec = build_environment_spec(
        _contract(read_only=True, **network),
        tmp_path,
        host_ports=[43123],
        provider_domains=["api.openai.com"],
        home=tmp_path,
    )

    assert not spec.network.unrestricted
    assert spec.network.domains == ("api.openai.com",)
    assert spec.network.host_ports == (43123,)
    assert not spec.network.local_network


def test_the_environment_is_what_the_caller_states_and_the_host_facts(
    tmp_path: Path, monkeypatch, host_facts: dict[str, str]
) -> None:
    """The boundary is another machine: the host environment is not inherited,
    only what the guest is told of the host."""
    monkeypatch.setenv("OPENAI_API_KEY", "not-for-the-guest")

    spec = build_environment_spec(
        _contract(),
        tmp_path,
        env={"GUILDBOTICS_MEMBER_BROKER_TOKEN": "t"},
        home=tmp_path,
    )

    assert spec.env == {**host_facts, "GUILDBOTICS_MEMBER_BROKER_TOKEN": "t"}


# --- host facts -----------------------------------------------------------------


def test_the_host_time_zone_is_read_afresh_for_every_environment(
    monkeypatch,
) -> None:
    """The Desktop outlives a change of zone: a later environment is told the
    zone the host has then."""
    zones = iter(["Asia/Tokyo", "America/Los_Angeles"])
    monkeypatch.setattr(spec_module, "reload_localzone", lambda: None)
    monkeypatch.setattr(spec_module, "get_localzone_name", lambda: next(zones))

    assert host_environment()["TZ"] == "Asia/Tokyo"
    assert host_environment()["TZ"] == "America/Los_Angeles"


@pytest.mark.parametrize(
    "zone", [None, "Not/A_Zone"], ids=["no name configured", "unknown name"]
)
def test_a_zone_without_an_iana_name_leaves_the_guest_on_utc(
    monkeypatch, zone: str | None
) -> None:
    monkeypatch.setattr(spec_module, "get_localzone_name", lambda: zone)

    assert "TZ" not in host_environment()


@pytest.mark.parametrize(
    "error",
    [ZoneInfoNotFoundError("Nowhere Standard Time"), OSError("no registry")],
    ids=["windows name without a mapping", "unreadable configuration"],
)
def test_a_zone_the_host_cannot_name_leaves_the_guest_on_utc(
    monkeypatch, error: Exception
) -> None:
    def fail() -> None:
        raise error

    monkeypatch.setattr(spec_module, "reload_localzone", fail)

    assert "TZ" not in host_environment()


# The zone set here is not the one the test process started in.
@pytest.mark.filterwarnings("ignore:Timezone offset does not match system offset")
def test_the_host_time_zone_is_found_through_tzlocal(monkeypatch) -> None:
    """Without the suite's fixed zone, the name comes from the host's own
    configuration; ``TZ`` is the one every platform reads first."""
    monkeypatch.setattr(spec_module, "reload_localzone", tzlocal.reload_localzone)
    monkeypatch.setattr(spec_module, "get_localzone_name", tzlocal.get_localzone_name)
    monkeypatch.setenv("TZ", "America/New_York")

    assert host_environment()["TZ"] == "America/New_York"


def test_windows_zone_names_map_to_iana_names() -> None:
    """Windows names its zones its own way; the guest is told the IANA name
    tzlocal maps the registry's name to."""
    from tzlocal.windows_tz import win_tz

    assert win_tz["Tokyo Standard Time"] == "Asia/Tokyo"
    assert win_tz["Pacific Standard Time"] == "America/Los_Angeles"


def test_a_turn_in_the_workspace_root_gets_its_state_directory_covered(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    (home / "Documents/GuildBotics").mkdir(parents=True)
    (workspace / ".guildbotics/local/clones/aiko").mkdir(parents=True)
    access = resolve_access(SharedGrants(), LocalGrants(), home, workspace=workspace)

    spec = build_environment_spec(
        AccessContract(network=NetworkPolicy(), access=access),
        workspace,
        nameservers=_NAMESERVERS,
        home=home,
    )

    state = guest_path((workspace / ".guildbotics").resolve())
    assert EnvironmentMount(state, None, readonly=False) in spec.mounts
    # A turn in a member's clone below it is not affected: the deny is
    # outside the opened tree.
    below = build_environment_spec(
        AccessContract(network=NetworkPolicy(), access=access),
        workspace / ".guildbotics/local/clones/aiko",
        nameservers=_NAMESERVERS,
        home=home,
    )
    assert all(mount.host is not None for mount in below.mounts)


def _ui_language_on(platform: str, setting: str, monkeypatch, fake_platform) -> None:
    """Make the host an operating system of ``platform`` whose UI language is
    ``setting``, kept where that operating system keeps it."""
    fake_platform(os_language, platform)
    monkeypatch.setattr(spec_module, "os_ui_language", os_language.os_ui_language)
    for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(name, raising=False)
    if platform == "darwin":
        listed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f'(\n    "{setting}",\n    "en-US"\n)\n'
        )
        monkeypatch.setattr(os_language.subprocess, "run", lambda *_a, **_k: listed)
    elif platform == "win32":
        lcid = {v: k for k, v in locale.windows_locale.items()}[setting]

        class GetUserDefaultUILanguage:
            restype = None

            def __call__(self) -> int:
                return lcid

        kernel32 = SimpleNamespace(GetUserDefaultUILanguage=GetUserDefaultUILanguage())
        monkeypatch.setattr(
            os_language.ctypes,
            "windll",
            SimpleNamespace(kernel32=kernel32),
            raising=False,
        )
    else:
        monkeypatch.setenv("LANG", setting)


@pytest.mark.parametrize(
    ("platform", "setting", "language"),
    [
        ("darwin", "fr-FR", "fr_FR"),
        ("darwin", "zh-Hans-CN", "zh_CN"),
        ("darwin", "en", "en"),
        ("win32", "ja_JP", "ja_JP"),
        ("linux", "pt_BR.UTF-8", "pt_BR"),
    ],
)
def test_the_host_ui_language_is_told_as_language(
    monkeypatch, fake_platform, platform: str, setting: str, language: str
) -> None:
    """Each operating system keeps its UI language its own way; the guest is
    told it as gettext names it, beside the zone."""
    _ui_language_on(platform, setting, monkeypatch, fake_platform)

    told = host_environment()

    assert told == {"TZ": "Asia/Tokyo", "LANGUAGE": language}
    # A command in the guest, which is Linux, reads back what it was told.
    fake_platform(os_language, "linux")
    monkeypatch.setenv("LANGUAGE", told["LANGUAGE"])
    guest = os_language.os_ui_language()
    assert guest is not None
    assert "_".join(filter(None, (guest.language, guest.territory))) == language


@pytest.mark.parametrize("setting", ["C.UTF-8", "POSIX"])
def test_a_host_that_names_no_ui_language_tells_the_guest_none(
    monkeypatch, fake_platform, setting: str
) -> None:
    _ui_language_on("linux", setting, monkeypatch, fake_platform)

    assert host_environment() == {"TZ": "Asia/Tokyo"}
