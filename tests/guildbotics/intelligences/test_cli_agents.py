import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from guildbotics.intelligences import cli_agents
from guildbotics.intelligences.cli_agents import (
    CLI_AGENTS,
    CliAgentProvision,
    CredentialBroker,
    cli_agent_default_path,
    cli_agent_name_from_path,
    require_cli_agent_path,
    resolve_default_cli_agent,
)


def test_a_tool_is_identified_by_its_definition_directory() -> None:
    """One tool, one spelling -- the same rule a model path follows."""
    assert cli_agent_name_from_path("cli_agents/codex/default.yml") == "codex"
    assert cli_agent_name_from_path("cli_agents/codex/reviewer.yml") == "codex"
    assert cli_agent_name_from_path("cli_agents/copilot/default.yml") == "copilot"
    # Anything that is not a definition path names no tool.
    assert cli_agent_name_from_path("codex") == ""
    assert cli_agent_name_from_path("cli_agents/codex.yml") == ""


def test_a_tools_default_definition_path_is_derived_from_its_name() -> None:
    assert cli_agent_default_path("codex") == "cli_agents/codex/default.yml"


def test_a_catalog_tools_definition_path_is_accepted() -> None:
    assert require_cli_agent_path("cli_agents/codex/default.yml", where="x") == "codex"
    assert require_cli_agent_path("cli_agents/codex/reviewer.yml", where="x") == "codex"


@pytest.mark.parametrize(
    "path",
    [
        # The catalog is closed: no adapter, no run.
        "cli_agents/mytool/default.yml",
        # A path that names no tool at all is just as unrunnable.
        "codex",
        "cli_agents/codex.yml",
    ],
)
def test_a_path_outside_the_catalog_is_rejected_with_the_entry_named(
    path: str,
) -> None:
    with pytest.raises(ValueError) as excinfo:
        require_cli_agent_path(path, where="AI CLI tool slot 'default'")

    message = str(excinfo.value)
    assert "AI CLI tool slot 'default'" in message
    assert path in message
    # The message teaches the fix: it lists every tool that can run.
    assert "codex, claude, grok, copilot, antigravity" in message


def test_every_catalog_tool_ships_a_definition_template() -> None:
    """A tool the editor offers must have a file its effort mapping lands in."""
    template_dir = (
        Path(__file__).parents[3] / "guildbotics/templates/intelligences/cli_agents"
    )
    shipped = {default.parent.name for default in template_dir.glob("*/default.yml")}

    assert shipped == {agent.name for agent in CLI_AGENTS}


def test_the_catalog_is_ordered_and_names_each_tools_binary() -> None:
    agents = {agent.name: agent for agent in CLI_AGENTS}

    assert agents["codex"].executable == "codex"
    assert agents["codex"].config_reference == "cli_agents/codex/default.yml"
    assert agents["grok"].label == "Grok Build"
    # Antigravity is the one tool whose binary is not named after it.
    assert agents["antigravity"].executable == "agy"
    assert [agent.order for agent in CLI_AGENTS] == sorted(
        agent.order for agent in CLI_AGENTS
    )


def test_resolve_default_cli_agent_names_the_catalog_tool(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    mapping = tmp_path / "intelligences/cli_agent_mapping.yml"
    mapping.parent.mkdir(parents=True, exist_ok=True)
    mapping.write_text(
        "default: cli_agents/antigravity/default.yml\n", encoding="utf-8"
    )

    assert resolve_default_cli_agent() == "antigravity"


def test_resolve_default_cli_agent_mapping_load_failure(monkeypatch) -> None:
    def _raise(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(cli_agents, "load_yaml_file", _raise)

    assert resolve_default_cli_agent() == ""


def test_resolve_default_cli_agent_outside_the_catalog(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    mapping = tmp_path / "intelligences/cli_agent_mapping.yml"
    mapping.parent.mkdir(parents=True, exist_ok=True)
    mapping.write_text("default: cli_agents/ghost/default.yml\n", encoding="utf-8")

    # No catalog tool is named "ghost".
    assert resolve_default_cli_agent() == ""


def test_every_provisioned_tool_names_its_api_domains_and_login() -> None:
    """A turn with closed egress still reaches the provider; the tool is
    nothing without its API, and a login that cannot be run cannot persist."""
    from guildbotics.intelligences.cli_agents import CLI_AGENTS

    for agent in CLI_AGENTS:
        provision = agent.provision
        assert provision.provisioned, agent.name
        # One way in: npm, or a script that pins its own version.
        assert bool(provision.package) != bool(provision.install), agent.name
        assert provision.api_domains, agent.name
        assert provision.login and provision.auth and provision.state_root, agent.name
        # The login is sealed, never persisted, nor inside what is.
        assert provision.credential_broker is not None, agent.name
        assert not any(
            provision.auth == entry or provision.auth.startswith(entry)
            for entry in provision.persisted
        ), agent.name
        expected = (
            {provision.state_root_env: f"/h/{provision.state_root}"}
            if provision.state_root_env
            else {}
        )
        if provision.auth_env:
            expected[provision.auth_env] = f"/h/{provision.state_root}/{provision.auth}"
        assert provision.environment("/h") == expected, agent.name


#: Spellings that leave the directory they are relative to, on either OS.
#: The catalog is written in POSIX, but these strings are joined onto host
#: paths too, where Windows reads `\` as a separator and `C:` as a drive.
_WAYS_OUT = (
    "./",
    ".",
    "/etc",
    "sessions/../..",
    "../outside",
    "..\\outside",
    "C:\\outside",
    "C:outside",
    "\\\\host\\share",
    "sessions\\..\\..",
)


@pytest.mark.parametrize("way_out", _WAYS_OUT)
def test_a_provision_only_names_places_inside_the_directory_above(way_out: str) -> None:
    """`persisted` is the allowlist of what outlives a turn, so the root is
    never one of its entries -- not even for a tool whose root has to be
    writable, which is exactly where everything else the tool reads
    (instructions, hooks, MCP servers, plugins, permissions) sits. The state
    root and the credentials are joined onto the same host store, so they
    follow the rule too."""
    with pytest.raises(ValidationError):
        CliAgentProvision(state_root=".tool", persisted=(way_out,))
    with pytest.raises(ValidationError):
        CliAgentProvision(state_root=".tool", auth=way_out)
    with pytest.raises(ValidationError):
        CliAgentProvision(state_root=way_out)


def test_a_tool_that_is_not_provisioned_yet_names_nothing() -> None:
    """Naming nothing is how an unprovisioned tool is spelled; naming the
    empty entry in the allowlist is something else -- it is the root."""
    assert CliAgentProvision().persisted == ()

    with pytest.raises(ValidationError):
        CliAgentProvision(state_root=".tool", persisted=("",))


#: Where each tool refreshes its login -- or, for one that never refreshes,
#: reads its account -- as observed from the tool itself. A tool added to the
#: catalog must be looked up and listed here.
_ACCOUNT_HOSTS = {
    "codex": "auth.openai.com",
    "claude": "platform.claude.com",
    "grok": "auth.x.ai",
    "copilot": "api.github.com",
    "antigravity": "oauth2.googleapis.com",
}


def test_every_tool_reaches_its_account_where_its_login_is() -> None:
    """A login outlives an access token only if the environment that holds
    it can refresh it, and the tool's usage only if it can read it there."""
    assert set(_ACCOUNT_HOSTS) == {agent.name for agent in CLI_AGENTS}
    for agent in CLI_AGENTS:
        host = _ACCOUNT_HOSTS[agent.name]
        assert any(
            host == domain or (domain.startswith("*.") and host.endswith(domain[1:]))
            for domain in agent.provision.api_domains
        ), agent.name


def _broker(*routes: str) -> CredentialBroker:
    return CredentialBroker(
        format="t",
        access_token=("a",),
        refresh_token=("r",),
        expires_at=("e",),
        upstream="https://api.example.test",
        routes=routes,
        base_url_env=("API_URL",),
        refresh=("tool", "refresh"),
    )


def test_a_route_is_forwarded_to_the_origin_it_names_or_to_the_upstream() -> None:
    broker = _broker("POST /v1:generate", "GET https://profile.example.test/me")

    assert broker.origin("POST", "/v1:generate") == "https://api.example.test"
    assert broker.origin("GET", "/me") == "https://profile.example.test"
    assert broker.origin("GET", "/v1:generate") is None
    assert broker.origin("GET", "/me/more") is None


@pytest.mark.parametrize(
    ("path", "forwarded"),
    [
        ("/agents/owner/repo", True),
        ("/agents/owner", True),
        ("/agents/Owner-1/repo.name_2", True),
        ("/agents", False),
        ("/agents/", False),
        ("/agentsX/owner", False),
        ("/agents/owner//repo", False),
        ("/agents/../me", False),
        ("/agents/owner/../../me", False),
        ("/agents/./owner", False),
        ("/agents/owner?x", False),
        ("/agents/owner#x", False),
        ("/agents/owner/%2e%2e", False),
    ],
)
def test_a_route_ending_in_a_star_forwards_the_plain_paths_under_it(
    path: str, forwarded: bool
) -> None:
    """Nothing it forwards is a path the upstream would take for another."""
    broker = _broker("GET /agents/*")

    assert (broker.origin("GET", path) is not None) == forwarded
    assert broker.origin("POST", "/agents/owner") is None


def test_a_path_under_a_star_route_may_be_one_of_its_own() -> None:
    """Only a path the star route would forward is taken by it."""
    broker = _broker("GET /me", "GET /me/*", "POST /me/more", "GET /me/.hidden")

    assert broker.origin("GET", "/me") == broker.origin("GET", "/me/more")
    assert broker.origin("GET", "/me/.hidden") == "https://api.example.test"


@pytest.mark.parametrize(
    "routes",
    [
        ("post /v1",),
        ("POST v1",),
        ("POST /v1?x=1",),
        ("GET http://profile.example.test/me",),
        ("GET https://profile.example.test",),
        # One path, two places to send it: which would get the token?
        ("GET /me", "GET https://profile.example.test/me"),
        ("GET /me/*", "GET https://profile.example.test/me/more"),
        ("GET /me/more", "GET /me/*"),
        ("GET /me/*", "GET /me/more/*"),
        ("GET /me/*", "GET /me/*"),
        ("GET /*",),
        ("GET /me/*/more",),
    ],
)
def test_a_route_that_does_not_name_one_place_is_refused(
    routes: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError):
        _broker(*routes)


def test_a_relayed_host_is_answered_over_tls_only() -> None:
    """The relay listens where the tool's own https URL leads."""
    fields = _broker("GET /me").model_dump()
    with pytest.raises(ValidationError):
        CredentialBroker(**{**fields, "relayed_hosts": ("profile.example.test",)})
    assert CredentialBroker(
        **{**fields, "relayed_hosts": ("profile.example.test",), "tls": True}
    ).tls


def test_a_provisioned_tool_without_a_brokered_login_is_refused() -> None:
    """A login a turn would hold is the one thing a turn must never hold."""
    with pytest.raises(ValidationError):
        CliAgentProvision(package="tool@1", state_root=".tool", auth="auth.json")


@pytest.mark.parametrize(
    "fields",
    [
        {"refresh": ()},
        {"expires_at": ()},
        {
            "refresh_token": (),
            "expires_at": (),
            "refresh": (),
            "empty_refresh_token": True,
        },
        {"stand_in_env": "TOKEN", "stand_in_command_env": "TOKEN_COMMAND"},
        {"stand_in_env": "TOKEN", "stand_in_also_env": ("TOKEN",)},
        {"stand_in_also_env": ("API_URL",)},
    ],
    ids=[
        "expires-unrefreshed",
        "refreshed-never-expiring",
        "no-place",
        "two-ways",
        "one-variable-twice",
        "a-variable-for-two-things",
    ],
)
def test_a_login_is_lent_and_refreshed_one_whole_way(fields: dict[str, object]) -> None:
    """A login that expires is refreshed and one that does not never is;
    anything between fails at a turn instead of here."""
    with pytest.raises(ValidationError):
        CredentialBroker(**{**_broker("GET /me").model_dump(), **fields})
    assert (
        CredentialBroker(
            **{
                **_broker("GET /me").model_dump(),
                "refresh_token": (),
                "expires_at": (),
                "refresh": (),
            }
        ).refresh
        == ()
    )
