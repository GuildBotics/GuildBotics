"""Send a host CLI command to the matching Desktop, before local execution."""

from pathlib import Path

import click
import httpx

from guildbotics.utils.i18n_tool import get_language
from guildbotics.utils.local_api import connect_local_api


def run_on_desktop(
    workspace: Path,
    command: str,
    args: tuple[str, ...],
    person: str | None,
    message: str,
    cwd: Path,
) -> str | None:
    """Return output, or None only when no matching Desktop accepted work.

    Probing is bounded. Execution has no timeout and is never retried locally:
    after POST, even a lost connection may mean the work already ran.
    """
    with connect_local_api(
        headers={"Accept-Language": get_language()}, timeout=None
    ) as client:
        if client is None:
            return None
        try:
            response = client.get("/health", timeout=2.0)
            response.raise_for_status()
            health = response.json()
            if not isinstance(health, dict) or health.get("workspace") != str(
                workspace.resolve()
            ):
                return None
        except (httpx.HTTPError, ValueError):
            return None
        try:
            response = client.post(
                "/commands/run",
                json={
                    "command": command,
                    "args": list(args),
                    "person": person,
                    "message": message,
                    "cwd": str(cwd),
                    "expected_workspace": str(workspace.resolve()),
                },
            )
            if response.is_error:
                try:
                    reason = str(response.json()["message"])
                except (ValueError, KeyError, TypeError):
                    reason = f"HTTP {response.status_code}: {response.text}"
                raise click.ClickException(reason)
            payload = response.json()
            return str(payload["output"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise click.ClickException(str(exc)) from exc
