from abc import ABC, abstractmethod

from guildbotics.loader.team_loader import TeamLoader


class LoaderFactory(ABC):

    @abstractmethod
    def create_team_loader(self) -> TeamLoader:
        """
        Abstract method to create a team loader.
        Returns:
            TeamLoader: An instance of the team loader.
        """
        pass
