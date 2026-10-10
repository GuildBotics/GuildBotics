"""The GitHub Project the setup form names, read before it is saved."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from guildbotics.entities.team import Project, Team
from guildbotics.integrations.github.github_ticket_manager import GitHubTicketManager

#: The board the setup form reads and prepares.
ProjectBoard = GitHubTicketManager


async def with_project_board(
    team: Team,
    *,
    owner: str,
    project_id: str,
    url: str,
    action: Callable[[GitHubTicketManager], Awaitable[Any]],
) -> Any:
    """Run *action* against the board of the Project the form identifies.

    The project identity comes from the (possibly unsaved) form, while the
    member roster and credentials come from the saved ``team``. Tries each
    member's credentials until one succeeds; returns the action result, or
    ``None`` when every attempt fails (so callers degrade gracefully).
    """
    members = [m for m in team.members if m.is_active] or list(team.members)
    board = Team(
        project=Project(
            name=team.project.name or "setup",
            services={
                "ticket_manager": {
                    "name": "GitHub",
                    "owner": owner,
                    "project_id": project_id,
                    "url": url,
                }
            },
        ),
        members=team.members,
    )
    logger = logging.getLogger(__name__)
    for member in members:
        # Construct inside the try: GitHubTicketManager.__init__ raises for a
        # member without a GitHub username, and such members must be skipped
        # (not surfaced as a 500) so a later credentialed member is still
        # tried.
        ticket_manager: GitHubTicketManager | None = None
        try:
            ticket_manager = GitHubTicketManager(logger, member, board)
            return await action(ticket_manager)
        except Exception:
            continue
        finally:
            if ticket_manager is not None:
                await ticket_manager.aclose()
    return None
