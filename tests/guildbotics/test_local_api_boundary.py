"""Every route this machine's server credential, or what it guards, travels
is classified here.

A token reaches only a destination that showed it knows the token: in Desktop,
the port the host's own child announced, dropped once the child exits; for
host clients, a process that proved it knows the token's digest. A browser's
session lives in its cookie and the CSRF value the API hands the page. That
holds only for the routes drawn into it, so the population is taken from the
code of all three languages, not from the routes someone happened to think of:

- Python: every importer of the client module, the owner file's, cookie's and
  CSRF header's names, and every caller of a client that sends through it
- the frontend: every function that enters, resumes or refreshes a session,
  sends the CSRF value, consents for an app, or asks the host for an entry link
- the host: every function that reads the owner token, writes it to the wire,
  or hands an entry link to a window

A new one fails here until it is classified.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import guildbotics
from guildbotics.app_api import auth, server
from guildbotics.utils import local_api

PACKAGE = Path(guildbotics.__file__).parent
REPOSITORY = PACKAGE.parent
FRONTEND = REPOSITORY / "desktop" / "src"
HOST = REPOSITORY / "desktop" / "src-tauri" / "src"

#: ``module -> names`` imported from ``guildbotics.utils.local_api``.
LOCAL_API_IMPORTS = {
    # The server: it reads its own records and the port it listens on.
    "app_api/api.py": {"API_PREFIX", "PROOF_PATH", "server_port"},
    "app_api/auth.py": {
        "NONCE_PATTERN",
        "TOKEN_ID_PATTERN",
        "base_url",
        "local_api_proof",
        "new_token",
        "owner_token_path",
        "parse_token",
        "server_directory",
    },
    "app_api/server.py": {"server_port"},
    # Where a browser comes back to: an address, no credential.
    "integrations/provider.py": {"base_url", "server_port"},
    # Proof-gated clients: the token is attached only after the proof.
    "cli/desktop_commands.py": {"connect_local_api"},
    "cli/member_client.py": {"connect_local_api"},
    "cli/service.py": {"connect_local_api", "server_port"},
    "hub/secret_transport.py": {"connect_local_api"},
    # ``open`` through the proof-gated client; ``connect`` keeps the token the
    # consent exchange answers, which it posted only the code and verifier for.
    "cli/server.py": {
        "API_PREFIX",
        "base_url",
        "connect_local_api",
        "keep_client_token",
        "server_port",
    },
}

#: ``(module, client function)`` for every caller of a proof-gated client.
CLIENT_CALLERS = {
    ("cli/run.py", "run_on_desktop"),
    ("cli/hub.py", "secret_transport.execute"),
    ("secrets/hub_client.py", "secret_transport.execute"),
}

#: ``(file, function) -> route`` in the frontend.
FRONTEND_ROUTES = {
    ("api/client.ts", ""): "the CSRF value, module-private, and its header name",
    ("api/client.ts", "enterSession"): "exchanges the entry code, keeps the value",
    ("api/client.ts", "resumeSession"): "reads the value of the cookie's session",
    ("api/client.ts", "send"): "refreshes a refused value once",
    ("api/client.ts", "attempt"): "header, on writes only",
    ("api/client.ts", "authorizeApp"): "consent, through request",
    ("api/client.ts", "onConnectionLost"): "a 401 ends the session's screens",
    ("api/backend.ts", "backendEntry"): "asks the host for an entry link",
    ("Bootstrap.tsx", ""): "import",
    ("Bootstrap.tsx", "Bootstrap"): "replaces the bundled page with the link",
    ("main.tsx", ""): "import",
    ("quick/main.tsx", ""): "import",
    ("session/entryPages.ts", ""): "the query resuming the session",
    ("session/EnterView.tsx", ""): "import",
    ("session/EnterView.tsx", "EnterView"): "the entry page's exchange",
    ("session/EntryPage.tsx", ""): "import",
    ("session/EntryPage.tsx", "AppSession"): "no session, no screens",
    ("session/AuthorizeView.tsx", ""): "import",
    ("session/AuthorizeView.tsx", "AuthorizeView"): "consent with the session",
}
_FRONTEND_CARRIERS = re.compile(
    r"X-GuildBotics-CSRF|\bcsrf\b|enterSession|resumeSession|authorizeApp"
    r"|backend_entry|backendEntry|/session\b|SESSION_QUERY|onConnectionLost"
)
_FRONTEND_FUNCTION = re.compile(
    r"^(?:export )?(?:default )?(?:async )?function\*? ?(\w+)"
    r"|^(?:export )?(?:const|let) (\w+) = (?:async )?\("
)

#: ``(file, function) -> route`` in the host.
HOST_ROUTES = {
    ("lib.rs", ""): "BackendState holds the token's path; the notice prefix",
    ("lib.rs", "owner_token_path"): "where the server keeps it",
    ("lib.rs", "read_owner_token"): "reads it",
    ("lib.rs", "after_stdout"): "only the notice for the port asked makes it Ready",
    ("lib.rs", "backend_request"): "Bearer header, only while Ready",
    ("lib.rs", "request_entry_link"): "through backend_request",
    ("lib.rs", "backend_entry_for"): "hands the bundled page the link",
    ("lib.rs", "request_backend_shutdown"): "through backend_request",
    ("lib.rs", "backend_has_active_work"): "through backend_request",
    ("lib.rs", "quit_needs_confirmation"): "through backend_has_active_work",
    ("lib.rs", "stop_backend"): "through request_backend_shutdown",
    ("lib.rs", "run"): "the token's path into the state, nothing to the sidecar",
}
_HOST_CARRIERS = re.compile(
    r"owner_token|Authorization|Bearer|PORT_NOTICE_PREFIX|TcpStream::connect"
    r"|backend_request\(|request_entry_link\(|request_backend_shutdown\("
    r"|backend_has_active_work\(|read_owner_token\("
)
_HOST_FUNCTION = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))? )?(?:(?:const|async|unsafe|extern \"\w+\") )*fn (\w+)"
)


def _python_modules() -> dict[str, ast.Module]:
    return {
        path.relative_to(PACKAGE).as_posix(): ast.parse(path.read_text("utf-8"))
        for path in PACKAGE.rglob("*.py")
    }


def test_only_the_classified_modules_reach_the_discovery_record():
    found: dict[str, set[str]] = {}
    for module, tree in _python_modules().items():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == (
                "guildbotics.utils.local_api"
            ):
                found.setdefault(module, set()).update(a.name for a in node.names)
            # The whole module is no classified name, so this always fails.
            if isinstance(node, ast.ImportFrom) and node.module == "guildbotics.utils":
                if any(a.name == "local_api" for a in node.names):
                    found.setdefault(module, set()).add("<the whole module>")
            assert not (
                isinstance(node, ast.Import)
                and any(a.name == "guildbotics.utils.local_api" for a in node.names)
            ), module
    assert found == LOCAL_API_IMPORTS


#: Each name of a credential's carrier, and the one module that may spell it.
_SPELLED_ONCE = {
    "owner.token": "utils/local_api.py",
    auth.COOKIE_PREFIX: "app_api/auth.py",
    auth.CSRF_HEADER: "app_api/auth.py",
}


def test_the_credentials_carriers_are_named_in_one_module():
    for module, tree in _python_modules().items():
        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        for name, owner in _SPELLED_ONCE.items():
            assert module == owner or not any(name in text for text in literals), (
                module,
                name,
            )


def test_every_caller_of_a_proof_gated_client_is_classified():
    found = set()
    for module, tree in _python_modules().items():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(
                a.name == "run_on_desktop" for a in node.names
            ):
                found.add((module, "run_on_desktop"))
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "execute"
                and isinstance(node.value, ast.Name)
                and node.value.id == "secret_transport"
            ):
                found.add((module, "secret_transport.execute"))
    assert found == CLIENT_CALLERS


#: A line comment or a block comment's line; ``*x = ...`` is Rust code.
#: Any other item starting at the margin ends the function before it.
_TOP_LEVEL = re.compile(r"^[^\s})\]]")
_COMMENT = re.compile(r"^\s*(?://|/\*|\*(?:[ /]|$))")


def _routes(
    root: Path,
    patterns: tuple[str, ...],
    carriers: dict[str, re.Pattern[str]],
    function: re.Pattern[str],
) -> set[tuple[str, str]]:
    """``(file, enclosing function)`` of every code line naming a carrier.

    ``carriers`` maps a file name to the pattern for it, ``""`` to the pattern
    for every other file. Comments and Rust's test module are not code.
    """
    found = set()
    for path in sorted(p for pattern in patterns for p in root.rglob(pattern)):
        name = path.relative_to(root).as_posix()
        if ".test." in name or name.startswith("test/"):
            continue
        carrier = carriers.get(name, carriers[""])
        enclosing, in_tests = "", False
        for line in path.read_text("utf-8").splitlines():
            in_tests = (in_tests or line.startswith("mod tests")) and line != "}"
            if in_tests or _COMMENT.match(line):
                continue
            if declared := function.match(line):
                enclosing = next(name for name in declared.groups() if name)
            elif _TOP_LEVEL.match(line):
                enclosing = ""
            if carrier.search(line):
                found.add((name, enclosing))
    return found


def test_every_frontend_route_of_the_session_is_classified():
    found = _routes(
        FRONTEND, ("*.ts", "*.tsx"), {"": _FRONTEND_CARRIERS}, _FRONTEND_FUNCTION
    )
    assert found == set(FRONTEND_ROUTES)


def test_every_host_route_of_the_token_is_classified():
    found = _routes(HOST, ("*.rs",), {"": _HOST_CARRIERS}, _HOST_FUNCTION)
    assert found == set(HOST_ROUTES)


def test_the_host_names_the_port_and_its_notice_as_the_server_does():
    host = (HOST / "lib.rs").read_text("utf-8")
    assert f'const PORT_NOTICE_PREFIX: &str = "{server.PORT_NOTICE_PREFIX}";' in host
    assert f'const SERVER_PORT_ENV: &str = "{local_api.PORT_ENV}";' in host
    assert f"const DEFAULT_SERVER_PORT: u16 = {local_api.DEFAULT_PORT};" in host
    # The sidecar takes no argument: it reads the port from the environment.
    assert '"--port"' not in host
