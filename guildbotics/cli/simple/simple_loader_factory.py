from guildbotics.loader import TeamLoader
from guildbotics.loader.yaml.yaml_team_loader import YamlTeamLoader
from guildbotics.runtime import LoaderFactory


class SimpleLoaderFactory(LoaderFactory):

    def create_team_loader(self) -> TeamLoader:
        """
        Create a default team loader using YAML.
        Returns:
            TeamLoader: An instance of YamlTeamLoader.
        """
        return YamlTeamLoader()
