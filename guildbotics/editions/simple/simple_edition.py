from guildbotics.editions.edition import Edition
from guildbotics.editions.simple.simple_brain_factory import SimpleBrainFactory
from guildbotics.editions.simple.simple_integration_factory import (
    SimpleIntegrationFactory,
)
from guildbotics.editions.simple.simple_loader_factory import SimpleLoaderFactory
from guildbotics.runtime import Context


class SimpleEdition(Edition):
    """Default YAML-based runtime edition."""

    def get_context(self, message: str = "") -> Context:
        return Context.get_default(
            SimpleLoaderFactory(),
            SimpleIntegrationFactory(),
            SimpleBrainFactory(),
            message=message,
        )

    def get_default_routines(self) -> list[str]:
        return ["workflows/ticket_driven_workflow"]
