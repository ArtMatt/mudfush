"""
World definition for the MUD Fishing Game
Rooms, exits, and world map
"""

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from items import (
    Item, TACKLE_BOX, OLD_BOOT, PLASTIC_WORM, NIGHTCRAWLERS,
    MINNOWS, WEARABLE_ITEMS, create_item_copy,
)


# Rooms that never get random ground loot
NO_GROUND_LOOT_ROOMS = frozenset({"store", "slick_store", "jail"})

# Common ground finds (item template, relative weight)
COMMON_GROUND_LOOT: List[Tuple[Item, int]] = [
    (OLD_BOOT, 25),
    (PLASTIC_WORM, 18),
    (NIGHTCRAWLERS, 15),
    (TACKLE_BOX, 8),
    (MINNOWS, 10),
]

# Chance each eligible room gets one common item on reset
COMMON_LOOT_CHANCE = 0.40

# Chance the whole world gets a single clothing drop this hour
WORLD_CLOTHING_CHANCE = 0.25


@dataclass
class Room:
    id: str
    name: str
    description: str
    exits: Dict[str, str] = field(default_factory=dict)  # direction -> room_id
    items: List[Item] = field(default_factory=list)
    is_water: bool = False  # Can fish here
    population: Optional[int] = None  # Fish population, 0-100%
    players: Set[str] = field(default_factory=set)  # Player names currently here
    npcs: List[str] = field(default_factory=list)  # Named NPCs present here

    def __post_init__(self):
        if self.is_water and self.population is None:
            self.population = random.randint(20, 90)

    def update_population(self):
        """Change fish population while keeping it in the 0-100 range."""
        if self.is_water:
            current = self.population or 0
            if current <= 0:
                delta = random.randint(1, 25)
            elif current >= 100:
                delta = -random.randint(1, 25)
            else:
                delta = random.choice(
                    list(range(-25, 0)) + list(range(1, 26))
                )
            self.population = max(0, min(100, current + delta))
    
    def get_description(self, include_players: bool = True, current_player: str = None) -> str:
        """Get the full room description with items and exits."""
        lines = []
        lines.append(f"\n{'='*50}")
        lines.append(f"  {self.name.upper()}")
        lines.append(f"{'='*50}")
        lines.append(f"\n{self.description}")
        
        # Show items in room
        if self.items:
            lines.append("\nYou see here:")
            for item in self.items:
                lines.append(f"  - {item.display_name}")
        
        # Show NPCs and other players together
        if include_players:
            also_here = list(self.npcs)
            also_here.extend(p for p in self.players if p != current_player)
            if also_here:
                lines.append("\nAlso here:")
                for name in also_here:
                    lines.append(f"  - {name}")
        
        # Show exits
        if self.exits:
            exit_str = ", ".join(self.exits.keys())
            lines.append(f"\nExits: [{exit_str}]")
        else:
            lines.append("\nExits: [none]")
        
        if self.is_water:
            lines.append("\n(You can \033[96mFISH\033[0m here)")
        
        return "\n".join(lines)


def _weighted_choice(options: List[Tuple[Item, int]]) -> Item:
    """Pick an item template by relative weight."""
    templates = [item for item, _ in options]
    weights = [weight for _, weight in options]
    return random.choices(templates, weights=weights, k=1)[0]


def reset_ground_items(rooms: Dict[str, Room]) -> Dict[str, int]:
    """
    Clear all ground items and respawn loot.
    Clothing is rare: at most one wearable in the whole world per reset,
    and only with WORLD_CLOTHING_CHANCE.
    Returns counts: {"common": n, "clothing": n}.
    """
    for room in rooms.values():
        room.items.clear()

    common_count = 0
    clothing_count = 0
    eligible = [
        room for room in rooms.values()
        if room.id not in NO_GROUND_LOOT_ROOMS
    ]

    for room in eligible:
        if random.random() < COMMON_LOOT_CHANCE:
            template = _weighted_choice(COMMON_GROUND_LOOT)
            room.items.append(create_item_copy(template))
            common_count += 1

    if eligible and random.random() < WORLD_CLOTHING_CHANCE:
        room = random.choice(eligible)
        clothing = random.choice(list(WEARABLE_ITEMS.values()))
        room.items.append(create_item_copy(clothing))
        clothing_count += 1

    return {"common": common_count, "clothing": clothing_count}


def create_world() -> Dict[str, Room]:
    """Create and return the game world."""
    
    rooms = {}
    
    # The Fishing Store (Starting Point)
    rooms["store"] = Room(
        id="store",
        name="Bubba's Bait & Tackle",
        description="""You're inside a rustic fishing store. The walls are covered with mounted 
fish, old photographs of prize catches, and hand-painted signs advertising 
bait prices. A glass display case shows off various lures and tackle. 
A worn stool and cash register wait behind the counter.

Type 'buy <item>' to purchase fishing gear, or 'list' to see what's for sale.""",
        exits={"south": "store_porch"},
        items=[],
        is_water=False,
        npcs=["Bubba"],
    )
    
    # Store Porch
    rooms["store_porch"] = Room(
        id="store_porch",
        name="Store Porch",
        description="""You're standing on the wooden porch of Bubba's Bait & Tackle. 
A creaky rocking chair sits empty beside the door. The smell of 
earthworms and fish bait drifts out from inside. A dirt path leads 
south toward the lake, and you can see the glimmer of water in the distance.""",
        exits={"north": "store", "south": "trail_north"},
        items=[],
        is_water=False
    )
    
    # Trail sections leading to the lake
    rooms["trail_north"] = Room(
        id="trail_north",
        name="Shady Trail (North)",
        description="""A winding dirt path cuts through tall oak trees. Sunlight filters 
through the canopy, creating dappled shadows on the ground. You can 
hear birds singing and the distant sound of water. The trail continues 
south toward the lake.""",
        exits={"north": "store_porch", "south": "trail_middle"},
        items=[],
        is_water=False
    )
    
    rooms["trail_middle"] = Room(
        id="trail_middle",
        name="Forest Crossroads",
        description="""The trail opens up into a small clearing. An old wooden signpost 
stands here, though the paint has mostly faded. You can make out 
arrows pointing in different directions:
  - North: "Bubba's Bait Shop"
  - South: "Lake View"
  - East: "Quiet Cove"
  - West: "Old Pier"

Wildflowers grow along the edges of the clearing.""",
        exits={"north": "trail_north", "south": "trail_south", "east": "east_path", "west": "west_path"},
        items=[],
        is_water=False
    )
    
    rooms["trail_south"] = Room(
        id="trail_south",
        name="Shady Trail (South)",
        description="""The path slopes gently downward toward the lake. Through the trees, 
you can see the sparkling blue water of Whispering Lake. The air 
becomes cooler and you can smell the fresh water. A few fish jump 
in the distance.""",
        exits={"north": "trail_middle", "south": "lake_shore"},
        items=[],
        is_water=False
    )
    
    # Lake Shore (Main fishing spot)
    rooms["lake_shore"] = Room(
        id="lake_shore",
        name="Lake Shore",
        description="""You stand on the sandy shore of Whispering Lake. The water laps 
gently at your feet. This is a popular fishing spot - you can see 
old footprints in the sand and a few bottle caps scattered about. 
The lake stretches out before you, deep and blue. Lily pads float 
near the eastern shore.""",
        exits={"north": "trail_south", "east": "shallow_cove", "west": "rocky_point"},
        items=[],
        is_water=True
    )
    
    # Eastern path to Quiet Cove
    rooms["east_path"] = Room(
        id="east_path",
        name="Overgrown Path",
        description="""A narrow, overgrown path winds eastward through thick brush before 
bending south. Spider webs stretch between branches, and you have to 
duck under low-hanging limbs. Few people seem to come this way. You 
hear frogs croaking somewhere to the south.""",
        exits={"west": "trail_middle", "south": "shallow_cove"},
        items=[],
        is_water=False
    )
    
    rooms["shallow_cove"] = Room(
        id="shallow_cove",
        name="Quiet Cove",
        description="""You've found a secluded cove where the water is shallow and calm. 
Cattails and reeds grow along the muddy bank. This looks like an 
excellent spot for catching bluegill and small bass. Dragonflies 
zip across the water's surface. The main shore is to the west, and 
an overgrown path leads back north.""",
        exits={"west": "lake_shore", "north": "east_path"},
        items=[],
        is_water=True
    )
    
    # Western path to Old Pier
    rooms["west_path"] = Room(
        id="west_path",
        name="Rocky Path",
        description="""A rocky path leads westward, with large boulders scattered on 
either side. The terrain is rough but passable. Through gaps in 
the rocks, you can see the old pier jutting out into the lake.""",
        exits={"east": "trail_middle", "west": "old_pier"},
        items=[],
        is_water=False
    )
    
    rooms["old_pier"] = Room(
        id="old_pier",
        name="Old Wooden Pier",
        description="""A weathered wooden pier extends out over the deeper part of the 
lake. Some boards are missing and it creaks ominously, but it 
seems sturdy enough. This is where the old-timers say the big 
fish lurk. The water here is dark and deep - perfect for catfish 
and pike. A rusty bucket sits at the end of the pier.""",
        exits={"east": "west_path", "south": "rocky_point"},
        items=[],
        is_water=True
    )
    
    rooms["rocky_point"] = Room(
        id="rocky_point",
        name="Rocky Point",
        description="""Large flat rocks jut out into the lake here, forming a natural 
fishing platform. The water is deeper than at the shore and runs 
swiftly past the rocks. Local legend says that 'Old Whiskers' - 
a massive carp that's never been caught - lives somewhere in 
these waters. The shore is to the east, the old pier is visible 
to the north, and a sketchy trail leads south around the lake.""",
        exits={"east": "lake_shore", "north": "old_pier", "south": "south_trail"},
        items=[],
        is_water=True
    )
    
    # Southern trail to Slick's
    rooms["south_trail"] = Room(
        id="south_trail",
        name="Muddy Trail",
        description="""A muddy, neglected trail winds along the southern edge of the lake.
Empty beer cans and cigarette butts litter the ground. The trees 
here are scraggly and the undergrowth is thick with thorns. You 
can see a run-down building through the trees to the south. The 
rocky point is back to the north.""",
        exits={"north": "rocky_point", "south": "slick_exterior"},
        items=[],
        is_water=False
    )
    
    rooms["slick_exterior"] = Room(
        id="slick_exterior",
        name="Slick's Surplus - Outside",
        description="""You stand in front of a ramshackle building with peeling paint and 
a crooked sign reading "SLICK'S SURPLUS - WE BUY & SELL EVERYTHING".
A rusty pickup truck with no wheels sits on cinder blocks nearby.
The windows are grimy and a neon "OPEN" sign flickers erratically.
Something about this place feels... off.""",
        exits={"north": "south_trail", "south": "slick_store"},
        items=[],
        is_water=False
    )
    
    rooms["slick_store"] = Room(
        id="slick_store",
        name="Slick's Surplus",
        description="""The inside of Slick's is cramped and dimly lit. Fishing gear hangs 
from the ceiling alongside car parts, old electronics, and things 
you can't quite identify. A scratched counter is piled with receipts 
and mysterious odds and ends. A neon OPEN sign buzzes overhead.

"Hey there, friend. Looking to make a DEAL?"

Type 'list' to see prices. They change often around here...
Type 'buy <item>' to purchase, 'sell <item>' to sell.""",
        exits={"north": "slick_exterior"},
        items=[],
        is_water=False,
        npcs=["Slick"],
    )

    rooms["jail"] = Room(
        id="jail",
        name="Town Jail",
        description="""You're in a small, damp jail cell. Iron bars form the door, and a 
hard wooden bench is bolted to the floor. Someone scratched "NO 
HACKING PLEASE" into the wall. A single bulb flickers above you.
The smell of old fish bait somehow still finds its way in here.""",
        exits={"out": "store"},
        items=[],
        is_water=False,
    )

    reset_ground_items(rooms)
    return rooms


# Direction aliases
DIRECTION_ALIASES = {
    "n": "north",
    "s": "south",
    "e": "east",
    "w": "west",
    "north": "north",
    "south": "south",
    "east": "east",
    "west": "west",
    "out": "out",
    "o": "out",
}
