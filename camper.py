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

import math
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from player import Player
    from world import Room
    from commands import CommandResult


# ---------------------------------------------------------------------------
# State, copied from mud.h's SHIP_* enum (the values the tick actually uses)
# ---------------------------------------------------------------------------

class ShipState(str, Enum):
    DOCKED = "docked"
    READY = "ready"
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
RENT_GOLD = 5             # Gus charges a little for the gas
TRAINER_ADEPT = 20        # the most Gus can teach you
TRAIN_COST = 2            # gold per practice session
CAMPER_KEY_ID = "camper_key"
CAMPER_KEY_ROOM = "slick_backroom"  # where a spare key keeps turning up
LAUNCH_FUEL = 100         # energy burned pulling out
LAND_FUEL = 25
HYPER_FUEL = 100


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
    owner: str = "Public"
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
    energy: int = 10000
    maxenergy: int = 10000
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


def _skill(player) -> int:
    return int(getattr(player, "wrenching", 0) or 0)


def _set_skill(player, value: int) -> None:
    player.wrenching = max(0, min(100, int(value)))


def _has_key(player) -> bool:
    """True if the player is carrying the camper's padlock key."""
    return any(item.id == CAMPER_KEY_ID for item in getattr(player, "inventory", []))


def _learn(player, success: bool) -> None:
    """Practice pays off a little at a time, win or lose."""
    skill = _skill(player)
    if skill >= 100:
        return
    if success:
        _set_skill(player, skill + random.randint(1, 4))
    elif random.randint(1, 100) < 30:
        _set_skill(player, skill + 1)


# ---------------------------------------------------------------------------
# World attachments — the garage, the camper's interior, and its stops
# ---------------------------------------------------------------------------

GARAGE_ROOM_ID = "slick_garage"
CAMPER_ROOM_ID = "camper_interior"

GARAGE_ROOM_IDS = frozenset({
    GARAGE_ROOM_ID,
    CAMPER_ROOM_ID,
    "alpha_minor",
    "beta_haven",
    "beta_forge",
    "gamma_reach",
    "gamma_ice",
})


def place_camper_key(rooms: Dict[str, "Room"]) -> bool:
    """
    Leave a padlock key on the floor of Slick's back room.

    Called on every ground-loot reset, so the key comes back after someone
    pockets it. The back room is already gated behind Curt, which is as much
    of a lock as the key itself needs.
    """
    from items import CAMPER_KEY, create_item_copy

    room = rooms.get(CAMPER_KEY_ROOM)
    if room is None:
        return False
    if any(item.id == CAMPER_KEY_ID for item in room.items):
        return False
    room.items.append(create_item_copy(CAMPER_KEY, roll_stats=False, condition=4))
    return True


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
            "Gus dozes in a lawn chair beside it. He says he'll let you "
            "PRACTICE WRENCHING on the old thing if you're bored."
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

    stops = [
        ("alpha_minor", "Alpha Minor Research Pad",
         "A quiet research pad on Alpha Minor. The sky is a hard black."),
        ("beta_haven", "Beta Haven Spaceport",
         "Beta Haven's landing pad. Warm wind rolls off a rust-colored plain."),
        ("beta_forge", "Beta Forge Cargo Pad",
         "A scarred cargo pad. Furnaces glow on the horizon."),
        ("gamma_reach", "Gamma Reach Spaceport",
         "A lonely pad at Gamma Reach. The star here is a cold white pin."),
        ("gamma_ice", "Gamma Ice Outpost",
         "Ice underfoot. The outpost is a single heated shack and this pad."),
    ]
    for rid, name, desc in stops:
        rooms[rid] = Room(
            id=rid,
            name=name,
            description=desc,
            exits={},
            items=[],
            is_water=False,
        )

    rooms[CAMPER_ROOM_ID] = Room(
        id=CAMPER_ROOM_ID,
        name="Inside the Airstream",
        description=(
            "The camper's dinette has been torn out and replaced with a "
            "single wrap-around console. Pilot, navigation, sensor, and "
            "weapons controls share the panel where a fold-down table used "
            "to be. The windows are shuttered.\n\n"
            "There is no door to walk through — use LEAVESHIP once you have "
            "set down and opened the hatch."
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
        owner="Public",
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
        return notes

    def _echo_cockpit(self, ship: Ship, message: str, notes: List[Broadcast]) -> None:
        notes.append(Broadcast(ship.cockpit_room, message))

    def _echo_pad(self, room_id: str, message: str, notes: List[Broadcast]) -> None:
        if room_id:
            notes.append(Broadcast(room_id, message))

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
            if ship.energy > 0:
                ship.energy = max(0, ship.energy - max(1, ship.currspeed // 50))

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

            if ship.starsystem and ship.currspeed > 0 and ship.state == ShipState.READY:
                self._echo_cockpit(
                    ship,
                    f"Speed: {ship.currspeed}  Coords: "
                    f"{ship.vx:.0f} {ship.vy:.0f} {ship.vz:.0f}",
                    notes,
                )

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
        ship.energy = max(0, ship.energy - LAUNCH_FUEL)
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
        ship.location = pad.room_id
        ship.lastdoc = pad.room_id
        ship.currspeed = 0
        ship.state = ShipState.DOCKED
        ship.energy = max(0, ship.energy - LAND_FUEL)
        if ship.owner == "Public":
            ship.energy = ship.maxenergy
            ship.missiles = ship.maxmissiles
            ship.hull = ship.maxhull
            ship.shield = 0
            self._echo_cockpit(ship, "Repairing and refueling ship...", notes)
        self._echo_cockpit(ship, "Landing sequence complete.", notes)
        self._echo_cockpit(ship, "You feel a slight thud as the ship sets down.", notes)
        self._echo_pad(pad.room_id, f"{ship.name} rolls back in and settles.", notes)

    # -- commands --------------------------------------------------------------

    def knows_room(self, room_id: str) -> bool:
        """True only inside the garage, the camper, or one of its stops."""
        if room_id in GARAGE_ROOM_IDS:
            return True
        return any(ship.cockpit_room == room_id for ship in self.ships)

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
        return {
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
            "launch": self.cmd_launch,
            "land": self.cmd_land,
            "status": self.cmd_status,
            "radar": self.cmd_radar,
            "trajectory": self.cmd_trajectory,
            "accelerate": self.cmd_accelerate,
            "calculate": self.cmd_calculate,
            "hyperspace": self.cmd_hyperspace,
            "target": self.cmd_target,
            "fire": self.cmd_fire,
            "recharge": self.cmd_recharge,
            "autopilot": self.cmd_autopilot,
            "practice": self.cmd_practice,
        }

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

    def cmd_unlock(self, player, args, result_cls):
        ship, err = self._ship_at_hand(player, args, result_cls, "unlock")
        if err:
            return err
        if not ship.locked:
            return self._result(result_cls, f"The {ship.name} is already unlocked.")
        if not _has_key(player):
            return self._result(
                result_cls,
                "The padlock is rusted but solid. You'd need the key for it.",
            )
        ship.locked = False
        return self._result(
            result_cls,
            f"The key turns stiffly and the padlock springs open.\n"
            f"You slip it off the latch of the {ship.name}.",
            [Broadcast(ship.location, f"{player.name} unlocks the {ship.name}.")],
        )

    def cmd_lock(self, player, args, result_cls):
        ship, err = self._ship_at_hand(player, args, result_cls, "lock")
        if err:
            return err
        if ship.locked:
            return self._result(result_cls, f"The {ship.name} is already locked.")
        if not _has_key(player):
            return self._result(result_cls, "You don't have the key for that padlock.")
        if ship.state != ShipState.DOCKED:
            return self._result(result_cls, "Not while it's out on the road.")
        if self.rooms.get(ship.cockpit_room) and self.rooms[ship.cockpit_room].players:
            return self._result(result_cls, "Someone is still inside.")
        ship.hatch_open = False
        ship.locked = True
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
        chance = _skill(player)
        if random.randint(1, 100) >= chance:
            _learn(player, False)
            return self._result(result_cls, "You fail to work the controls properly!")
        if ship.owner == "Public":
            if player.gold < RENT_GOLD:
                return self._result(
                    result_cls,
                    f"You can't afford to rent this ship, it costs {RENT_GOLD} gold.",
                )
            player.gold -= RENT_GOLD
            rent_line = f"You pay {RENT_GOLD} gold to rent the ship.\n"
        else:
            rent_line = ""
        if ship.energy <= 0:
            return self._result(result_cls, "This ship has no fuel.")
        notes = []
        if ship.hatch_open:
            ship.hatch_open = False
            self._echo_pad(ship.location, f"The door on the {ship.name} bangs shut.", notes)
            self._echo_cockpit(ship, "The door bangs shut.", notes)
        ship.lastdoc = ship.location
        ship.state = ShipState.LAUNCH
        ship.currspeed = ship.realspeed
        _learn(player, True)
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
        if ship.state != ShipState.READY or not ship.starsystem:
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
        chance = _skill(player)
        if random.randint(1, 100) >= chance:
            _learn(player, False)
            return self._result(result_cls, "You fail to work the controls properly!")
        ship.dest = pad.name
        ship.state = ShipState.LAND
        ship.currspeed = 0
        _learn(player, True)
        return self._result(result_cls, f"Landing sequence initiated for {pad.name}.")

    def cmd_status(self, player, args, result_cls):
        ship, err = self._need_ship(player, result_cls, seat="look")
        if err:
            return err
        sysname = ship.starsystem.name if ship.starsystem else "(docked)"
        target = ship.target.name if ship.target else "none"
        lines = [
            f"{ship.name}:",
            f"System: {sysname}   State: {ship.state.value}",
            f"Current Coordinates: {ship.vx:.0f} {ship.vy:.0f} {ship.vz:.0f}",
            f"Current Heading: {ship.hx:.0f} {ship.hy:.0f} {ship.hz:.0f}",
            f"Current Speed: {ship.currspeed}/{ship.realspeed}",
            f"Hull: {ship.hull}/{ship.maxhull}   Condition: {ship.state.value}",
            f"Shields: {ship.shield}/{ship.maxshield}   Energy(fuel): {ship.energy}/{ship.maxenergy}",
            f"Lasers: {ship.lasers}   Missiles: {ship.missiles}/{ship.maxmissiles}",
            f"Current Target: {target}",
            f"Autopilot: {'on' if ship.autopilot else 'off'}   Door: {'open' if ship.hatch_open else 'shut'}",
        ]
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
        chance = _skill(player)
        if random.randint(1, 100) >= max(chance, 40):
            return self._result(result_cls, "You fail to work the controls properly!")
        lines = [f"{ship.starsystem.name} — radar"]
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
        if len(lines) == 1:
            lines.append("  (empty sky)")
        return self._result(result_cls, "\n".join(lines))

    def cmd_trajectory(self, player, args, result_cls):
        ship, err = self._need_ship(player, result_cls)
        if err:
            return err
        if ship.state == ShipState.HYPERSPACE:
            return self._result(result_cls, "You can only do that in realspace!")
        if ship.state != ShipState.READY or not ship.starsystem:
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
        if ship.state != ShipState.READY or not ship.starsystem:
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
        if ship.state != ShipState.READY:
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
        chance = _skill(player)
        if random.randint(1, 100) >= max(chance, 30):
            _learn(player, False)
            return self._result(result_cls, "You fail to work the controls properly!")
        ship.currjump = dest
        ship.jumpx, ship.jumpy, ship.jumpz = jx, jy, jz
        ship.hyperdistance = max(1, _galaxy_distance(ship.starsystem, dest))
        _learn(player, True)
        return self._result(
            result_cls,
            f"Hyperspace course plotted to {dest.name} "
            f"({jx:.0f} {jy:.0f} {jz:.0f}), distance {ship.hyperdistance}.",
        )

    def cmd_hyperspace(self, player, args, result_cls):
        ship, err = self._need_ship(player, result_cls)
        if err:
            return err
        if ship.state != ShipState.READY or not ship.starsystem:
            return self._result(result_cls, "Please wait until the ship has finished its current maneuver.")
        if ship.currjump is None or ship.hyperdistance <= 0:
            return self._result(result_cls, "You need to calculate a jump first.")
        if ship.energy < HYPER_FUEL:
            return self._result(result_cls, "There's not enough fuel!")
        chance = _skill(player)
        if random.randint(1, 100) >= chance:
            _learn(player, False)
            return self._result(result_cls, "You fail to work the controls properly!")
        ship.energy -= HYPER_FUEL
        ship.state = ShipState.HYPERSPACE
        ship.currspeed = 0
        self._leave_system(ship)
        _learn(player, True)
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
            ship.energy = max(0, ship.energy - 5)
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
        cockpit = self.rooms.get(ship.cockpit_room)
        garage = self.rooms.get(GARAGE_ROOM_ID)
        if cockpit and garage:
            for name in list(cockpit.players):
                cockpit.players.discard(name)
                garage.players.add(name)
                self.eject_queue.append((name, garage.id))
                notes.append(Broadcast(
                    garage.id,
                    f"{name} is thrown clear as {ship.name} comes apart, "
                    f"and wakes up back in the garage.",
                ))
        self._leave_system(ship)
        ship.state = ShipState.DOCKED
        ship.location = GARAGE_ROOM_ID
        ship.lastdoc = GARAGE_ROOM_ID
        ship.hull = ship.maxhull
        ship.energy = ship.maxenergy
        ship.shield = 0
        ship.currspeed = 0
        ship.target = None
        ship.hatch_open = False

    def cmd_recharge(self, player, args, result_cls):
        ship, err = self._need_ship(player, result_cls)
        if err:
            return err
        if ship.energy < 50:
            return self._result(result_cls, "There's not enough energy to charge the shields.")
        if ship.shield >= ship.maxshield:
            return self._result(result_cls, "Shields are already at maximum.")
        amount = min(50, ship.maxshield - ship.shield, ship.energy // 2)
        ship.shield += amount
        ship.energy -= amount
        return self._result(result_cls, f"Shields charged +{amount} ({ship.shield}/{ship.maxshield}).")

    def cmd_autopilot(self, player, args, result_cls):
        ship = self.ship_from_cockpit(player.current_room)
        if ship is None:
            return self._result(result_cls, "You must be in the cockpit of a ship to do that!")
        ship.autopilot = not ship.autopilot
        state = "engaged" if ship.autopilot else "disengaged"
        return self._result(result_cls, f"Autopilot {state}.")

    def cmd_practice(self, player, args, result_cls):
        room = self.rooms.get(player.current_room)
        if not room or "Gus" not in room.npcs:
            return None
        skill_name = (args or "wrenching").strip().lower()
        if skill_name not in ("wrenching", "wrench", "tinkering", "mechanics", "driving"):
            return self._result(
                result_cls,
                "Gus tells you, 'I only teach wrenching. Type: practice wrenching'",
            )
        skill = _skill(player)
        if skill >= TRAINER_ADEPT:
            return self._result(
                result_cls,
                "Gus tells you, 'I've taught you everything I know about wrenching. "
                "You'll have to practice it on your own now...'",
            )
        if player.gold < TRAIN_COST:
            return self._result(
                result_cls,
                f"Gus tells you, 'I charge {TRAIN_COST} gold, and you don't have it.'",
            )
        player.gold -= TRAIN_COST
        bump = max(5, player.get_effective_attribute("intelligence"))
        _set_skill(player, min(TRAINER_ADEPT, skill + bump))
        new = _skill(player)
        extra = ""
        if new >= TRAINER_ADEPT:
            extra = (
                "\nGus tells you, 'You'll have to practice it on your own now...'"
            )
        return self._result(
            result_cls,
            f"You practice wrenching. ({new}%){extra}",
        )


# Not linked from the in-game help on purpose.
GARAGE_HELP = """
BACK GARAGE (south of Slick's Surplus):
  practice wrenching    - Gus will train you from 0% to 20%
  ships                 - What's parked here
  unlock airstream      - Needs the padlock key
  open / close          - The camper door
  board airstream       - Climb in
  launch                - Pull out (needs wrenching skill)
  status / radar        - Instruments
  trajectory <x> <y> <z> / accelerate <speed>
  calculate [system x y z] / hyperspace
  land                  - List stops; land <name> within 200 units
  target / fire lasers  - Combat
  recharge / autopilot  - Shields and auto
  leaveship             - After you set down and open up
"""
