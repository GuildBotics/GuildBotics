"""Every route this machine's server credential, or what it guards, travels
is classified here.

A token reaches only a destination that showed it knows the token: a host
client attaches it only to a process that proved it knows the token's digest.
A browser's session lives in its cookie and the CSRF value the API hands the
page. That holds only for the routes drawn into it, so the population is taken
from the code of both languages, not from the routes someone happened to think
of:

- Python: every importer of the client module, the owner file's, cookie's and
  CSRF header's names, and every caller of a client that sends through it
- the frontend: every function that enters, resumes or refreshes a session,
  sends the CSRF value, or consents for an app

A new one fails here until it is classified.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import guildbotics
from guildbotics.app_api import auth, server
from guildbotics.utils import local_api

PACKAGE = Path(guildbotics.__file__).parent
REPOSITORY = PACKAGE.parent
FRONTENDS = {
    "desktop": REPOSITORY / "desktop" / "src",
    "ui": REPOSITORY / "packages" / "ui" / "src",
}

#: ``module -> names`` imported from ``guildbotics.utils.local_api``.
LOCAL_API_IMPORTS = {
    # The server: it reads its own records and the port it listens on.
    "app_api/api.py": {"API_PREFIX", "PROOF_PATH", "server_port"},
    "app_api/auth.py": {
        "LANGUAGE_HEADER",
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
    "cli/service.py": {"connect_local_api"},
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
}

#: ``(file, function) -> route`` in the frontend.
DESKTOP_ROUTES = {
    ("api/client.ts", ""): "configures the shared transport and its CSRF header name",
    ("api/client.ts", "enterSession"): "exchanges the entry code, keeps the value",
    ("api/client.ts", "resumeSession"): "reads the value of the cookie's session",
    ("api/client.ts", "authorizeApp"): "consent, through request",
    ("api/client.ts", "onConnectionLost"): "a 401 ends the session's screens",
    ("main.tsx", ""): "import",
    ("session/entryPages.ts", ""): "the query resuming the session",
    ("session/EnterView.tsx", ""): "import",
    ("session/EnterView.tsx", "EnterView"): "the entry page's exchange",
    ("session/EntryPage.tsx", ""): "import",
    ("session/EntryPage.tsx", "AppSession"): "no session, no screens",
    ("session/AuthorizeView.tsx", ""): "import",
    ("session/AuthorizeView.tsx", "AuthorizeView"): "consent with the session",
}
FRONTEND_ROUTES = {
    "desktop": DESKTOP_ROUTES,
    "ui": {
        ("api.ts", "createApiClient"): (
            "per-instance closure owns the CSRF value, write header and one refresh"
        ),
    },
}
_FRONTEND_CARRIERS = re.compile(
    r"X-GuildBotics-CSRF|\bcsrf\b|enterSession|resumeSession|authorizeApp"
    r"|/session\b|SESSION_QUERY|onConnectionLost|\b(?:get|set)Csrf\b"
)
_FRONTEND_FUNCTION = re.compile(
    r"^(?:export )?(?:default )?(?:async )?function\*? ?(\w+)"
    r"|^(?:export )?(?:const|let) (\w+) = (?:async )?\("
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
    assert found == CLIENT_CALLERS


#: A line comment or a block comment's line.
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
    for every other file. Comments are not code.
    """
    found = set()
    for path in sorted(p for pattern in patterns for p in root.rglob(pattern)):
        name = path.relative_to(root).as_posix()
        if ".test." in name or name.startswith("test/"):
            continue
        carrier = carriers.get(name, carriers[""])
        enclosing = ""
        for line in path.read_text("utf-8").splitlines():
            if _COMMENT.match(line):
                continue
            if declared := function.match(line):
                enclosing = next(name for name in declared.groups() if name)
            elif _TOP_LEVEL.match(line):
                enclosing = ""
            if carrier.search(line):
                found.add((name, enclosing))
    return found


def _frontend_routes() -> set[tuple[str, str, str]]:
    return {
        (name, path, enclosing)
        for name, root in FRONTENDS.items()
        for path, enclosing in _routes(
            root, ("*.ts", "*.tsx"), {"": _FRONTEND_CARRIERS}, _FRONTEND_FUNCTION
        )
    }


def test_every_frontend_route_of_the_session_is_classified():
    assert _frontend_routes() == {
        (name, path, enclosing)
        for name, routes in FRONTEND_ROUTES.items()
        for path, enclosing in routes
    }


@pytest.mark.parametrize(
    "expression",
    ['request("/session")', "transport.getCsrf()", "transport.setCsrf(value)"],
)
def test_shared_frontend_routes_are_discovered(tmp_path, monkeypatch, expression):
    (tmp_path / "extra.ts").write_text(
        f"export function extraSession() {{ return {expression}; }}\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(FRONTENDS, "ui", tmp_path)
    assert ("ui", "extra.ts", "extraSession") in _frontend_routes()
