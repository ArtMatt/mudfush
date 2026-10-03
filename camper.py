"""
Garage subsystem: the old camper behind Slick's and the runs it still makes.

Everything about the restoration project lives in this one file:
  - the garage, the camper's interior, and the stops on its route
  - the camper's condition and trip state machine
  - the 1-second tick and the slower departure / arrival steps
  - every command a player uses once they are inside

Mudfush only has to:
  1. call add_garage_to_world(rooms) when building the map
  2. construct GarageEngine(rooms)
  3. tick engine.update() once a second and relay its broadcasts
  4. route camper commands through engine.handle()

Keep this quiet. The camper is padlocked on purpose: a player without the
key should only ever see a rusted-out trailer up on blocks.
"""

from __future__ import annotations

import json
import math
import random
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from items import Item
    from player import Player
    from world import Room
    from commands import CommandResult


# ---------------------------------------------------------------------------
# State, copied from mud.h's SHIP_* enum (the values the tick actually uses)
# ---------------------------------------------------------------------------

class ShipState(str, Enum):
    DOCKED = "docked"
    READY = "ready"
    ORBIT = "orbit"
    BUSY = "busy"
    BUSY_2 = "busy_2"
    BUSY_3 = "busy_3"
    LAUNCH = "launch"
    LAUNCH_2 = "launch_2"
    LAND = "land"
    LAND_2 = "land_2"
    HYPERSPACE = "hyperspace"
    DISABLED = "disabled"


class DockState(str, Enum):
    """SWL shipstate2: ship-to-ship docking runs beside the flight state."""
    NONE = "none"
    DOCK = "dock"
    DOCK_2 = "dock_2"
    DOCK_3 = "dock_3"       # locked together


LAND_RANGE = 200          # how close you must be to set down
DOCK_RANGE = 200          # per-axis box for docking with another ship
ORBIT_RANGE = 200         # snap into orbit this close to a planet
COORD_UPDATE_SECONDS = 15 # cockpit position ping while flying
RENT_GOLD = 5             # a little to take the public Airstream out
CAMPER_KEY_ID = "camper_key"
PUBLIC_SHIP_ID = "public_airstream"
PUBLIC_OWNER = "Public"
DRIFTER_SHIP_ID = "drifter"
DRIFTER_NAME = "The Drifter"
DRIFTER_OWNER = "Autopilot"
DRIFTER_COCKPIT_ROOM = "drifter_cockpit"
DRIFTER_CRUISE_SECONDS = 5 * 60
SHIPS_PATH = Path("saves/.ships.json")


def ground_cleanup_keeps(room) -> list:
    """The Drifter's gem is not ground clutter. Sweeps leave it in the cockpit."""
    if getattr(room, "id", None) != DRIFTER_COCKPIT_ROOM:
        return []
    from items import ItemType

    return [item for item in room.items if item.item_type == ItemType.GEM]

# Hulls. The Airstream is the garage rental; the Skipjack is the two-room
# ship players buy and sell at any pad off Earth.
HULL_AIRSTREAM = "airstream"
HULL_SKIPJACK = "skipjack"
SKIPJACK_BASE_PRICE = 10000
SKIPJACK_STOCK = {"engine": 100, "hyper": 100, "cargo": 50}
DELL_PAD_ID = "beta_forge"       # Dell buys and sells upgrade modules here
MODULE_BUYBACK = 0.5             # Dell pays half for a loose module
CARGO_WORDS = frozenset({"cargo", "hold", "cargohold"})
CARGO_CYCLE_SECONDS = 3 * 60 * 60
CARGO_BUY_MULTIPLIER = 3        # off-planet buyers pay triple listed value
# Older saves parked on Alpha Minor before it became Europa.
DOCK_ALIASES = {"alpha_minor": "europa"}


class Broadcast:
    """One message for mud_server.handle_broadcast / cockpit occupants."""

    def __init__(self, room_id: str, message: str):
        self.room_id = room_id
        self.message = message

    def wire(self) -> str:
        return f"ROOM:{self.room_id}:{self.message}"


@dataclass
class LandingPad:
    name: str
    room_id: str
    x: int
    y: int
    z: int


BODY_PLANET = "planet"
BODY_MOON = "moon"
BODY_STATION = "station"
BODY_KINDS = (BODY_PLANET, BODY_MOON, BODY_STATION)


@dataclass
class Planet:
    """A body a ship can orbit and land on. Planet, moon, or station: same rules."""
    name: str
    x: int
    y: int
    z: int
    pads: List[LandingPad] = field(default_factory=list)
    kind: str = BODY_PLANET

    @property
    def label(self) -> str:
        return self.kind.upper()


def Moon(name: str, x: int, y: int, z: int, pads: List[LandingPad]) -> Planet:
    return Planet(name, x, y, z, pads, kind=BODY_MOON)


def Station(name: str, x: int, y: int, z: int, pads: List[LandingPad]) -> Planet:
    return Planet(name, x, y, z, pads, kind=BODY_STATION)


@dataclass
class Star:
    name: str
    x: int
    y: int
    z: int


@dataclass
class StarSystem:
    name: str
    galaxy_x: int
    galaxy_y: int
    stars: List[Star] = field(default_factory=list)
    planets: List[Planet] = field(default_factory=list)
    ships: List["Ship"] = field(default_factory=list)

    def pads(self) -> List[LandingPad]:
        pads: List[LandingPad] = []
        for planet in self.planets:
            pads.extend(planet.pads)
        return pads

    def pad_named(self, name: str) -> Optional[LandingPad]:
        needle = name.strip().lower()
        for pad in self.pads():
            if pad.name.lower().startswith(needle) or needle in pad.name.lower():
                return pad
        return None

    def planet_near(self, pad: LandingPad) -> Planet:
        for planet in self.planets:
            if pad in planet.pads:
                return planet
        return self.planets[0]


@dataclass
class Ship:
    name: str
    owner: str = PUBLIC_OWNER
    ship_id: str = ""
    cockpit_room: str = ""
    location: str = ""          # pad room id while docked
    lastdoc: str = ""
    hatch_open: bool = False
    locked: bool = True
    state: ShipState = ShipState.DOCKED
    starsystem: Optional[StarSystem] = None
    vx: float = 0.0
    vy: float = 0.0
    vz: float = 0.0
    hx: float = 1.0
    hy: float = 1.0
    hz: float = 1.0
    currspeed: int = 0
    realspeed: int = 200
    hyperspeed: int = 100
    hull: int = 500
    maxhull: int = 500
    shield: int = 0
    maxshield: int = 250
    lasers: int = 2
    missiles: int = 8
    maxmissiles: int = 8
    laser_cool: int = 0
    dest: str = ""
    currjump: Optional[StarSystem] = None
    jumpx: float = 0.0
    jumpy: float = 0.0
    jumpz: float = 0.0
    hyperdistance: int = 0
    autopilot: bool = False
    automated: bool = False          # The Drifter: flies itself, never for sale
    cruise_left: int = 0             # seconds of realspace flight before the next jump
    target: Optional["Ship"] = None
    home: str = ""
    orbit: Optional[str] = None  # planet name while held in orbit
    hull_type: str = HULL_AIRSTREAM
    hold_room: str = ""          # cargo hold south of the cockpit (Skipjack)
    cargo: List["Item"] = field(default_factory=list)
    # slot -> installed upgrade module id, or None for the stock part
    modules: Dict[str, Optional[str]] = field(
        default_factory=lambda: {"engine": None, "hyper": None, "cargo": None}
    )
    cargo_capacity: int = 0      # lbs of fish the hold takes
    dock_state: DockState = DockState.NONE
    docked_ship: Optional["Ship"] = None   # the hull we're locked to (or approaching)

    def docking_in_progress(self) -> bool:
        return self.dock_state in (DockState.DOCK, DockState.DOCK_2)

    def is_docked_with(self) -> Optional["Ship"]:
        """The ship we're locked to, once the sequence has finished."""
        if self.dock_state == DockState.DOCK_3 and self.docked_ship is not None:
            return self.docked_ship
        return None

    def matches(self, name: str) -> bool:
        needle = name.strip().lower()
        full = self.name.lower()
        if full == needle or full.startswith(needle):
            return True
        if needle in full.split():
            return True
        return False

    def in_space(self) -> bool:
        return self.starsystem is not None and self.state != ShipState.DOCKED

    def busy(self) -> bool:
        return self.state in (
            ShipState.LAUNCH, ShipState.LAUNCH_2,
            ShipState.LAND, ShipState.LAND_2,
            ShipState.HYPERSPACE,
            ShipState.BUSY, ShipState.BUSY_2, ShipState.BUSY_3,
        )

    def is_skipjack(self) -> bool:
        return self.hull_type == HULL_SKIPJACK

    def interior_rooms(self) -> List[str]:
        return [room for room in (self.cockpit_room, self.hold_room) if room]

    def cargo_weight(self) -> float:
        return round(sum(float(item.weight or 0.0) for item in self.cargo), 2)

    def stat_for(self, slot: str) -> int:
        """Effective stat for a slot: installed upgrade or the stock part."""
        from items import module_stat

        installed = self.modules.get(slot)
        if installed:
            return module_stat(installed)
        if self.is_skipjack():
            return SKIPJACK_STOCK[slot]
        return {"engine": 200, "hyper": 100, "cargo": 0}[slot]

    def apply_modules(self) -> None:
        """Recompute speeds and hold size from the installed modules."""
        self.realspeed = self.stat_for("engine")
        self.hyperspeed = self.stat_for("hyper")
        self.cargo_capacity = self.stat_for("cargo")

    def module_value(self) -> int:
        from items import module_price

        return sum(module_price(mid) for mid in self.modules.values() if mid)

    def sale_price(self) -> int:
        return SKIPJACK_BASE_PRICE + self.module_value()


def _distance(ax, ay, az, bx, by, bz) -> float:
    return math.sqrt((ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2)


def _galaxy_distance(a: StarSystem, b: StarSystem) -> int:
    # SWL calculate: (abs(xpos)+abs(ypos))/2
    return (abs(a.galaxy_x - b.galaxy_x) + abs(a.galaxy_y - b.galaxy_y)) // 2


def _facing(ship: Ship, target: Ship) -> bool:
    """SWL is_facing: heading dotted with vector-to-target."""
    dx = target.vx - ship.vx
    dy = target.vy - ship.vy
    dz = target.vz - ship.vz
    mag = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
    hmag = math.sqrt(ship.hx * ship.hx + ship.hy * ship.hy + ship.hz * ship.hz) or 1.0
    return (dx / mag) * (ship.hx / hmag) + (dy / mag) * (ship.hy / hmag) + (dz / mag) * (ship.hz / hmag) > 0.3


def _has_key(player) -> bool:
    """True if the player is carrying the camper's padlock key."""
    return any(item.id == CAMPER_KEY_ID for item in getattr(player, "inventory", []))


_SHIP_NAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9' -]*[A-Za-z0-9])?")


def _clean_ship_name(raw: str) -> Optional[str]:
    """A short display name, or None if it isn't one."""
    text = " ".join(raw.split())
    if not text or len(text) > 32 or not _SHIP_NAME_RE.fullmatch(text):
        return None
    return text


def _ship_names_clash(a: str, b: str) -> bool:
    """True when the two names would answer the same ship command."""
    left, right = a.lower(), b.lower()
    if left == right or left.startswith(right) or right.startswith(left):
        return True
    return left in right.split() or right in left.split()


def _owner_token(name: str) -> str:
    token = re.sub(r"[^a-zA-Z0-9]+", "_", (name or "").strip()).strip("_").lower()
    return token or "owner"


# ---------------------------------------------------------------------------
# World attachments — the garage, the camper's interior, and its stops
# ---------------------------------------------------------------------------

GARAGE_ROOM_ID = "slick_garage"
CAMPER_ROOM_ID = "camper_interior"

# Pads besides the garage. Fishing pads grow a hole to the east. A pad with
# a "buyer" is a dry market: that NPC buys one species from a cargo hold.
REMOTE_PADS = (
    {
        "id": "europa",
        "name": "Europa Landing",
        "description": (
            "A landing pad scraped into Europa's ice. Jupiter's bulk hangs "
            "overhead, and the sky is a hard black. East of the pad, a "
            "chain-link walkway drops toward a dark pool that shouldn't "
            "be liquid."
        ),
        "hole_name": "Europa Fishing Hole",
        "hole_description": (
            "A round basin of black water sits under a grated catwalk. The "
            "surface is too still, then dimples as if something large turned "
            "over underneath. You could FISH here, if you wanted to."
        ),
    },
    {
        "id": "beta_haven",
        "name": "Haven Spaceport",
        "description": (
            "Haven's landing pad, under the dull red coal of Betelgeuse. "
            "Warm wind rolls off a rust-colored plain. East of the tarmac, "
            "a ditch holds water the color of old pennies."
        ),
        "hole_name": "Haven Fishing Hole",
        "hole_description": (
            "Copper-stained water fills a cut in the plain. It smells like "
            "wet metal and algae. Things flick just under the film. You "
            "could FISH here."
        ),
    },
    {
        "id": "beta_forge",
        "name": "Forge Cargo Pad",
        "description": (
            "A scarred cargo pad under Betelgeuse. Furnaces glow on the "
            "horizon. East, a slag trench has filled with water that steams "
            "in the heat."
        ),
        "hole_name": "Forge Fishing Hole",
        "hole_description": (
            "The slag trench is a fishing hole now, somehow. The water is "
            "warm and cloudy, and heat-shimmer makes the far bank crawl. "
            "You could FISH here."
        ),
    },
    {
        "id": "gamma_reach",
        "name": "Reach Spaceport",
        "description": (
            "A lonely pad at Reach, with Vega a cold white pin overhead. "
            "East, a shallow crater holds a sheet of water that reflects the "
            "wrong sky."
        ),
        "hole_name": "Reach Fishing Hole",
        "hole_description": (
            "The crater pool is glassy and wrong. Your reflection lags a "
            "half-second behind you. Rings spread from casts that haven't "
            "happened yet. You could FISH here."
        ),
    },
    {
        "id": "gamma_ice",
        "name": "Ice Outpost",
        "description": (
            "Ice underfoot, Vega small and sharp overhead. The outpost is a "
            "single heated shack and this pad. East, someone has kept a hole "
            "chopped in the ice."
        ),
        "hole_name": "Ice Fishing Hole",
        "hole_description": (
            "A square hole in the ice, edges glazed from repeated thawing. "
            "The water below is darker and warmer than it has any right to "
            "be. You could FISH here."
        ),
    },
    {
        "id": "sirius_brightwater",
        "name": "Whiteflare Pad",
        "description": (
            "A pale pad on Whiteflare. Sirius burns white-blue and throws "
            "hard shadows across bare rock. There is no water here."
        ),
    },
    {
        "id": "sirius_quay",
        "name": "Cinder Lot",
        "description": (
            "A cinder lot with crates stacked under an awning. The air "
            "smells like a market, and there is no water anywhere."
        ),
        "buyer": {
            "name": "Mara",
            "species": ("bluegill", "walleye", "pike"),
            "blurb": (
                "Mara buys fish out of cargo holds and nothing else. She "
                "posts one species at a time and leaves the rest of the hold alone."
            ),
        },
    },
    {
        "id": "rigel_bluewater",
        "name": "Bluestone Pad",
        "description": (
            "Bluestone's pad sits on a shelf of blue stone. Rigel is a hard "
            "blue-white point. The stone is dry all the way to the horizon."
        ),
    },
    {
        "id": "rigel_exchange",
        "name": "Rigel Exchange",
        "description": (
            "A windowless exchange hall on a waterless rock. Scales are set "
            "into the floor, and the only weather is the ventilator."
        ),
        "buyer": {
            "name": "Holt",
            "species": ("pebble_perch", "trout", "moon_darter"),
            "blurb": (
                "Holt calls prices through a grate. He buys one species from "
                "a cargo hold. He takes his species and leaves the rest."
            ),
        },
    },
    {
        "id": "deneb_swanpool",
        "name": "Swanrock Pad",
        "description": (
            "Swanrock's pad is a ring of decking on bare stone, with Deneb "
            "bright in the Swan. Dust blows across the circle."
        ),
    },
    {
        "id": "deneb_wharf",
        "name": "Deneb Yard",
        "description": (
            "A yard of cranes and crates on dry rock. The only fish here "
            "are already dead and priced."
        ),
        "buyer": {
            "name": "Bornkt",
            "species": ("bass", "catfish", "sting_puffer"),
            "blurb": (
                "Bornkt keeps a chalkboard of the one species she is buying. "
                "She takes those and ignores the rest of the hold."
            ),
        },
    },
    {
        "id": "void_drift",
        "name": "Drift Station",
        "description": (
            "A station hung in a patch of sky with no star. The docking arm "
            "is the whole port. There is no water, no weather, and no shore."
        ),
        "buyer": {
            "name": "Nix",
            "species": ("mud_carp", "zen_guppy", "legendary_carp"),
            "blurb": (
                "Nix buys odd fish out of cargo holds, one species at a time. "
                "She takes her species and leaves everything else in the hold."
            ),
        },
    },
)

FISHING_HOLE_IDS = frozenset(
    f"{pad['id']}_hole" for pad in REMOTE_PADS if pad.get("hole_name")
)
REMOTE_PAD_IDS = frozenset(pad["id"] for pad in REMOTE_PADS)

GARAGE_ROOM_IDS = frozenset({
    GARAGE_ROOM_ID,
    CAMPER_ROOM_ID,
}) | REMOTE_PAD_IDS | FISHING_HOLE_IDS


def add_garage_to_world(rooms: Dict[str, "Room"]) -> None:
    """Add the garage south of Slick's, the camper's interior, and its stops."""
    from world import Room

    rooms[GARAGE_ROOM_ID] = Room(
        id=GARAGE_ROOM_ID,
        name="Back Garage",
        description=(
            "Slick's garage is more storage than workshop. Bald tires are "
            "stacked to the rafters and the floor is a map of old oil "
            "stains. Taking up most of the bay is a broken-down Airstream "
            "camper up on blocks, its aluminum skin dented and hazed gray. "
            "A heavy padlock hangs through the door latch.\n\n"
            "Gus dozes in a lawn chair beside it."
        ),
        exits={"north": "slick_store"},
        items=[],
        is_water=False,
        npcs=["Gus"],
    )
    store = rooms.get("slick_store")
    if store:
        store.exits["south"] = GARAGE_ROOM_ID
        store.description = (
            store.description.rstrip()
            + "\n\nA propped-open door at the back leads south into Slick's garage."
        )

    stops = REMOTE_PADS
    for pad in stops:
        description = (
            pad["description"]
            + "\n\nA weathered placard by the tarmac reads: SKIPJACK HULLS "
            "BOUGHT AND SOLD. Type 'list' for terms."
        )
        npcs = []
        exits = {}
        if pad["id"] == DELL_PAD_ID:
            description += (
                "\n\nDell runs a parts stall out of a shipping container "
                "here, engines and hyperdrive cores racked behind her like "
                "cordwood. She buys and sells ship upgrade modules."
            )
            npcs.append("Dell")
        buyer = pad.get("buyer")
        if buyer:
            description += f"\n\n{buyer['blurb']}"
            npcs.append(buyer["name"])
        if pad.get("hole_name"):
            hole_id = f"{pad['id']}_hole"
            exits["east"] = hole_id
            rooms[hole_id] = Room(
                id=hole_id,
                name=pad["hole_name"],
                description=pad["hole_description"],
                exits={"west": pad["id"]},
                items=[],
                is_water=True,
            )
        rooms[pad["id"]] = Room(
            id=pad["id"],
            name=pad["name"],
            description=description,
            exits=exits,
            items=[],
            is_water=False,
            npcs=npcs,
        )

    rooms[CAMPER_ROOM_ID] = Room(
        id=CAMPER_ROOM_ID,
        name="Inside the Airstream",
        description=(
            "The camper's dinette has been torn out and replaced with a "
            "single wrap-around console. Pilot, navigation, sensor, and "
            "weapons controls share the panel where a fold-down table used "
            "to be. The windows are shuttered.\n\n"
            "A strip of masking tape names the surviving buttons: "
            "LAUNCH (LAU), ACCELERATE (ACC), CALCULATE (CAL), "
            "HYPERSPACE (HYP), LAND (LAN), RADAR (RAD), and STATUS (STA). "
            "You might want to try hitting them.\n\n"
            "There is no door to walk through. Once you've set down and "
            "opened up, LEAVE (LEA)."
        ),
        exits={},
        items=[],
        is_water=False,
    )


def buyer_species_id(buyer: dict, now: Optional[float] = None) -> str:
    """The one species this buyer wants during the current 3-hour block."""
    now = time.time() if now is None else now
    species = buyer["species"]
    return species[int(now // CARGO_CYCLE_SECONDS) % len(species)]


def cycle_remaining(now: Optional[float] = None) -> int:
    now = time.time() if now is None else now
    return CARGO_CYCLE_SECONDS - (int(now) % CARGO_CYCLE_SECONDS)


def _format_remaining(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def _fish_named(fish_id: str):
    from items import CATCHABLE_FISH

    for fish, _weight in CATCHABLE_FISH:
        if fish.id == fish_id:
            return fish
    return None


def _pad_entry(room_id: str) -> dict:
    for pad in REMOTE_PADS:
        if pad["id"] == room_id:
            return pad
    raise KeyError(room_id)


def _landing(room_id: str, x: int, y: int, z: int) -> LandingPad:
    return LandingPad(_pad_entry(room_id)["name"], room_id, x, y, z)


def default_starsystems() -> List[StarSystem]:
    """Real stars on the galactic plane, plus one starless void."""
    sol = StarSystem(
        name="Sol", galaxy_x=0, galaxy_y=0,
        stars=[Star("Sol", -7000, 0, 0)],
        planets=[
            Planet("Earth", 1000, 1000, 1000, [
                LandingPad("Back Garage", GARAGE_ROOM_ID, 1000, 1000, 1000),
            ]),
            Moon("Europa", -1800, 900, 2200, [
                _landing("europa", -1800, 900, 2200),
            ]),
        ],
    )
    betelgeuse = StarSystem(
        name="Betelgeuse", galaxy_x=5000, galaxy_y=1200,
        stars=[Star("Betelgeuse", 0, -7000, 0)],
        planets=[
            Planet("Haven", 1400, -900, 1700, [
                _landing("beta_haven", 1400, -900, 1700),
            ]),
            Planet("Forge", -2200, 1300, -1200, [
                _landing("beta_forge", -2200, 1300, -1200),
            ]),
        ],
    )
    vega = StarSystem(
        name="Vega", galaxy_x=-3200, galaxy_y=4800,
        stars=[Star("Vega", 0, 0, -7000)],
        planets=[
            Planet("Reach", 1800, 1400, -1000, [
                _landing("gamma_reach", 1800, 1400, -1000),
            ]),
            Planet("Ice", -1600, -1800, 1600, [
                _landing("gamma_ice", -1600, -1800, 1600),
            ]),
        ],
    )
    sirius = StarSystem(
        name="Sirius", galaxy_x=2000, galaxy_y=-4500,
        stars=[Star("Sirius", 7000, 0, 0)],
        planets=[
            Planet("Whiteflare", 1400, -900, 1700, [
                _landing("sirius_brightwater", 1400, -900, 1700),
            ]),
            Planet("Cinder", -2200, 1300, -1200, [
                _landing("sirius_quay", -2200, 1300, -1200),
            ]),
        ],
    )
    rigel = StarSystem(
        name="Rigel", galaxy_x=-6000, galaxy_y=-2000,
        stars=[Star("Rigel", 0, 7000, 0)],
        planets=[
            Planet("Bluestone", 1800, 1400, -1000, [
                _landing("rigel_bluewater", 1800, 1400, -1000),
            ]),
            Planet("Exchange", -1600, -1800, 1600, [
                _landing("rigel_exchange", -1600, -1800, 1600),
            ]),
        ],
    )
    deneb = StarSystem(
        name="Deneb", galaxy_x=4500, galaxy_y=5500,
        stars=[Star("Deneb", 0, 0, 7000)],
        planets=[
            Planet("Swanrock", 1200, 800, 1500, [
                _landing("deneb_swanpool", 1200, 800, 1500),
            ]),
            Planet("Yard", -2000, 400, -900, [
                _landing("deneb_wharf", -2000, 400, -900),
            ]),
        ],
    )
    void = StarSystem(
        name="Void", galaxy_x=-1500, galaxy_y=-5500,
        stars=[],
        planets=[
            Station("Drift", 0, 0, 0, [
                _landing("void_drift", 0, 0, 0),
            ]),
        ],
    )
    return [sol, betelgeuse, vega, sirius, rigel, deneb, void]


STAR_MAP_COLUMNS = 48
STAR_MAP_ROWS = 17


def render_star_map(systems: List[StarSystem], here: Optional[StarSystem]) -> str:
    """Plain-text star map. O is a system, + is the one you're in."""
    if not systems:
        return "The chart is blank."
    xs = [s.galaxy_x for s in systems]
    ys = [s.galaxy_y for s in systems]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(max_x - min_x, 1)
    span_y = max(max_y - min_y, 1)
    longest = max(len(s.name) for s in systems)
    width = STAR_MAP_COLUMNS + longest + 2
    grid = [[" "] * width for _ in range(STAR_MAP_ROWS)]

    def place(row: int, col: int, text: str) -> None:
        for offset, ch in enumerate(text):
            if 0 <= col + offset < width:
                grid[row][col + offset] = ch

    for system in systems:
        col = round((system.galaxy_x - min_x) / span_x * (STAR_MAP_COLUMNS - 1))
        row = round((max_y - system.galaxy_y) / span_y * (STAR_MAP_ROWS - 1))
        marker = "+" if system is here else "O"
        place(row, col, f"{marker} {system.name}")

    lines = ["".join(cells).rstrip() for cells in grid]
    if here is not None:
        footer = f"O star system   + you are here ({here.name})"
    else:
        footer = "O star system   + you are here (in hyperspace, nowhere yet)"
    return "\n".join(["STAR MAP", *lines, footer])


def default_ships(systems: List[StarSystem]) -> List[Ship]:
    ship = Ship(
        name="Silver Airstream",
        owner=PUBLIC_OWNER,
        ship_id=PUBLIC_SHIP_ID,
        cockpit_room=CAMPER_ROOM_ID,
        location=GARAGE_ROOM_ID,
        lastdoc=GARAGE_ROOM_ID,
        home="Sol",
    )
    return [ship]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class GarageEngine:
    def __init__(self, rooms: Dict[str, "Room"]):
        self.rooms = rooms
        self.systems = default_starsystems()
        self.ships = default_ships(self.systems)
        self._ticks = 0
        self.eject_queue: List[Tuple[str, str]] = []
        self.regional_weather = None  # RegionalWeather, set by attach_weather
        self._pad_index: Dict[str, LandingPad] = {}
        for system in self.systems:
            for pad in system.pads():
                self._pad_index[pad.room_id] = pad
        self.load_ships()
        self._ensure_drifter()
        self.ensure_cockpit_rooms()

    def attach_rooms(self, rooms: Dict[str, "Room"]) -> None:
        """Keep ship interiors after a world rebuild."""
        self.rooms = rooms
        self.ensure_cockpit_rooms()
        self._stock_drifter_gem()

    # -- weather ---------------------------------------------------------------

    def attach_weather(self, earth_weather):
        """Give every body off Earth its own hourly sky. Earth keeps the lake's."""
        from weather import RegionalWeather

        names = [
            planet.name for system in self.systems for planet in system.planets
        ]
        self.regional_weather = RegionalWeather(earth_weather, names)
        return self.regional_weather

    def planet_for_room(self, room_id: str) -> Optional[Planet]:
        """The body under a pad, its fishing hole, or a ship parked at one."""
        ship = self.ship_from_interior(room_id)
        if ship is not None:
            if ship.state != ShipState.DOCKED or not ship.location:
                return None
            room_id = ship.location
        if room_id.endswith("_hole"):
            room_id = room_id[: -len("_hole")]
        pad = self.pad_for_room(room_id)
        system = self.system_for_pad(room_id)
        if pad is None or system is None:
            return None
        return system.planet_near(pad)

    def weather_for_room(self, room_id: str):
        """The sky over this room. None aboard a ship that is not parked."""
        if self.regional_weather is None:
            return None
        ship = self.ship_from_interior(room_id)
        if ship is not None and (ship.state != ShipState.DOCKED or not ship.location):
            return None
        planet = self.planet_for_room(room_id)
        return self.regional_weather.for_planet(planet.name if planet else None)

    def fishing_planets(self) -> List[Planet]:
        """Bodies with water to fish: Earth's lake and every pad with a hole."""
        found: List[Planet] = []
        for system in self.systems:
            for planet in system.planets:
                for pad in planet.pads:
                    if pad.room_id == GARAGE_ROOM_ID or f"{pad.room_id}_hole" in FISHING_HOLE_IDS:
                        found.append(planet)
                        break
        return found

    def rooms_on_planet(self, name: str) -> List[str]:
        """Pad and fishing-hole rooms on a body, for weather announcements."""
        rooms: List[str] = []
        for system in self.systems:
            for planet in system.planets:
                if planet.name != name:
                    continue
                for pad in planet.pads:
                    rooms.append(pad.room_id)
                    hole = f"{pad.room_id}_hole"
                    if hole in FISHING_HOLE_IDS:
                        rooms.append(hole)
        return rooms

    def radio_report(self, player, result_cls):
        """From a cockpit: every fishing planet's sky and what each buyer wants."""
        if self.ship_from_cockpit(player.current_room) is None:
            return self._result(result_cls, "You don't have a radio.")
        lines = [
            "You tune the radio. Static, then a flat voice reading the boards.",
            "",
            "WEATHER",
        ]
        if self.regional_weather is None:
            lines.append("  No weather reports today.")
        else:
            for planet in self.fishing_planets():
                sky = self.regional_weather.for_planet(planet.name).get_current_weather()
                lines.append(f"  {planet.name:<10} {sky.name}")
        lines.extend(["", "BUYERS"])
        for pad in REMOTE_PADS:
            buyer = pad.get("buyer")
            if not buyer:
                continue
            wanted_id = buyer_species_id(buyer)
            wanted = _fish_named(wanted_id)
            species = wanted.name if wanted else wanted_id
            system = self.system_for_pad(pad["id"])
            where = f"{pad['name']}, {system.name}" if system else pad["name"]
            lines.append(f"  {buyer['name']:<8} {species:<18} {where}")
        lines.append(f"  Boards change in {_format_remaining(cycle_remaining())}.")
        return self._result(result_cls, "\n".join(lines))

    def owned_ship_for(self, player_name: str) -> Optional[Ship]:
        for ship in self.ships:
            if ship.automated:
                continue
            if ship.owner.lower() == player_name.lower() and ship.owner != PUBLIC_OWNER:
                return ship
        return None

    def can_create_owned_ship_at(self, room_id: str) -> bool:
        return room_id in REMOTE_PAD_IDS

    def create_owned_ship(
        self, player, pad_room_id: str, name: Optional[str] = None
    ) -> Optional[Ship]:
        """One personal Skipjack per player, only at pads off Earth."""
        if self.owned_ship_for(player.name):
            return None
        if not self.can_create_owned_ship_at(pad_room_id):
            return None
        token = _owner_token(player.name)
        ship = Ship(
            name=name or f"{player.name}'s Skipjack",
            owner=player.name,
            ship_id=f"ship_{token}",
            cockpit_room=f"skipjack_cockpit_{token}",
            hold_room=f"skipjack_hold_{token}",
            hull_type=HULL_SKIPJACK,
            location=pad_room_id,
            lastdoc=pad_room_id,
            home="Sol",
            locked=True,
            hatch_open=False,
            state=ShipState.DOCKED,
        )
        ship.apply_modules()
        self.ships.append(ship)
        self.ensure_cockpit_rooms()
        self.save_ships()
        return ship

    def remove_ship(self, ship: Ship) -> None:
        """Forget a personal ship and its interior rooms."""
        self._leave_system(ship)
        for room_id in ship.interior_rooms():
            self.rooms.pop(room_id, None)
        if ship in self.ships:
            self.ships.remove(ship)
        self.save_ships()

    def ensure_cockpit_rooms(self) -> None:
        """Build any missing cockpit / cargo hold rooms for every ship."""
        from world import Room

        template = self.rooms.get(CAMPER_ROOM_ID)
        camper_description = (
            template.description
            if template
            else "The camper's dinette has been torn out and replaced with a console."
        )
        for ship in self.ships:
            if not ship.cockpit_room:
                continue
            if ship.cockpit_room not in self.rooms:
                if ship.automated:
                    description = (
                        "Nobody is flying The Drifter. The pilot seat is empty "
                        "and the console runs itself: a racing engine and a "
                        "deep-space hyperdrive, both locked in. It holds a "
                        "heading for five minutes, then jumps.\n\n"
                        "A gem sits on the copilot chair when the route is "
                        "fresh. Take it and get back to your own ship, then "
                        "DETACH. The autopilot will not release the lock from "
                        "this side."
                    )
                    exits = {}
                elif ship.is_skipjack():
                    description = (
                        "The Skipjack's cockpit is two seats and a wrap-around "
                        "console that has been repaired more than once. The "
                        "windows are shuttered.\n\n"
                        "Masking tape labels the surviving buttons: LAUNCH "
                        "(LAU), ACCELERATE (ACC), CALCULATE (CAL), HYPERSPACE "
                        "(HYP), LAND (LAN), RADAR (RAD), and STATUS (STA).\n\n"
                        "A hatch in the deck leads south to the cargo hold. "
                        "Once you've set down and opened up, LEAVE (LEA)."
                    )
                    exits = {"south": ship.hold_room} if ship.hold_room else {}
                else:
                    description = camper_description
                    exits = {}
                self.rooms[ship.cockpit_room] = Room(
                    id=ship.cockpit_room,
                    name=f"Inside {ship.name}",
                    description=description,
                    exits=exits,
                    items=[],
                    is_water=False,
                )
            if ship.hold_room and ship.hold_room not in self.rooms:
                self.rooms[ship.hold_room] = Room(
                    id=ship.hold_room,
                    name=f"Cargo Hold of {ship.name}",
                    description=(
                        "A low steel hold lined with tie-down rails. It smells "
                        "of cold and fish. Racks along the bulkhead take the "
                        "ship's modules: engine, hyperdrive core, and cargo "
                        "unit. Type CARGO to see what's stowed, INSTALL or "
                        "UNINSTALL to swap modules. The cockpit is north."
                    ),
                    exits={"north": ship.cockpit_room},
                    items=[],
                    is_water=False,
                )

    ensure_ship_rooms = ensure_cockpit_rooms

    def _ship_to_save(self, ship: Ship) -> Dict:
        from items import item_to_save_dict

        dock = ship.lastdoc or ship.location or GARAGE_ROOM_ID
        if dock not in self._pad_index:
            dock = GARAGE_ROOM_ID
        return {
            "ship_id": ship.ship_id or PUBLIC_SHIP_ID,
            "name": ship.name,
            "owner": ship.owner,
            "hull_type": ship.hull_type,
            "cockpit_room": ship.cockpit_room,
            "hold_room": ship.hold_room,
            "location": dock,
            "lastdoc": dock,
            "locked": bool(ship.locked),
            "hatch_open": False,
            "hull": ship.hull,
            "maxhull": ship.maxhull,
            "shield": ship.shield,
            "maxshield": ship.maxshield,
            "missiles": ship.missiles,
            "maxmissiles": ship.maxmissiles,
            "home": ship.home,
            "modules": dict(ship.modules),
            "cargo": [item_to_save_dict(item) for item in ship.cargo],
        }

    def save_ships(self) -> None:
        SHIPS_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ships": [
                self._ship_to_save(ship)
                for ship in self.ships
                if not ship.automated
            ]
        }
        SHIPS_PATH.write_text(json.dumps(payload, indent=2))

    def load_ships(self) -> None:
        if not SHIPS_PATH.exists():
            return
        try:
            payload = json.loads(SHIPS_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            return
        saved = payload.get("ships") or []
        by_id = {ship.ship_id: ship for ship in self.ships if ship.ship_id}
        for entry in saved:
            ship_id = entry.get("ship_id") or ""
            if ship_id == DRIFTER_SHIP_ID:
                continue
            dock = entry.get("lastdoc") or entry.get("location") or GARAGE_ROOM_ID
            dock = DOCK_ALIASES.get(dock, dock)
            if dock not in self._pad_index:
                dock = GARAGE_ROOM_ID
            existing = by_id.get(ship_id)
            if existing is None and ship_id == PUBLIC_SHIP_ID:
                existing = next(
                    (ship for ship in self.ships if ship.owner == PUBLIC_OWNER),
                    None,
                )
            if existing is not None:
                existing.location = dock
                existing.lastdoc = dock
                existing.locked = bool(entry.get("locked", True))
                existing.hatch_open = False
                existing.state = ShipState.DOCKED
                existing.hull = int(entry.get("hull", existing.hull))
                existing.shield = int(entry.get("shield", 0))
                existing.missiles = int(entry.get("missiles", existing.missiles))
                existing.orbit = None
                existing.starsystem = None
                existing.currspeed = 0
                continue
            if entry.get("owner") in ("", PUBLIC_OWNER):
                continue
            from items import MODULE_SPECS, item_from_save_dict, is_ancient_fish_id

            owner = entry.get("owner") or PUBLIC_OWNER
            token = _owner_token(owner)
            # Every personal ship is a Skipjack; older one-room saves convert.
            modules = {"engine": None, "hyper": None, "cargo": None}
            for slot, module_id in (entry.get("modules") or {}).items():
                if slot in modules and module_id in MODULE_SPECS:
                    modules[slot] = module_id
            cargo = []
            for item_data in entry.get("cargo") or []:
                try:
                    if is_ancient_fish_id(item_data.get("id", "")):
                        continue
                    cargo.append(item_from_save_dict(item_data))
                except (KeyError, ValueError):
                    continue
            ship = Ship(
                name=entry.get("name") or f"{owner}'s Skipjack",
                owner=owner,
                ship_id=ship_id or f"ship_{token}",
                hull_type=HULL_SKIPJACK,
                cockpit_room=entry.get("cockpit_room") or f"skipjack_cockpit_{token}",
                hold_room=entry.get("hold_room") or f"skipjack_hold_{token}",
                location=dock,
                lastdoc=dock,
                locked=bool(entry.get("locked", True)),
                hatch_open=False,
                hull=int(entry.get("hull", 500)),
                maxhull=int(entry.get("maxhull", 500)),
                shield=int(entry.get("shield", 0)),
                maxshield=int(entry.get("maxshield", 250)),
                missiles=int(entry.get("missiles", 8)),
                maxmissiles=int(entry.get("maxmissiles", 8)),
                home=entry.get("home") or "Sol",
                state=ShipState.DOCKED,
                modules=modules,
                cargo=cargo,
            )
            ship.apply_modules()
            self.ships.append(ship)

    def systems_matching(self, name: str) -> List[StarSystem]:
        """Systems whose name starts with this abbreviation. 'bet' is Betelgeuse."""
        needle = name.strip().lower()
        if not needle:
            return []
        return [system for system in self.systems if system.name.lower().startswith(needle)]

    def system_named(self, name: str) -> Optional[StarSystem]:
        hits = self.systems_matching(name)
        if len(hits) == 1:
            return hits[0]
        return None

    def ship_named(self, name: str) -> Optional[Ship]:
        for ship in self.ships:
            if ship.matches(name):
                return ship
        return None

    def ships_at(self, room_id: str) -> List[Ship]:
        return [s for s in self.ships if s.state == ShipState.DOCKED and s.location == room_id]

    def ship_from_cockpit(self, room_id: str) -> Optional[Ship]:
        for ship in self.ships:
            if ship.cockpit_room == room_id:
                return ship
        return None

    def ship_from_interior(self, room_id: str) -> Optional[Ship]:
        """The ship whose cockpit or cargo hold this room is."""
        for ship in self.ships:
            if room_id in ship.interior_rooms():
                return ship
        return None

    def _players_aboard(self, ship: Ship) -> List[str]:
        names: List[str] = []
        for room_id in ship.interior_rooms():
            room = self.rooms.get(room_id)
            if room:
                names.extend(room.players)
        return names

    def pad_for_room(self, room_id: str) -> Optional[LandingPad]:
        return self._pad_index.get(room_id)

    def system_for_pad(self, room_id: str) -> Optional[StarSystem]:
        for system in self.systems:
            if any(p.room_id == room_id for p in system.pads()):
                return system
        return None

    def describe_ships_here(self, room_id: str) -> str:
        parked = self.ships_at(room_id)
        if not parked:
            return ""
        lines = ["\nParked here:"]
        for ship in parked:
            if ship.locked:
                state = "padlocked"
            else:
                state = "door open" if ship.hatch_open else "door shut"
            lines.append(f"  - {ship.name} ({state})")
        return "\n".join(lines)

    # -- tick ------------------------------------------------------------------

    def update(self) -> List[Broadcast]:
        """
        Called once per second.
        Move ships every tick (SWL move_ships ~ 1s).
        Advance launch/land/hyper every 2 ticks (compressed from 10s pulses).
        """
        notes: List[Broadcast] = []
        self._ticks += 1
        self._move_ships(notes)
        if self._ticks % 2 == 0:
            self._update_space(notes)
        self._apply_orbits(notes)
        if self._ticks % COORD_UPDATE_SECONDS == 0:
            self._echo_positions(notes)
        self._tick_drifter(notes)
        return notes

    def drifter(self) -> Optional[Ship]:
        return next((ship for ship in self.ships if ship.automated), None)

    def _ensure_drifter(self) -> None:
        """The unmanned ship. Recreated in flight on every boot, never sold."""
        if self.drifter() is not None:
            return
        ship = Ship(
            name=DRIFTER_NAME,
            owner=DRIFTER_OWNER,
            ship_id=DRIFTER_SHIP_ID,
            cockpit_room=DRIFTER_COCKPIT_ROOM,
            hull_type=HULL_SKIPJACK,
            locked=False,
            hatch_open=False,
            autopilot=True,
            automated=True,
            modules={"engine": "engine_racing", "hyper": "hyper_deep", "cargo": None},
        )
        ship.apply_modules()
        self.ships.append(ship)
        self.ensure_cockpit_rooms()
        self._launch_drifter(ship, [])

    def _open_space_point(self) -> Tuple[float, float, float]:
        """A spot clear of the local planets, moons, and stations."""
        return tuple(
            random.choice((-1.0, 1.0)) * random.randint(4000, 8000)
            for _ in range(3)
        )

    def _stock_drifter_gem(self) -> None:
        from items import ItemType, create_gem

        ship = self.drifter()
        if ship is None:
            return
        room = self.rooms.get(ship.cockpit_room)
        if room is None:
            return
        if any(item.item_type == ItemType.GEM for item in room.items):
            return
        room.items.append(create_gem())

    def _arm_drifter_cruise(self, ship: Ship, notes: List[Broadcast]) -> None:
        """Start five minutes of full-speed flight and leave a gem if needed."""
        ship.autopilot = True
        ship.state = ShipState.READY
        ship.orbit = None
        ship.currspeed = ship.realspeed
        ship.cruise_left = DRIFTER_CRUISE_SECONDS
        ship.hx = random.uniform(-1.0, 1.0) or 1.0
        ship.hy = random.uniform(-1.0, 1.0)
        ship.hz = random.uniform(-1.0, 1.0)
        self._stock_drifter_gem()
        self._echo_cockpit(
            ship,
            "The autopilot picks a heading and pushes the racing engine to full.",
            notes,
        )

    def _launch_drifter(self, ship: Ship, notes: List[Broadcast]) -> None:
        system = random.choice(self.systems)
        x, y, z = self._open_space_point()
        self._enter_system(ship, system, x, y, z)
        self._arm_drifter_cruise(ship, notes)
        self._echo_system(
            system,
            f"{ship.name} drops out of hyperspace and runs for open space.",
            notes,
        )

    def _drifter_jump(self, ship: Ship, notes: List[Broadcast]) -> None:
        origin = ship.starsystem
        choices = [system for system in self.systems if system is not origin]
        if not choices:
            ship.cruise_left = DRIFTER_CRUISE_SECONDS
            return
        dest = random.choice(choices)
        ship.jumpx, ship.jumpy, ship.jumpz = self._open_space_point()
        ship.currjump = dest
        ship.hyperdistance = max(1, _galaxy_distance(origin, dest)) if origin else 1
        ship.orbit = None
        ship.currspeed = 0
        ship.state = ShipState.HYPERSPACE
        if origin is not None:
            self._echo_system(
                origin, f"{ship.name} vanishes into hyperspace.", notes
            )
        self._leave_system(ship)
        self._echo_cockpit(
            ship, f"The autopilot jumps for {dest.name}. Stars smear into lines.", notes
        )

    def _tick_drifter(self, notes: List[Broadcast]) -> None:
        """Cruise for five minutes, jump, repeat. A dock holds the clock."""
        ship = self.drifter()
        if ship is None:
            return
        if ship.docked_ship is not None:
            ship.currspeed = 0
            return
        if ship.state == ShipState.HYPERSPACE:
            return
        if ship.cruise_left > 0:
            ship.state = ShipState.READY
            ship.currspeed = ship.realspeed
            ship.cruise_left -= 1
            if ship.cruise_left > 0:
                return
        self._drifter_jump(ship, notes)

    def _echo_cockpit(self, ship: Ship, message: str, notes: List[Broadcast]) -> None:
        notes.append(Broadcast(ship.cockpit_room, message))

    def _echo_pad(self, room_id: str, message: str, notes: List[Broadcast]) -> None:
        if room_id:
            notes.append(Broadcast(room_id, message))

    def _echo_positions(self, notes: List[Broadcast]) -> None:
        """Occasional cockpit readout so flying isn't silent."""
        skip = (
            ShipState.DOCKED, ShipState.DISABLED,
            ShipState.LAUNCH, ShipState.LAUNCH_2,
            ShipState.LAND, ShipState.LAND_2,
            ShipState.HYPERSPACE,
        )
        for ship in self.ships:
            if ship.state in skip or not ship.starsystem:
                continue
            line = (
                f"Speed: {ship.currspeed}  Coords: "
                f"{ship.vx:.0f} {ship.vy:.0f} {ship.vz:.0f}"
            )
            if ship.orbit:
                line += f"  (orbit {ship.orbit})"
            self._echo_cockpit(ship, line, notes)

    def _move_ships(self, notes: List[Broadcast]) -> None:
        for ship in self.ships:
            if not ship.starsystem or ship.currspeed <= 0:
                continue
            if ship.state == ShipState.HYPERSPACE:
                continue
            change = math.sqrt(ship.hx ** 2 + ship.hy ** 2 + ship.hz ** 2)
            if change <= 0:
                continue
            dx, dy, dz = ship.hx / change, ship.hy / change, ship.hz / change
            ship.vx += dx * (ship.currspeed / 5)
            ship.vy += dy * (ship.currspeed / 5)
            ship.vz += dz * (ship.currspeed / 5)

    def _apply_orbits(self, notes: List[Broadcast]) -> None:
        """Hold a ship on a planet once it comes within ORBIT_RANGE."""
        skip = (
            ShipState.DOCKED, ShipState.DISABLED,
            ShipState.LAUNCH, ShipState.LAUNCH_2,
            ShipState.LAND, ShipState.LAND_2,
            ShipState.HYPERSPACE,
        )
        for ship in self.ships:
            if ship.automated:
                continue
            if ship.state in skip or not ship.starsystem:
                continue
            bound = None
            if ship.orbit:
                bound = next(
                    (planet for planet in ship.starsystem.planets if planet.name == ship.orbit),
                    None,
                )
                if bound is None or _distance(
                    ship.vx, ship.vy, ship.vz, bound.x, bound.y, bound.z
                ) > ORBIT_RANGE:
                    self._echo_cockpit(ship, f"You break orbit around {ship.orbit}.", notes)
                    ship.orbit = None
                    if ship.state == ShipState.ORBIT:
                        ship.state = ShipState.READY
                    bound = None
            if bound is not None:
                if ship.currspeed <= 0:
                    ship.vx, ship.vy, ship.vz = float(bound.x), float(bound.y), float(bound.z)
                    ship.currspeed = 0
                    if ship.state == ShipState.READY:
                        ship.state = ShipState.ORBIT
                continue
            nearest = None
            nearest_dist = None
            for planet in ship.starsystem.planets:
                dist = _distance(ship.vx, ship.vy, ship.vz, planet.x, planet.y, planet.z)
                if dist > ORBIT_RANGE:
                    continue
                if nearest_dist is None or dist < nearest_dist:
                    nearest, nearest_dist = planet, dist
            if nearest is None:
                continue
            ship.orbit = nearest.name
            ship.vx, ship.vy, ship.vz = float(nearest.x), float(nearest.y), float(nearest.z)
            ship.currspeed = 0
            if ship.state in (ShipState.READY, ShipState.BUSY, ShipState.BUSY_2, ShipState.BUSY_3):
                ship.state = ShipState.ORBIT
            self._echo_cockpit(
                ship,
                f"The ship settles into orbit around the {nearest.kind} {nearest.name}.",
                notes,
            )

    def _echo_ship(self, ship: Ship, message: str, notes: List[Broadcast]) -> None:
        """Everyone aboard, cockpit and hold alike."""
        for room_id in ship.interior_rooms():
            notes.append(Broadcast(room_id, message))

    def _echo_system(self, system: Optional[StarSystem], message: str, notes: List[Broadcast]) -> None:
        if system is None:
            return
        for ship in system.ships:
            self._echo_cockpit(ship, message, notes)

    def _update_space(self, notes: List[Broadcast]) -> None:
        for ship in self.ships:
            if ship.laser_cool > 0:
                ship.laser_cool -= 1

            # A docked partner that is no longer in the same system lets go.
            if ship.docked_ship is not None and (
                ship.docked_ship.starsystem is None
                or ship.docked_ship.starsystem is not ship.starsystem
            ):
                self._clear_dock(ship)

            if ship.dock_state == DockState.DOCK_2:
                self._finish_dock(ship, notes)
            elif ship.dock_state == DockState.DOCK:
                ship.dock_state = DockState.DOCK_2

            if ship.state == ShipState.HYPERSPACE:
                ship.hyperdistance -= ship.hyperspeed * 2
                if ship.hyperdistance <= 0:
                    dest = ship.currjump
                    if dest is None:
                        self._echo_cockpit(ship, "Ship lost in hyperspace. Make new calculations.", notes)
                    else:
                        self._enter_system(ship, dest, ship.jumpx, ship.jumpy, ship.jumpz)
                        ship.state = ShipState.READY
                        ship.home = dest.name
                        self._echo_cockpit(ship, "Hyperjump complete.", notes)
                        self._echo_cockpit(
                            ship,
                            "The ship lurches slightly as it comes out of hyperspace.",
                            notes,
                        )
                        if ship.automated:
                            self._echo_system(
                                dest,
                                f"{ship.name} drops out of hyperspace and runs for open space.",
                                notes,
                            )
                            self._arm_drifter_cruise(ship, notes)
                else:
                    self._echo_cockpit(
                        ship,
                        f"Remaining jump distance: {ship.hyperdistance}",
                        notes,
                    )

            if ship.state == ShipState.BUSY_3:
                self._echo_cockpit(ship, "Maneuver complete.", notes)
                if ship.orbit and ship.currspeed <= 0:
                    ship.state = ShipState.ORBIT
                else:
                    ship.state = ShipState.READY
            elif ship.state == ShipState.BUSY_2:
                ship.state = ShipState.BUSY_3
            elif ship.state == ShipState.BUSY:
                ship.state = ShipState.BUSY_2

            if ship.state == ShipState.LAND_2:
                self._finish_land(ship, notes)
            elif ship.state == ShipState.LAND:
                ship.state = ShipState.LAND_2

            if ship.state == ShipState.LAUNCH_2:
                self._finish_launch(ship, notes)
            elif ship.state == ShipState.LAUNCH:
                ship.state = ShipState.LAUNCH_2

    def _enter_system(self, ship: Ship, system: StarSystem, x, y, z) -> None:
        if ship.starsystem and ship in ship.starsystem.ships:
            ship.starsystem.ships.remove(ship)
        ship.starsystem = system
        if ship not in system.ships:
            system.ships.append(ship)
        ship.vx, ship.vy, ship.vz = float(x), float(y), float(z)

    def _leave_system(self, ship: Ship) -> None:
        if ship.starsystem and ship in ship.starsystem.ships:
            ship.starsystem.ships.remove(ship)
        ship.starsystem = None

    def _finish_launch(self, ship: Ship, notes: List[Broadcast]) -> None:
        system = self.system_for_pad(ship.lastdoc or ship.location)
        if system is None:
            self._echo_cockpit(ship, "Launch path blocked .. Launch aborted.", notes)
            self._echo_pad(ship.location, f"{ship.name} slowly sets back down.", notes)
            ship.state = ShipState.DOCKED
            return
        pad = self.pad_for_room(ship.lastdoc or ship.location)
        self._enter_system(ship, system, pad.x if pad else 0, pad.y if pad else 0, pad.z if pad else 0)
        ship.hx = random.choice((-1.0, 1.0))
        ship.hy = random.choice((-1.0, 1.0))
        ship.hz = random.choice((-1.0, 1.0))
        ship.vx += ship.hx * ship.currspeed * 2
        ship.vy += ship.hy * ship.currspeed * 2
        ship.vz += ship.hz * ship.currspeed * 2
        pad_room = ship.location
        ship.location = ""
        ship.state = ShipState.READY
        self._echo_cockpit(ship, "Launch complete.", notes)
        self._echo_cockpit(
            ship,
            "The ship leaves the platform far behind as it flies into space.",
            notes,
        )
        self._echo_pad(pad_room, f"{ship.name} pulls out and is gone.", notes)

    def _clear_dock(self, ship: Ship) -> None:
        """Drop the docking link on both hulls."""
        other = ship.docked_ship
        ship.docked_ship = None
        ship.dock_state = DockState.NONE
        if other is not None and other.docked_ship is ship:
            other.docked_ship = None
            other.dock_state = DockState.NONE

    def _finish_dock(self, ship: Ship, notes: List[Broadcast]) -> None:
        """SWL dockship: two ticks after the approach starts, the hulls lock."""
        target = ship.docked_ship
        if target is None or target.starsystem is not ship.starsystem:
            self._echo_cockpit(ship, "Docking aborted. The other ship is gone.", notes)
            self._clear_dock(ship)
            return
        ship.dock_state = DockState.DOCK_3
        target.dock_state = DockState.DOCK_3
        ship.vx, ship.vy, ship.vz = target.vx, target.vy, target.vz
        ship.currspeed = 0
        target.currspeed = 0
        self._echo_cockpit(ship, "Docking sequence complete.", notes)
        self._echo_ship(ship, "You feel a slight thud as the ship locks in with the next.", notes)
        self._echo_cockpit(target, "Docking sequence complete.", notes)
        self._echo_ship(target, "You feel a slight thud as the ship locks in with the next.", notes)
        self._echo_system(
            ship.starsystem, f"{ship.name} and {target.name} have docked.", notes
        )

    def _dock_block(self, ship: Ship) -> Optional[str]:
        """Why a maneuver can't start while docking is under way or finished."""
        if ship.docking_in_progress():
            return "Not while docking procedures are going on."
        if ship.is_docked_with() is not None:
            return "Detach from the docked ship first."
        return None

    def _finish_land(self, ship: Ship, notes: List[Broadcast]) -> None:
        pad = None
        if ship.starsystem:
            pad = ship.starsystem.pad_named(ship.dest)
        if pad is None:
            self._echo_cockpit(ship, "Could not complete approach. Landing aborted.", notes)
            if ship.state != ShipState.DISABLED:
                ship.state = ShipState.READY
            return
        self._leave_system(ship)
        ship.orbit = None
        ship.location = pad.room_id
        ship.lastdoc = pad.room_id
        ship.currspeed = 0
        ship.state = ShipState.DOCKED
        if self._is_public(ship):
            ship.missiles = ship.maxmissiles
            ship.hull = ship.maxhull
            ship.shield = 0
            self._echo_cockpit(ship, "Repairing ship...", notes)
        self._echo_cockpit(ship, "Landing sequence complete.", notes)
        self._echo_cockpit(ship, "You feel a slight thud as the ship sets down.", notes)
        self._echo_pad(pad.room_id, f"{ship.name} rolls back in and settles.", notes)
        self.save_ships()

    # -- commands --------------------------------------------------------------

    def knows_room(self, room_id: str) -> bool:
        """True only inside the garage, a ship, or one of the stops."""
        if room_id in GARAGE_ROOM_IDS:
            return True
        return self.ship_from_interior(room_id) is not None

    def handle(self, player, command: str, args: str, result_cls) -> Optional["CommandResult"]:
        # Anywhere else in the world these words stay unknown commands, so
        # nobody stumbles onto the camper by typing at the lake.
        if not self.knows_room(player.current_room):
            return None
        fn = self._commands().get(command)
        if fn is None:
            return None
        return fn(player, args, result_cls)

    def _commands(self) -> Dict[str, Callable]:
        commands = {
            "ships": self.cmd_ships,
            "unlock": self.cmd_unlock,
            "lock": self.cmd_lock,
            "openhatch": self.cmd_openhatch,
            "open": self.cmd_openhatch,
            "closehatch": self.cmd_closehatch,
            "close": self.cmd_closehatch,
            "board": self.cmd_board,
            "enter": self.cmd_board,
            "leaveship": self.cmd_leaveship,
            "leave": self.cmd_leaveship,
            "launch": self.cmd_launch,
            "land": self.cmd_land,
            "status": self.cmd_status,
            "radar": self.cmd_radar,
            "trajectory": self.cmd_trajectory,
            "accelerate": self.cmd_accelerate,
            "acc": self.cmd_accelerate,
            "calculate": self.cmd_calculate,
            "cal": self.cmd_calculate,
            "hyperspace": self.cmd_hyperspace,
            "hyper": self.cmd_hyperspace,
            "target": self.cmd_target,
            "fire": self.cmd_fire,
            "recharge": self.cmd_recharge,
            "autopilot": self.cmd_autopilot,
            "docking": self.cmd_docking,
            "dock": self.cmd_docking,
            "detach": self.cmd_detach,
            "map": self.cmd_map,
            "return": self.cmd_return,
            "rename": self.cmd_rename,
            "cargo": self.cmd_cargo,
            "hold": self.cmd_cargo,
            "install": self.cmd_install,
            "uninstall": self.cmd_uninstall,
            "modules": self.cmd_modules,
        }
        for name, fn in list(commands.items()):
            if len(name) > 3:
                commands.setdefault(name[:3], fn)
        return commands

    def _result(self, result_cls, message: str, broadcasts: Optional[List[Broadcast]] = None):
        wire = None
        if broadcasts:
            wire = "|".join(b.wire() for b in broadcasts)
        return result_cls(message=message, broadcast=wire)

    def _need_ship(self, player, result_cls, seat: str = "cockpit"):
        ship = self.ship_from_cockpit(player.current_room)
        if ship is None:
            return None, self._result(
                result_cls, "You must be in the cockpit of a ship to do that!"
            )
        if ship.autopilot and seat != "look":
            return None, self._result(
                result_cls, "The ship is set on autopilot, you'll have to turn it off first."
            )
        return ship, None

    def _can_maneuver(self, ship: Ship) -> bool:
        return bool(
            ship.starsystem
            and ship.state in (ShipState.READY, ShipState.ORBIT)
        )

    def cmd_ships(self, player, args, result_cls):
        parked = self.ships_at(player.current_room)
        aboard = self.ship_from_cockpit(player.current_room)
        lines = []
        if aboard:
            lines.append(f"You are inside {aboard.name}.")
            if aboard.is_docked_with() is not None:
                lines.append(
                    f"Docked with {aboard.docked_ship.name}. Type 'board dock' to cross over."
                )
        if parked:
            lines.append("Parked here:")
            for ship in parked:
                if ship.locked:
                    state = "padlocked"
                else:
                    state = "open" if ship.hatch_open else "shut"
                lines.append(f"  {ship.name}  owner:{ship.owner}  door:{state}")
        if not lines:
            return self._result(result_cls, "There's nothing parked here.")
        return self._result(result_cls, "\n".join(lines))

    def _ship_at_hand(self, player, args, result_cls, verb: str):
        """Resolve the camper the player is standing next to, or inside."""
        ship = self.ship_from_cockpit(player.current_room)
        if ship is not None:
            return ship, None
        parked = self.ships_at(player.current_room)
        if args.strip():
            ship = self.ship_named(args)
            if ship is None or ship.location != player.current_room:
                return None, self._result(result_cls, "You don't see that here.")
            return ship, None
        if len(parked) == 1:
            return parked[0], None
        if not parked:
            return None, self._result(result_cls, "There's nothing here to {}.".format(verb))
        return None, self._result(result_cls, "{} which one?".format(verb.capitalize()))

    def _is_public(self, ship: Ship) -> bool:
        return ship.owner == PUBLIC_OWNER

    def _controls_lock(self, player, ship: Ship) -> bool:
        if self._is_public(ship):
            return _has_key(player)
        return ship.owner.lower() == player.name.lower()

    def cmd_unlock(self, player, args, result_cls):
        ship, err = self._ship_at_hand(player, args, result_cls, "unlock")
        if err:
            return err
        if not ship.locked:
            return self._result(result_cls, f"The {ship.name} is already unlocked.")
        if not self._controls_lock(player, ship):
            if self._is_public(ship):
                return self._result(
                    result_cls,
                    "The padlock is rusted but solid. You'd need the key for it.",
                )
            return self._result(result_cls, "You can't unlock someone else's ship.")
        ship.locked = False
        self.save_ships()
        if self._is_public(ship):
            message = (
                f"The key turns stiffly and the padlock springs open.\n"
                f"You slip it off the latch of the {ship.name}."
            )
        else:
            message = f"You unlock {ship.name}."
        return self._result(
            result_cls,
            message,
            [Broadcast(ship.location, f"{player.name} unlocks the {ship.name}.")],
        )

    def cmd_lock(self, player, args, result_cls):
        ship, err = self._ship_at_hand(player, args, result_cls, "lock")
        if err:
            return err
        if ship.locked:
            return self._result(result_cls, f"The {ship.name} is already locked.")
        if not self._controls_lock(player, ship):
            if self._is_public(ship):
                return self._result(result_cls, "You don't have the key for that padlock.")
            return self._result(result_cls, "You can't lock someone else's ship.")
        if ship.state != ShipState.DOCKED:
            return self._result(result_cls, "Not while it's out on the road.")
        if self._players_aboard(ship):
            return self._result(result_cls, "Someone is still inside.")
        ship.hatch_open = False
        ship.locked = True
        self.save_ships()
        return self._result(
            result_cls,
            f"You snap the padlock shut on the {ship.name}.",
            [Broadcast(ship.location, f"{player.name} locks up the {ship.name}.")],
        )

    def cmd_openhatch(self, player, args, result_cls):
        ship, err = self._ship_at_hand(player, args, result_cls, "open")
        if err:
            return err
        if ship.locked:
            return self._result(
                result_cls,
                "It's padlocked shut."
                + (" You have a key that might fit — try UNLOCK AIRSTREAM."
                   if _has_key(player) else ""),
            )
        if ship.state != ShipState.DOCKED:
            return self._result(result_cls, "Please wait till the ship is properly docked.")
        if ship.hatch_open:
            return self._result(result_cls, "The door is already open.")
        if not self._is_public(ship) and ship.owner.lower() != player.name.lower():
            return self._result(result_cls, "You can't open someone else's ship.")
        ship.hatch_open = True
        notes = [
            Broadcast(ship.location, f"The door on the {ship.name} swings open."),
            Broadcast(ship.cockpit_room, "The door swings open."),
        ]
        return self._result(result_cls, f"You open up the {ship.name}.", notes)

    def cmd_closehatch(self, player, args, result_cls):
        ship, err = self._ship_at_hand(player, args, result_cls, "close")
        if err:
            return err
        if not ship.hatch_open:
            return self._result(result_cls, "The door is already shut.")
        ship.hatch_open = False
        notes = [
            Broadcast(ship.location, f"The door on the {ship.name} bangs shut."),
            Broadcast(ship.cockpit_room, "The door bangs shut."),
        ]
        return self._result(result_cls, f"You close up the {ship.name}.", notes)

    def cmd_board(self, player, args, result_cls):
        if args.strip().lower() == "dock":
            return self._board_docked(player, result_cls)
        ship, err = self._ship_at_hand(player, args, result_cls, "board")
        if err:
            return err
        if ship.location != player.current_room or ship.state != ShipState.DOCKED:
            return self._result(result_cls, "You don't see that here.")
        if ship.locked:
            return self._result(result_cls, "It's padlocked shut.")
        if not ship.hatch_open:
            return self._result(result_cls, "The door is shut.")
        cockpit = self.rooms.get(ship.cockpit_room)
        pad = self.rooms.get(player.current_room)
        if not cockpit or not pad:
            return self._result(result_cls, "The ship has no interior.")
        pad.players.discard(player.name)
        player.current_room = ship.cockpit_room
        cockpit.players.add(player.name)
        notes = [
            Broadcast(pad.id, f"{player.name} boards {ship.name}."),
            Broadcast(cockpit.id, f"{player.name} enters the cockpit."),
        ]
        return self._result(
            result_cls,
            f"You enter {ship.name}.\n{cockpit.get_description(current_player=player.name)}",
            notes,
        )

    def _board_docked(self, player, result_cls):
        """SWL 'board dock': cross the airlock into the ship you're locked to."""
        in_ship = self.ship_from_cockpit(player.current_room)
        if in_ship is None:
            if self.ship_from_interior(player.current_room) is not None:
                return self._result(result_cls, "The airlock is in the cockpit. Head north.")
            return self._result(result_cls, "You need to be at the entrance.")
        if in_ship.is_docked_with() is None:
            if in_ship.docking_in_progress():
                return self._result(result_cls, "Wait until docking sequence is complete.")
            return self._result(result_cls, "This ship isn't currently docked.")
        ship = in_ship.docked_ship
        cockpit = self.rooms.get(ship.cockpit_room)
        here = self.rooms.get(player.current_room)
        if not cockpit or not here:
            return self._result(result_cls, "That ship has no entrance!")
        here.players.discard(player.name)
        player.current_room = cockpit.id
        cockpit.players.add(player.name)
        notes = [
            Broadcast(here.id, f"{player.name} enters {ship.name}."),
            Broadcast(cockpit.id, f"{player.name} enters the ship."),
        ]
        return self._result(
            result_cls,
            f"You enter {ship.name}.\n{cockpit.get_description(current_player=player.name)}",
            notes,
        )

    def cmd_docking(self, player, args, result_cls):
        """SWL do_docking: pull alongside another ship and lock hulls."""
        ship, err = self._need_ship(player, result_cls)
        if err:
            return err
        if ship.state == ShipState.DISABLED:
            return self._result(result_cls, "The ship's drive is disabled. Unable to dock.")
        if ship.docked_ship is not None:
            return self._result(result_cls, "The ship is already docked!")
        if ship.state == ShipState.HYPERSPACE:
            return self._result(result_cls, "You can only do that in realspace!")
        if not self._can_maneuver(ship):
            return self._result(result_cls, "Please wait until the ship has finished its current maneuver.")
        if ship.starsystem is None:
            return self._result(result_cls, "Dock with whom?")
        if not args.strip():
            return self._result(result_cls, "Dock with who?")
        target = None
        for candidate in ship.starsystem.ships:
            if candidate is not ship and candidate.matches(args):
                target = candidate
                break
        if target is None:
            if ship.matches(args):
                return self._result(result_cls, "You can't dock your ship inside itself!")
            return self._result(result_cls, "That ship isn't here!")
        if target.docked_ship is not None:
            return self._result(result_cls, "That ship is already docked!")
        if target.state in (ShipState.LAND, ShipState.LAND_2, ShipState.HYPERSPACE):
            return self._result(result_cls, "That ship isn't here!")
        if (
            abs(target.vx - ship.vx) > DOCK_RANGE
            or abs(target.vy - ship.vy) > DOCK_RANGE
            or abs(target.vz - ship.vz) > DOCK_RANGE
        ):
            return self._result(
                result_cls,
                "That ship is too far away! You'll have to fly a little closer.",
            )
        ship.dock_state = DockState.DOCK
        ship.currspeed = 0
        target.dock_state = DockState.DOCK
        target.currspeed = 0
        ship.docked_ship = target
        target.docked_ship = ship
        notes: List[Broadcast] = []
        self._echo_ship(
            target,
            f"You are being docked by {ship.name}.\nDocking sequence initiated.",
            notes,
        )
        self._echo_cockpit(ship, f"{player.name} begins the docking sequence.", notes)
        self._echo_ship(ship, "The ship slowly begins its docking approach.", notes)
        return self._result(result_cls, "Docking sequence initiated.", notes)

    def cmd_detach(self, player, args, result_cls):
        """SWL do_detach: open the airlocks and let the other hull go."""
        ship = self.ship_from_cockpit(player.current_room)
        if ship is None:
            return self._result(result_cls, "You don't seem to be in the pilot seat!")
        if ship.automated:
            return self._result(
                result_cls,
                "The autopilot won't release the lock from this side. "
                "Get back to your own ship and detach.",
            )
        other = ship.docked_ship
        if other is None:
            return self._result(result_cls, "There doesn't seem to be any ships docked to you.")
        self._clear_dock(ship)
        notes: List[Broadcast] = []
        self._echo_ship(
            ship,
            f"You hear air escaping as the airlocks open, and {other.name} detaches.",
            notes,
        )
        self._echo_ship(
            other,
            f"You hear air escaping as the airlocks open, and {ship.name} detaches.",
            notes,
        )
        return self._result(result_cls, f"You detach from {other.name}.", notes)

    def cmd_leaveship(self, player, args, result_cls):
        ship = self.ship_from_cockpit(player.current_room)
        if ship is None:
            aboard = self.ship_from_interior(player.current_room)
            if aboard is not None:
                return self._result(result_cls, "The hatch is in the cockpit. Head north.")
            return self._result(result_cls, "I see no exit here.")
        if ship.state != ShipState.DOCKED:
            return self._result(result_cls, "Please wait till the ship is properly docked.")
        if not ship.hatch_open:
            return self._result(result_cls, "You need to open the door first.")
        pad = self.rooms.get(ship.location)
        cockpit = self.rooms.get(ship.cockpit_room)
        if not pad or not cockpit:
            return self._result(result_cls, "The landing pad is missing.")
        cockpit.players.discard(player.name)
        player.current_room = pad.id
        pad.players.add(player.name)
        notes = [
            Broadcast(cockpit.id, f"{player.name} exits the ship."),
            Broadcast(pad.id, f"{player.name} leaves {ship.name}."),
        ]
        extra = self.describe_ships_here(pad.id)
        return self._result(
            result_cls,
            f"You exit {ship.name}.\n{pad.get_description(current_player=player.name)}{extra}",
            notes,
        )

    def cmd_return(self, player, args, result_cls):
        """Send the empty rental back to the garage so the next pilot can take it."""
        ship = self.ship_from_interior(player.current_room)
        aboard = ship is not None
        if ship is not None and not self._is_public(ship):
            return self._result(
                result_cls,
                "Only the rental goes back to the garage. Your ship stays where you leave it.",
            )
        if ship is None:
            parked = [s for s in self.ships_at(player.current_room) if self._is_public(s)]
            ship = parked[0] if parked else None
        if ship is None:
            return self._result(result_cls, "The rental ship isn't here.")
        if ship.state != ShipState.DOCKED:
            return self._result(
                result_cls, "Land it first, then send it back to the garage."
            )
        if ship.location == GARAGE_ROOM_ID:
            return self._result(
                result_cls, f"The {ship.name} is already at the garage."
            )
        others = [name for name in self._players_aboard(ship) if name != player.name]
        if others:
            return self._result(
                result_cls, "Someone is still aboard. They need to step out first."
            )

        notes: List[Broadcast] = []
        stepped_out = ""
        if aboard:
            pad = self.rooms.get(ship.location)
            if pad is None:
                return self._result(result_cls, "The landing pad is missing.")
            room = self.rooms.get(player.current_room)
            if room is not None:
                room.players.discard(player.name)
            player.current_room = pad.id
            pad.players.add(player.name)
            stepped_out = "You step out. "
        self._park_rental_at_garage(ship, notes)
        return self._result(
            result_cls,
            stepped_out
            + f"The {ship.name} lifts off by itself and heads back to the garage.",
            notes,
        )

    def _park_rental_at_garage(self, ship: Ship, notes: List[Broadcast]) -> None:
        origin = ship.location
        self._leave_system(ship)
        ship.orbit = None
        ship.state = ShipState.DOCKED
        ship.location = GARAGE_ROOM_ID
        ship.lastdoc = GARAGE_ROOM_ID
        ship.hatch_open = False
        ship.locked = True
        ship.currspeed = 0
        ship.autopilot = False
        ship.target = None
        ship.dest = ""
        ship.currjump = None
        ship.hyperdistance = 0
        ship.hull = ship.maxhull
        ship.shield = 0
        ship.missiles = ship.maxmissiles
        ship.laser_cool = 0
        self.save_ships()
        if origin and origin != GARAGE_ROOM_ID:
            notes.append(Broadcast(
                origin,
                f"The {ship.name} lifts off by itself and heads back to the garage.",
            ))
        notes.append(Broadcast(
            GARAGE_ROOM_ID,
            f"The {ship.name} sets down. The rental is ready for another pilot.",
        ))

    def cmd_launch(self, player, args, result_cls):
        ship, err = self._need_ship(player, result_cls)
        if err:
            return err
        if ship.state not in (ShipState.DOCKED, ShipState.DISABLED):
            return self._result(result_cls, "The ship is not docked right now.")
        if self._is_public(ship):
            if player.gold < RENT_GOLD:
                return self._result(
                    result_cls,
                    f"You can't afford to rent this ship, it costs {RENT_GOLD} gold.",
                )
            player.gold -= RENT_GOLD
            rent_line = f"You pay {RENT_GOLD} gold to rent the ship.\n"
        elif ship.owner.lower() != player.name.lower():
            return self._result(result_cls, "You can't launch someone else's ship.")
        else:
            rent_line = ""
        notes = []
        if ship.hatch_open:
            ship.hatch_open = False
            self._echo_pad(ship.location, f"The door on the {ship.name} bangs shut.", notes)
            self._echo_cockpit(ship, "The door bangs shut.", notes)
        ship.lastdoc = ship.location
        ship.state = ShipState.LAUNCH
        ship.currspeed = ship.realspeed
        self.save_ships()
        self._echo_pad(ship.location, f"{ship.name} begins to launch.", notes)
        self._echo_cockpit(ship, "The ship hums as it lifts off the ground.", notes)
        return self._result(
            result_cls,
            rent_line + "Launch sequence initiated.",
            notes,
        )

    def cmd_land(self, player, args, result_cls):
        ship, err = self._need_ship(player, result_cls)
        if err:
            return err
        if ship.state == ShipState.HYPERSPACE:
            return self._result(result_cls, "You can only do that in realspace!")
        if ship.docking_in_progress():
            return self._result(result_cls, "Wait until after docking procedures are complete.")
        if ship.is_docked_with() is not None:
            return self._result(result_cls, "Detach from the docked ship first.")
        if not self._can_maneuver(ship):
            return self._result(result_cls, "Please wait until the ship has finished its current maneuver.")
        if not args.strip():
            lines = ["Land where?", "", "Choices:"]
            for pad in ship.starsystem.pads():
                dist = _distance(ship.vx, ship.vy, ship.vz, pad.x, pad.y, pad.z)
                lines.append(f"  {pad.name}  ({pad.x} {pad.y} {pad.z})  range {dist:.0f}")
            return self._result(result_cls, "\n".join(lines))
        pad = ship.starsystem.pad_named(args)
        if pad is None:
            return self._result(result_cls, "I don't see that here. Type land by itself for a list.")
        dist = _distance(ship.vx, ship.vy, ship.vz, pad.x, pad.y, pad.z)
        if dist > LAND_RANGE:
            return self._result(
                result_cls,
                f"You're too far away. Get within {LAND_RANGE} (currently {dist:.0f}).",
            )
        ship.dest = pad.name
        ship.state = ShipState.LAND
        ship.currspeed = 0
        return self._result(result_cls, f"Landing sequence initiated for {pad.name}.")

    def cmd_status(self, player, args, result_cls):
        ship, err = self._need_ship(player, result_cls, seat="look")
        if err:
            return err
        sysname = ship.starsystem.name if ship.starsystem else "(docked)"
        target = ship.target.name if ship.target else "none"
        if ship.orbit:
            sysname = f"{sysname}  Orbit: {ship.orbit}"
        lines = [
            f"{ship.name}:",
            f"System: {sysname}   State: {ship.state.value}",
            f"Current Coordinates: {ship.vx:.0f} {ship.vy:.0f} {ship.vz:.0f}",
            f"Current Heading: {ship.hx:.0f} {ship.hy:.0f} {ship.hz:.0f}",
            f"Current Speed: {ship.currspeed}/{ship.realspeed}",
            f"Hull: {ship.hull}/{ship.maxhull}   Condition: {ship.state.value}",
            f"Shields: {ship.shield}/{ship.maxshield}",
            f"Lasers: {ship.lasers}   Missiles: {ship.missiles}/{ship.maxmissiles}",
            f"Current Target: {target}",
            f"Autopilot: {'on' if ship.autopilot else 'off'}   Door: {'open' if ship.hatch_open else 'shut'}",
        ]
        if ship.is_docked_with() is not None:
            lines.append(f"Docked with: {ship.docked_ship.name}")
        elif ship.docking_in_progress() and ship.docked_ship is not None:
            lines.append(f"Docking with: {ship.docked_ship.name} (in progress)")
        if ship.is_skipjack():
            lines.append(
                f"Hyperdrive: {ship.hyperspeed}   Cargo: "
                f"{ship.cargo_weight():.1f}/{ship.cargo_capacity} lbs"
            )
            lines.extend(self._module_lines(ship))
        return self._result(result_cls, "\n".join(lines))

    def cmd_radar(self, player, args, result_cls):
        ship, err = self._need_ship(player, result_cls, seat="look")
        if err:
            return err
        if ship.state == ShipState.DOCKED:
            return self._result(result_cls, "Wait until after you launch!")
        if ship.state == ShipState.HYPERSPACE:
            return self._result(result_cls, "You can only do that in realspace!")
        if not ship.starsystem:
            return self._result(result_cls, "You can't do that until you've finished launching!")
        lines = [
            f"{ship.starsystem.name} — radar",
            f"  YOU {ship.name:20} {ship.vx:6.0f} {ship.vy:6.0f} {ship.vz:6.0f}  "
            f"speed {ship.currspeed}"
            + (f"  orbit {ship.orbit}" if ship.orbit else ""),
        ]
        for star in ship.starsystem.stars:
            dist = _distance(ship.vx, ship.vy, ship.vz, star.x, star.y, star.z)
            lines.append(f"  STAR {star.name:20} {star.x:6} {star.y:6} {star.z:6}  range {dist:.0f}")
        for planet in ship.starsystem.planets:
            dist = _distance(ship.vx, ship.vy, ship.vz, planet.x, planet.y, planet.z)
            lines.append(
                f"  {planet.label:8}{planet.name:17} {planet.x:6} {planet.y:6} {planet.z:6}  "
                f"range {dist:.0f}"
            )
        for other in ship.starsystem.ships:
            if other is ship:
                continue
            dist = _distance(ship.vx, ship.vy, ship.vz, other.vx, other.vy, other.vz)
            tag = ""
            if other.automated:
                tag = "  autopilot"
            elif other.is_docked_with() is not None:
                tag = f"  docked with {other.docked_ship.name}"
            lines.append(
                f"  SHIP {other.name:20} {other.vx:6.0f} {other.vy:6.0f} {other.vz:6.0f}  range {dist:.0f}{tag}"
            )
        return self._result(result_cls, "\n".join(lines))

    def cmd_trajectory(self, player, args, result_cls):
        ship, err = self._need_ship(player, result_cls)
        if err:
            return err
        if ship.state == ShipState.HYPERSPACE:
            return self._result(result_cls, "You can only do that in realspace!")
        blocked = self._dock_block(ship)
        if blocked:
            return self._result(result_cls, blocked)
        if not self._can_maneuver(ship):
            return self._result(result_cls, "Please wait until the ship has finished its current maneuver.")
        parts = args.split()
        if len(parts) < 3:
            return self._result(result_cls, "Set a course: trajectory <x> <y> <z>")
        try:
            tx, ty, tz = float(parts[0]), float(parts[1]), float(parts[2])
        except ValueError:
            return self._result(result_cls, "Those coordinates don't make sense.")
        ship.hx = tx - ship.vx
        ship.hy = ty - ship.vy
        ship.hz = tz - ship.vz
        ship.state = ShipState.BUSY
        return self._result(
            result_cls,
            f"New course set toward {tx:.0f} {ty:.0f} {tz:.0f}.",
        )

    def cmd_accelerate(self, player, args, result_cls):
        ship, err = self._need_ship(player, result_cls)
        if err:
            return err
        if ship.state == ShipState.HYPERSPACE:
            return self._result(result_cls, "You can only do that in realspace!")
        blocked = self._dock_block(ship)
        if blocked:
            return self._result(result_cls, blocked)
        if not self._can_maneuver(ship):
            return self._result(result_cls, "Please wait until the ship has finished its current maneuver.")
        if not args.strip():
            return self._result(result_cls, "Accelerate to what speed?")
        try:
            speed = int(args.split()[0])
        except ValueError:
            return self._result(result_cls, "Speed has to be a number.")
        speed = max(0, min(ship.realspeed, speed))
        ship.currspeed = speed
        ship.state = ShipState.BUSY
        return self._result(result_cls, f"Speed set to {speed}.")

    def cmd_calculate(self, player, args, result_cls):
        ship, err = self._need_ship(player, result_cls)
        if err:
            return err
        if ship.state == ShipState.DOCKED:
            return self._result(result_cls, "You can only do that in realspace.")
        if not ship.starsystem:
            return self._result(result_cls, "You can only do that in realspace.")
        if ship.state == ShipState.HYPERSPACE:
            return self._result(result_cls, "You can only do that in realspace!")
        blocked = self._dock_block(ship)
        if blocked:
            return self._result(result_cls, blocked)
        if not self._can_maneuver(ship):
            return self._result(result_cls, "Please wait until the ship has finished its current maneuver.")
        parts = args.split()
        if not parts:
            lines = ["Format: Calculate <starsystem> <entry x> <entry y> <entry z>", "Possible destinations:"]
            for system in self.systems:
                dist = _galaxy_distance(ship.starsystem, system)
                lines.append(f"  {system.name:20} jump-distance {dist}")
            return self._result(result_cls, "\n".join(lines))
        hits = self.systems_matching(parts[0])
        if not hits:
            return self._result(result_cls, "No such starsystem.")
        if len(hits) > 1:
            names = ", ".join(system.name for system in hits)
            return self._result(result_cls, f"Which system? {names}.")
        dest = hits[0]
        coords = parts[1:4]
        if len(coords) < 3:
            return self._result(result_cls, "Format: Calculate <starsystem> <entry x> <entry y> <entry z>")
        try:
            jx, jy, jz = float(coords[0]), float(coords[1]), float(coords[2])
        except ValueError:
            return self._result(result_cls, "Those coordinates don't make sense.")
        ship.currjump = dest
        ship.jumpx, ship.jumpy, ship.jumpz = jx, jy, jz
        ship.hyperdistance = max(1, _galaxy_distance(ship.starsystem, dest))
        return self._result(
            result_cls,
            f"Hyperspace course plotted to {dest.name} "
            f"({jx:.0f} {jy:.0f} {jz:.0f}), distance {ship.hyperdistance}.",
        )

    def _player_system(self, player) -> Optional[StarSystem]:
        """The star system the player is standing in, or None in hyperspace."""
        ship = self.ship_from_interior(player.current_room)
        if ship is not None:
            if ship.starsystem is not None:
                return ship.starsystem
            if ship.state == ShipState.HYPERSPACE:
                return None
            return self.system_for_pad(ship.location or ship.lastdoc)
        return self.system_for_pad(player.current_room)

    def cmd_map(self, player, args, result_cls):
        here = self._player_system(player)
        return self._result(result_cls, render_star_map(self.systems, here))

    def cmd_hyperspace(self, player, args, result_cls):
        ship, err = self._need_ship(player, result_cls)
        if err:
            return err
        blocked = self._dock_block(ship)
        if blocked:
            return self._result(result_cls, blocked)
        if not self._can_maneuver(ship):
            return self._result(result_cls, "Please wait until the ship has finished its current maneuver.")
        if ship.currjump is None or ship.hyperdistance <= 0:
            return self._result(result_cls, "You need to calculate a jump first.")
        ship.orbit = None
        ship.state = ShipState.HYPERSPACE
        ship.currspeed = 0
        self._leave_system(ship)
        return self._result(
            result_cls,
            "You push forward on the hyperspace lever. Stars smear into lines.",
        )

    def cmd_target(self, player, args, result_cls):
        ship, err = self._need_ship(player, result_cls)
        if err:
            return err
        if not ship.starsystem or ship.state == ShipState.HYPERSPACE:
            return self._result(result_cls, "You can only do that in realspace!")
        if not args.strip():
            ship.target = None
            return self._result(result_cls, "Target cleared.")
        other = None
        for candidate in ship.starsystem.ships:
            if candidate is not ship and candidate.matches(args):
                other = candidate
                break
        if other is None:
            return self._result(result_cls, "That ship is not on your sensors.")
        ship.target = other
        return self._result(result_cls, f"Target locked: {other.name}.")

    def cmd_fire(self, player, args, result_cls):
        ship, err = self._need_ship(player, result_cls)
        if err:
            return err
        if not ship.starsystem or ship.state == ShipState.HYPERSPACE:
            return self._result(result_cls, "You can only do that in realspace!")
        if ship.target is None:
            return self._result(result_cls, "You need to choose a target first.")
        target = ship.target
        if target.starsystem is not ship.starsystem:
            ship.target = None
            return self._result(result_cls, "Your target seems to have left.")
        if target.automated:
            return self._result(
                result_cls,
                f"{target.name} doesn't answer your guns. Its course never wavers.",
            )
        weapon = (args or "lasers").split()[0].lower()
        dist = _distance(ship.vx, ship.vy, ship.vz, target.vx, target.vy, target.vz)
        notes: List[Broadcast] = []
        if weapon.startswith("las"):
            if ship.laser_cool > 0:
                return self._result(result_cls, "The lasers are still recharging.")
            if dist > 1000:
                return self._result(result_cls, "That ship is out of laser range.")
            if not _facing(ship, target):
                return self._result(
                    result_cls,
                    "The main laser can only fire forward. You'll need to turn your ship!",
                )
            dmg = random.randint(8, 18) * ship.lasers
            ship.laser_cool = 1
        elif weapon.startswith("mis"):
            if ship.missiles <= 0:
                return self._result(result_cls, "You have no missiles left.")
            if dist > 1200:
                return self._result(result_cls, "That ship is out of missile range.")
            ship.missiles -= 1
            dmg = random.randint(30, 50)
        else:
            return self._result(result_cls, "Fire what? lasers or missiles")
        absorbed = min(target.shield, dmg)
        target.shield -= absorbed
        hull_hit = dmg - absorbed
        target.hull = max(0, target.hull - hull_hit)
        self._echo_cockpit(target, f"The ship is hit by {ship.name}!", notes)
        msg = f"Your shot hits {target.name} for {dmg} ({absorbed} absorbed by shields)."
        if target.hull <= 0:
            msg += f"\n{target.name} breaks apart."
            self._destroy_ship(target, notes)
            ship.target = None
        return self._result(result_cls, msg, notes)

    def _destroy_ship(self, ship: Ship, notes: List[Broadcast]) -> None:
        garage = self.rooms.get(GARAGE_ROOM_ID)
        for room_id in ship.interior_rooms():
            room = self.rooms.get(room_id)
            if not room or not garage:
                continue
            for name in list(room.players):
                room.players.discard(name)
                garage.players.add(name)
                self.eject_queue.append((name, garage.id))
                notes.append(Broadcast(
                    garage.id,
                    f"{name} is thrown clear as {ship.name} comes apart, "
                    f"and wakes up back in the garage.",
                ))
        if ship.docked_ship is not None:
            self._echo_ship(
                ship.docked_ship,
                f"{ship.name} tears free of the airlock as it comes apart.",
                notes,
            )
            self._clear_dock(ship)
        self._leave_system(ship)
        ship.orbit = None
        ship.state = ShipState.DOCKED
        ship.location = GARAGE_ROOM_ID
        ship.lastdoc = GARAGE_ROOM_ID
        ship.hull = ship.maxhull
        ship.shield = 0
        ship.currspeed = 0
        ship.target = None
        ship.hatch_open = False
        self.save_ships()

    def cmd_recharge(self, player, args, result_cls):
        ship, err = self._need_ship(player, result_cls)
        if err:
            return err
        if ship.shield >= ship.maxshield:
            return self._result(result_cls, "Shields are already at maximum.")
        amount = min(50, ship.maxshield - ship.shield)
        ship.shield += amount
        return self._result(result_cls, f"Shields charged +{amount} ({ship.shield}/{ship.maxshield}).")

    def cmd_autopilot(self, player, args, result_cls):
        ship = self.ship_from_cockpit(player.current_room)
        if ship is None:
            return self._result(result_cls, "You must be in the cockpit of a ship to do that!")
        ship.autopilot = not ship.autopilot
        state = "engaged" if ship.autopilot else "disengaged"
        return self._result(result_cls, f"Autopilot {state}.")

    def _refresh_interior_names(self, ship: Ship) -> None:
        cockpit = self.rooms.get(ship.cockpit_room)
        if cockpit is not None:
            cockpit.name = f"Inside {ship.name}"
        hold = self.rooms.get(ship.hold_room) if ship.hold_room else None
        if hold is not None:
            hold.name = f"Cargo Hold of {ship.name}"

    def _ship_name_taken(self, ship: Ship, new_name: str) -> bool:
        return any(
            other is not ship and _ship_names_clash(other.name, new_name)
            for other in self.ships
        )

    def cmd_rename(self, player, args, result_cls):
        """Rename the owner's ship while standing on the pad it is parked at."""
        room_id = player.current_room
        on_pad = room_id == GARAGE_ROOM_ID or room_id in REMOTE_PAD_IDS
        if not on_pad:
            return self._result(
                result_cls,
                "You can rename your ship from the pad where it's parked.",
            )
        owned = self.owned_ship_for(player.name)
        if owned is None:
            return self._result(result_cls, "You don't have a ship to rename.")
        if owned.location != room_id or owned.state != ShipState.DOCKED:
            return self._result(
                result_cls, "Your ship isn't parked on this pad."
            )
        text = " ".join(args.split())
        if text.lower().startswith("ship "):
            text = text.split(maxsplit=1)[1]
        new_name = _clean_ship_name(text)
        if new_name is None:
            return self._result(
                result_cls,
                "Rename it to what? Letters, numbers, spaces, apostrophes, "
                "or hyphens, up to 32 characters.",
            )
        if new_name.lower() == owned.name.lower():
            return self._result(
                result_cls, f"It's already called {owned.name}."
            )
        if self._ship_name_taken(owned, new_name):
            return self._result(
                result_cls,
                "Another ship already goes by something too close to that.",
            )
        old_name = owned.name
        owned.name = new_name
        self._refresh_interior_names(owned)
        self.save_ships()
        return self._result(
            result_cls,
            f"You rename {old_name} to {new_name}.",
            [Broadcast(room_id, f"{player.name} renames {old_name} to {new_name}.")],
        )

    # -- pad dealer: Skipjack hulls, and Dell's modules at Beta Forge ---------

    def is_dealer_pad(self, room_id: str) -> bool:
        return room_id in REMOTE_PAD_IDS

    def _module_lines(self, ship: Ship) -> List[str]:
        from items import MODULE_ITEMS, MODULE_SLOTS, MODULE_SLOT_LABELS, module_price

        lines = ["Modules:"]
        for slot in MODULE_SLOTS:
            installed = ship.modules.get(slot)
            if installed:
                name = MODULE_ITEMS[installed].name
                value = f"{module_price(installed)}g"
            else:
                name = "stock"
                value = "not for sale"
            unit = "lbs" if slot == "cargo" else ""
            lines.append(
                f"  {MODULE_SLOT_LABELS[slot]:<26} {name:<24} "
                f"{ship.stat_for(slot)}{unit}  ({value})"
            )
        return lines

    def pad_listing(self, player, room_id: str) -> str:
        from items import MODULE_ITEMS, MODULE_SLOT_LABELS, module_slot

        lines = ["", "=" * 50, "  LANDING PAD - SHIP DEALER", "=" * 50, ""]
        owned = self.owned_ship_for(player.name)
        lines.append(
            f"SKIPJACK HULL ............ {SKIPJACK_BASE_PRICE}g   (one per pilot)"
        )
        lines.append(
            f"  Two rooms: cockpit and cargo hold. Stock engine {SKIPJACK_STOCK['engine']}, "
            f"hyperdrive {SKIPJACK_STOCK['hyper']}, hold {SKIPJACK_STOCK['cargo']} lbs."
        )
        if owned is None:
            lines.append("  Type 'buy ship' to take one home. You'll name it on the spot.")
        else:
            here = owned.location == room_id and owned.state == ShipState.DOCKED
            lines.append(
                f"  You own {owned.name}. Sell price here: {owned.sale_price()}g "
                f"(hull {SKIPJACK_BASE_PRICE} + modules {owned.module_value()})."
            )
            if here:
                lines.append("  Type 'sell ship' with an empty hold and nobody aboard.")
                lines.append("  Type 'rename <name>' to give it a new name.")
            else:
                lines.append("  It has to be parked on this pad to sell it.")
        if room_id == DELL_PAD_ID:
            lines.append("")
            lines.append("DELL'S MODULES (buy at list, she pays half on the way back):")
            for item_id, item in MODULE_ITEMS.items():
                slot = module_slot(item)
                lines.append(
                    f"  {item.name:<24} {item.value:>6}g   {MODULE_SLOT_LABELS[slot]}"
                )
            lines.append("  Stock parts are never for sale. Install what you buy in your hold.")
        buyer = _pad_entry(room_id).get("buyer")
        if buyer:
            wanted = _fish_named(buyer_species_id(buyer))
            species_name = wanted.name if wanted else buyer_species_id(buyer)
            lines.append("")
            lines.append(
                f"{buyer['name'].upper()} is buying {species_name} "
                f"for the next {_format_remaining(cycle_remaining())}."
            )
            lines.append(
                f"  They buy every {species_name} in the hold and leave the rest. "
                f"Pays {CARGO_BUY_MULTIPLIER}× listed value. Type 'sell cargo'."
            )
        lines.append("")
        lines.append(f"Your gold: {player.gold}")
        return "\n".join(lines)

    @staticmethod
    def _names_ship(query: str) -> bool:
        return query.strip().lower() in {
            "ship", "skipjack", "hull", "a ship", "the ship", "my ship",
        }

    def pad_buy(self, player, room_id: str, query: str, result_cls):
        from items import MODULE_ITEMS, create_module

        query = (query or "").strip().lower()
        if not query:
            return self._result(result_cls, "Buy what? Type 'list' to see terms.")
        if self._names_ship(query):
            if self.owned_ship_for(player.name):
                return self._result(
                    result_cls, "You already own a ship. Sell it first."
                )
            if player.gold < SKIPJACK_BASE_PRICE:
                return self._result(
                    result_cls,
                    f"A Skipjack runs {SKIPJACK_BASE_PRICE} gold. You're short.",
                )
            if not self.can_create_owned_ship_at(room_id):
                return self._result(result_cls, "Nobody sells hulls here.")
            player.ship_naming_pad = room_id
            return self._result(
                result_cls,
                f"A Skipjack runs {SKIPJACK_BASE_PRICE} gold. The dealer slides "
                "the paperwork over and taps the blank line.\n"
                "What do you want to call it? Type the name, or 'cancel'.",
            )
        if room_id != DELL_PAD_ID:
            return self._result(
                result_cls, "Only hulls change hands here. Dell at the Forge sells modules."
            )
        match = next(
            (item for item in MODULE_ITEMS.values()
             if query == item.id or query in item.name.lower()),
            None,
        )
        if match is None:
            return self._result(result_cls, 'Dell shrugs. "Don\'t stock that. Try LIST."')
        if player.gold < match.value:
            return self._result(
                result_cls, f'Dell shakes her head. "That\'s {match.value} gold. Come back heavier."'
            )
        player.gold -= match.value
        player.add_item(create_module(match.id))
        return self._result(
            result_cls,
            f"Dell slides you a {match.name} for {match.value} gold. "
            f"\"Install it yourself, in the hold.\" You have {player.gold} gold left.",
        )

    def awaiting_ship_name(self, player) -> bool:
        """True while a hull purchase is waiting for the player to name it."""
        pad = getattr(player, "ship_naming_pad", None)
        if not pad:
            return False
        if player.current_room != pad:
            player.ship_naming_pad = None
            return False
        return True

    def name_new_ship(self, player, raw_name: str, result_cls):
        """Finish a pending hull purchase with the name the player typed."""
        room_id = player.ship_naming_pad
        text = " ".join((raw_name or "").split())
        if text.lower() in {"cancel", "never mind", "nevermind", "no"}:
            player.ship_naming_pad = None
            return self._result(
                result_cls, "You slide the paperwork back. No sale."
            )
        new_name = _clean_ship_name(text)
        if new_name is None:
            return self._result(
                result_cls,
                "That won't fit on the registry. Letters, numbers, spaces, "
                "apostrophes, or hyphens, up to 32 characters. Or 'cancel'.",
            )
        if any(_ship_names_clash(other.name, new_name) for other in self.ships):
            return self._result(
                result_cls,
                "Another ship already goes by something too close to that. "
                "Try another name, or 'cancel'.",
            )
        if self.owned_ship_for(player.name):
            player.ship_naming_pad = None
            return self._result(
                result_cls, "You already own a ship. Sell it first."
            )
        if player.gold < SKIPJACK_BASE_PRICE:
            player.ship_naming_pad = None
            return self._result(
                result_cls,
                f"A Skipjack runs {SKIPJACK_BASE_PRICE} gold. You're short.",
            )
        ship = self.create_owned_ship(player, room_id, name=new_name)
        player.ship_naming_pad = None
        if ship is None:
            return self._result(result_cls, "Nobody sells hulls here.")
        player.gold -= SKIPJACK_BASE_PRICE
        return self._result(
            result_cls,
            f"You sign for a Skipjack. {ship.name} is rolled onto the pad, "
            f"padlocked, keys in your hand.\nYou have {player.gold} gold left.\n"
            "UNLOCK it, OPEN it, and BOARD.",
            [Broadcast(room_id, f"{player.name} takes delivery of {ship.name}.")],
        )

    def pad_sell(self, player, room_id: str, query: str, result_cls):
        from items import ItemType, MODULE_ITEMS, module_price

        query = (query or "").strip().lower()
        if not query:
            return self._result(result_cls, self.pad_listing(player, room_id))
        if query in CARGO_WORDS or query == "fish":
            return self.sell_cargo(player, room_id, result_cls)
        if self._names_ship(query):
            ship = self.owned_ship_for(player.name)
            if ship is None:
                return self._result(result_cls, "You don't own a ship to sell.")
            if ship.state != ShipState.DOCKED or ship.location != room_id:
                return self._result(result_cls, "Your ship isn't parked on this pad.")
            if ship.cargo:
                return self._result(
                    result_cls,
                    f"Empty the hold first ({ship.cargo_weight():.1f} lbs still stowed).",
                )
            if self._players_aboard(ship):
                return self._result(result_cls, "Someone is still aboard.")
            price = ship.sale_price()
            name = ship.name
            self.remove_ship(ship)
            player.gold += price
            player.total_gold_earned += price
            return self._result(
                result_cls,
                f"You sign {name} over for {price} gold "
                f"(hull {SKIPJACK_BASE_PRICE} + modules {price - SKIPJACK_BASE_PRICE}).\n"
                f"You have {player.gold} gold.",
                [Broadcast(room_id, f"{name} is towed off the pad.")],
            )
        if room_id != DELL_PAD_ID:
            return self._result(
                result_cls, "Only hulls change hands here. Dell at the Forge buys modules."
            )
        item = player.find_item(query)
        if item is None or item.item_type != ItemType.MODULE:
            return self._result(result_cls, 'Dell squints. "I only buy modules."')
        if player.is_wearing_or_equipped(item):
            return self._result(result_cls, "Unequip that first.")
        price = max(1, int(module_price(item.id) * MODULE_BUYBACK))
        player.remove_item(item)
        player.gold += price
        player.total_gold_earned += price
        return self._result(
            result_cls,
            f"Dell looks the {item.name} over and pays {price} gold. "
            f"You have {player.gold} gold.",
        )

    # -- cargo hold ----------------------------------------------------------

    def sell_cargo(self, player, room_id: str, result_cls):
        """Sell every fish of the buyer's species. Leave the rest in the hold."""
        from items import is_ancient_fish_id

        try:
            buyer = _pad_entry(room_id).get("buyer")
        except KeyError:
            buyer = None
        if not buyer:
            return self._result(result_cls, "Nobody here buys fish out of a hold.")
        ship, err = self.cargo_ship_for(player)
        if err:
            return self._result(result_cls, err)
        if ship.location != room_id or ship.state != ShipState.DOCKED:
            return self._result(result_cls, "Your ship isn't parked on this pad.")
        if not ship.cargo:
            return self._result(result_cls, "The hold is empty.")
        wanted_id = buyer_species_id(buyer)
        wanted = _fish_named(wanted_id)
        species_name = wanted.name if wanted else wanted_id
        selling = [
            item for item in ship.cargo
            if item.id == wanted_id and not is_ancient_fish_id(item.id)
        ]
        if not selling:
            return self._result(
                result_cls,
                f"{buyer['name']} doesn't see any {species_name} in the hold.",
            )
        total = sum(
            max(1, int(item.value or 0)) * CARGO_BUY_MULTIPLIER
            for item in selling
        )
        count = len(selling)
        ship.cargo = [
            item for item in ship.cargo
            if item.id != wanted_id or is_ancient_fish_id(item.id)
        ]
        player.gold += total
        player.total_gold_earned += total
        self.save_ships()
        left = ""
        if ship.cargo:
            left = f" {len(ship.cargo)} other fish stay in the hold."
        return self._result(
            result_cls,
            f"{buyer['name']} takes {count} {species_name} from the hold "
            f"and pays {total} gold.{left} You have {player.gold} gold.",
            [Broadcast(room_id, f"{buyer['name']} buys {species_name} from a hold.")],
        )

    def cargo_ship_for(self, player) -> Tuple[Optional[Ship], Optional[str]]:
        """
        The owner's ship whose hold is reachable from where they stand:
        inside it, or on the pad it's docked at.
        """
        aboard = self.ship_from_interior(player.current_room)
        if aboard is not None:
            if self._is_public(aboard) or not aboard.is_skipjack():
                return None, "Rental ships don't have a cargo hold."
            if aboard.owner.lower() != player.name.lower():
                return None, "That's not your hold to rummage in."
            return aboard, None
        ship = self.owned_ship_for(player.name)
        if ship is None:
            return None, "You don't own a ship."
        if ship.state != ShipState.DOCKED or ship.location != player.current_room:
            return None, "Your ship isn't parked here."
        return ship, None

    def cargo_display(self, ship: Ship) -> str:
        lines = [
            "",
            f"  CARGO HOLD - {ship.name.upper()}",
            "=" * 40,
            f"  {ship.cargo_weight():.1f}/{ship.cargo_capacity} lbs of fish",
        ]
        if not ship.cargo:
            lines.append("  (empty)")
            return "\n".join(lines)
        for index, item in enumerate(ship.cargo, start=1):
            lines.append(f"  {index}) {item.display_name}  {float(item.weight or 0):.1f} lb")
        return "\n".join(lines)

    def find_cargo_item(self, ship: Ship, query: str):
        query = query.strip().lower().rstrip(")")
        if query.isdigit():
            index = int(query) - 1
            if 0 <= index < len(ship.cargo):
                return ship.cargo[index]
            return None
        for item in ship.cargo:
            if item.matches(query):
                return item
        return None

    def cmd_cargo(self, player, args, result_cls):
        ship, err = self.cargo_ship_for(player)
        if err:
            return self._result(result_cls, err)
        return self._result(result_cls, self.cargo_display(ship))

    def cargo_refusal(self, item) -> Optional[str]:
        from items import ItemType, is_ancient_fish_id

        if item.item_type != ItemType.FISH:
            return "Only fish go in the hold."
        if is_ancient_fish_id(item.id):
            return "That fish will not stay in the hold. It wants the water."
        return None

    def cargo_put(self, player, item, result_cls):
        ship, err = self.cargo_ship_for(player)
        if err:
            return self._result(result_cls, err)
        if player.is_wearing_or_equipped(item):
            return self._result(result_cls, "You should unequip that before storing it.")
        refusal = self.cargo_refusal(item)
        if refusal:
            return self._result(result_cls, refusal)
        weight = float(item.weight or 0.0)
        if ship.cargo_weight() + weight > ship.cargo_capacity + 1e-9:
            return self._result(
                result_cls,
                f"The hold can't take that much. "
                f"({ship.cargo_weight():.1f}/{ship.cargo_capacity} lbs)",
            )
        player.remove_item(item)
        ship.cargo.append(item)
        self.save_ships()
        return self._result(
            result_cls,
            f"You stow the {item.display_name} in the hold.\n{self.cargo_display(ship)}",
        )

    def cargo_put_all(self, player, result_cls, query: str = ""):
        from items import ItemType, items_matching

        ship, err = self.cargo_ship_for(player)
        if err:
            return self._result(result_cls, err)
        pool = list(player.inventory)
        if query:
            pool = items_matching(pool, query)
            if not pool:
                return self._result(
                    result_cls, f"You're not carrying any '{query}'."
                )
        fish = [
            item for item in pool
            if item.item_type == ItemType.FISH
            and not player.is_wearing_or_equipped(item)
        ]
        if not fish:
            if query:
                return self._result(result_cls, "Only fish go in the hold.")
            return self._result(result_cls, "You're not carrying any fish to stow.")
        stowed, left, refused = [], [], []
        load = ship.cargo_weight()
        for item in fish:
            if self.cargo_refusal(item):
                refused.append(item)
                continue
            weight = float(item.weight or 0.0)
            if load + weight > ship.cargo_capacity + 1e-9:
                left.append(item)
                continue
            load += weight
            stowed.append(item)
        if not stowed:
            if refused and not left:
                return self._result(result_cls, "Those fish will not stay in the hold.")
            return self._result(
                result_cls,
                f"The hold can't take any of that. "
                f"({ship.cargo_weight():.1f}/{ship.cargo_capacity} lbs)",
            )
        for item in stowed:
            player.remove_item(item)
            ship.cargo.append(item)
        self.save_ships()
        label = f"the {query}" if query else "what fits"
        lines = [f"You stow {label} in the hold:"]
        lines.extend(f"  - {item.display_name}" for item in stowed)
        if left:
            lines.append("Too heavy for what's left:")
            lines.extend(f"  - {item.display_name}" for item in left)
        if refused:
            lines.append("Those fish will not stay in the hold:")
            lines.extend(f"  - {item.display_name}" for item in refused)
        lines.append(self.cargo_display(ship))
        return self._result(result_cls, "\n".join(lines))

    def cargo_take(self, player, query: str, result_cls):
        ship, err = self.cargo_ship_for(player)
        if err:
            return self._result(result_cls, err)
        item = self.find_cargo_item(ship, query)
        if item is None:
            return self._result(result_cls, f"There's no '{query}' in the hold.")
        ship.cargo.remove(item)
        player.inventory.append(item)
        self.save_ships()
        return self._result(result_cls, f"You take the {item.display_name} from the hold.")

    def cargo_take_all(self, player, result_cls, query: str = ""):
        from items import items_matching

        ship, err = self.cargo_ship_for(player)
        if err:
            return self._result(result_cls, err)
        if not ship.cargo:
            return self._result(result_cls, "The hold is empty.")
        if query:
            taken = items_matching(ship.cargo, query)
            if not taken:
                return self._result(
                    result_cls, f"There's no '{query}' in the hold."
                )
            for item in taken:
                ship.cargo.remove(item)
        else:
            taken = list(ship.cargo)
            ship.cargo = []
        player.inventory.extend(taken)
        self.save_ships()
        label = f"the {query}" if query else "the hold"
        lines = [f"You take {label} out:" if query else "You clear out the hold:"]
        lines.extend(f"  - {item.display_name}" for item in taken)
        return self._result(result_cls, "\n".join(lines))

    # -- modules ---------------------------------------------------------------

    def _module_ship(self, player):
        """Owner's docked Skipjack, from inside it or from its pad."""
        ship, err = self.cargo_ship_for(player)
        if err:
            return None, err
        if ship.state != ShipState.DOCKED:
            return None, "Not while it's out on the road."
        return ship, None

    def cmd_modules(self, player, args, result_cls):
        ship, err = self.cargo_ship_for(player)
        if err:
            return self._result(result_cls, err)
        return self._result(result_cls, "\n".join(self._module_lines(ship)))

    def cmd_install(self, player, args, result_cls):
        from items import ItemType, MODULE_ITEMS, module_slot

        ship, err = self._module_ship(player)
        if err:
            return self._result(result_cls, err)
        query = (args or "").strip()
        if not query:
            return self._result(result_cls, "Install which module? (see MODULES)")
        item = player.find_item(query)
        if item is None or item.item_type != ItemType.MODULE:
            return self._result(result_cls, "You're not carrying a module by that name.")
        slot = module_slot(item)
        if slot is None:
            return self._result(result_cls, "That module doesn't fit anything on this ship.")
        old_id = ship.modules.get(slot)
        if old_id == item.id:
            return self._result(result_cls, f"A {item.name} is already installed.")
        lines = []
        if old_id:
            from items import create_module

            player.inventory.append(create_module(old_id))
            lines.append(f"You pull the {MODULE_ITEMS[old_id].name} and set it aside.")
        player.remove_item(item)
        ship.modules[slot] = item.id
        ship.apply_modules()
        self.save_ships()
        lines.append(f"You bolt the {item.name} into the {slot} rack.")
        lines.extend(self._module_lines(ship))
        return self._result(result_cls, "\n".join(lines))

    def cmd_uninstall(self, player, args, result_cls):
        from items import MODULE_ITEMS, MODULE_SLOTS, create_module, module_slot

        ship, err = self._module_ship(player)
        if err:
            return self._result(result_cls, err)
        query = (args or "").strip().lower()
        slot = None
        if query in MODULE_SLOTS:
            slot = query
        elif query in ("hyperdrive", "drive"):
            slot = "hyper"
        elif query in ("hold", "freezer", "reefer"):
            slot = "cargo"
        else:
            for module_id, item in MODULE_ITEMS.items():
                if query and (query == module_id or query in item.name.lower()):
                    slot = module_slot(item)
                    break
        if slot is None:
            return self._result(
                result_cls, "Uninstall what? engine, hyper, or cargo."
            )
        installed = ship.modules.get(slot)
        if not installed:
            return self._result(
                result_cls, f"The {slot} rack holds the stock part. Nothing to pull."
            )
        if slot == "cargo":
            stock = SKIPJACK_STOCK["cargo"]
            if ship.cargo_weight() > stock:
                return self._result(
                    result_cls,
                    f"The stock hold only takes {stock} lbs. Unload some fish first.",
                )
        ship.modules[slot] = None
        ship.apply_modules()
        player.inventory.append(create_module(installed))
        self.save_ships()
        lines = [f"You pull the {MODULE_ITEMS[installed].name}. The stock part goes back in."]
        lines.extend(self._module_lines(ship))
        return self._result(result_cls, "\n".join(lines))


# Not linked from the in-game help on purpose.
GARAGE_HELP = """
BACK GARAGE (south of Slick's Surplus):
  ships                 - What's parked here
  unlock airstream      - Needs the padlock key
  open / close          - The camper door
  board airstream       - Climb in
  launch                - Pull out
  status / radar        - Instruments
  radio                 - Weather on each fishing planet and what the buyers want
  trajectory <x> <y> <z> / accelerate <speed> (acc)
  calculate (cal) [system x y z] / hyperspace (hyper)
  map                   - Star map; + marks the system you're in
  land                  - List stops; land <name> within 200 units
  target / fire lasers  - Combat
  recharge / autopilot  - Shields and auto
  docking <ship>        - Lock hulls with a ship within 200 units (dock)
  board dock / detach   - Cross the airlock / let the other ship go
  The Drifter           - Unmanned. Cruises 5 minutes, then jumps. Dock for the gem.
  leave / leaveship     - After you set down and open up
  return                - Send the empty rental back to the garage

SKIPJACK (any pad off Earth):
  list / buy ship / sell ship   - 10000g hull, one per pilot, sells hull + modules
                                  (buy ship asks you to name it first)
  rename <name>                - Rename it while it is parked on this pad
  cargo / hold                  - What's stowed (owner only, fish by the pound)
  put <fish> cargo / take <fish> cargo / put all [fish] cargo / take all [fish] cargo
  modules / install <module> / uninstall <engine|hyper|cargo>
  Dell at the Forge buys and sells upgrade modules (list / buy / sell).
  At Cinder Lot, Rigel Exchange, Deneb Yard, and Drift Station,
  sell cargo sells every fish of the species that buyer wants
  and leaves the rest in the hold (they pay 3× the listed value).
"""
