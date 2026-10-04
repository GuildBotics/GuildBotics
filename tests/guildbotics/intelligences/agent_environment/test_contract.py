from __future__ import annotations

import os
import re
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

import pytest
import yaml
from pydantic import ValidationError

from guildbotics.intelligences.agent_environment.contract import (
    AccessContract,
    AccessContractError,
    SENSITIVE_HOME_DIRECTORIES,
    DocumentGrant,
    FILESYSTEM_GRANTS_PATH,
    LOCAL_GRANTS_FILENAME,
    LocalGrants,
    LocalPathGrant,
    NetworkPolicy,
    SharedGrants,
    exchange_dir,
    exchange_tmp_dir,
    grant_spelling,
    load_local_grants,
    load_shared_grants,
    parse_local_grants,
    parse_shared_grants,
    redact_path,
    resolve_access,
    validate_mount_source,
    validate_workspace_location,
)
from guildbotics.utils.i18n_tool import t

_CLOSED = {"mode": "deny", "allowed_domains": [], "allow_local_network": False}


@pytest.mark.parametrize("state_exists", [False, True])
def test_an_inactive_registered_workspace_is_protected_before_creation(
    tmp_path, monkeypatch, state_exists
):
    from guildbotics.utils.workspace_state import register_workspace

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    inactive = tmp_path / "inactive"
    if state_exists:
        (inactive / ".guildbotics").mkdir(parents=True)
    register_workspace(inactive)
    with pytest.raises(AccessContractError):
        resolve_access(
            SharedGrants(documents=[DocumentGrant(path="new", access="read")]),
            LocalGrants(paths=[LocalPathGrant(path=str(inactive), access="read")]),
            home,
        )
    assert not (home / "new").exists()
    assert not (home / "Documents/GuildBotics").exists()
    access = resolve_access(SharedGrants(), LocalGrants(), home, create=False)
    with pytest.raises(AccessContractError):
        validate_mount_source(
            inactive / ".guildbotics/local/clones/aiko",
            access.denied,
            grant=True,
            create=True,
        )
    assert not (inactive / ".guildbotics/local").exists()


def test_workspace_locations_inside_grants_or_exchange_are_refused(
    tmp_path, monkeypatch
):
    from guildbotics.intelligences.agent_environment import contract

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(
        contract,
        "load_shared_grants",
        lambda: SharedGrants(documents=[DocumentGrant(path="shared", access="read")]),
    )
    monkeypatch.setattr(contract, "load_local_grants", LocalGrants)
    for target in (home / "shared/workspace", home / "Documents/GuildBotics/workspace"):
        with pytest.raises(AccessContractError):
            validate_workspace_location(target)
        assert not target.exists()
    assert (
        validate_workspace_location(home / "projects/workspace")
        == home / "projects/workspace"
    )


def _native(masked: str) -> str:
    """A masked path as the device running this test spells it."""
    return masked.replace("/", os.sep)


def test_an_absent_network_block_is_closed() -> None:
    policy = NetworkPolicy()

    assert policy == NetworkPolicy()
    assert policy.model_dump(mode="json") == _CLOSED


def test_a_full_network_block_round_trips() -> None:
    raw = {
        "mode": "allowlist",
        "allowed_domains": ["registry.npmjs.org"],
        "allow_local_network": True,
    }

    policy = NetworkPolicy.model_validate(raw)

    assert policy.model_dump(mode="json") == raw


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (
            "deny",
            "Input should be a valid dictionary",
        ),
        (
            {**_CLOSED, "mode": "allowlist"},
            t("intelligences.agent_environment.grants.allowlist_needs_domain"),
        ),
        (
            {**_CLOSED, "allowed_domains": ["docs.npmjs.com"]},
            t("intelligences.agent_environment.grants.domains_need_allowlist"),
        ),
        ({**_CLOSED, "x": 1}, "Extra inputs"),
        (
            {**_CLOSED, "mode": "allowlist", "allowed_domains": ["https://a.example"]},
            t(
                "intelligences.agent_environment.grants.not_a_domain",
                domain="https://a.example",
            ),
        ),
        ({"command": _CLOSED, "web": _CLOSED}, "Extra inputs"),
    ],
)
def test_inconsistent_network_blocks_are_rejected(raw: object, message: str) -> None:
    with pytest.raises(ValidationError, match=re.escape(message)):
        NetworkPolicy.model_validate(raw)


def test_a_yaml_boolean_mode_is_rejected_rather_than_read_as_a_mode() -> None:
    """`mode: off` is `False` under YAML 1.1; it must not pass as anything."""
    raw = yaml.safe_load("mode: off\nallowed_domains: []\nallow_local_network: false\n")
    assert raw["mode"] is False

    with pytest.raises(ValidationError):
        NetworkPolicy.model_validate(raw)


# --- shared grants: documents --------------------------------------------------


def test_shared_grants_parse_documents() -> None:
    grants = parse_shared_grants(
        {
            "documents": [
                {"path": "Documents/shared", "access": "read"},
                {"path": "Projects/generated", "access": "read_write"},
            ],
        },
        where="grants",
    )

    assert grants.documents == [
        DocumentGrant(path="Documents/shared", access="read"),
        DocumentGrant(path="Projects/generated", access="read_write"),
    ]
    assert parse_shared_grants(None, where="grants") == SharedGrants()
    # Nothing machine-shaped is shared: a device's paths do not belong here.
    with pytest.raises(AccessContractError):
        parse_shared_grants({"paths": []}, where="grants")


@pytest.mark.parametrize(
    "path",
    ["/etc", "C:\\Users\\x", "", ".", "..", "Documents/../secrets", "a//b", "./a"],
)
def test_a_document_grant_must_name_a_directory_below_the_home(path: str) -> None:
    """Documents are the workspace's: shared, so never a device's absolute path."""
    with pytest.raises(AccessContractError):
        parse_shared_grants(
            {"documents": [{"path": path, "access": "read"}]}, where="grants"
        )


def test_local_grants_parse_paths_and_denies_that_may_be_absolute() -> None:
    grants = parse_local_grants(
        {
            "paths": [
                {"path": ".cache/uv", "access": "read_write"},
                {"path": "/opt/homebrew", "access": "read"},
            ],
            "deny": ["/opt/homebrew/etc", ".local/share/some-app"],
        },
        where="local",
    )

    assert grants.paths[0].absolute is False
    assert grants.paths[1].absolute is True
    assert grants.deny == ["/opt/homebrew/etc", ".local/share/some-app"]
    with pytest.raises(AccessContractError):
        parse_local_grants({"paths": [{"path": "..", "access": "read"}]}, where="local")
    with pytest.raises(AccessContractError):
        parse_local_grants({"deny": ["a/../b"]}, where="local")


def test_the_grant_files_are_optional(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    config = workspace / ".guildbotics/config"
    config.mkdir(parents=True)
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(config))
    monkeypatch.setattr(
        "guildbotics.intelligences.agent_environment.contract.get_workspace_local_path",
        lambda *parts: workspace.joinpath(".guildbotics", "local", *parts),
    )

    # No shared file: nothing beyond what GuildBotics grants on its own.
    assert load_shared_grants() == SharedGrants()
    assert load_local_grants() == LocalGrants()

    shared = config / FILESYSTEM_GRANTS_PATH
    shared.parent.mkdir(parents=True)
    shared.write_text(
        "documents:\n  - path: Documents/shared\n    access: read\n", encoding="utf-8"
    )
    local = workspace / ".guildbotics/local" / LOCAL_GRANTS_FILENAME
    local.parent.mkdir(parents=True)
    local.write_text(
        "paths:\n  - path: .cache/uv\n    access: read_write\ndeny: [/opt/x]\n",
        encoding="utf-8",
    )

    assert load_shared_grants() == SharedGrants(
        documents=[DocumentGrant(path="Documents/shared", access="read")]
    )
    assert load_local_grants() == LocalGrants(
        paths=[LocalPathGrant(path=".cache/uv", access="read_write")], deny=["/opt/x"]
    )


# --- resolution on this device -------------------------------------------------


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return home


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_a_missing_document_directory_is_created_for_a_turn_and_reported_for_a_preview(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    shared = SharedGrants(
        documents=[DocumentGrant(path="Projects/out", access="read_write")]
    )

    # The built-in exchange directory leads; the file's own entry follows.
    preview = resolve_access(shared, LocalGrants(), home, create=False)
    assert preview.documents[1].present is False
    assert not (home / "Projects/out").exists()

    turn = resolve_access(shared, LocalGrants(), home)
    assert turn.documents[1].path == (home / "Projects/out").resolve()
    assert turn.documents[1].present is True
    assert (home / "Projects/out").is_dir()
    assert (home / "Documents/GuildBotics").is_dir()


def test_a_document_that_is_a_file_or_leaves_the_home_is_refused(
    tmp_path: Path, symlinks
) -> None:
    home = _home(tmp_path)
    (home / "notes").write_text("x", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "link").symlink_to(outside, target_is_directory=True)

    not_a_directory = t(
        "intelligences.agent_environment.grants.document_not_a_directory", path="notes"
    )
    with pytest.raises(AccessContractError):
        resolve_access(
            SharedGrants(documents=[DocumentGrant(path="notes", access="read")]),
            LocalGrants(),
            home,
        )
    outside = t(
        "intelligences.agent_environment.grants.document_outside_home", path="link"
    )
    with pytest.raises(AccessContractError):
        resolve_access(
            SharedGrants(documents=[DocumentGrant(path="link", access="read")]),
            LocalGrants(),
            home,
        )


def test_the_exchange_directory_is_under_the_home(tmp_path: Path) -> None:
    home = _home(tmp_path)

    assert exchange_dir(home) == home / "Documents/GuildBotics"
    assert exchange_tmp_dir(home) == home / "Documents/GuildBotics/tmp"
    # The built-in grant and the directory the Desktop writes to are the same
    # place, so a pasted file is reachable whatever the grants file says -- an
    # entry for it there is ignored, not merged into a second row.
    listed = SharedGrants(
        documents=[DocumentGrant(path="Documents/GuildBotics", access="read")]
    )
    for shared in (SharedGrants(), listed):
        (grant,) = resolve_access(shared, LocalGrants(), home, create=False).documents
        assert (grant.path, grant.access, grant.builtin, grant.present) == (
            exchange_dir(home),
            "read_write",
            True,
            False,
        )


def test_reaches_answers_what_the_mounts_would_show(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / "Documents/GuildBotics/tmp").mkdir(parents=True)
    (home / ".ssh").mkdir()
    (home / "Projects/out/private").mkdir(parents=True)
    cwd = tmp_path / "clone"
    cwd.mkdir()
    access = resolve_access(
        SharedGrants(
            documents=[
                DocumentGrant(path="Projects/out", access="read"),
                DocumentGrant(path="Projects/absent", access="read"),
            ]
        ),
        LocalGrants(deny=["Projects/private"]),
        home,
        create=False,
    )

    assert access.reaches(home / "Documents/GuildBotics/tmp/a.png")
    assert access.reaches(home / "Projects/out/report.md")
    assert access.reaches(cwd / "src/main.py", cwd)
    # Outside every opened root, inside a closed corner, under a document
    # directory that does not exist here, or in the working directory of a
    # turn that has not been named: not reachable.
    assert not access.reaches(cwd / "src/main.py")
    assert not access.reaches(home / "Projects/private/key.pem")
    assert not access.reaches(home / "Projects/absent/x.md")
    assert not access.reaches(home / "Desktop/shot.png")
    assert not access.reaches(home / ".ssh/id_ed25519", home)


def test_the_builtin_denies_include_absent_credential_directories(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    (home / ".ssh").mkdir()
    (home / ".local/share/keyrings").mkdir(parents=True)
    private = tmp_path / "private"

    access = resolve_access(
        SharedGrants(),
        LocalGrants(deny=[str(private), ".local/share/some-app"]),
        home,
    )

    assert [(d.path, d.builtin) for d in access.denied] == [
        *((home / name, True) for name in SENSITIVE_HOME_DIRECTORIES),
        (tmp_path / ".guildbotics", True),
        (private, False),
        (home / ".local/share/some-app", False),
    ]
    too_broad = t(
        "intelligences.agent_environment.grants.deny_too_broad", path=str(home)
    )
    with pytest.raises(AccessContractError, match=re.escape(too_broad)):
        resolve_access(SharedGrants(), LocalGrants(deny=[str(home)]), home)


def test_a_local_path_must_exist_on_this_device(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / ".cache/uv").mkdir(parents=True)
    tools = tmp_path / "tools"
    tools.mkdir()

    access = resolve_access(
        SharedGrants(),
        LocalGrants(
            paths=[
                LocalPathGrant(path=".cache/uv", access="read_write"),
                LocalPathGrant(path=str(tools), access="read"),
            ]
        ),
        home,
    )
    assert [(g.path, g.access) for g in access.paths] == [
        ((home / ".cache/uv").resolve(), "read_write"),
        (tools.resolve(), "read"),
    ]

    missing = LocalGrants(paths=[LocalPathGrant(path="/opt/nowhere", access="read")])
    missing_here = t(
        "intelligences.agent_environment.grants.local_path_missing", path="/opt/nowhere"
    )
    with pytest.raises(AccessContractError, match=re.escape(missing_here)):
        resolve_access(SharedGrants(), missing, home)
    # A preview points at the row instead of failing as a whole.
    preview = resolve_access(SharedGrants(), missing, home, create=False)
    assert [(g.path, g.present) for g in preview.paths] == [
        (Path("/opt/nowhere").resolve(), False)
    ]
    too_broad = t(
        "intelligences.agent_environment.grants.local_path_too_broad", path=str(home)
    )
    with pytest.raises(AccessContractError):
        resolve_access(
            SharedGrants(),
            LocalGrants(paths=[LocalPathGrant(path=str(home), access="read")]),
            home,
        )


def test_the_requested_policy_masks_device_paths(tmp_path: Path) -> None:
    home = _home(tmp_path)
    workspace = home / "work" / "ws"
    workspace.mkdir(parents=True)
    (home / "Documents/shared").mkdir(parents=True)
    (home / ".ssh").mkdir()
    contract = AccessContract(
        access=resolve_access(
            SharedGrants(
                documents=[DocumentGrant(path="Documents/shared", access="read")],
            ),
            LocalGrants(deny=[".local/share/x"]),
            home,
        )
    )

    policy = contract.requested_policy(
        workspace / ".guildbotics/local/clones/aiko",
        home=home.resolve(),
        workspace_root=workspace.resolve(),
    )

    assert policy["filesystem"] == {
        "working_directory": _native("<workspace>/.guildbotics/local/clones/aiko"),
        "documents": [
            {
                "path": _native("$HOME/Documents/GuildBotics"),
                "access": "read_write",
                "present": True,
            },
            {
                "path": _native("$HOME/Documents/shared"),
                "access": "read",
                "present": True,
            },
        ],
        "paths": [],
        "denied": [
            *(
                {"path": _native("$HOME/" + name), "builtin": True}
                for name in SENSITIVE_HOME_DIRECTORIES
            ),
            {"path": str(tmp_path / ".guildbotics"), "builtin": True},
            {"path": _native("$HOME/.local/share/x"), "builtin": False},
        ],
    }
    assert policy["network"] == _CLOSED
    assert policy["read_only"] is False


def test_the_requested_policy_records_what_a_read_only_turn_reaches(
    tmp_path: Path,
) -> None:
    """The declared network is recorded as the turn reaches it: not at all."""
    allowlist = NetworkPolicy(mode="allowlist", allowed_domains=["github.com"])

    policy = AccessContract(network=allowlist, read_only=True).requested_policy(
        tmp_path, home=tmp_path
    )

    assert policy["read_only"] is True
    assert policy["network"] == _CLOSED
    assert AccessContract(network=allowlist).requested_policy(tmp_path, home=tmp_path)[
        "network"
    ] == allowlist.model_dump(mode="json")


def test_grant_spelling_is_how_the_grant_file_names_a_path() -> None:
    # Relative to the home when under it, absolute otherwise, in the OS's own
    # separators: what a device reports for a tree is exactly what closing it
    # with `deny` is written as, on Windows too.
    home = PureWindowsPath("C:/Users/me")
    assert grant_spelling(PureWindowsPath("C:/Users/me/AppData/Local/x"), home) == (
        "AppData\\Local\\x"
    )
    assert grant_spelling(PureWindowsPath("C:/Program Files/Git"), home) == (
        "C:\\Program Files\\Git"
    )
    posix = PurePosixPath("/Users/me")
    assert grant_spelling(PurePosixPath("/Users/me/.local"), posix) == ".local"
    assert grant_spelling(PurePosixPath("/opt/homebrew"), posix) == "/opt/homebrew"
    assert grant_spelling(posix, posix) == "/Users/me"


@pytest.mark.parametrize(
    ("pure", "home", "outside", "sibling"),
    [
        (
            PureWindowsPath,
            "C:/Users/me",
            "C:/Program Files/Git",
            "C:/Users/me2/x",
        ),
        (PurePosixPath, "/Users/me", "/opt/homebrew/bin", "/Users/me2/x"),
    ],
    ids=["windows", "posix"],
)
def test_redaction_leaves_unrelated_paths_alone(
    pure: type[PurePath], home: str, outside: str, sibling: str
) -> None:
    # The masked path keeps the separators of the device it names, so both
    # spellings are checked wherever this runs.
    assert redact_path(pure(outside), pure(home)) == str(pure(outside))
    assert redact_path(pure(home), pure(home)) == "$HOME"
    # A sibling whose name merely starts with the home path is not inside it.
    assert redact_path(pure(sibling), pure(home)) == str(pure(sibling))


def test_redaction_spells_the_masked_path_as_its_own_device_does() -> None:
    assert (
        redact_path(
            PureWindowsPath("C:/Users/me/AppData/Local/x"),
            PureWindowsPath("C:/Users/me"),
        )
        == "$HOME\\AppData\\Local\\x"
    )
    assert (
        redact_path(PurePosixPath("/Users/me/.local/x"), PurePosixPath("/Users/me"))
        == "$HOME/.local/x"
    )


def test_the_workspace_state_directory_is_a_builtin_deny(tmp_path: Path) -> None:
    """The selected workspace's `.guildbotics` closes like a credential
    directory, so a turn run in the workspace root cannot read the shared
    configuration or the other members' clones; a workspace without one
    (or none selected) adds nothing."""
    home = _home(tmp_path)
    workspace = tmp_path / "ws"
    (workspace / ".guildbotics" / "local" / "clones" / "kenji").mkdir(parents=True)

    denied = resolve_access(SharedGrants(), LocalGrants(), home, workspace=workspace)
    assert (workspace / ".guildbotics", True) in [
        (d.path, d.builtin) for d in denied.denied
    ]
    assert not denied.reaches(workspace / ".guildbotics/config/team.yml", workspace)
    assert denied.reaches(workspace / "README.md", workspace)
    bare = resolve_access(
        SharedGrants(), LocalGrants(), home, workspace=tmp_path / "bare"
    )
    assert any(d.path == tmp_path / "bare/.guildbotics" for d in bare.denied)
