from abc import ABC, abstractmethod

from guildbotics.runtime import Context


class SetupTool(ABC):
    """Abstract base class for setup tools."""

    @abstractmethod
    def get_context(self, message: str = "") -> Context:
        """Get the runtime context."""
        pass

    @abstractmethod
    def init_project(self) -> None:
        """Initialize a new project."""
        pass

    @abstractmethod
    def add_member(self) -> None:
        """Add a new member to the project."""
        pass

    @abstractmethod
    def verify_environment(self) -> None:
        """Verify the project environment."""
        pass
