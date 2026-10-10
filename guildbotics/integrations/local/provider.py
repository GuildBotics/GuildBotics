"""The reference provider: a code host and a board kept as files of the
workspace, reached with no credential.

It is chosen with ``name: local`` for ``code_hosting_service`` and
``ticket_manager`` and is not offered by the setup screens: it is what the
ports' contracts and the capabilities are exercised with besides a hosted
provider.
"""

from guildbotics.integrations.local.code_hosting import LocalCodeHostingService
from guildbotics.integrations.local.ticket_board import LocalTicketManager
from guildbotics.integrations.provider import Provider

LOCAL = Provider(
    name="local",
    code_hosting=lambda logger, person, team: LocalCodeHostingService(person, team),
    ticket_manager=LocalTicketManager,
)
