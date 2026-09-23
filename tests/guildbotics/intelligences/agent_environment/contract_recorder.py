"""What a provider CLI sends, observed from outside a real microVM.

A :class:`Recorder` stands where the provider's API would be. It answers the
requests a CLI makes when it is pointed here (scripted answers, else 403), and
it is also the proxy the CLI is given, so a request that bypasses the pointed
URL shows up as a ``CONNECT`` to its real host. The recorder answers that
tunnel itself, with a certificate for the host from the run's CA, so what the
request carries is recorded too; it is then refused, and nothing leaves the
device. A TLS side presents certificates for whatever name the
client asks for, signed by a CA made for the run, for providers whose pointed
URL must be HTTPS. Every credential involved is synthetic.
"""

from __future__ import annotations

import datetime as dt
import json
import ssl
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from guildbotics.intelligences.agent_environment.spec import GUEST_HOST_ALIAS

#: A scripted answer: (status, headers, body), or None for the default 403.
Answer = Callable[[str, str, str], tuple[int, dict[str, str], bytes] | None]


@dataclass(frozen=True)
class Seen:
    """One request the recorder saw."""

    kind: str  # "http", "tls", or "connect"
    host: str
    method: str = ""
    path: str = ""
    authorization: str = ""
    headers: tuple[str, ...] = ()


def _certificate(
    name: str, signer: tuple[ec.EllipticCurvePrivateKey, x509.Name] | None = None
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    """A key and certificate for ``name``: a CA when ``signer`` is None, else
    a server certificate for the name, signed by it."""
    now = dt.datetime.now(dt.UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    signing_key, issuer = signer or (key, subject)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(hours=6))
    )
    builder = (
        builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        if signer is None
        else builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name)]), False
        )
    )
    return key, builder.sign(signing_key, hashes.SHA256())


class Recorder:
    """A plain port (API + proxy) and a TLS port, recording what arrives."""

    def __init__(self, answer: Answer | None = None) -> None:
        self.seen: list[Seen] = []
        self._answer = answer or (lambda host, method, path: None)
        self._lock = threading.Lock()
        self._dir = Path(tempfile.mkdtemp())
        self._ca_key, self._ca = _certificate("contract probe CA")
        self.ca_pem = self._ca.public_bytes(serialization.Encoding.PEM)
        self._contexts: dict[str, ssl.SSLContext] = {}
        self.port = self._serve(tls=False)
        self.tls_port = self._serve(tls=True)
        self.url = f"http://{GUEST_HOST_ALIAS}:{self.port}"
        self.tls_url = f"https://{GUEST_HOST_ALIAS}:{self.tls_port}"

    def proxy_environment(self) -> dict[str, str]:
        """Route what bypasses the pointed URL here, where it is refused."""
        return {
            **{
                name: self.url
                for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
            },
            "NO_PROXY": GUEST_HOST_ALIAS,
            "no_proxy": GUEST_HOST_ALIAS,
        }

    def ca_environment(self, path: str) -> dict[str, str]:
        """Make the guest's TLS clients trust the run's CA, written at path."""
        return {"SSL_CERT_FILE": path, "NODE_EXTRA_CA_CERTS": path}

    def connects(self) -> set[str]:
        return {seen.host for seen in self.seen if seen.kind == "connect"}

    def carrying(self, credential: str) -> set[str]:
        """The hosts a request carrying ``credential`` in Authorization reached."""
        return {
            seen.host
            for seen in self.seen
            if seen.kind != "connect" and credential in seen.authorization
        }

    def requests(self, path_prefix: str = "") -> list[Seen]:
        return [
            s
            for s in self.seen
            if s.kind != "connect" and s.path.startswith(path_prefix)
        ]

    def _context(self, name: str) -> ssl.SSLContext:
        with self._lock:
            if name not in self._contexts:
                key, cert = _certificate(name, (self._ca_key, self._ca.subject))
                base = self._dir / name
                (base.with_suffix(".crt")).write_bytes(
                    cert.public_bytes(serialization.Encoding.PEM)
                )
                (base.with_suffix(".key")).write_bytes(
                    key.private_bytes(
                        serialization.Encoding.PEM,
                        serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption(),
                    )
                )
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(
                    base.with_suffix(".crt"), base.with_suffix(".key")
                )
                context.set_alpn_protocols(["http/1.1"])
                self._contexts[name] = context
            return self._contexts[name]

    def _serve(self, *, tls: bool) -> int:
        server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler(tls))
        if tls:
            context = self._context(GUEST_HOST_ALIAS)

            def by_name(sock: ssl.SSLObject, name: str | None, _context: Any) -> None:
                sock.context = self._context(name or GUEST_HOST_ALIAS)

            context.sni_callback = by_name
            server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return int(server.server_address[1])

    def _handler(self, tls: bool) -> type[BaseHTTPRequestHandler]:
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def do_CONNECT(self) -> None:
                host = self.path.rsplit(":", 1)[0]
                recorder.seen.append(Seen("connect", host))
                self.send_response(200)
                self.end_headers()
                self.wfile.flush()
                try:
                    tunnel = recorder._context(host).wrap_socket(
                        self.connection, server_side=True
                    )
                    recorder._handler(tls=True)(
                        tunnel, self.client_address, self.server
                    )
                except (OSError, ssl.SSLError):
                    pass  # The client did not trust the run's CA, or went away.
                self.close_connection = True

            def _any(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                if length:
                    self.rfile.read(length)
                host = (self.headers.get("host") or "").rsplit(":", 1)[0]
                recorder.seen.append(
                    Seen(
                        "tls" if tls else "http",
                        host,
                        self.command,
                        self.path,
                        self.headers.get("authorization") or "",
                        tuple(sorted(k.lower() for k in self.headers.keys())),
                    )
                )
                status, headers, body = recorder._answer(
                    host, self.command, self.path
                ) or (
                    403,
                    {"content-type": "application/json"},
                    b'{"error":{"message":"SYNTHETIC_UPSTREAM_REACHED"}}',
                )
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _any

        return Handler


def json_answer(body: Any) -> tuple[int, dict[str, str], bytes]:
    return 200, {"content-type": "application/json"}, json.dumps(body).encode()
