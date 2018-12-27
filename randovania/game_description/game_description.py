"""Classes that describes the raw data of a game world."""
import copy
import re
from typing import List, Dict, Tuple, Iterator, FrozenSet, Iterable

from randovania.game_description.area import Area
from randovania.game_description.dock import DockWeaknessDatabase
from randovania.game_description.node import DockNode, TeleporterNode, Node, TeleporterConnection
from randovania.game_description.requirements import RequirementSet, SatisfiableRequirements
from randovania.game_description.resources import ResourceInfo, \
    ResourceGain, CurrentResources, ResourceDatabase, DamageResourceInfo, SimpleResourceInfo, \
    PickupDatabase
from randovania.game_description.world import World
from randovania.resolver.game_patches import GamePatches


class WorldList:
    worlds: List[World]

    _nodes_to_area: Dict[Node, Area]
    _nodes_to_world: Dict[Node, World]
    
    def __deepcopy__(self, memodict):
        return WorldList(
            worlds=copy.deepcopy(self.worlds, memodict),
        )
    
    def __init__(self, worlds: List[World]):
        self.worlds = worlds
        self._nodes_to_area, self._nodes_to_world = _calculate_nodes_to_area_world(worlds)

    def world_by_asset_id(self, asset_id: int) -> World:
        for world in self.worlds:
            if world.world_asset_id == asset_id:
                return world
        raise KeyError("Unknown asset_id: {}".format(asset_id))

    @property
    def all_areas(self) -> Iterator[Area]:
        for world in self.worlds:
            yield from world.areas

    @property
    def all_nodes(self) -> Iterator[Node]:
        for world in self.worlds:
            yield from world.all_nodes

    def node_name(self, node: Node, with_world=False) -> str:
        prefix = "{}/".format(self.nodes_to_world(node).name) if with_world else ""
        return "{}{}/{}".format(prefix, self.nodes_to_area(node).name, node.name)

    def node_from_name(self, name: str) -> Node:
        match = re.match("(?:([^/]+)/)?([^/]+)/([^/]+)", name)
        if match is None:
            raise ValueError("Invalid name: {}".format(name))

        world_name, area_name, node_name = match.group(1, 2, 3)
        for world in self.worlds:
            if world_name is not None and world.name != world_name:
                continue

            for area in world.areas:
                if area.name != area_name:
                    continue

                for node in area.nodes:
                    if node.name == node_name:
                        return node

        raise ValueError("Unknown name: {}".format(name))

    def nodes_to_world(self, node: Node) -> World:
        return self._nodes_to_world[node]

    def nodes_to_area(self, node: Node) -> Area:
        return self._nodes_to_area[node]

    def resolve_dock_node(self, node: DockNode, patches: GamePatches) -> Node:
        world = self.nodes_to_world(node)
        original_area = self.nodes_to_area(node)

        connection = patches.dock_connection.get((original_area.area_asset_id, node.dock_index),
                                                 node.default_connection)

        target_area = world.area_by_asset_id(connection.area_asset_id)
        return target_area.node_with_dock_index(connection.dock_index)

    def resolve_teleporter_node(self, node: TeleporterNode, patches: GamePatches) -> Node:
        connection = patches.elevator_connection.get(node.teleporter_instance_id, node.default_connection)
        return self.resolve_teleporter_connection(connection)

    def resolve_teleporter_connection(self, connection: TeleporterConnection) -> Node:
        world = self.world_by_asset_id(connection.world_asset_id)
        area = world.area_by_asset_id(connection.area_asset_id)
        if area.default_node_index == 255:
            raise IndexError("Area '{}' does not have a default_node_index".format(area.name))
        return area.nodes[area.default_node_index]

    def connections_from(self, node: Node, patches: GamePatches) -> Iterator[Tuple[Node, RequirementSet]]:
        """
        Queries all nodes from other areas you can go from a given node. Aka, doors and teleporters
        :param patches:
        :param node:
        :return: Generator of pairs Node + RequirementSet for going to that node
        """
        if isinstance(node, DockNode):
            # TODO: respect is_blast_shield: if already opened once, no requirement needed.
            # Includes opening form behind with different criteria
            try:
                target_node = self.resolve_dock_node(node, patches)
                original_area = self.nodes_to_area(node)
                dock_weakness = patches.dock_weakness.get((original_area.area_asset_id, node.dock_index),
                                                          node.default_dock_weakness)

                yield target_node, dock_weakness.requirements
            except IndexError:
                # TODO: fix data to not having docks pointing to nothing
                yield None, RequirementSet.impossible()

        if isinstance(node, TeleporterNode):
            try:
                yield self.resolve_teleporter_node(node, patches), RequirementSet.trivial()
            except IndexError:
                # TODO: fix data to not have teleporters pointing to areas with invalid default_node_index
                print("Teleporter is broken!", node)
                yield None, RequirementSet.impossible()

    def area_connections_from(self, node: Node) -> Iterator[Tuple[Node, RequirementSet]]:
        """
        Queries all nodes from the same area you can go from a given node.
        :param node:
        :return: Generator of pairs Node + RequirementSet for going to that node
        """
        area = self.nodes_to_area(node)
        for target_node, requirements in area.connections[node].items():
            yield target_node, requirements

    def potential_nodes_from(self, node: Node, patches: GamePatches) -> Iterator[Tuple[Node, RequirementSet]]:
        """
        Queries all nodes you can go from a given node, checking doors, teleporters and other nodes in the same area.
        :param node:
        :param patches:
        :return: Generator of pairs Node + RequirementSet for going to that node
        """
        yield from self.connections_from(node, patches)
        yield from self.area_connections_from(node)

    def simplify_connections(self,
                             static_resources: CurrentResources,
                             resource_database: ResourceDatabase) -> None:
        """
        Simplifies all Node connections, assuming the given resources will never change their quantity.
        This is removes all checking for tricks and difficulties in runtime since these never change.
        :param static_resources:
        :return:
        """
        for world in self.worlds:
            for area in world.areas:
                for connections in area.connections.values():
                    for target, value in connections.items():
                        connections[target] = value.simplify(static_resources, resource_database)

    def calculate_relevant_resources(self, patches: GamePatches) -> FrozenSet[ResourceInfo]:
        results = set()

        for node in self.all_nodes:
            for _, requirements in self.potential_nodes_from(node, patches):
                for alternative in requirements.alternatives:
                    for individual in alternative.items:
                        results.add(individual.resource)

        return frozenset(results)


def _calculate_dangerous_resources_in_db(db: DockWeaknessDatabase) -> Iterator[SimpleResourceInfo]:
    for list_by_type in db:
        for dock_weakness in list_by_type:
            yield from dock_weakness.requirements.dangerous_resources


def _calculate_dangerous_resources_in_areas(areas: Iterator[Area]) -> Iterator[SimpleResourceInfo]:
    for area in areas:
        for node in area.nodes:
            for connection in area.connections[node].values():
                yield from connection.dangerous_resources


class GameDescription:
    game: int
    game_name: str
    dock_weakness_database: DockWeaknessDatabase

    resource_database: ResourceDatabase
    pickup_database: PickupDatabase
    victory_condition: RequirementSet
    starting_world_asset_id: int
    starting_area_asset_id: int
    starting_items: ResourceGain
    item_loss_items: ResourceGain
    dangerous_resources: FrozenSet[SimpleResourceInfo]
    world_list: WorldList

    def __deepcopy__(self, memodict):
        return GameDescription(
            game=self.game,
            game_name=self.game_name,
            resource_database=self.resource_database,
            pickup_database=self.pickup_database,
            dock_weakness_database=self.dock_weakness_database,
            worlds=copy.deepcopy(self.world_list.worlds, memodict),
            victory_condition=self.victory_condition,
            starting_world_asset_id=self.starting_world_asset_id,
            starting_area_asset_id=self.starting_area_asset_id,
            starting_items=self.starting_items,
            item_loss_items=self.item_loss_items,
        )

    def __init__(self,
                 game: int,
                 game_name: str,
                 dock_weakness_database: DockWeaknessDatabase,

                 resource_database: ResourceDatabase,
                 pickup_database: PickupDatabase,
                 victory_condition: RequirementSet,
                 starting_world_asset_id: int,
                 starting_area_asset_id: int,
                 starting_items: ResourceGain,
                 item_loss_items: ResourceGain,
                 worlds: List[World],
                 ):
        self.game = game
        self.game_name = game_name
        self.dock_weakness_database = dock_weakness_database

        self.resource_database = resource_database
        self.pickup_database = pickup_database
        self.victory_condition = victory_condition
        self.starting_world_asset_id = starting_world_asset_id
        self.starting_area_asset_id = starting_area_asset_id
        self.starting_items = starting_items
        self.item_loss_items = item_loss_items
        self.world_list = WorldList(worlds)

        self.dangerous_resources = frozenset(
            _calculate_dangerous_resources_in_areas(self.world_list.all_areas)) | frozenset(
            _calculate_dangerous_resources_in_db(self.dock_weakness_database))

    def simplify_connections(self, resources):
        self.world_list.simplify_connections(resources, self.resource_database)


def _resources_for_damage(resource: DamageResourceInfo, database: ResourceDatabase) -> Iterator[ResourceInfo]:
    yield database.energy_tank
    yield resource.reductions[0].inventory_item


def calculate_interesting_resources(satisfiable_requirements: SatisfiableRequirements,
                                    resources: CurrentResources,
                                    database: ResourceDatabase) -> FrozenSet[ResourceInfo]:
    """A resource is considered interesting if it isn't satisfied and it belongs to any satisfiable RequirementList """

    def helper():
        # For each possible requirement list
        for requirement_list in satisfiable_requirements:
            # If it's not satisfied, there's at least one IndividualRequirement in it that can be collected
            if not requirement_list.satisfied(resources, database):

                for individual in requirement_list.values():
                    # Ignore those with the `negate` flag. We can't "uncollect" a resource to satisfy these.
                    # Finally, if it's not satisfied then we're interested in collecting it
                    if not individual.negate and not individual.satisfied(resources, database):
                        if isinstance(individual.resource, DamageResourceInfo):
                            yield from _resources_for_damage(individual.resource, database)
                        else:
                            yield individual.resource

    return frozenset(helper())


def _calculate_nodes_to_area_world(worlds: Iterable[World]):
    nodes_to_area = {}
    nodes_to_world = {}

    for world in worlds:
        for area in world.areas:
            for node in area.nodes:
                if node in nodes_to_area:
                    raise ValueError(
                        "Trying to map {} to {}, but already mapped to {}".format(
                            node, area, nodes_to_area[node]))
                nodes_to_area[node] = area
                nodes_to_world[node] = world

    return nodes_to_area, nodes_to_world
