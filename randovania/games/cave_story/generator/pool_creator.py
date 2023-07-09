
from randovania.game_description.game_description import GameDescription
from randovania.games.cave_story.layout.cs_configuration import CSConfiguration
from randovania.generator.pickup_pool import PoolResults
from randovania.layout.base.base_configuration import BaseConfiguration


def pool_creator(results: PoolResults, configuration: BaseConfiguration, game: GameDescription) -> None:
    assert isinstance(configuration, CSConfiguration)
