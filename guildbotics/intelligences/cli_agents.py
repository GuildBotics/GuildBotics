from __future__ import annotations

import os
import re
import shutil
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from guildbotics.utils.fileio import get_config_path, load_yaml_file

#: Every AI CLI tool lives at ``cli_agents/<tool>/default.yml`` and a slot may
#: add ``cli_agents/<tool>/<slot>.yml`` beside it -- the same two-level shape
#: model definitions use, so both sides derive identity from the directory.
CLI_AGENT_DEFAULT_FILENAME = "default.yml"
CLI_AGENT_ROOT = "cli_agents"
#: ``cli_agents/<tool>/<slot>.yml``
_CLI_AGENT_PATH_PARTS = 3
_CLI_AGENT_TOOL_INDEX = 1

#: The usage panels Claude Code and Antigravity print headlessly, without an
#: agent turn. They read the account, so running one is also what makes the
#: tool refresh an expired login. Claude Code must not pile a resumable
#: session onto disk for each one.
CLAUDE_USAGE_COMMAND = (
    "claude",
    "-p",
    "/usage",
    "--output-format",
    "json",
    "--no-session-persistence",
)
ANTIGRAVITY_USAGE_COMMAND = ("agy", "-p", "/usage", "--output-format", "json")


def _names_a_place_inside(value: str) -> bool:
    """Whether ``value`` stays inside the directory it is relative to.

    The catalog spells these the way the guest does, in POSIX, but the very
    same strings are joined onto host paths, where Windows reads ``\\`` as a
    separator and ``C:`` as a drive. So a name is checked the stricter of the
    two ways: a backslash, a drive, a leading separator or a ``..`` is not a
    name inside the directory, it is a way out of it.
    """
    name = value.rstrip("/")
    path = PureWindowsPath(name)
    return (
        bool(path.parts)
        and "\\" not in name
        and not path.anchor  # A drive, or a leading separator, on either OS.
        and ".." not in path.parts
    )


class CredentialBroker(BaseModel):
    """How a tool's login is kept off the turn's microVM and lent to it.

    The login lives sealed on this device (see ``credential_vault``), and a
    turn holds a stand-in that authenticates nothing: a credentials file of
    the same shape with no refresh token and an expiry far away, or -- for a
    tool that takes its login from a command (``stand_in_command_env``) or
    from a variable (``stand_in_env``) -- that. Either way the tool never
    tries to refresh it. A login with no refresh token and no expiry
    (``refresh_token``, ``expires_at`` and ``refresh`` left empty) lasts
    until the user signs out, and one the provider refuses is logged in
    again. The tool is pointed at a gateway outside the microVM (``base_url_env``)
    that accepts only the stand-in, only for ``routes``, and forwards the
    request to ``upstream`` -- or to the origin the route names -- with the
    real access token.

    A tool that takes its API over HTTPS only (``tls``) is answered over TLS,
    with a certificate from a CA of the turn's own that the turn trusts
    beside the system's. A host the tool reaches at a URL of its own that no
    setting moves (``relayed_hosts``) resolves, inside the turn, to a relay
    onto the gateway, which answers for that name too. What else a turn
    must reach with no credential in it is ``turn_domains``.

    What the gateway cannot reach -- refreshing the login, and the tool's
    own account endpoints its ``/usage`` reads -- runs in an environment of
    its own that holds the real credentials in memory and nothing of the
    user's (``refresh`` is the command that makes the tool refresh a login
    it is told has expired).

    The token fields are JSON paths into the credentials file ``auth``; a
    ``*`` stands for the one key an object has, where the tool names it after
    the account. A stand-in file is built from them and from ``turn_fields``
    alone -- the non-secret fields the tool needs to run a turn -- so a
    credential the file gains in a later version of the tool never reaches a
    turn.

    A tool that reads the expiry from the access token itself (``jwt``) is
    lent a stand-in shaped as one: an unsigned JWT with an expiry of its own
    and the ``stand_in_claims`` of the JWT at ``stand_in_claims_from`` (the
    account a tool reads from its id token, which the stand-in replaces
    too), with the turn's secret where the signature goes.
    """

    model_config = ConfigDict(frozen=True)

    #: Part of the sealed record's identity, so one shape is never read as
    #: another's.
    format: str
    access_token: tuple[str, ...]
    refresh_token: tuple[str, ...] = ()
    expires_at: tuple[str, ...] = ()
    #: Milliseconds since the epoch, an RFC 3339 timestamp, or the ``exp``
    #: claim of the JWT at ``expires_at``.
    expires_format: Literal["epoch_ms", "rfc3339", "jwt"] = "epoch_ms"
    #: The non-secret fields the stand-in carries beside the token and expiry.
    turn_fields: tuple[tuple[str, ...], ...] = ()
    #: Whether the stand-in names an empty refresh token, for a tool that
    #: reads no login without the field.
    empty_refresh_token: bool = False
    #: The login's JWT a ``jwt`` stand-in takes ``stand_in_claims`` from, and
    #: replaces as well as the access token.
    stand_in_claims_from: tuple[str, ...] = ()
    stand_in_claims: tuple[tuple[str, ...], ...] = ()
    #: The origin the gateway forwards a route to unless the route names one.
    upstream: str
    #: ``METHOD /path``, or ``METHOD https://origin/path``, the gateway
    #: forwards; a query string is not part of the match. A path that ends in
    #: ``/*`` forwards the paths under it (see :meth:`origin`). Everything else
    #: is refused before it leaves the device.
    routes: tuple[str, ...]
    #: The variables that point the tool at the gateway.
    base_url_env: tuple[str, ...]
    #: Appended to the gateway's origin in ``base_url_env``.
    base_url_path: str = ""
    #: The variable naming a command the tool runs for its login, for a tool
    #: that takes the stand-in that way (it prints ``access_token`` and
    #: ``expires_in`` as JSON) rather than from a file.
    stand_in_command_env: str = ""
    #: The variable the stand-in itself is given in, for a tool that takes it
    #: that way.
    stand_in_env: str = ""
    #: The variables the stand-in is given in as well, however the tool takes
    #: its login: for a part of the tool that authenticates by one of its own.
    stand_in_also_env: tuple[str, ...] = ()
    #: How a stand-in begins, for a tool that takes only tokens of a shape.
    stand_in_prefix: str = "guildbotics-stand-in-"
    #: What the tool is told beside the gateway's URL.
    turn_environment: tuple[tuple[str, str], ...] = ()
    tls: bool = False
    relayed_hosts: tuple[str, ...] = ()
    turn_domains: tuple[str, ...] = ()
    refresh: tuple[str, ...] = ()

    @field_validator("routes")
    @classmethod
    def _routes_are_method_and_path(cls, routes: tuple[str, ...]) -> tuple[str, ...]:
        seen: list[tuple[str, str]] = []
        for route in routes:
            method, _, target = route.partition(" ")
            origin, path = _split_origin(target)
            if (
                not method.isupper()
                or not path.startswith("/")
                or "?" in path
                or "*" in path.removesuffix("/*")
                or path == "/*"
                or (origin and not origin.startswith("https://"))
                # Two routes for one path: which would get the token?
                or any(
                    method == other
                    and (
                        _covers(path, taken.replace("*", "x"))
                        or _covers(taken, path.replace("*", "x"))
                    )
                    for other, taken in seen
                )
            ):
                raise ValueError(f"'{route}' is not a 'METHOD /path' of its own")
            seen.append((method, path))
        return routes

    @model_validator(mode="after")
    def _names_one_way_for_each(self) -> CredentialBroker:
        """A login that expires is refreshed, and one that does not never is;
        a stand-in reaches the turn one way; a relayed host is reached at a
        URL of its own, which is HTTPS."""
        refreshed = (self.refresh_token, self.expires_at, self.refresh)
        if any(refreshed) and not all(refreshed):
            raise ValueError("refresh_token, expires_at and refresh go together")
        if self.empty_refresh_token and not self.refresh_token:
            raise ValueError("an empty refresh token needs its place")
        if self.stand_in_env and self.stand_in_command_env:
            raise ValueError("a stand-in reaches the turn one way")
        told = [
            *self.stand_in_also_env,
            *self.base_url_env,
            *filter(None, (self.stand_in_env, self.stand_in_command_env)),
        ]
        if len(set(told)) != len(told):
            raise ValueError("a variable tells the turn one thing")
        if self.relayed_hosts and not self.tls:
            raise ValueError("relayed hosts are answered over TLS only")
        return self

    def origin(self, method: str, path: str) -> str | None:
        """The origin the gateway forwards ``method`` ``path`` to, or None
        when it forwards it nowhere."""
        for route in self.routes:
            route_method, _, target = route.partition(" ")
            origin, route_path = _split_origin(target)
            if route_method == method and _covers(route_path, path):
                return origin or self.upstream
        return None


#: A segment a ``/*`` route forwards: plain names only, so that no path the
#: upstream would resolve elsewhere (``..``, an encoded ``/`` or ``?``) is one.
_SEGMENT = re.compile(r"[A-Za-z0-9_~@:+-][A-Za-z0-9._~@:+-]*")


def _covers(route: str, path: str) -> bool:
    """Whether the route path ``route`` names ``path``: the path itself, or,
    for one that ends in ``/*``, any path of plain segments under it."""
    if not route.endswith("/*"):
        return path == route
    prefix = route.removesuffix("*")
    return path.startswith(prefix) and all(
        _SEGMENT.fullmatch(segment) for segment in path[len(prefix) :].split("/")
    )


def _split_origin(target: str) -> tuple[str, str]:
    """``https://origin/path`` as its origin and path; a bare path has none."""
    if target.startswith("/"):
        return "", target
    scheme, _, rest = target.partition("://")
    host, slash, path = rest.partition("/")
    return f"{scheme}://{host}", slash + path


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
    ``persisted`` outlive a turn, bound from this device's own store: the
    session directories (spelled with a trailing slash) and the account files
    that hold no credential. A persisted file is bound as that one file: the
    tool may rewrite it in place, but replacing it by renaming another file
    over it fails (``EBUSY``). Everything else under the root -- settings,
    skills, plugins -- is the snapshot's and returns to it every turn, so
    nothing an agent changes there reaches the next turn or another member.

    The login is ``auth``, the file the tool's login command leaves under the
    state root (where ``auth_env`` points it, for a tool that keeps it
    elsewhere). It never enters a turn: it is sealed on this device and lent
    to turns through a gateway, as ``credential_broker`` says. A provisioned
    tool always has one.
    """

    model_config = ConfigDict(frozen=True)

    package: str = ""
    #: A shell script the snapshot build runs instead of an npm install; it
    #: must leave the CLI on the PATH at one pinned version.
    install: str = ""
    state_root: str = ""
    state_root_env: str = ""
    auth: str = ""
    #: The variable that points the tool at ``auth``, for a tool whose
    #: credentials do not stay at their default place under the state root.
    auth_env: str = ""
    persisted: tuple[str, ...] = ()
    #: The login command, run interactively inside the environment.
    login: tuple[str, ...] = ()
    #: The provider's own domains, which the environments that hold the
    #: login reach: the login, its refresh, and the tool's questions about
    #: its account. ``*.example.com`` is a suffix. A turn reaches the API
    #: through the gateway instead.
    api_domains: tuple[str, ...] = ()
    credential_broker: CredentialBroker | None = None

    @field_validator("persisted")
    @classmethod
    def _entries_stay_under_the_root(
        cls, persisted: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Every persisted entry names something strictly under the state root.

        The root itself is never one of them: an entry that is the root, or
        climbs out of it, would make that allowlist say nothing.
        """
        for entry in persisted:
            if not _names_a_place_inside(entry):
                raise ValueError(f"'{entry}' is not an entry under the state root")
        return persisted

    @field_validator("state_root", "auth")
    @classmethod
    def _stays_where_it_belongs(cls, value: str) -> str:
        """The state root is a place under the home, and ``auth`` under it.

        Both are joined onto this device's store as well as onto the guest's
        home, so the same rule the allowlist follows holds for them: an empty
        value is a tool that is not provisioned yet, and anything else names
        a place inside.
        """
        if value and not _names_a_place_inside(value):
            raise ValueError(f"'{value}' is not a place inside the directory above it")
        return value

    @model_validator(mode="after")
    def _a_provisioned_login_is_brokered(self) -> CliAgentProvision:
        if self.provisioned and self.credential_broker is None:
            raise ValueError("a provisioned tool's login is brokered")
        return self

    @property
    def provisioned(self) -> bool:
        """Whether the snapshot puts this tool into the environment."""
        return bool(self.package or self.install)

    @property
    def turn_domains(self) -> tuple[str, ...]:
        """What every turn of the tool reaches directly: nothing that carries
        a credential of it."""
        broker = self.credential_broker
        return broker.turn_domains if broker else ()

    def environment(self, home: str) -> dict[str, str]:
        """The variables that point the tool at its state under ``home``."""
        root = f"{home}/{self.state_root}"
        variables = {self.state_root_env: root} if self.state_root_env else {}
        if self.auth_env:
            variables[self.auth_env] = f"{root}/{self.auth}"
        return variables


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
        # Device auth prints a URL and a code instead of opening a browser
        # the environment has not. The login is brokered: a turn holds
        # stand-in JWTs, and the adapter points ChatGPT and a model provider
        # of its own at the gateway (`base_url_env` is read by the adapter,
        # which passes it as configuration). Codex refreshes a login whose
        # access token has expired whenever it first needs it; listing the
        # models needs it, and makes no turn.
        provision=CliAgentProvision(
            package="@openai/codex@0.153.4",
            state_root=".codex",
            state_root_env="CODEX_HOME",
            auth="auth.json",
            persisted=("sessions/",),
            login=("codex", "login", "--device-auth"),
            # A ChatGPT login talks to chatgpt.com and refreshes through
            # auth.openai.com.
            api_domains=(
                "chatgpt.com",
                "*.chatgpt.com",
                "api.openai.com",
                "auth.openai.com",
                "*.openai.com",
            ),
            credential_broker=CredentialBroker(
                format="codex-chatgpt",
                access_token=("tokens", "access_token"),
                refresh_token=("tokens", "refresh_token"),
                expires_at=("tokens", "access_token"),
                expires_format="jwt",
                # Without `last_refresh` Codex sends no token at all.
                turn_fields=(
                    ("auth_mode",),
                    ("tokens", "account_id"),
                    ("last_refresh",),
                ),
                empty_refresh_token=True,
                # What Codex shows of the account and sends as its headers.
                stand_in_claims_from=("tokens", "id_token"),
                stand_in_claims=(
                    ("email",),
                    ("https://api.openai.com/auth", "chatgpt_plan_type"),
                    ("https://api.openai.com/auth", "chatgpt_account_id"),
                ),
                upstream="https://chatgpt.com",
                # Inference (compaction too), the model catalog, the rate
                # limits and settings a turn checks first, the plugins, and
                # the account's connected apps (an MCP server of ChatGPT's).
                # Analytics stay closed.
                routes=(
                    "POST /backend-api/codex/responses",
                    "GET /backend-api/codex/models",
                    "GET /backend-api/wham/usage",
                    "GET /backend-api/wham/rate-limit-reset-credits",
                    "GET /backend-api/wham/settings/user",
                    "GET /backend-api/ps/plugins/*",
                    "GET /backend-api/plugins/featured",
                    "POST /backend-api/ps/mcp",
                ),
                base_url_env=("GUILDBOTICS_CODEX_BASE_URL",),
                base_url_path="/backend-api",
                # The connected apps take the token from here, not the login.
                stand_in_also_env=("CODEX_CONNECTORS_TOKEN",),
                refresh=("codex", "debug", "models"),
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
        # Claude Code renames its files into place, but writes them in place
        # when the rename fails, so the bound files keep every refresh.
        # The login itself is brokered: a turn sends its inference through
        # ANTHROPIC_BASE_URL, while `/usage` and the refresh talk to the
        # account endpoints directly and so run where the login is.
        provision=CliAgentProvision(
            package="@anthropic-ai/claude-code@2.1.263",
            state_root=".claude",
            state_root_env="CLAUDE_CONFIG_DIR",
            auth=".credentials.json",
            persisted=(".claude.json", "projects/"),
            login=("claude", "auth", "login"),
            # A subscription login refreshes through platform.claude.com.
            api_domains=(
                "api.anthropic.com",
                "*.anthropic.com",
                "claude.ai",
                "platform.claude.com",
            ),
            credential_broker=CredentialBroker(
                format="claude-oauth",
                access_token=("claudeAiOauth", "accessToken"),
                refresh_token=("claudeAiOauth", "refreshToken"),
                expires_at=("claudeAiOauth", "expiresAt"),
                # What `/usage` and the plan checks read of the login.
                turn_fields=(
                    ("claudeAiOauth", "scopes"),
                    ("claudeAiOauth", "subscriptionType"),
                    ("claudeAiOauth", "rateLimitTier"),
                ),
                upstream="https://api.anthropic.com",
                routes=("POST /v1/messages", "POST /v1/messages/count_tokens"),
                base_url_env=("ANTHROPIC_BASE_URL",),
                # What else Claude Code sends goes to the account endpoints
                # with the stand-in, where it can only fail.
                turn_environment=(("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1"),),
                refresh=CLAUDE_USAGE_COMMAND,
            ),
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
        # the installer's leftovers are removed from the snapshot's home.
        # The login is brokered. A turn takes its stand-in from an external
        # auth provider command and sends everything to the chat proxy, which
        # GROK_CLI_CHAT_PROXY_BASE_URL points at the gateway; billing (its
        # usage) needs the real login and is read where the login is. The
        # sealed login is the auth.json `grok login` leaves, renamed into
        # place beside its lock file, with one entry named for the account.
        provision=CliAgentProvision(
            install=(
                "curl -fsSL https://x.ai/cli/install.sh"
                " | GROK_BIN_DIR=/usr/local/bin bash -s 1.0.34\n"
                "cp -L /usr/local/bin/grok /usr/local/bin/grok.bin\n"
                "mv -f /usr/local/bin/grok.bin /usr/local/bin/grok\n"
                "rm -f /usr/local/bin/agent\n"
                'rm -rf "$HOME/.grok"\n'
                "grok --version"
            ),
            state_root=".grok",
            state_root_env="GROK_HOME",
            auth="auth/auth.json",
            auth_env="GROK_AUTH_PATH",
            persisted=("agent_id", "sessions/"),
            login=("grok", "login", "--device-auth"),
            api_domains=("x.ai", "*.x.ai", "grok.com", "*.grok.com"),
            credential_broker=CredentialBroker(
                format="grok-oidc",
                access_token=("*", "key"),
                refresh_token=("*", "refresh_token"),
                expires_at=("*", "expires_at"),
                expires_format="rfc3339",
                upstream="https://cli-chat-proxy.grok.com",
                # Telemetry (`POST /v1/traces`) stays closed.
                routes=(
                    "GET /v1/user",
                    "GET /v1/settings",
                    "GET /v1/models",
                    "GET /v1/bundle/archive",
                    "GET /v1/subagents/bundle",
                    "POST /v1/responses",
                ),
                base_url_env=("GROK_CLI_CHAT_PROXY_BASE_URL",),
                base_url_path="/v1",
                stand_in_command_env="GROK_AUTH_PROVIDER_COMMAND",
                # Lists the models without a turn; it needs the login, so a
                # login told it has expired is refreshed first.
                refresh=("grok", "--no-auto-update", "models"),
            ),
        ),
    ),
    CliAgentInfo(
        name="copilot",
        label="GitHub Copilot",
        order=40,
        executable="copilot",
        config_reference=f"{CLI_AGENT_ROOT}/copilot/{CLI_AGENT_DEFAULT_FILENAME}",
        # Without a system credential store -- there is none in the
        # environment -- the login keeps its token in `config.json` at the
        # state root (after a line of comments), under a key named for the
        # account. It neither expires nor refreshes. The login is brokered:
        # a turn takes the stand-in from COPILOT_GITHUB_TOKEN, which must look
        # like a GitHub token, and both of its API URLs -- GitHub's and
        # Copilot's own -- point at the gateway. What it reads of the GitHub
        # API is the account and its policy; of Copilot's, beside inference,
        # the hosted GitHub MCP server (read-only, as the user) and the
        # repository's custom agents. The telemetry stays closed.
        provision=CliAgentProvision(
            package="@github/copilot@1.0.86",
            state_root=".copilot",
            state_root_env="COPILOT_HOME",
            auth="config.json",
            persisted=("session-state/",),
            login=("copilot", "login", "--device-code"),
            api_domains=(
                "github.com",
                "api.github.com",
                "*.githubcopilot.com",
                "*.githubusercontent.com",
            ),
            credential_broker=CredentialBroker(
                format="copilot-oauth",
                access_token=("authTokens", "*", "token"),
                upstream="https://api.individual.githubcopilot.com",
                # Inference goes where the model's supported endpoints say.
                routes=(
                    "GET https://api.github.com/copilot_internal/user",
                    "GET https://api.github.com/copilot_internal/managed_settings",
                    "GET /models",
                    "POST /chat/completions",
                    "POST /responses",
                    "POST /v1/messages",
                    "POST /mcp/readonly",
                    "GET /agents/swe/custom-agents/*",
                ),
                base_url_env=("COPILOT_DEBUG_GITHUB_API_URL", "COPILOT_API_URL"),
                stand_in_env="COPILOT_GITHUB_TOKEN",
                # 1.0.86 takes `gho_` followed by URL-safe characters.
                stand_in_prefix="gho_",
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
                "*.googleusercontent.com",
            ),
            # The login is brokered. CLOUD_CODE_URL moves the Cloud Code API,
            # over HTTPS only; the eligibility check at start reads the
            # account's profile from a URL no setting moves, and then its
            # picture, which carries no credential. Its telemetry carries the
            # token and a turn goes without it. `/usage` reads the quota
            # without a turn, refreshing a login that has expired.
            credential_broker=CredentialBroker(
                format="antigravity-oauth",
                access_token=("token", "access_token"),
                refresh_token=("token", "refresh_token"),
                expires_at=("token", "expiry"),
                expires_format="rfc3339",
                turn_fields=(("token", "token_type"), ("auth_method",)),
                upstream="https://daily-cloudcode-pa.googleapis.com",
                routes=(
                    "POST /v1internal:loadCodeAssist",
                    "POST /v1internal:fetchAvailableModels",
                    "POST /v1internal:fetchAdminControls",
                    "POST /v1internal:fetchUserInfo",
                    "POST /v1internal:retrieveUserQuotaSummary",
                    "POST /v1internal:listExperiments",
                    "POST /v1internal:streamGenerateContent",
                    # Names a conversation's owner; it carries its ID alone.
                    "POST /v1internal:writeTrajectoryAcls",
                    "GET https://www.googleapis.com/oauth2/v2/userinfo",
                ),
                base_url_env=("CLOUD_CODE_URL",),
                tls=True,
                relayed_hosts=("www.googleapis.com",),
                turn_domains=("lh3.googleusercontent.com",),
                refresh=ANTIGRAVITY_USAGE_COMMAND,
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
