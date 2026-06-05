from abc import ABC, abstractmethod

from guildbotics.runtime import Context


class Edition(ABC):
    """Runtime edition: provides the runtime context and default routines.

    An edition selects the concrete loader / integration / brain factories used
    at runtime. Project and member setup is handled by the GUI (``app_api``)
    through the setup services, not by this class.
    """

    @abstractmethod
    def get_context(self, message: str = "") -> Context:
        """Get the runtime context."""
        ...

    @abstractmethod
    def get_default_routines(self) -> list[str]:
        """Get the default routine commands for the project."""
        ...
