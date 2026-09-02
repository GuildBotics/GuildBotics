from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from guildbotics.intelligences.sandbox import (
    FILESYSTEM_GRANTS_PATH,
    LOCAL_GRANTS_FILENAME,
    DocumentGrant,
    LocalGrants,
    LocalPathGrant,
    NetworkPolicy,
    SandboxContract,
    SandboxContractError,
    SharedGrants,
    executable_read_roots,
    load_local_grants,
    load_shared_grants,
    parse_local_grants,
    parse_network_policy,
    parse_shared_grants,
    redact_path,
    resolve_access,
    path_read_trees,
)

_CLOSED = {
    "command": {"mode": "deny", "allowed_domains": [], "allow_local_network": False},
    "web": {"mode": "deny", "allowed_domains": []},
}


def test_an_absent_network_block_is_closed_on_both_routes() -> None:
    policy = parse_network_policy(None, where="x")

    assert policy == NetworkPolicy()
    assert policy.command.mode == "deny"
    assert policy.web.mode == "deny"
    assert policy.command.allow_local_network is False


def test_a_full_network_block_round_trips() -> None:
    raw = {
        "command": {
            "mode": "allowlist",
            "allowed_domains": ["registry.npmjs.org"],
            "allow_local_network": True,
        },
        "web": {"mode": "unrestricted", "allowed_domains": []},
    }

    policy = parse_network_policy(raw, where="x")

    assert policy.model_dump(mode="json") == raw


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"command": _CLOSED["command"]}, "must state both 'command' and 'web'"),
        ("deny", "must be a mapping"),
        (
            {**_CLOSED, "command": {**_CLOSED["command"], "mode": "allowlist"}},
            "needs at least one allowed domain",
        ),
        (
            {
                **_CLOSED,
                "web": {"mode": "deny", "allowed_domains": ["docs.npmjs.com"]},
            },
            "only used with mode 'allowlist'",
        ),
        (
            {**_CLOSED, "web": {"mode": "unrestricted", "allowed_domains": [], "x": 1}},
            "Extra inputs",
        ),
        (
            {
                **_CLOSED,
                "web": {"mode": "allowlist", "allowed_domains": ["https://a.example"]},
            },
            "is not a domain name",
        ),
    ],
)
def test_partial_or_inconsistent_network_blocks_are_rejected(
    raw: object, message: str
) -> None:
    with pytest.raises(SandboxContractError, match=message) as excinfo:
        parse_network_policy(raw, where="AI CLI tool 'x'")

    assert "AI CLI tool 'x'" in str(excinfo.value)


def test_a_yaml_boolean_mode_is_rejected_rather_than_read_as_a_mode() -> None:
    """`mode: off` is `False` under YAML 1.1; it must not pass as anything."""
    raw = yaml.safe_load(
        "command:\n  mode: off\n  allowed_domains: []\n  allow_local_network: false\n"
        "web:\n  mode: off\n  allowed_domains: []\n"
    )
    assert raw["command"]["mode"] is False

    with pytest.raises(SandboxContractError):
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
    with pytest.raises(SandboxContractError):
        parse_shared_grants({"paths": []}, where="grants")


@pytest.mark.parametrize(
    "path",
    ["/etc", "C:\\Users\\x", "", ".", "..", "Documents/../secrets", "a//b", "./a"],
)
def test_a_document_grant_must_name_a_directory_below_the_home(path: str) -> None:
    """Documents are the workspace's: shared, so never a device's absolute path."""
    with pytest.raises(SandboxContractError):
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
    with pytest.raises(SandboxContractError):
        parse_local_grants({"paths": [{"path": "..", "access": "read"}]}, where="local")
    with pytest.raises(SandboxContractError):
        parse_local_grants({"deny": ["a/../b"]}, where="local")


def test_the_grant_files_are_optional(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    config = workspace / ".guildbotics/config"
    config.mkdir(parents=True)
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(config))
    monkeypatch.setattr(
        "guildbotics.intelligences.sandbox.get_workspace_local_path",
        lambda *parts: workspace.joinpath(".guildbotics", "local", *parts),
    )

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

    preview = resolve_access(shared, LocalGrants(), "", home, create=False)
    assert preview.documents[0].present is False
    assert not (home / "Projects/out").exists()
    assert preview.entries() == ()

    turn = resolve_access(shared, LocalGrants(), "", home)
    assert turn.documents[0].path == (home / "Projects/out").resolve()
    assert turn.documents[0].present is True
    assert (home / "Projects/out").is_dir()


def test_a_document_that_is_a_file_or_leaves_the_home_is_refused(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    (home / "notes").write_text("x", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SandboxContractError, match="is not a directory"):
        resolve_access(
            SharedGrants(documents=[DocumentGrant(path="notes", access="read")]),
            LocalGrants(),
            "",
            home,
        )
    with pytest.raises(SandboxContractError, match="below the home directory"):
        resolve_access(
            SharedGrants(documents=[DocumentGrant(path="link", access="read")]),
            LocalGrants(),
            "",
            home,
        )


def test_a_path_directory_opens_the_tree_its_executables_link_into(
    tmp_path: Path,
) -> None:
    """The shape of a Homebrew install: links in bin into a versioned Cellar."""
    home = _home(tmp_path)
    prefix = tmp_path / "opt/homebrew"
    real = _executable(prefix / "Cellar/node@24/24.16.0/bin/node")
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin/node").symlink_to(real)
    daemon = _executable(prefix / "Cellar/redis/7.0/bin/redis-server")
    (prefix / "sbin").mkdir()
    (prefix / "sbin/redis-server").symlink_to(daemon)

    trees, excluded = path_read_trees(
        os.pathsep.join([str(prefix / "bin"), str(prefix / "sbin")]), home
    )

    # The links in `bin` and the package in `Cellar` share the prefix, which
    # also holds the libraries the binary links (`opt/libuv`): the prefix is
    # what running anything from there needs, and both PATH entries led to it.
    assert [(t.path, t.sources) for t in trees] == [
        (prefix.resolve(), (prefix / "bin", prefix / "sbin"))
    ]
    assert excluded == ()


def test_a_link_and_package_too_far_apart_are_granted_separately(
    tmp_path: Path,
) -> None:
    """`/opt/a/bin/x` linking into `/opt/b` would share only `/opt`: too shallow."""
    home = _home(tmp_path)
    real = _executable(tmp_path / "b/lib/x/bin/x")
    (tmp_path / "a/bin").mkdir(parents=True)
    (tmp_path / "a/bin/x").symlink_to(real)
    # tmp_path itself is deep enough to be a tree; pretend it is not by
    # checking the rule directly on shallow paths.
    from guildbotics.intelligences.sandbox import _is_grantable_tree

    assert _is_grantable_tree(Path("/opt/homebrew"), home) is True
    assert _is_grantable_tree(Path("/opt"), home) is False
    assert _is_grantable_tree(home / ".local", home) is True
    assert _is_grantable_tree(home, home) is False


def test_a_root_inside_a_credential_directory_is_left_out_with_its_reason(
    tmp_path: Path,
) -> None:
    """Nobody reviews a derived grant, so a warning would not do here. The
    shape of a Codex standalone install linked from ~/.local/bin."""
    home = _home(tmp_path)
    _executable(home / ".local/bin/uv")
    release = home / ".codex/packages/standalone/releases/1.0/bin"
    _executable(release / "codex")
    (home / ".codex/packages/standalone/current").symlink_to(
        Path("releases/1.0"), target_is_directory=True
    )
    (home / ".local/bin/codex").symlink_to(
        home / ".codex/packages/standalone/current/bin/codex"
    )

    trees, excluded = path_read_trees(str(home / ".local/bin"), home)

    assert [(t.path, t.sources) for t in trees] == [
        ((home / ".local").resolve(), (home / ".local/bin",))
    ]
    assert [(t.path, t.source, t.reason) for t in excluded] == [
        (
            (home / ".codex/packages/standalone").resolve(),
            home / ".local/bin",
            "it is under ~/.codex",
        )
    ]


def test_a_version_manager_shim_directory_opens_the_manager(tmp_path: Path) -> None:
    """rbenv / pyenv expose `shims` that dispatch into `versions` beside them."""
    home = _home(tmp_path)
    _executable(home / ".rbenv/shims/ruby")
    (home / ".rbenv/versions/3.3.0/bin").mkdir(parents=True)

    trees, _ = path_read_trees(str(home / ".rbenv/shims"), home)

    assert [t.path for t in trees] == [(home / ".rbenv").resolve()]


def test_platform_directories_and_missing_entries_are_skipped(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _executable(home / ".cargo/bin/cargo")
    search_path = os.pathsep.join(
        ["/usr/bin", "/bin", "", str(tmp_path / "missing"), str(home / ".cargo/bin")]
    )

    trees, excluded = path_read_trees(search_path, home)

    assert [t.path for t in trees] == [(home / ".cargo").resolve()]
    assert excluded == ()


def test_the_builtin_denies_are_the_credential_directories_that_exist(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    (home / ".ssh").mkdir()
    (home / ".local/share/keyrings").mkdir(parents=True)
    _executable(home / ".local/bin/uv")

    access = resolve_access(
        SharedGrants(),
        LocalGrants(deny=["/opt/homebrew/etc", ".local/share/some-app"]),
        str(home / ".local/bin"),
        home,
    )

    # `~/.local` opens for uv; the keyring corner inside it closes on top.
    assert [t.path for t in access.trees] == [(home / ".local").resolve()]
    assert [(d.path, d.builtin) for d in access.denied] == [
        ((home / ".ssh").resolve(), True),
        ((home / ".local/share/keyrings").resolve(), True),
        (Path("/opt/homebrew/etc").resolve(), False),
        ((home / ".local/share/some-app").resolve(), False),
    ]
    with pytest.raises(SandboxContractError, match="whole home directory"):
        resolve_access(SharedGrants(), LocalGrants(deny=[str(home)]), "", home)


def test_a_denied_tree_is_not_granted_at_all(tmp_path: Path) -> None:
    """A deny on a derived tree removes it: the user opted out of that PATH."""
    home = _home(tmp_path)
    _executable(home / ".local/bin/uv")
    _executable(home / ".cargo/bin/cargo")

    access = resolve_access(
        SharedGrants(),
        LocalGrants(deny=[".cargo"]),
        os.pathsep.join([str(home / ".local/bin"), str(home / ".cargo/bin")]),
        home,
    )

    assert [t.path for t in access.trees] == [
        (home / ".local").resolve(),
        (home / ".cargo").resolve(),
    ]
    assert [g.path for g in access.entries()] == [(home / ".local").resolve()]


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
        "",
        home,
    )
    assert [(g.path, g.access) for g in access.paths] == [
        ((home / ".cache/uv").resolve(), "read_write"),
        (tools.resolve(), "read"),
    ]

    missing = LocalGrants(paths=[LocalPathGrant(path="/opt/nowhere", access="read")])
    with pytest.raises(SandboxContractError, match="does not exist on this device"):
        resolve_access(SharedGrants(), missing, "", home)
    # A preview points at the row instead of failing as a whole.
    preview = resolve_access(SharedGrants(), missing, "", home, create=False)
    assert [(g.path, g.present) for g in preview.paths] == [
        (Path("/opt/nowhere").resolve(), False)
    ]
    assert preview.entries() == ()
    with pytest.raises(SandboxContractError, match="whole home directory"):
        resolve_access(
            SharedGrants(),
            LocalGrants(paths=[LocalPathGrant(path=str(home), access="read")]),
            "",
            home,
        )


def test_entries_merge_documents_derived_trees_and_local_paths_once(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    uv = _executable(home / ".local/bin/uv")
    (home / "Documents/shared").mkdir(parents=True)
    (home / ".cache/uv").mkdir(parents=True)

    access = resolve_access(
        SharedGrants(
            documents=[DocumentGrant(path="Documents/shared", access="read")],
        ),
        LocalGrants(
            paths=[
                LocalPathGrant(path=".cache/uv", access="read_write"),
                # A local grant on a tree the PATH also derives keeps its
                # broader access.
                LocalPathGrant(path=".local", access="read_write"),
            ]
        ),
        str(uv.parent),
        home,
    )

    assert [(g.path, g.access) for g in access.entries()] == [
        ((home / "Documents/shared").resolve(), "read"),
        ((home / ".cache/uv").resolve(), "read_write"),
        ((home / ".local").resolve(), "read_write"),
    ]


def test_executable_read_roots_follow_link_files_and_link_directories(
    tmp_path: Path,
) -> None:
    """The shape of a Codex standalone install: a link in ~/.local/bin to a
    `current` link directory beside versioned releases."""
    home = _home(tmp_path)
    release = home / ".codex/packages/standalone/releases/1.0/bin"
    _executable(release / "codex")
    (home / ".codex/packages/standalone/current").symlink_to(
        Path("releases/1.0"), target_is_directory=True
    )
    (home / ".local/bin").mkdir(parents=True)
    invoked = home / ".local/bin/codex"
    invoked.symlink_to(home / ".codex/packages/standalone/current/bin/codex")

    roots = executable_read_roots(invoked, home)

    # The release directory sits inside `standalone` and is not repeated.
    assert roots == (home / ".local/bin", home / ".codex/packages/standalone")
    # The provider's own root, where its credentials live, is not granted.
    assert home / ".codex" not in roots


def test_executable_read_roots_never_grant_the_home_or_the_root(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    (home / "bin").mkdir(parents=True)
    real = _executable(home / "tool")
    (home / "bin/tool").symlink_to(real)

    roots = executable_read_roots(home / "bin/tool", home)

    assert roots == (home / "bin",)


def test_the_requested_policy_masks_device_paths(tmp_path: Path) -> None:
    home = _home(tmp_path)
    workspace = home / "work" / "ws"
    workspace.mkdir(parents=True)
    (home / "Documents/shared").mkdir(parents=True)
    uv = _executable(home / ".local/bin/uv")
    (home / ".ssh").mkdir()
    contract = SandboxContract(
        access=resolve_access(
            SharedGrants(
                documents=[DocumentGrant(path="Documents/shared", access="read")],
            ),
            LocalGrants(deny=[".local/share/x"]),
            str(uv.parent),
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
            {"path": "$HOME/Documents/shared", "access": "read", "present": True}
        ],
        "paths": [],
        "trees": [{"path": "$HOME/.local", "sources": ["$HOME/.local/bin"]}],
        "excluded": [],
        "denied": [
            {"path": "$HOME/.ssh", "builtin": True},
            {"path": "$HOME/.local/share/x", "builtin": False},
        ],
    }
    assert policy["network"] == _CLOSED


def test_redaction_leaves_unrelated_paths_alone(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert redact_path(Path("/opt/homebrew/bin"), home) == "/opt/homebrew/bin"
    assert redact_path(home, home) == "$HOME"
    # A sibling whose name merely starts with the home path is not inside it.
    assert redact_path(Path(str(home) + "2/x"), home) == str(home) + "2/x"
