from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict

from guildbotics.utils.fileio import get_config_path, load_yaml_file

#: Every AI CLI tool lives at ``cli_agents/<tool>/default.yml`` and a slot may
#: add ``cli_agents/<tool>/<slot>.yml`` beside it -- the same two-level shape
#: model definitions use, so both sides derive identity from the directory.
CLI_AGENT_DEFAULT_FILENAME = "default.yml"
CLI_AGENT_ROOT = "cli_agents"
#: ``cli_agents/<tool>/<slot>.yml``
_CLI_AGENT_PATH_PARTS = 3
_CLI_AGENT_TOOL_INDEX = 1


class CliAgentProvision(BaseModel):
    """How a tool is put into the agent environment, and what of it persists.

    ``package`` is the npm package the environment's snapshot installs, at
    the version this adapter was verified with: the CLI and the adapter that
    speaks to it are tested together and shipped together, so the version is
    GuildBotics' to pin. A tool that is not on npm names an ``install``
    script instead, which pins its version the same way. A tool with neither
    is not provisioned yet.

    The tool keeps its state under ``state_root`` in the home directory (the
    directory ``state_root_env`` points it at). Only the entries in
    ``persisted`` outlive a turn, bound from this device's own store:
    ``auth`` and the session directories (spelled with a trailing slash).
    Everything else under the root -- settings, skills, plugins -- is the
    snapshot's and returns to it every turn, so nothing an agent changes
    there reaches the next turn or another member.
    """

    model_config = ConfigDict(frozen=True)

    package: str = ""
    #: A shell script the snapshot build runs instead of an npm install; it
    #: must leave the CLI on the PATH at one pinned version.
    install: str = ""
    state_root: str = ""
    state_root_env: str = ""
    #: The persisted file whose presence means the tool is logged in.
    auth: str = ""
    persisted: tuple[str, ...] = ()
    #: The login command, run interactively inside the environment.
    login: tuple[str, ...] = ()
    #: The provider's own domains, which every turn may reach whatever its
    #: network mode: the tool is nothing without its API. ``*.example.com``
    #: is a suffix. GuildBotics' list, not the user's.
    api_domains: tuple[str, ...] = ()

    @property
    def provisioned(self) -> bool:
        """Whether the snapshot puts this tool into the environment."""
        return bool(self.package or self.install)

    def environment(self, home: str) -> dict[str, str]:
        """The variables that point the tool at its state root under ``home``."""
        if not self.state_root_env:
            return {}
        return {self.state_root_env: f"{home}/{self.state_root}"}


class CliAgentInfo(BaseModel):
    """A selectable AI CLI tool."""

    name: str
    label: str = ""
    order: int = 1000
    executable: str = ""
    config_reference: str = ""
    provision: CliAgentProvision = CliAgentProvision()


#: The complete catalog of selectable AI CLI tools, in display order. Every tool
#: is driven by a built-in adapter, so supporting a new one means implementing
#: an adapter here rather than dropping a YAML file into a workspace.
CLI_AGENTS: tuple[CliAgentInfo, ...] = (
    CliAgentInfo(
        name="codex",
        label="Codex",
        order=10,
        executable="codex",
        config_reference=f"{CLI_AGENT_ROOT}/codex/{CLI_AGENT_DEFAULT_FILENAME}",
        # auth.json is rewritten in place (truncated, never renamed), so a
        # file bound over it keeps every refresh. Device auth prints a URL
        # and a code instead of opening a browser the environment has not.
        provision=CliAgentProvision(
            package="@openai/codex@0.153.4",
            state_root=".codex",
            state_root_env="CODEX_HOME",
            auth="auth.json",
            persisted=("auth.json", "sessions/"),
            login=("codex", "login", "--device-auth"),
            # A ChatGPT login talks to chatgpt.com, an API key to
            # api.openai.com, and both refresh through auth.openai.com.
            api_domains=(
                "chatgpt.com",
                "*.chatgpt.com",
                "api.openai.com",
                "auth.openai.com",
                "*.openai.com",
            ),
        ),
    ),
    CliAgentInfo(
        name="claude",
        label="Claude Code",
        order=20,
        executable="claude",
        config_reference=f"{CLI_AGENT_ROOT}/claude/{CLI_AGENT_DEFAULT_FILENAME}",
        # With CLAUDE_CONFIG_DIR set, the account file `.claude.json` moves
        # under it beside the credentials, so the whole state sits in one root.
        provision=CliAgentProvision(
            package="@anthropic-ai/claude-code@2.1.263",
            state_root=".claude",
            state_root_env="CLAUDE_CONFIG_DIR",
            auth=".credentials.json",
            persisted=(".credentials.json", ".claude.json", "projects/"),
            login=("claude", "auth", "login"),
            # To be confirmed against a real turn when the adapter moves
            # into the environment.
            api_domains=("api.anthropic.com", "*.anthropic.com", "claude.ai"),
        ),
    ),
    CliAgentInfo(
        name="grok",
        label="Grok Build",
        order=30,
        executable="grok",
        config_reference=f"{CLI_AGENT_ROOT}/grok/{CLI_AGENT_DEFAULT_FILENAME}",
        # Grok Build ships as a native binary through its own installer, which
        # takes the version to install and the directory to link it from; the
        # binary itself lands under the home, so it is copied into place and
        # the installer's leftovers are removed from the snapshot's home. Its
        # own sandbox refuses to start on Linux without bubblewrap.
        provision=CliAgentProvision(
            install=(
                "apt-get update\n"
                "apt-get install -y --no-install-recommends bubblewrap\n"
                "apt-get clean\n"
                "rm -rf /var/lib/apt/lists/*\n"
                "curl -fsSL https://x.ai/cli/install.sh"
                " | GROK_BIN_DIR=/usr/local/bin bash -s 1.0.13\n"
                "cp -L /usr/local/bin/grok /usr/local/bin/grok.bin\n"
                "mv -f /usr/local/bin/grok.bin /usr/local/bin/grok\n"
                "rm -f /usr/local/bin/agent\n"
                'rm -rf "$HOME/.grok"\n'
                "grok --version"
            ),
            state_root=".grok",
            state_root_env="GROK_HOME",
            auth="auth.json",
            persisted=("auth.json", "agent_id", "sessions/"),
            login=("grok", "login", "--device-auth"),
            api_domains=("x.ai", "*.x.ai", "grok.com", "*.grok.com"),
        ),
    ),
    CliAgentInfo(
        name="copilot",
        label="GitHub Copilot",
        order=40,
        executable="copilot",
        config_reference=f"{CLI_AGENT_ROOT}/copilot/{CLI_AGENT_DEFAULT_FILENAME}",
        # Without a system credential store -- there is none in the
        # environment -- the login keeps its token in a file under the state
        # root; which file is confirmed against a real login.
        provision=CliAgentProvision(
            package="@github/copilot@1.0.83",
            state_root=".copilot",
            state_root_env="COPILOT_HOME",
            auth="config.json",  # holds `authTokens` beside the login names
            persisted=("config.json", "session-state/"),
            login=("copilot", "login", "--device-code"),
            api_domains=(
                "github.com",
                "api.github.com",
                "*.githubcopilot.com",
                "*.githubusercontent.com",
            ),
        ),
    ),
    CliAgentInfo(
        name="antigravity",
        label="Antigravity",
        order=50,
        executable="agy",
        config_reference=f"{CLI_AGENT_ROOT}/antigravity/{CLI_AGENT_DEFAULT_FILENAME}",
        # Antigravity's installer always takes the latest release and the CLI
        # updates itself, so the snapshot fetches one release by the versioned
        # URL its manifest names and checks the digest the manifest gives.
        provision=CliAgentProvision(
            install=(
                'case "$(uname -m)" in\n'
                "  x86_64) platform=linux-x64 archive=cli_linux_x64"
                " sha512=0629fe69e6949b35707935ef35da016074ea29a5d989a05f740713e0a9e927bf52ff1eada0204d3338779a469c938b6b7c5c44de2d296e5e8db255d26568de38 ;;\n"
                "  aarch64) platform=linux-arm archive=cli_linux_arm64"
                " sha512=f6dd6057a82dcbc4ab0878d99c4b84cfc45c3e2f12647eaf435322ecdd18d0190620bca943185f542431b93f34f5ea19cf84e8fdb902e64529e110bfa0a5a46f ;;\n"
                '  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;\n'
                "esac\n"
                'curl -fsSL "https://storage.googleapis.com/antigravity-public'
                '/antigravity-cli/1.2.1-5123043593420800/$platform/$archive.tar.gz"'
                " -o /tmp/agy.tar.gz\n"
                'echo "$sha512  /tmp/agy.tar.gz" | sha512sum -c -\n'
                "tar -xzf /tmp/agy.tar.gz -C /usr/local/bin antigravity\n"
                "mv /usr/local/bin/antigravity /usr/local/bin/agy\n"
                "chmod +x /usr/local/bin/agy\n"
                "rm /tmp/agy.tar.gz\n"
                "agy --version"
            ),
            # `agy` has no login command: a print-mode run without a saved
            # login prints the Google sign-in URL and takes the code on stdin.
            # Conversations span the per-conversation databases and the brain
            # transcripts; the project they belong to lives under config.
            state_root=".gemini",
            auth="antigravity-cli/antigravity-oauth-token",
            persisted=(
                "antigravity-cli/antigravity-oauth-token",
                "antigravity-cli/conversations/",
                "antigravity-cli/brain/",
                "antigravity-cli/cache/",
                "config/",
            ),
            login=("agy", "--print", "Reply with OK."),
            api_domains=(
                "cloudcode-pa.googleapis.com",
                "*.googleapis.com",
                "accounts.google.com",
            ),
        ),
    ),
)


GUI_APP_PATHS = (
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)


def cli_agent_info(name: str) -> CliAgentInfo:
    """The catalog entry for a tool name."""
    for agent in CLI_AGENTS:
        if agent.name == name:
            return agent
    raise ValueError(f"'{name}' is not a supported AI CLI tool")


def get_cli_agent_search_path(path: str | None = None) -> str:
    current = os.environ.get("PATH") if path is None else path
    if path is not None and current == "":
        return ""
    home = Path.home()
    entries = [
        str(home / ".guildbotics/bin"),
        *[entry for entry in (current or os.defpath).split(os.pathsep) if entry],
        *[
            str(home / ".local/bin"),
            str(home / "bin"),
            str(home / ".cargo/bin"),
            str(home / ".volta/bin"),
        ],
    ]
    entries.extend(GUI_APP_PATHS)
    unique: dict[str, str] = {}
    for entry in entries:
        key = os.path.normcase(os.path.normpath(entry))
        unique.setdefault(key, entry)
    return os.pathsep.join(unique.values())


def resolve_cli_agent_path(executable: str, path: str | None = None) -> str:
    if not executable:
        return ""
    return shutil.which(executable, path=get_cli_agent_search_path(path)) or ""


def cli_agent_name_from_path(path: str) -> str:
    """Return the tool a definition path belongs to.

    Identity is the directory, exactly as a model definition's provider is
    (``models/<provider>/<slot>.yml``), so one tool has one spelling.
    """
    parts = path.split("/")
    if len(parts) < _CLI_AGENT_PATH_PARTS or parts[0] != CLI_AGENT_ROOT:
        return ""
    return parts[_CLI_AGENT_TOOL_INDEX]


def cli_agent_default_path(name: str) -> str:
    """The definition path of a tool's own default."""
    return f"{CLI_AGENT_ROOT}/{name}/{CLI_AGENT_DEFAULT_FILENAME}"


def require_cli_agent_path(path: str, *, where: str) -> str:
    """Return the catalog tool a definition path names.

    The catalog is closed: a tool outside ``CLI_AGENTS`` has no adapter and
    cannot run, so every boundary that accepts a mapping rejects such a path
    here -- with a message naming the mapping entry -- instead of failing deep
    inside a turn with an unknown-adapter error.

    Args:
        path: A definition path such as ``cli_agents/<tool>/<slot>.yml``.
        where: The mapping entry to name in the error message.

    Returns:
        The tool name the path belongs to.

    Raises:
        ValueError: If the path does not name a catalog tool.
    """
    tool = cli_agent_name_from_path(path)
    if any(agent.name == tool for agent in CLI_AGENTS):
        return tool
    supported = ", ".join(agent.name for agent in CLI_AGENTS)
    raise ValueError(
        f"{where}: '{path}' does not name a supported AI CLI tool"
        f" (supported: {supported})"
    )


def cli_agent_executable(name: str) -> str:
    """Return the executable name for a catalog AI CLI tool."""
    for agent in CLI_AGENTS:
        if agent.name == name:
            return agent.executable
    return ""


def resolve_default_cli_executable() -> str:
    """Return the executable (binary) of the team's default AI CLI tool."""
    try:
        mapping = cast(
            dict[str, Any],
            load_yaml_file(get_config_path("intelligences/cli_agent_mapping.yml")),
        )
        default_file = str(mapping.get("default", ""))
    except Exception:
        return ""

    return cli_agent_executable(cli_agent_name_from_path(default_file))
