"""Combine the private gateway CA with system trust roots."""

import tempfile
from pathlib import Path
from typing import Any


def trust_turn(
    turn: dict[str, Any],
    *,
    system_ca: Path = Path("/etc/ssl/certs/ca-certificates.crt"),
) -> dict[str, Any]:
    """Append the private gateway CA to system roots, never replace them."""
    env = dict(turn.get("env", {}))
    announced = env.get("NODE_EXTRA_CA_CERTS")
    if announced is None:
        return turn
    system = system_ca.read_bytes()
    authority = Path(announced).read_bytes()
    if not system.strip() or not authority.strip():
        raise ValueError("The system and gateway CA bundles must be non-empty")
    with tempfile.NamedTemporaryFile(
        prefix="guildbotics-ca-",
        suffix=".pem",
        dir=Path(announced).parent,
        delete=False,
    ) as bundle:
        bundle.write(system + b"\n" + authority)
        path = bundle.name
    env.update(SSL_CERT_FILE=path, REQUESTS_CA_BUNDLE=path)
    return {**turn, "env": env}
