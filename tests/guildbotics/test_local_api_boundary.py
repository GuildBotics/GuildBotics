"""Every route the Local API session token, or what it guards, travels is
classified here.

The token reaches only a destination its holder vouched for: in Desktop, the
port the sidecar announced after binding it, dropped once the sidecar exits;
for host clients, a process that proved it holds the token. That holds only
for the routes drawn into it, so the population is taken from the code of all
three languages, not from the routes someone happened to think of:

- Python: every importer of the discovery record module, the token header
  literal, the record's file name, and every caller of a client that sends
  through it
- the frontend: every function that reads the connection, writes the token
  into a header or URL, hands the destination to a browser callback, or ships
  a built-in token
- the host: every function that reads the token, writes it to the wire, or
  hands it to the sidecar or the frontend

A new one fails here until it is classified.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import guildbotics
from guildbotics.app_api import server

PACKAGE = Path(guildbotics.__file__).parent
REPOSITORY = PACKAGE.parent
FRONTEND = REPOSITORY / "desktop" / "src"
HOST = REPOSITORY / "desktop" / "src-tauri" / "src"

#: ``module -> names`` imported from ``guildbotics.utils.local_api``.
LOCAL_API_IMPORTS = {
    "app_api/server.py": {"LocalApiEndpoint"},
    "app_api/api.py": {"PROOF_PATH", "TOKEN_HEADER", "local_api_proof"},
    "app_api/models.py": {"NONCE_PATTERN"},
    # Proof-gated clients: the token is attached only after the proof.
    "cli/desktop_commands.py": {"connect_local_api"},
    "hub/secret_transport.py": {"connect_local_api"},
}

#: ``(module, client function)`` for every caller of a proof-gated client.
CLIENT_CALLERS = {
    ("cli/run.py", "run_on_desktop"),
    ("cli/hub.py", "secret_transport.execute"),
    ("secrets/hub_client.py", "secret_transport.execute"),
}

#: ``(file, function) -> route`` in the frontend.
FRONTEND_ROUTES = {
    ("api/client.ts", ""): "the connection itself, module-private",
    ("api/client.ts", "configureApi"): "sets the connection",
    ("api/client.ts", "disconnectApi"): "clears the connection",
    ("api/client.ts", "connected"): "the one reader of the connection",
    ("api/client.ts", "memberAvatarUrl"): "URL query, loaded by <img>",
    ("api/client.ts", "startGitHubAppRegistration"): "browser callback destination",
    ("api/client.ts", "uploadFile"): "header",
    ("api/client.ts", "subscribeEvents"): "URL query, WebSocket",
    ("api/client.ts", "request"): "header",
    ("api/backend.ts", ""): "shipped code: inlined only into a preview build",
    ("api/backend.ts", "startBackend"): "connection from the announced port",
    ("api/backend.ts", "waitForHealth"): "header, preview backend only",
}
_FRONTEND_CARRIERS = re.compile(
    r"X-GuildBotics-Session-Token|token=|[\"']token[\"']|callback_base_url"
    r"|VITE_GUILDBOTICS_API_|backend_info|\bconfigureApi\("
)
#: Read only where the connection lives.
_CONNECTION_READS = re.compile(r"\bconnection\b|\bconnected\(\)")
_FRONTEND_FUNCTION = re.compile(
    r"^(?:export )?(?:default )?(?:async )?function\*? ?(\w+)"
    r"|^(?:export )?(?:const|let) (\w+) = (?:async )?\("
)

#: ``(file, function) -> route`` in the host.
HOST_ROUTES = {
    ("lib.rs", ""): "BackendState holds the token; the notice prefix",
    ("lib.rs", "after_stdout"): "reads the port notice",
    ("lib.rs", "backend_info"): "hands the token to the frontend once Ready",
    ("lib.rs", "backend_request"): "header, only while Ready",
    ("lib.rs", "request_backend_shutdown"): "through backend_request",
    ("lib.rs", "backend_has_active_work"): "through backend_request",
    ("lib.rs", "quit_needs_confirmation"): "through backend_has_active_work",
    ("lib.rs", "stop_backend"): "through request_backend_shutdown",
    ("lib.rs", "run"): "environment of the sidecar, never argv",
}
_HOST_CARRIERS = re.compile(
    r"X-GuildBotics-Session-Token|\.token\b|\btoken\b|PORT_NOTICE_PREFIX"
    r"|backend_request\(|request_backend_shutdown\(|backend_has_active_work\("
    r"|TcpStream::connect|\"--port\""
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


def test_the_token_header_and_the_record_are_named_in_one_module():
    for module, tree in _python_modules().items():
        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        if module != "utils/local_api.py":
            assert "X-GuildBotics-Session-Token" not in literals, module
            assert "app-api.json" not in literals, module


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
            if carrier.search(line):
                found.add((name, enclosing))
    return found


def test_every_frontend_route_of_the_token_is_classified():
    client = re.compile(f"{_FRONTEND_CARRIERS.pattern}|{_CONNECTION_READS.pattern}")
    found = _routes(
        FRONTEND,
        ("*.ts", "*.tsx"),
        {"": _FRONTEND_CARRIERS, "api/client.ts": client},
        _FRONTEND_FUNCTION,
    )
    assert found == set(FRONTEND_ROUTES)


def test_every_host_route_of_the_token_is_classified():
    found = _routes(HOST, ("*.rs",), {"": _HOST_CARRIERS}, _HOST_FUNCTION)
    assert found == set(HOST_ROUTES)


def test_the_host_learns_the_port_only_from_the_sidecar_notice():
    host = (HOST / "lib.rs").read_text("utf-8")
    assert f'const PORT_NOTICE_PREFIX: &str = "{server.PORT_NOTICE_PREFIX}";' in host
    assert '"--port", "0"' in host
