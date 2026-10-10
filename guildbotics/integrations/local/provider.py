"""The reference provider: a code host, a board, and a chat kept as files of
the workspace, reached with no credential.

It is chosen with ``name: local`` for ``code_hosting_service``,
``ticket_manager``, and ``chat_service``, and is not offered by the setup
screens: it is what the ports' contracts and the capabilities are exercised
with besides a hosted provider.
"""

from guildbotics.integrations.local.chat import LocalChatListener, LocalChatService
from guildbotics.integrations.local.code_hosting import LocalCodeHostingService
from guildbotics.integrations.local.ticket_board import LocalTicketManager
from guildbotics.integrations.provider import Chat, Provider

LOCAL = Provider(
    name="local",
    code_hosting=lambda logger, person, team: LocalCodeHostingService(person, team),
    ticket_manager=LocalTicketManager,
    chat=Chat(
        service=lambda logger, person, team: LocalChatService(person),
        event_listener=lambda logger, team, persons, on_activity: LocalChatListener(
            logger, on_activity
        ),
    ),
)
