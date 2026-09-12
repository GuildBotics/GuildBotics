from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
import yaml

from guildbotics.intelligences.agent_environment.contract import (
    AccessContract,
    AccessContractError,
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
    parse_network_policy,
    parse_shared_grants,
    redact_path,
    resolve_access,
    sensitive_grant_reason,
)
from guildbotics.utils.i18n_tool import t

_CLOSED = {"mode": "deny", "allowed_domains": [], "allow_local_network": False}


def test_an_absent_network_block_is_closed() -> None:
    policy = parse_network_policy(None, where="x")

    assert policy == NetworkPolicy()
    assert policy.model_dump(mode="json") == _CLOSED


def test_a_full_network_block_round_trips() -> None:
    raw = {
        "mode": "allowlist",
        "allowed_domains": ["registry.npmjs.org"],
        "allow_local_network": True,
    }

    policy = parse_network_policy(raw, where="x")

    assert policy.model_dump(mode="json") == raw


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (
            "deny",
            t(
                "intelligences.agent_environment.grants.network_not_a_mapping",
                where="AI CLI tool 'x'",
            ),
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
    with pytest.raises(AccessContractError, match=re.escape(message)) as excinfo:
        parse_network_policy(raw, where="AI CLI tool 'x'")

    assert "AI CLI tool 'x'" in str(excinfo.value)


def test_a_yaml_boolean_mode_is_rejected_rather_than_read_as_a_mode() -> None:
    """`mode: off` is `False` under YAML 1.1; it must not pass as anything."""
    raw = yaml.safe_load("mode: off\nallowed_domains: []\nallow_local_network: false\n")
    assert raw["mode"] is False

    with pytest.raises(AccessContractError):
        parse_network_policy(raw, where="x")


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
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    (home / "notes").write_text("x", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "link").symlink_to(outside, target_is_directory=True)

    not_a_directory = t(
        "intelligences.agent_environment.grants.document_not_a_directory", path="notes"
    )
    with pytest.raises(AccessContractError, match=re.escape(not_a_directory)):
        resolve_access(
            SharedGrants(documents=[DocumentGrant(path="notes", access="read")]),
            LocalGrants(),
            home,
        )
    outside = t(
        "intelligences.agent_environment.grants.document_outside_home", path="link"
    )
    with pytest.raises(AccessContractError, match=re.escape(outside)):
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
        LocalGrants(deny=["Projects/out/private"]),
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
    assert not access.reaches(home / "Projects/out/private/key.pem")
    assert not access.reaches(home / "Projects/absent/x.md")
    assert not access.reaches(home / "Desktop/shot.png")
    assert not access.reaches(home / ".ssh/id_ed25519", home)


def test_the_builtin_denies_are_the_credential_directories_that_exist(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    (home / ".ssh").mkdir()
    (home / ".local/share/keyrings").mkdir(parents=True)

    access = resolve_access(
        SharedGrants(),
        LocalGrants(deny=["/opt/homebrew/etc", ".local/share/some-app"]),
        home,
    )

    # `~/.local` opens for uv; the keyring corner inside it closes on top.
    assert [(d.path, d.builtin) for d in access.denied] == [
        ((home / ".ssh").resolve(), True),
        ((home / ".local/share/keyrings").resolve(), True),
        (Path("/opt/homebrew/etc").resolve(), False),
        ((home / ".local/share/some-app").resolve(), False),
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
    with pytest.raises(AccessContractError, match=re.escape(too_broad)):
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
        "working_directory": "<workspace>/.guildbotics/local/clones/aiko",
        "documents": [
            {
                "path": "$HOME/Documents/GuildBotics",
                "access": "read_write",
                "present": True,
            },
            {"path": "$HOME/Documents/shared", "access": "read", "present": True},
        ],
        "paths": [],
        "denied": [
            {"path": "$HOME/.ssh", "builtin": True},
            {"path": "$HOME/.local/share/x", "builtin": False},
        ],
    }
    assert policy["network"] == _CLOSED


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


def test_redaction_leaves_unrelated_paths_alone(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert redact_path(Path("/opt/homebrew/bin"), home) == "/opt/homebrew/bin"
    assert redact_path(home, home) == "$HOME"
    # A sibling whose name merely starts with the home path is not inside it.
    assert redact_path(Path(str(home) + "2/x"), home) == str(home) + "2/x"


def test_the_workspace_state_directory_is_a_builtin_deny(tmp_path: Path) -> None:
    """The selected workspace's `.guildbotics` closes like a credential
    directory, so a turn run in the workspace root cannot read the shared
    configuration or the other members' clones; a workspace without one
    (or none selected) adds nothing."""
    home = _home(tmp_path)
    workspace = tmp_path / "ws"
    (workspace / ".guildbotics" / "local" / "clones" / "kenji").mkdir(parents=True)

    denied = resolve_access(SharedGrants(), LocalGrants(), home, workspace=workspace)
    assert [(d.path, d.builtin) for d in denied.denied] == [
        ((workspace / ".guildbotics").resolve(), True)
    ]
    assert not denied.reaches(workspace / ".guildbotics/config/team.yml", workspace)
    assert denied.reaches(workspace / "README.md", workspace)
    assert (
        resolve_access(
            SharedGrants(), LocalGrants(), home, workspace=tmp_path / "bare"
        ).denied
        == ()
    )

    assert (
        sensitive_grant_reason(str(workspace / ".guildbotics/local"), home, workspace)
        == "<workspace>/.guildbotics"
    )
    assert sensitive_grant_reason(str(workspace / "docs"), home, workspace) == ""
