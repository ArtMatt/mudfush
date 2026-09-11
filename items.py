"""
Items for the MUD Fishing Game
"""

import random
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple


class ItemType(Enum):
    FISHING_POLE = "fishing_pole"
    LURE = "lure"
    BAIT = "bait"
    FISH = "fish"
    CONTAINER = "container"
    WEARABLE = "wearable"
    TOOLKIT = "toolkit"
    BEER = "beer"
    GEM = "gem"
    MISC = "misc"


class WearSlot(Enum):
    HEAD = "head"
    NECK = "neck"
    CHEST = "chest"
    HANDS = "hands"
    FINGERS = "fingers"
    LEGS = "legs"
    FEET = "feet"


# Inventory filter keywords -> item type / wear slot archetypes
INVENTORY_TYPE_FILTERS = {
    "pole": ItemType.FISHING_POLE,
    "poles": ItemType.FISHING_POLE,
    "rod": ItemType.FISHING_POLE,
    "rods": ItemType.FISHING_POLE,
    "fishing_pole": ItemType.FISHING_POLE,
    "fishingpole": ItemType.FISHING_POLE,
    "lure": ItemType.LURE,
    "lures": ItemType.LURE,
    "bait": ItemType.BAIT,
    "baits": ItemType.BAIT,
    "fish": ItemType.FISH,
    "fishes": ItemType.FISH,
    "toolkit": ItemType.TOOLKIT,
    "toolkits": ItemType.TOOLKIT,
    "kit": ItemType.TOOLKIT,
    "kits": ItemType.TOOLKIT,
    "beer": ItemType.BEER,
    "beers": ItemType.BEER,
    "gem": ItemType.GEM,
    "gems": ItemType.GEM,
    "container": ItemType.CONTAINER,
    "containers": ItemType.CONTAINER,
    "box": ItemType.CONTAINER,
    "boxes": ItemType.CONTAINER,
    "wearable": ItemType.WEARABLE,
    "wearables": ItemType.WEARABLE,
    "clothing": ItemType.WEARABLE,
    "clothes": ItemType.WEARABLE,
    "apparel": ItemType.WEARABLE,
    "misc": ItemType.MISC,
}

INVENTORY_SLOT_FILTERS = {
    "head": WearSlot.HEAD,
    "hat": WearSlot.HEAD,
    "hats": WearSlot.HEAD,
    "cap": WearSlot.HEAD,
    "caps": WearSlot.HEAD,
    "neck": WearSlot.NECK,
    "necklace": WearSlot.NECK,
    "necklaces": WearSlot.NECK,
    "chest": WearSlot.CHEST,
    "vest": WearSlot.CHEST,
    "vests": WearSlot.CHEST,
    "coat": WearSlot.CHEST,
    "coats": WearSlot.CHEST,
    "shirt": WearSlot.CHEST,
    "shirts": WearSlot.CHEST,
    "hands": WearSlot.HANDS,
    "glove": WearSlot.HANDS,
    "gloves": WearSlot.HANDS,
    "fingers": WearSlot.FINGERS,
    "finger": WearSlot.FINGERS,
    "ring": WearSlot.FINGERS,
    "rings": WearSlot.FINGERS,
    "legs": WearSlot.LEGS,
    "pants": WearSlot.LEGS,
    "trousers": WearSlot.LEGS,
    "waders": WearSlot.LEGS,
    "feet": WearSlot.FEET,
    "boot": WearSlot.FEET,
    "boots": WearSlot.FEET,
    "shoe": WearSlot.FEET,
    "shoes": WearSlot.FEET,
    "sandal": WearSlot.FEET,
    "sandals": WearSlot.FEET,
}


# Condition 0-9 descriptive labels
CONDITION_NAMES = {
    0: "broken",
    1: "ruined",
    2: "battered",
    3: "disheveled",
    4: "worn",
    5: "bog-standard",
    6: "decent",
    7: "nice",
    8: "fine",
    9: "new",
}

# ANSI colors for condition quality in terminal/SSH clients
_ANSI_RESET = "\033[0m"
_CONDITION_COLORS = {
    "red": "\033[31m",      # broken (0)
    "yellow": "\033[33m",   # above broken through 50% (1-4)
    "green": "\033[32m",    # above 50% to just below new (5-8)
    "teal": "\033[36m",     # new (9)
}
_ANSI_ESCAPE_RE = re.compile(r"\033\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """Remove ANSI color codes from text."""
    return _ANSI_ESCAPE_RE.sub("", text)


def colorize_condition(condition: int, label: str) -> str:
    """
    Color a condition label by quality:
      0 broken           -> red
      1-4 (up to 50%)    -> yellow
      5-8 (below new)    -> green
      9 new              -> teal
    """
    level = max(0, min(9, condition))
    if level <= 0:
        color = _CONDITION_COLORS["red"]
    elif level <= 4:
        color = _CONDITION_COLORS["yellow"]
    elif level <= 8:
        color = _CONDITION_COLORS["green"]
    else:
        color = _CONDITION_COLORS["teal"]
    return f"{color}{label}{_ANSI_RESET}"


# Fish size quality levels (same color bands as condition)
FISH_SIZE_QUALITY = {
    "tiny": 0,      # red
    "small": 2,     # yellow
    "average": 5,   # green
    "large": 7,     # green
    "trophy": 9,    # teal
}


def colorize_fish_size(size: Optional[str]) -> str:
    """Color a fish size label using the shared quality scheme."""
    if not size:
        return ""
    quality = FISH_SIZE_QUALITY.get(size.lower(), 5)
    return colorize_condition(quality, size)

ATTRIBUTES = (
    "strength",
    "dexterity",
    "constitution",
    "intelligence",
    "wisdom",
    "charisma",
)

ATTRIBUTE_ABBREV = {
    "strength": "STR",
    "dexterity": "DEX",
    "constitution": "CON",
    "intelligence": "INT",
    "wisdom": "WIS",
    "charisma": "CHA",
}

# Distinct terminal colors for each attribute (also used by gems)
ATTRIBUTE_COLORS = {
    "strength": "\033[31m",       # red
    "dexterity": "\033[33m",      # yellow
    "constitution": "\033[32m",   # green
    "intelligence": "\033[34m",   # blue
    "wisdom": "\033[35m",         # purple/magenta
    "charisma": "\033[95m",       # pink
}

ATTRIBUTE_COLOR_NAMES = {
    "strength": "red",
    "dexterity": "yellow",
    "constitution": "green",
    "intelligence": "blue",
    "wisdom": "purple",
    "charisma": "pink",
}

GEM_DISPLAY_NAMES = {
    "strength": "red gem",
    "dexterity": "yellow gem",
    "constitution": "green gem",
    "intelligence": "blue gem",
    "wisdom": "purple gem",
    "charisma": "pink gem",
}


def sort_modifiers(mods: List[Tuple[str, int]]) -> List[Tuple[str, int]]:
    """Order modifiers STR, DEX, CON, INT, WIS, CHA (then any unknowns)."""
    order = {attr: i for i, attr in enumerate(ATTRIBUTES)}
    cleaned = [(a, v) for a, v in mods if v > 0]
    return sorted(cleaned, key=lambda pair: order.get(pair[0], 999))


def attribute_abbrev(attribute: Optional[str]) -> str:
    """Return the 3-letter abbreviation for an attribute name."""
    if not attribute:
        return ""
    return ATTRIBUTE_ABBREV.get(attribute.lower(), attribute[:3].upper())


def colorize_attribute(attribute: Optional[str], label: str) -> str:
    """Color a label using the attribute's associated color."""
    if not attribute:
        return label
    color = ATTRIBUTE_COLORS.get(attribute.lower())
    if not color:
        return label
    return f"{color}{label}{_ANSI_RESET}"


def attribute_color_name(attribute: Optional[str]) -> str:
    """Human color name for an attribute (e.g. strength -> red)."""
    if not attribute:
        return "strange"
    return ATTRIBUTE_COLOR_NAMES.get(attribute.lower(), "strange")

# Equipment that can receive attribute modifiers
EQUIPMENT_TYPES = {
    ItemType.FISHING_POLE,
    ItemType.LURE,
    ItemType.BAIT,
    ItemType.WEARABLE,
}

# Fishing gear and toolkits degrade on use (not clothing/jewelry)
DEGRADABLE_TYPES = {
    ItemType.FISHING_POLE,
    ItemType.LURE,
    ItemType.BAIT,
    ItemType.TOOLKIT,
}


def roll_modifier_value() -> int:
    """
    Roll a modifier from 0 to +10 with exponential rarity (fishing gear).
    Relative weights follow 10^(10-n):
      +0 ~ 90%, +1 ~ 9%, +2 ~ 0.9%, ... +10 extremely rare.
    """
    weights = [10 ** (10 - n) for n in range(11)]
    return random.choices(range(11), weights=weights, k=1)[0]


def roll_wearable_modifier_value() -> int:
    """
    Clothing/accessories always get at least +1.
    Exact chances for higher bonuses:
      +2 = 1/10^1 (10%), +3 = 1/10^2 (1%), ... +n = 1/10^(n-1).
    Remaining probability goes to +1.
    """
    higher = [10 ** (-(n - 1)) for n in range(2, 11)]
    weights = [1.0 - sum(higher)] + higher
    return random.choices(range(1, 11), weights=weights, k=1)[0]


# Chance an equipment item rolls two attribute bonuses instead of one
DUAL_MODIFIER_CHANCE = 1e-1  # 10%


def roll_modifiers(item_type: Optional[ItemType] = None) -> List[Tuple[str, int]]:
    """
    Roll one attribute bonus normally, or two (10% / 1e-1) distinct attributes.
    Each attribute rolls its value independently with the usual odds.
    Fishing gear may roll +0 (omitted); wearables are always +1 or higher.
    """
    count = 2 if random.random() < DUAL_MODIFIER_CHANCE else 1
    attrs = random.sample(list(ATTRIBUTES), k=count)
    results: List[Tuple[str, int]] = []
    for attr in attrs:
        if item_type == ItemType.WEARABLE:
            value = roll_wearable_modifier_value()
        else:
            value = roll_modifier_value()
        if value > 0:
            results.append((attr, value))
    return sort_modifiers(results)


def roll_modifier(item_type: Optional[ItemType] = None) -> Tuple[Optional[str], int]:
    """
    Roll a single (attribute, value).
    Wearables always get +1 or higher; other equipment may roll +0 (no modifier).
    """
    if item_type == ItemType.WEARABLE:
        value = roll_wearable_modifier_value()
        return random.choice(ATTRIBUTES), value

    value = roll_modifier_value()
    if value == 0:
        return None, 0
    return random.choice(ATTRIBUTES), value


def roll_condition(min_condition: int = 0, max_condition: int = 9) -> int:
    """Roll an item condition between min and max inclusive."""
    return random.randint(min_condition, max_condition)


@dataclass
class Item:
    id: str
    name: str
    description: str
    item_type: ItemType
    takeable: bool = True
    value: int = 0
    fishing_power: int = 0  # For poles
    attraction: int = 0     # For lures/bait
    weight: float = 0.0     # For fish (in lbs)
    wear_slot: Optional[WearSlot] = None
    condition: int = 5      # 0-9, bog-standard by default
    # Legacy single-mod fields (kept in sync with modifiers[0] for compatibility)
    modifier_attribute: Optional[str] = None
    modifier_value: int = 0  # 0 to +10
    modifiers: List[Tuple[str, int]] = field(default_factory=list)
    fish_size: Optional[str] = None
    gem_attribute: Optional[str] = None  # For gems: which attribute they enhance

    def __post_init__(self):
        if self.modifiers:
            self.modifiers = sort_modifiers(self.modifiers)
        elif self.modifier_attribute and self.modifier_value > 0:
            self.modifiers = [(self.modifier_attribute, self.modifier_value)]
        self._sync_legacy_modifiers()

    def _sync_legacy_modifiers(self):
        """Keep first-mod legacy fields aligned with modifiers list."""
        if self.modifiers:
            self.modifier_attribute = self.modifiers[0][0]
            self.modifier_value = self.modifiers[0][1]
        else:
            self.modifier_attribute = None
            self.modifier_value = 0

    def set_modifiers(self, mods: List[Tuple[str, int]]) -> None:
        """Replace modifiers (ordered STR→CHA) and sync legacy fields."""
        self.modifiers = sort_modifiers(mods)
        self._sync_legacy_modifiers()

    def bonus_for(self, attribute: str) -> int:
        """Total bonus this item grants to the given attribute."""
        return sum(v for a, v in self.modifiers if a == attribute)

    def boosts_attribute(self, attribute: str) -> bool:
        return self.bonus_for(attribute) > 0

    def apply_gem(self, gem: "Item") -> Tuple[str, int]:
        """
        Apply a gem's attribute bonus to this item.
        Returns (attribute, new_bonus_value).
        """
        attr = gem.gem_attribute
        if not attr:
            raise ValueError("Gem has no attribute")
        new_mods: List[Tuple[str, int]] = []
        found = False
        new_value = 1
        for a, v in self.modifiers:
            if a == attr:
                new_value = v + 1
                new_mods.append((a, new_value))
                found = True
            else:
                new_mods.append((a, v))
        if not found:
            new_mods.append((attr, 1))
            new_value = 1
        self.set_modifiers(new_mods)
        return attr, new_value

    def _modifier_suffix(self, colored: bool = False) -> str:
        """Display fragment like ' of +1 CON +2 STR'."""
        parts = []
        for a, v in self.modifiers:
            label = attribute_abbrev(a)
            if colored:
                label = colorize_attribute(a, label)
            parts.append(f"+{v} {label}")
        if not parts:
            return ""
        return " of " + " ".join(parts)

    def __hash__(self):
        return hash(self.id)

    @property
    def condition_name(self) -> str:
        """Human-readable condition label (no color)."""
        return CONDITION_NAMES.get(max(0, min(9, self.condition)), "bog-standard")

    @property
    def colored_condition_name(self) -> str:
        """Condition label with quality color for terminal display."""
        return colorize_condition(self.condition, self.condition_name)

    @property
    def plain_display_name(self) -> str:
        """Full item name without ANSI colors (matching, logs, uppercase)."""
        if self.item_type == ItemType.BEER:
            return self.name
        if self.item_type == ItemType.GEM:
            return self.name
        size = f"{self.fish_size} " if self.fish_size else ""
        base = f"{self.condition_name} {size}{self.name}"
        return f"{base}{self._modifier_suffix(colored=False)}"

    @property
    def display_name(self) -> str:
        """
        Full item name including colored condition, fish size, and modifier.
        Examples: 'new wool cap', 'new wool cap of +1 CHA +2 STR'
        """
        if self.item_type == ItemType.BEER:
            return self.name
        if self.item_type == ItemType.GEM:
            attr = self.gem_attribute
            return colorize_attribute(attr, self.name)
        if self.fish_size:
            size = f"{colorize_fish_size(self.fish_size)} "
        else:
            size = ""
        base = f"{self.colored_condition_name} {size}{self.name}"
        return f"{base}{self._modifier_suffix(colored=True)}"

    def matches(self, query: str) -> bool:
        """True if query matches id, base name, or display name."""
        q = query.lower()
        plain = self.plain_display_name.lower()
        return (
            q == self.id
            or q in self.name.lower()
            or q in plain
        )

    def matches_inventory_filter(self, query: str) -> bool:
        """
        True if this item matches an inventory filter query.
        Known archetypes (pole, fish, head/hat, ...) match by type or wear
        slot only. Other text still matches by name/id/display.
        """
        q = query.strip().lower()
        if not q:
            return True
        type_match = INVENTORY_TYPE_FILTERS.get(q)
        if type_match is not None:
            return self.item_type == type_match
        slot_match = INVENTORY_SLOT_FILTERS.get(q)
        if slot_match is not None:
            return self.wear_slot == slot_match
        return self.matches(q)

    def is_degradable(self) -> bool:
        """Fishing gear and toolkits degrade on use; clothing uses timed wear."""
        return self.item_type in DEGRADABLE_TYPES

    def is_broken(self) -> bool:
        return self.condition <= 0

    def can_be_repaired(self) -> bool:
        """True if this item can be repaired with a toolkit."""
        return self.item_type != ItemType.FISH and self.condition < 9

    def degrade(self, amount: int = 1, *, allow_wearable: bool = False) -> Optional[str]:
        """
        Lower condition for degradable gear (or wearables when allow_wearable).
        Returns a message if condition changed, else None.
        """
        if self.condition <= 0:
            return None
        if not self.is_degradable():
            if not (allow_wearable and self.item_type == ItemType.WEARABLE):
                return None

        old_name = self.condition_name
        self.condition = max(0, self.condition - amount)

        if self.condition == 0:
            return (
                f"Your {self.name} is now "
                f"{colorize_condition(0, 'broken')}!"
            )
        if old_name != self.condition_name:
            return (
                f"Your {self.name} looks "
                f"{self.colored_condition_name}."
            )
        return None


# Fishing Poles
BASIC_POLE = Item(
    id="basic_pole",
    name="basic fishing pole",
    description="A simple bamboo fishing pole. Good for beginners.",
    item_type=ItemType.FISHING_POLE,
    value=10,
    fishing_power=1,
)

SPINNING_ROD = Item(
    id="spinning_rod",
    name="spinning rod",
    description="A quality spinning rod with a smooth reel. Popular among casual anglers.",
    item_type=ItemType.FISHING_POLE,
    value=50,
    fishing_power=3,
)

PRO_ROD = Item(
    id="pro_rod",
    name="professional fishing rod",
    description="A top-of-the-line carbon fiber rod. The choice of tournament fishers.",
    item_type=ItemType.FISHING_POLE,
    value=200,
    fishing_power=5,
)

# Lures
PLASTIC_WORM = Item(
    id="plastic_worm",
    name="plastic worm",
    description="A wiggly purple plastic worm. Bass love these.",
    item_type=ItemType.LURE,
    value=2,
    attraction=2,
)

SPINNER_LURE = Item(
    id="spinner_lure",
    name="spinner lure",
    description="A shiny metal spinner that flashes in the water.",
    item_type=ItemType.LURE,
    value=5,
    attraction=3,
)

GOLDEN_LURE = Item(
    id="golden_lure",
    name="golden lure",
    description="A legendary golden lure said to attract the biggest fish.",
    item_type=ItemType.LURE,
    value=100,
    attraction=5,
)

# Bait
NIGHTCRAWLERS = Item(
    id="nightcrawlers",
    name="container of nightcrawlers",
    description="A small container filled with wriggling nightcrawlers.",
    item_type=ItemType.BAIT,
    value=3,
    attraction=2,
)

MINNOWS = Item(
    id="minnows",
    name="bucket of minnows",
    description="A bucket of live minnows swimming around.",
    item_type=ItemType.BAIT,
    value=5,
    attraction=3,
)

# Fish (caught while fishing)
BLUEGILL = Item(
    id="bluegill",
    name="bluegill",
    description="A small but feisty bluegill.",
    item_type=ItemType.FISH,
    value=5,
    weight=0.5,
)

BASS = Item(
    id="bass",
    name="largemouth bass",
    description="A nice largemouth bass with a big mouth.",
    item_type=ItemType.FISH,
    value=15,
    weight=3.0,
)

CATFISH = Item(
    id="catfish",
    name="sleepy catfish",
    description="A whiskered sleepy catfish. Watch out for the barbs!",
    item_type=ItemType.FISH,
    value=20,
    weight=5.0,
)

TROUT = Item(
    id="trout",
    name="rainbow trout",
    description="A beautiful rainbow trout with iridescent scales.",
    item_type=ItemType.FISH,
    value=25,
    weight=2.5,
)

PIKE = Item(
    id="pike",
    name="northern pike",
    description="A fearsome northern pike with razor-sharp teeth.",
    item_type=ItemType.FISH,
    value=40,
    weight=8.0,
)

MUD_CARP = Item(
    id="mud_carp",
    name="mud carp",
    description=(
        "A heavy, sluggish carp coated in lake muck. Hard to reel, "
        "harder to sell."
    ),
    item_type=ItemType.FISH,
    value=8,
    weight=14.0,
)

PEBBLE_PERCH = Item(
    id="pebble_perch",
    name="pebble perch",
    description="A tiny drab perch barely bigger than a pebble. Easy catch, meager payday.",
    item_type=ItemType.FISH,
    value=3,
    weight=0.4,
)

MOON_DARTER = Item(
    id="moon_darter",
    name="moon darter",
    description=(
        "A rare, shimmering darter no bigger than your hand. "
        "Collectors pay well for its silvery glow."
    ),
    item_type=ItemType.FISH,
    value=45,
    weight=1.0,
)

WALLEYE = Item(
    id="walleye",
    name="walleye",
    description="A solid mid-sized walleye with glassy eyes. A fair catch for a fair price.",
    item_type=ItemType.FISH,
    value=22,
    weight=4.0,
)

STING_PUFFER = Item(
    id="sting_puffer",
    name="sting puffer",
    description=(
        "A puffed-up fish with venomous spines. Valuable to collectors, "
        "painful to land bare-handed."
    ),
    item_type=ItemType.FISH,
    value=70,
    weight=2.0,
)

ZEN_GUPPY = Item(
    id="zen_guppy",
    name="zen guppy",
    description=(
        "A tiny, serene guppy that seems to glow with calm. Rare, light, "
        "and oddly soothing to hold."
    ),
    item_type=ItemType.FISH,
    value=35,
    weight=0.2,
)

LEGENDARY_CARP = Item(
    id="legendary_carp",
    name="Old Whiskers",
    description="The legendary carp known as 'Old Whiskers'. Locals say he's been in this lake for 50 years!",
    item_type=ItemType.FISH,
    value=500,
    weight=25.0,
)

ANCIENT_WHISKERS = Item(
    id="ancient_whiskers",
    name="Ancient Whiskers",
    description=(
        "An impossibly old carp marked by deep scars and long silver whiskers. "
        "The lake itself seems quieter around it."
    ),
    item_type=ItemType.FISH,
    value=510,
    weight=46.0,
)

# Misc items
TACKLE_BOX = Item(
    id="tackle_box",
    name="tackle box",
    description="A weathered tackle box. It's seen better days.",
    item_type=ItemType.CONTAINER,
    takeable=True,
    value=15,
)

OLD_BOOT = Item(
    id="old_boot",
    name="old boot",
    description="A waterlogged old boot. Not much use for fishing.",
    item_type=ItemType.MISC,
    value=0,
)

FISHING_HAT = Item(
    id="fishing_hat",
    name="lucky fishing hat",
    description="A faded fishing hat covered in old lures and pins.",
    item_type=ItemType.WEARABLE,
    wear_slot=WearSlot.HEAD,
    value=5,
)

TOOLKIT = Item(
    id="toolkit",
    name="toolkit",
    description=(
        "A rare set of pliers, oil, and spare parts for mending fishing gear "
        "and clothing. Each repair wears it down."
    ),
    item_type=ItemType.TOOLKIT,
    takeable=True,
    value=2000,
)

BEER = Item(
    id="beer",
    name="unopened beer",
    description=(
        "A cold unopened beer. Drinking it might inspire you to improve "
        "yourself — if you dare."
    ),
    item_type=ItemType.BEER,
    takeable=True,
    value=0,
    condition=9,
)

# Items that shops will never buy or sell
UNSELLABLE_TYPES = frozenset({ItemType.BEER, ItemType.GEM})
GEMMABLE_TYPES = frozenset({
    ItemType.FISHING_POLE,
    ItemType.LURE,
    ItemType.BAIT,
    ItemType.WEARABLE,
})


def create_beer() -> Item:
    """Create a fresh unopened beer for inventory."""
    return Item(
        id=BEER.id,
        name=BEER.name,
        description=BEER.description,
        item_type=ItemType.BEER,
        takeable=True,
        value=0,
        condition=9,
    )


def create_gem(attribute: Optional[str] = None) -> Item:
    """Create a gem tied to an attribute (random if none given)."""
    attr = attribute if attribute in ATTRIBUTES else random.choice(ATTRIBUTES)
    name = GEM_DISPLAY_NAMES[attr]
    color = ATTRIBUTE_COLOR_NAMES[attr]
    return Item(
        id=f"gem_{attr}",
        name=name,
        description=(
            f"A small {color} gem that hums with latent power. "
            f"It seems tied to {attr}."
        ),
        item_type=ItemType.GEM,
        takeable=True,
        value=0,
        condition=9,
        gem_attribute=attr,
    )


def _wearable(item_id: str, name: str, description: str,
              slot: WearSlot, value: int) -> Item:
    """Create a wearable item for a body slot."""
    return Item(
        id=item_id,
        name=name,
        description=description,
        item_type=ItemType.WEARABLE,
        wear_slot=slot,
        value=value,
    )


WEARABLE_ITEMS = {
    # Head
    "wool_cap": _wearable("wool_cap", "wool cap", "A warm knitted cap.", WearSlot.HEAD, 6),
    "rain_hat": _wearable("rain_hat", "rain hat", "A yellow waterproof rain hat.", WearSlot.HEAD, 12),
    "captains_hat": _wearable("captains_hat", "captain's hat", "A crisp white hat with a brass anchor.", WearSlot.HEAD, 30),
    # Neck
    "rope_necklace": _wearable("rope_necklace", "rope necklace", "A simple necklace tied from cord.", WearSlot.NECK, 4),
    "silver_chain": _wearable("silver_chain", "silver chain", "A polished silver chain.", WearSlot.NECK, 25),
    "shark_tooth_necklace": _wearable("shark_tooth_necklace", "shark tooth necklace", "A tooth hung from a leather cord.", WearSlot.NECK, 18),
    # Chest
    "canvas_vest": _wearable("canvas_vest", "canvas vest", "A sturdy vest with many pockets.", WearSlot.CHEST, 15),
    "raincoat": _wearable("raincoat", "raincoat", "A heavy coat that smells faintly of rubber.", WearSlot.CHEST, 28),
    "wool_sweater": _wearable("wool_sweater", "wool sweater", "A thick, comfortable sweater.", WearSlot.CHEST, 20),
    # Hands
    "work_gloves": _wearable("work_gloves", "work gloves", "Scuffed leather work gloves.", WearSlot.HANDS, 8),
    "fishing_gloves": _wearable("fishing_gloves", "fishing gloves", "Grip-friendly gloves for handling slippery fish.", WearSlot.HANDS, 16),
    "silk_gloves": _wearable("silk_gloves", "silk gloves", "Impractical but remarkably elegant gloves.", WearSlot.HANDS, 35),
    # Fingers
    "wooden_ring": _wearable("wooden_ring", "wooden ring", "A hand-carved wooden ring.", WearSlot.FINGERS, 3),
    "silver_ring": _wearable("silver_ring", "silver ring", "A plain silver band.", WearSlot.FINGERS, 22),
    "gold_ring": _wearable("gold_ring", "gold ring", "A gleaming gold band.", WearSlot.FINGERS, 60),
    # Legs
    "canvas_trousers": _wearable("canvas_trousers", "canvas trousers", "Hard-wearing canvas trousers.", WearSlot.LEGS, 14),
    "waders": _wearable("waders", "fishing waders", "Waterproof waders made for the lake.", WearSlot.LEGS, 32),
    "silk_pants": _wearable("silk_pants", "silk pants", "Flashy trousers unsuited to muddy trails.", WearSlot.LEGS, 40),
    # Feet
    "leather_boots": _wearable("leather_boots", "leather boots", "Reliable brown leather boots.", WearSlot.FEET, 18),
    "rubber_boots": _wearable("rubber_boots", "rubber boots", "Tall waterproof rubber boots.", WearSlot.FEET, 24),
    "old_sandals": _wearable("old_sandals", "old sandals", "Worn sandals with fraying straps.", WearSlot.FEET, 5),
}


# Fish that can be caught (with rarity weights)
CATCHABLE_FISH = [
    (BLUEGILL, 400),
    (PEBBLE_PERCH, 380),
    (BASS, 250),
    (MUD_CARP, 200),
    (WALLEYE, 160),
    (CATFISH, 150),
    (TROUT, 120),
    (PIKE, 70),
    (MOON_DARTER, 18),
    (STING_PUFFER, 12),
    (ZEN_GUPPY, 5),
    (LEGENDARY_CARP, 1),  # ~10× rarer than the old 1/100 base weight
]

# Items available in the store
STORE_INVENTORY = {
    "basic_pole": (BASIC_POLE, 5),
    "spinning_rod": (SPINNING_ROD, 3),
    "pro_rod": (PRO_ROD, 1),
    "plastic_worm": (PLASTIC_WORM, 20),
    "spinner_lure": (SPINNER_LURE, 10),
    "golden_lure": (GOLDEN_LURE, 1),
    "nightcrawlers": (NIGHTCRAWLERS, 15),
    "minnows": (MINNOWS, 10),
    "toolkit": (TOOLKIT, 1),  # Rare
    **{item_id: (item, 3) for item_id, item in WEARABLE_ITEMS.items()},
}


def create_item_copy(
    item: Item,
    roll_stats: bool = True,
    condition: Optional[int] = None,
) -> Item:
    """
    Create a copy of an item for inventory/world placement.

    When roll_stats is True:
      - Condition is rolled (store-quality 5-9 for equipment, 0-9 otherwise)
        unless an explicit condition is provided.
      - Equipment gets rolled attribute modifiers (10% chance of two stats).
    """
    if condition is not None:
        rolled_condition = max(0, min(9, condition))
    elif roll_stats:
        if item.item_type in (ItemType.BEER, ItemType.GEM):
            rolled_condition = 9
        elif item.item_type in EQUIPMENT_TYPES:
            # Store / found gear tends to be usable
            rolled_condition = roll_condition(5, 9)
        elif item.item_type == ItemType.FISH:
            rolled_condition = roll_condition(6, 9)
        else:
            rolled_condition = roll_condition(0, 9)
    else:
        rolled_condition = item.condition

    mods: List[Tuple[str, int]] = []
    if roll_stats and item.item_type in EQUIPMENT_TYPES:
        mods = roll_modifiers(item.item_type)
    elif not roll_stats:
        mods = list(item.modifiers)

    return Item(
        id=item.id,
        name=item.name,
        description=item.description,
        item_type=item.item_type,
        takeable=item.takeable,
        value=item.value,
        fishing_power=item.fishing_power,
        attraction=item.attraction,
        weight=item.weight,
        wear_slot=item.wear_slot,
        condition=rolled_condition,
        modifiers=mods,
        fish_size=item.fish_size,
        gem_attribute=item.gem_attribute,
    )
