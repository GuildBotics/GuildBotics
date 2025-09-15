from abc import ABC, abstractmethod

from guildbotics.entities import Team


class TeamLoader(ABC):
    @abstractmethod
    def load(self) -> Team:
        """
        Abstract method to load a team.
        Returns:
            Team: The Team object.
        """
        pass
