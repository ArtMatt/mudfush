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


LAND_RANGE = 200          # how close you must be to set down
ORBIT_RANGE = 200         # snap into orbit this close to a planet
COORD_UPDATE_SECONDS = 15 # cockpit position ping while flying
RENT_GOLD = 5             # a little to take the public Airstream out
CAMPER_KEY_ID = "camper_key"
PUBLIC_SHIP_ID = "public_airstream"
PUBLIC_OWNER = "Public"
SHIPS_PATH = Path("saves/.ships.json")

# Hulls. The Airstream is the garage rental; the Skipjack is the two-room
# ship players buy and sell at any pad off Alpha Prime.
HULL_AIRSTREAM = "airstream"
HULL_SKIPJACK = "skipjack"
SKIPJACK_BASE_PRICE = 10000
SKIPJACK_STOCK = {"engine": 100, "hyper": 100, "cargo": 25}
DELL_PAD_ID = "beta_forge"       # Dell buys and sells upgrade modules here
MODULE_BUYBACK = 0.5             # Dell pays half for a loose module
CARGO_WORDS = frozenset({"cargo", "hold", "cargohold"})


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


@dataclass
class Planet:
    name: str
    x: int
    y: int
    z: int
    pads: List[LandingPad] = field(default_factory=list)


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


def _owner_token(name: str) -> str:
    token = re.sub(r"[^a-zA-Z0-9]+", "_", (name or "").strip()).strip("_").lower()
    return token or "owner"


# ---------------------------------------------------------------------------
# World attachments — the garage, the camper's interior, and its stops
# ---------------------------------------------------------------------------

GARAGE_ROOM_ID = "slick_garage"
CAMPER_ROOM_ID = "camper_interior"

# Pads besides the garage, each with a fishing hole off the east side.
REMOTE_PADS = (
    {
        "id": "alpha_minor",
        "name": "Alpha Minor Research Pad",
        "description": (
            "A quiet research pad on Alpha Minor. The sky is a hard black. "
            "East of the pad, a chain-link walkway drops toward a dark pool "
            "that shouldn't be liquid."
        ),
        "hole_name": "Alpha Minor Fishing Hole",
        "hole_description": (
            "A round basin of black water sits under a grated catwalk. The "
            "surface is too still, then dimples as if something large turned "
            "over underneath. You could FISH here, if you wanted to."
        ),
    },
    {
        "id": "beta_haven",
        "name": "Beta Haven Spaceport",
        "description": (
            "Beta Haven's landing pad. Warm wind rolls off a rust-colored "
            "plain. East of the tarmac, a ditch holds water the color of "
            "old pennies."
        ),
        "hole_name": "Beta Haven Fishing Hole",
        "hole_description": (
            "Copper-stained water fills a cut in the plain. It smells like "
            "wet metal and algae. Things flick just under the film. You "
            "could FISH here."
        ),
    },
    {
        "id": "beta_forge",
        "name": "Beta Forge Cargo Pad",
        "description": (
            "A scarred cargo pad. Furnaces glow on the horizon. East, a "
            "slag trench has filled with water that steams in the heat."
        ),
        "hole_name": "Beta Forge Fishing Hole",
        "hole_description": (
            "The slag trench is a fishing hole now, somehow. The water is "
            "warm and cloudy, and heat-shimmer makes the far bank crawl. "
            "You could FISH here."
        ),
    },
    {
        "id": "gamma_reach",
        "name": "Gamma Reach Spaceport",
        "description": (
            "A lonely pad at Gamma Reach. The star here is a cold white pin. "
            "East, a shallow crater holds a sheet of water that reflects the "
            "wrong sky."
        ),
        "hole_name": "Gamma Reach Fishing Hole",
        "hole_description": (
            "The crater pool is glassy and wrong. Your reflection lags a "
            "half-second behind you. Rings spread from casts that haven't "
            "happened yet. You could FISH here."
        ),
    },
    {
        "id": "gamma_ice",
        "name": "Gamma Ice Outpost",
        "description": (
            "Ice underfoot. The outpost is a single heated shack and this "
            "pad. East, someone has kept a hole chopped in the ice."
        ),
        "hole_name": "Gamma Ice Fishing Hole",
        "hole_description": (
            "A square hole in the ice, edges glazed from repeated thawing. "
            "The water below is darker and warmer than it has any right to "
            "be. You could FISH here."
        ),
    },
)

FISHING_HOLE_IDS = frozenset(f"{pad['id']}_hole" for pad in REMOTE_PADS)
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
        hole_id = f"{pad['id']}_hole"
        description = (
            pad["description"]
            + "\n\nA weathered placard by the tarmac reads: SKIPJACK HULLS "
            "BOUGHT AND SOLD. Type 'list' for terms."
        )
        npcs = []
        if pad["id"] == DELL_PAD_ID:
            description += (
                "\n\nDell runs a parts stall out of a shipping container "
                "here, engines and hyperdrive cores racked behind her like "
                "cordwood. She buys and sells ship upgrade modules."
            )
            npcs = ["Dell"]
        rooms[pad["id"]] = Room(
            id=pad["id"],
            name=pad["name"],
            description=description,
            exits={"east": hole_id},
            items=[],
            is_water=False,
            npcs=npcs,
        )
        rooms[hole_id] = Room(
            id=hole_id,
            name=pad["hole_name"],
            description=pad["hole_description"],
            exits={"west": pad["id"]},
            items=[],
            is_water=True,
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


def default_starsystems() -> List[StarSystem]:
    """The Alpha / Beta / Gamma layout the camper's route runs through."""
    alpha = StarSystem(
        name="Alpha", galaxy_x=0, galaxy_y=0,
        stars=[Star("Alpha Sun", -7000, 0, 0)],
        planets=[
            Planet("Alpha Prime", 1000, 1000, 1000, [
                LandingPad("Back Garage", GARAGE_ROOM_ID, 1000, 1000, 1000),
            ]),
            Planet("Alpha Minor", -1800, 900, 2200, [
                LandingPad("Alpha Minor Research Pad", "alpha_minor", -1800, 900, 2200),
            ]),
        ],
    )
    beta = StarSystem(
        name="Beta", galaxy_x=5000, galaxy_y=1200,
        stars=[Star("Beta Star", 0, -7000, 0)],
        planets=[
            Planet("Beta Haven", 1400, -900, 1700, [
                LandingPad("Beta Haven Spaceport", "beta_haven", 1400, -900, 1700),
            ]),
            Planet("Beta Forge", -2200, 1300, -1200, [
                LandingPad("Beta Forge Cargo Pad", "beta_forge", -2200, 1300, -1200),
            ]),
        ],
    )
    gamma = StarSystem(
        name="Gamma", galaxy_x=-3200, galaxy_y=4800,
        stars=[Star("Gamma Star", 0, 0, -7000)],
        planets=[
            Planet("Gamma Reach", 1800, 1400, -1000, [
                LandingPad("Gamma Reach Spaceport", "gamma_reach", 1800, 1400, -1000),
            ]),
            Planet("Gamma Ice", -1600, -1800, 1600, [
                LandingPad("Gamma Ice Outpost", "gamma_ice", -1600, -1800, 1600),
            ]),
        ],
    )
    return [alpha, beta, gamma]


def default_ships(systems: List[StarSystem]) -> List[Ship]:
    ship = Ship(
        name="Silver Airstream",
        owner=PUBLIC_OWNER,
        ship_id=PUBLIC_SHIP_ID,
        cockpit_room=CAMPER_ROOM_ID,
        location=GARAGE_ROOM_ID,
        lastdoc=GARAGE_ROOM_ID,
        home="Alpha",
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
        self._pad_index: Dict[str, LandingPad] = {}
        for system in self.systems:
            for pad in system.pads():
                self._pad_index[pad.room_id] = pad
        self.load_ships()
        self.ensure_cockpit_rooms()

    def attach_rooms(self, rooms: Dict[str, "Room"]) -> None:
        """Keep ship interiors after a world rebuild."""
        self.rooms = rooms
        self.ensure_cockpit_rooms()

    def owned_ship_for(self, player_name: str) -> Optional[Ship]:
        for ship in self.ships:
            if ship.owner.lower() == player_name.lower() and ship.owner != PUBLIC_OWNER:
                return ship
        return None

    def can_create_owned_ship_at(self, room_id: str) -> bool:
        return room_id in REMOTE_PAD_IDS

    def create_owned_ship(self, player, pad_room_id: str) -> Optional[Ship]:
        """One personal Skipjack per player, only at pads off Alpha Prime."""
        if self.owned_ship_for(player.name):
            return None
        if not self.can_create_owned_ship_at(pad_room_id):
            return None
        token = _owner_token(player.name)
        ship = Ship(
            name=f"{player.name}'s Skipjack",
            owner=player.name,
            ship_id=f"ship_{token}",
            cockpit_room=f"skipjack_cockpit_{token}",
            hold_room=f"skipjack_hold_{token}",
            hull_type=HULL_SKIPJACK,
            location=pad_room_id,
            lastdoc=pad_room_id,
            home="Alpha",
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
                if ship.is_skipjack():
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
        payload = {"ships": [self._ship_to_save(ship) for ship in self.ships]}
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
            dock = entry.get("lastdoc") or entry.get("location") or GARAGE_ROOM_ID
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
                home=entry.get("home") or "Alpha",
                state=ShipState.DOCKED,
                modules=modules,
                cargo=cargo,
            )
            ship.apply_modules()
            self.ships.append(ship)

    def system_named(self, name: str) -> Optional[StarSystem]:
        needle = name.strip().lower()
        for system in self.systems:
            if system.name.lower().startswith(needle):
                return system
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
        return notes

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
                f"The ship settles into orbit around {nearest.name}.",
                notes,
            )

    def _update_space(self, notes: List[Broadcast]) -> None:
        for ship in self.ships:
            if ship.laser_cool > 0:
                ship.laser_cool -= 1

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
            return self._result(result_cls, "That's not your camper.")
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
            return self._result(result_cls, "That's not your camper.")
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
            lines.append(f"  PLANET {planet.name:18} {planet.x:6} {planet.y:6} {planet.z:6}  range {dist:.0f}")
        for other in ship.starsystem.ships:
            if other is ship:
                continue
            dist = _distance(ship.vx, ship.vy, ship.vz, other.vx, other.vy, other.vz)
            lines.append(
                f"  SHIP {other.name:20} {other.vx:6.0f} {other.vy:6.0f} {other.vz:6.0f}  range {dist:.0f}"
            )
        return self._result(result_cls, "\n".join(lines))

    def cmd_trajectory(self, player, args, result_cls):
        ship, err = self._need_ship(player, result_cls)
        if err:
            return err
        if ship.state == ShipState.HYPERSPACE:
            return self._result(result_cls, "You can only do that in realspace!")
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
        if not self._can_maneuver(ship):
            return self._result(result_cls, "Please wait until the ship has finished its current maneuver.")
        parts = args.split()
        if not parts:
            lines = ["Format: Calculate <starsystem> <entry x> <entry y> <entry z>", "Possible destinations:"]
            for system in self.systems:
                dist = _galaxy_distance(ship.starsystem, system)
                lines.append(f"  {system.name:20} jump-distance {dist}")
            return self._result(result_cls, "\n".join(lines))
        dest = self.system_named(parts[0])
        if dest is None:
            return self._result(result_cls, "No such starsystem.")
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

    def cmd_hyperspace(self, player, args, result_cls):
        ship, err = self._need_ship(player, result_cls)
        if err:
            return err
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
            lines.append("  Type 'buy ship' to take one home.")
        else:
            here = owned.location == room_id and owned.state == ShipState.DOCKED
            lines.append(
                f"  You own {owned.name}. Sell price here: {owned.sale_price()}g "
                f"(hull {SKIPJACK_BASE_PRICE} + modules {owned.module_value()})."
            )
            if here:
                lines.append("  Type 'sell ship' with an empty hold and nobody aboard.")
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
            ship = self.create_owned_ship(player, room_id)
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
        if room_id != DELL_PAD_ID:
            return self._result(
                result_cls, "Only hulls change hands here. Dell at Beta Forge sells modules."
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

    def pad_sell(self, player, room_id: str, query: str, result_cls):
        from items import ItemType, MODULE_ITEMS, module_price

        query = (query or "").strip().lower()
        if not query:
            return self._result(result_cls, self.pad_listing(player, room_id))
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
                result_cls, "Only hulls change hands here. Dell at Beta Forge buys modules."
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

    def cargo_ship_for(self, player) -> Tuple[Optional[Ship], Optional[str]]:
        """
        The owner's ship whose hold is reachable from where they stand:
        inside it, or on the pad it's docked at.
        """
        aboard = self.ship_from_interior(player.current_room)
        if aboard is not None:
            if aboard.owner.lower() != player.name.lower():
                return None, "That's not your hold to rummage in."
            if not aboard.is_skipjack():
                return None, "This ship has no cargo hold."
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

    def cargo_put_all(self, player, result_cls):
        from items import ItemType

        ship, err = self.cargo_ship_for(player)
        if err:
            return self._result(result_cls, err)
        fish = [
            item for item in list(player.inventory)
            if item.item_type == ItemType.FISH
            and not player.is_wearing_or_equipped(item)
        ]
        if not fish:
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
        lines = ["You stow what fits in the hold:"]
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

    def cargo_take_all(self, player, result_cls):
        ship, err = self.cargo_ship_for(player)
        if err:
            return self._result(result_cls, err)
        if not ship.cargo:
            return self._result(result_cls, "The hold is empty.")
        taken = list(ship.cargo)
        ship.cargo = []
        player.inventory.extend(taken)
        self.save_ships()
        lines = ["You clear out the hold:"]
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
  trajectory <x> <y> <z> / accelerate <speed> (acc)
  calculate (cal) [system x y z] / hyperspace (hyper)
  land                  - List stops; land <name> within 200 units
  target / fire lasers  - Combat
  recharge / autopilot  - Shields and auto
  leave / leaveship     - After you set down and open up

SKIPJACK (any pad off Alpha Prime):
  list / buy ship / sell ship   - 10000g hull, one per pilot, sells hull + modules
  cargo / hold                  - What's stowed (owner only, fish by the pound)
  put <fish> cargo / take <fish> cargo / put all cargo / take all cargo
  modules / install <module> / uninstall <engine|hyper|cargo>
  Dell at Beta Forge buys and sells upgrade modules (list / buy / sell).
"""
