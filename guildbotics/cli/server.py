"""Entering the GuildBotics server's screens from a browser, and connecting
this machine's CLI to a server it reaches through SSH forwarding."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import click
import httpx

from guildbotics.utils.i18n_tool import t
from guildbotics.utils.local_api import (
    API_PREFIX,
    base_url,
    connect_local_api,
    keep_client_token,
    server_port,
)

#: How long ``connect`` waits for the owner to answer the consent screen.
_CONSENT_TIMEOUT_SECONDS = 600
_CALLBACK_PATH = "/callback"


@click.command(name="open")
@click.option(
    "--print",
    "print_link",
    is_flag=True,
    help="Print the link instead of opening it, to paste into a browser on "
    "another machine.",
)
def open_screens(print_link: bool) -> None:
    """Open the GuildBotics screens in the browser.

    The browser enters with a single-use link that expires after 60 seconds.
    """
    with connect_local_api(timeout=10.0) as client:
        if client is None:
            raise click.ClickException(t("cli.server.unreachable"))
        try:
            url = str(client.post("/session/links").raise_for_status().json()["url"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise click.ClickException(t("cli.server.refused", reason=exc)) from exc
    if print_link or not webbrowser.open(url):
        click.echo(url)


@click.command(name="connect")
def connect() -> None:
    """Connect this machine's CLI to the GuildBotics server.

    For a machine that reaches the server through SSH forwarding: the owner
    allows this machine on the server's consent screen, and the token it is
    given is kept in this machine's OS keychain.
    """
    port = server_port()
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(32)
    received: dict[str, str] = {}
    done = threading.Event()

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            query = {key: values[0] for key, values in parse_qs(parts.query).items()}
            # Only the consent's own answer ends the wait: another local
            # request knows no state.
            if parts.path != _CALLBACK_PATH or query.get("state") != state:
                self.send_error(404)
                return
            received.update(query)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(t("cli.server.callback_received").encode())
            done.set()

        def log_message(self, format: str, *args: object) -> None:
            pass

    listener = HTTPServer(("127.0.0.1", 0), Callback)
    redirect_uri = f"http://127.0.0.1:{listener.server_port}{_CALLBACK_PATH}"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    query = urlencode(
        {
            "client": _client_name(),
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge.rstrip(b"=").decode(),
            "code_challenge_method": "S256",
        }
    )
    url = f"{base_url(port)}/-/authorize?{query}"
    thread = threading.Thread(
        target=listener.serve_forever, name="guildbotics-consent-callback", daemon=True
    )
    thread.start()
    try:
        click.echo(t("cli.server.consent", url=url))
        webbrowser.open(url)
        answered = done.wait(_CONSENT_TIMEOUT_SECONDS)
    finally:
        listener.shutdown()
        listener.server_close()
    if not answered:
        raise click.ClickException(t("cli.server.consent_timeout"))
    if "code" not in received:
        raise click.ClickException(t("cli.server.denied"))
    try:
        response = httpx.post(
            base_url(port) + API_PREFIX + "/apps/token",
            json={
                "code": received["code"],
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
            trust_env=False,
            timeout=10.0,
        )
        token = str(response.raise_for_status().json()["token"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise click.ClickException(t("cli.server.refused", reason=exc)) from exc
    keep_client_token(port, token)
    click.echo(t("cli.server.connected"))


def _client_name() -> str:
    """How the consent screen and the server's records name this machine; a
    later consent of the same name replaces its token."""
    machine = re.sub(r"[^A-Za-z0-9 ._-]", "-", socket.gethostname().split(".")[0])
    return f"GuildBotics CLI {machine}"[:64].strip()
