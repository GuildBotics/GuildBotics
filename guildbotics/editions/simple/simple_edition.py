from guildbotics.editions.edition import Edition
from guildbotics.editions.simple.simple_brain_factory import SimpleBrainFactory
from guildbotics.editions.simple.simple_integration_factory import (
    SimpleIntegrationFactory,
)
from guildbotics.editions.simple.simple_loader_factory import SimpleLoaderFactory
from guildbotics.runtime import Context

# The single source of the routine command name in this edition. Routine
# candidates are discovered from each command's own ``routine`` declaration; this
# constant only names the edition's preferred default to seed / fall back to when
# several candidates exist.
DEFAULT_ROUTINE_COMMAND = "workflows/ticket_driven_workflow"


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
        return [DEFAULT_ROUTINE_COMMAND]
