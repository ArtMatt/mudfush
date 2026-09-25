"""
Command parser and handlers for the MUD Fishing Game
"""

import random
import re
import time
import math
from typing import Optional, Callable, Dict, List, Tuple, TYPE_CHECKING
from dataclasses import dataclass, field

from player import Player, PlayerManager, TRUCK_CAPACITY
from world import Room, DIRECTION_ALIASES, TRUCK_ROOM_ID
from camper import FISHING_HOLE_IDS
from items import (
    Item, ItemType, STORE_INVENTORY, CATCHABLE_FISH, create_item_copy,
    DEGRADABLE_TYPES, strip_ansi, CONDITION_NAMES, colorize_condition,
    condition_word,
    colorize_fish_size, attribute_abbrev, colorize_attribute,
    attribute_color_name, PLASTIC_WORM, create_beer, create_gem,
    create_glowing_lure, create_shard, UNSELLABLE_TYPES, GEMMABLE_TYPES,
    SPECIALTY_LURE, LURE_FORBIDDEN_SPECIES,
    SPECIALTY_LURE_MAX_MULT, attuned_lure_name, attuned_lure_description,
    specialty_lure_essence_from_fish, is_ancient_fish_id,
    colorize_fish_species, colorize_ancient_fish,
    normalize_container_shards, CAMPER_KEY,
)
from market import (
    BubbaFishQuest,
    Market,
    StoreType,
    apply_condition_sell_price,
    apply_charisma_sell_bonus,
    create_random_norm_fish_quest,
)
from lake_state import LakeCycleState, UNIQUE_FISH_SPECS
from fishermen import Fisherman, FishermanManager, NPC_CATCH_MAX_SECONDS

if TYPE_CHECKING:
    from weather import WeatherSystem

SING_COOLDOWN_HOURS = 12
SLICK_WORM_DEAL_COST = 10
SLICK_WORM_DEAL_COUNT = 3
SLICK_DEAL_YES = frozenset({"yes", "y", "sure", "deal", "ok", "okay", "yeah", "yep"})
SLICK_DEAL_NO = frozenset({"no", "n", "nah", "nope", "pass"})
GEM_CATCH_CHANCE = 200  # 1 in 200
MOUTH_LURE_CATCH_CHANCE = 300  # 1 in 300 — glowing lure in a fish's mouth

ATTRIBUTE_ALIASES = {
    "str": "strength",
    "strength": "strength",
    "dex": "dexterity",
    "dexterity": "dexterity",
    "con": "constitution",
    "constitution": "constitution",
    "int": "intelligence",
    "intelligence": "intelligence",
    "wis": "wisdom",
    "wisdom": "wisdom",
    "cha": "charisma",
    "charisma": "charisma",
}

# Non-ASCII/control chars, plus characters commonly used for injection/escaping
# Note: ')' is allowed so inventory numbers like 'ex 1)' still work.
_HACK_CHAR_RE = re.compile(
    r"[^\x20-\x7e]"  # non-printable / non-ASCII
    r"|[<>\\/\[\]{}(`;|&$*=#@%^~]"  # injection / shell / Python metacharacters
)

NPC_DESCRIPTIONS = {
    "bubba": (
        "\nBUBBA\n"
        "A weathered old angler with kind eyes, a faded flannel shirt, "
        "and the smell of bait clinging to him. He runs the bait shop "
        "with a fair hand and a soft spot for a good tune."
    ),
    "slick": (
        "\nSLICK\n"
        "A wiry man with slicked-back hair, a gold tooth, and eyes that "
        "never stop moving. He looks like he'd sell you your own boots "
        "and call it a bargain."
    ),
    "cliff": (
        "\nCLIFF\n"
        "A quiet man in a paint-stained apron, visor pulled low. His hands "
        "are nicked from years of wire and hooks. Jig bodies, skirts, and "
        "tins of beads cover the bench. He looks up only when you set "
        "something down."
    ),
    "norm": (
        "\nNORM\n"
        "Cliff's young son is crouched in the grass, arranging pebbles and "
        "fish-shaped sticks into a game only he understands."
    ),
    "gus": (
        "\nGUS\n"
        "A heavy-set old man asleep in a lawn chair with a cap over his "
        "eyes. His knuckles are permanently grease-stained. He claims he "
        "can fix anything with wheels, given enough time and nobody "
        "watching."
    ),
    "curt": (
        "\nCURT\n"
        "A broad, quiet man in rolled shirtsleeves sits behind a scarred "
        "green table. He rolls three dice across his knuckles without looking."
    ),
}

FISHING_SPOT_LANDMARKS = {
    "lake_shore": "along the sandy shore",
    "shallow_cove": "among the reeds in the quiet cove",
    "old_pier": "beside the old wooden boards",
    "rocky_point": "out by the flat rocks",
}

JIG_COMPONENTS = (
    "bead",
    "skirt",
    "swivel",
    "thread",
    "spinner blade",
    "split ring",
    "trailer hook",
    "chenille",
    "feather",
    "rattler",
    "weed guard",
    "paint flake",
    "tinsel",
    "keeper",
    "eyelet",
)

CLIFF_COMPONENT_LINES = (
    "I think this is what you're looking for.",
    "This'll sit right on that jig.",
    "Match the bait. That's the whole trick.",
    "Here. Don't lose it in the grass.",
)

NORM_MAX_GUESSES = 8
CEELO_ROOM_ID = "slick_backroom"
CEELO_ACCESS_CHARISMA = 8
CEELO_SPEND_UNLOCK = 500


@dataclass
class NormRound:
    target: BubbaFishQuest
    guesses: int = 0


@dataclass
class DelayStage:
    """One timed beat in a multi-step command (e.g. fishing)."""
    delay_seconds: float
    message: Optional[str] = None
    broadcast: Optional[str] = None


@dataclass
class ReelChallenge:
    """Interactive reeling phase with harmful and beneficial events."""
    total_seconds: float
    response_seconds: float
    intelligence: int
    wisdom: int


@dataclass
class CommandResult:
    """Result of a command execution."""
    message: str  # Message to send to the player
    broadcast: Optional[str] = None  # Message to broadcast to room
    success: bool = True
    delay_seconds: float = 0
    immediate_message: Optional[str] = None
    deferred: Optional[Callable[[], str]] = None  # Runs after delay; returns final message
    stages: List[DelayStage] = field(default_factory=list)
    reel_challenge: Optional[ReelChallenge] = None


class GameCommands:
    """Handles all game commands."""
    
    def __init__(self, rooms: Dict[str, Room], player_manager: PlayerManager,
                 weather: Optional['WeatherSystem'] = None,
                 market: Optional[Market] = None,
                 lake_state: Optional[LakeCycleState] = None,
                 fishermen: Optional[FishermanManager] = None):
        self.rooms = rooms
        self.player_manager = player_manager
        self.weather = weather
        self.market = market
        self.lake_state = lake_state
        self.fishermen = fishermen
        self.garage = None  # GarageEngine, set by the server if enabled
        self.norm_rounds: Dict[str, NormRound] = {}
        self.ceelo_tip_handler: Optional[Callable[[Player, int], CommandResult]] = None
        self.ceelo_roll_handler: Optional[Callable[[Player], CommandResult]] = None
        self.population_cycle: int = 0
        
        # Command mapping
        self.commands: Dict[str, Callable] = {
            "look": self.cmd_look,
            "l": self.cmd_look,
            "ls": self.cmd_look,
            "cd": self.cmd_look,
            "go": self.cmd_go,
            "north": lambda p, a: self.cmd_go(p, "north"),
            "south": lambda p, a: self.cmd_go(p, "south"),
            "east": lambda p, a: self.cmd_go(p, "east"),
            "west": lambda p, a: self.cmd_go(p, "west"),
            "n": lambda p, a: self.cmd_go(p, "north"),
            "s": lambda p, a: self.cmd_go(p, "south"),
            "e": lambda p, a: self.cmd_go(p, "east"),
            "w": lambda p, a: self.cmd_go(p, "west"),
            "get": self.cmd_get,
            "take": self.cmd_get,
            "pick": self.cmd_get,
            "put": self.cmd_put,
            "truck": self.cmd_truck,
            "drop": self.cmd_drop,
            "use": self.cmd_use,
            "dump": self.cmd_chum,
            "chum": self.cmd_chum,
            "release": self.cmd_release,
            "feed": self.cmd_feed,
            "salvage": self.cmd_salvage,
            "inventory": self.cmd_inventory,
            "inv": self.cmd_inventory,
            "i": self.cmd_inventory,
            "examine": self.cmd_examine,
            "ex": self.cmd_examine,
            "look at": self.cmd_examine,
            "equip": self.cmd_equip,
            "eq": self.cmd_equip,
            "wear": self.cmd_equip,
            "don": self.cmd_equip,
            "unequip": self.cmd_unequip,
            "uneq": self.cmd_unequip,
            "remove": self.cmd_unequip,
            "rem": self.cmd_unequip,
            "stats": self.cmd_stats,
            "stat": self.cmd_stats,
            "attributes": self.cmd_stats,
            "meditate": self.cmd_meditate,
            "med": self.cmd_meditate,
            "fish": self.cmd_fish,
            "cast": self.cmd_fish,
            "repair": self.cmd_repair,
            "fix": self.cmd_repair,
            "consider": self.cmd_consider,
            "con": self.cmd_consider,
            "sense": self.cmd_sense,
            "ponder": self.cmd_ponder,
            "brag": self.cmd_brag,
            "appraise": self.cmd_appraise,
            "app": self.cmd_appraise,
            "drink": self.cmd_drink,  # Secret — beer level-up
            "gem": self.cmd_gem,  # Secret — socket gems into gear
            "say": self.cmd_say,
            "nod": self.cmd_nod,
            "tip": self.cmd_tip,
            "roll": self.cmd_roll,
            "give": self.cmd_give,
            "show": self.cmd_show,
            "shout": self.cmd_shout,
            "yell": self.cmd_shout,
            "who": self.cmd_who,
            "players": self.cmd_who,
            "help": self.cmd_help,
            "?": self.cmd_help,
            "weather": self.cmd_weather,
            "buy": self.cmd_buy,
            "list": self.cmd_list,
            "sell": self.cmd_sell,
            "save": self.cmd_save,
            "sing": self.cmd_sing,  # Secret command!
            "punch": self.cmd_punch,  # Secret — violence gets you jailed
            "kick": self.cmd_kick,  # Secret — violence gets you jailed
            "quit": self.cmd_quit,
            "exit": self.cmd_quit,
            "out": lambda p, a: self.cmd_go(p, "out"),
            "o": lambda p, a: self.cmd_go(p, "out"),
        }
    
    def parse_and_execute(self, player: Player, input_text: str) -> CommandResult:
        """Parse input and execute the appropriate command."""
        input_text = input_text.strip()
        if not input_text:
            return CommandResult("", success=False)

        if _HACK_CHAR_RE.search(input_text):
            return self._handle_hacking_attempt(player)
        
        parts = input_text.lower().split(maxsplit=1)
        command = parts[0]
        args = parts[1] if len(parts) > 1 else ""

        # Slick's once-per-visit worm deal reply
        if (
            player.slick_deal_pending
            and player.current_room == "slick_store"
            and not args
        ):
            if command in SLICK_DEAL_YES:
                return self._accept_slick_worm_deal(player)
            if command in SLICK_DEAL_NO:
                return self._decline_slick_worm_deal(player)

        # Beer drink confirmation / attribute choice
        if player.beer_confirm_attr and not args:
            if command in SLICK_DEAL_YES:
                return self._confirm_beer_improve(player, True)
            if command in SLICK_DEAL_NO:
                return self._confirm_beer_improve(player, False)
        if player.beer_awaiting_attr and not args:
            attribute = ATTRIBUTE_ALIASES.get(command)
            if attribute:
                return self._choose_beer_attribute(player, attribute)

        if command in ("board", "enter", "boa", "ent"):
            boarded = self._maybe_refuse_truck_board(player, args)
            if boarded is not None:
                return boarded
        if command in ("take", "get", "pick") and self._args_name_truck(args):
            return self.cmd_take_from_truck(player, args)
        
        if command in self.commands:
            return self.commands[command](player, args)
        
        # Check for direction shortcuts
        if command in DIRECTION_ALIASES:
            return self.cmd_go(player, DIRECTION_ALIASES[command])

        if self.garage:
            handled = self.garage.handle(player, command, args, CommandResult)
            if handled is not None:
                return handled

        return CommandResult(f"Unknown command: '{command}'. Type 'help' for a list of commands.")

    def _accept_slick_worm_deal(self, player: Player) -> CommandResult:
        """Player accepts Slick's 3-worms-for-10 deal."""
        player.slick_deal_pending = False
        if player.gold < SLICK_WORM_DEAL_COST:
            return CommandResult(
                'Slick snorts. "Broke, huh? Come back when you\'ve got '
                f'{SLICK_WORM_DEAL_COST} gold."'
            )
        player.gold -= SLICK_WORM_DEAL_COST
        unlock = self._record_slick_spend(player, SLICK_WORM_DEAL_COST)
        worms = []
        for _ in range(SLICK_WORM_DEAL_COUNT):
            worm = create_item_copy(PLASTIC_WORM, condition=9)
            player.add_item(worm)
            worms.append(worm)
        names = ", ".join(w.display_name for w in worms)
        return CommandResult(
            message=(
                f'Slick grins wide and bags up three worms. "Smart choice."\n'
                f"You pay {SLICK_WORM_DEAL_COST} gold and receive: {names}.\n"
                f"You now have {player.gold} gold."
                f"{unlock}"
            ),
            broadcast=(
                f"ROOM:slick_store:Slick sells {player.name} a special "
                f"three-worm deal."
            ),
        )

    def _record_slick_spend(self, player: Player, amount: int) -> str:
        """Track Slick purchases and return his one-time back-room invitation."""
        player.slick_gold_spent += max(0, int(amount))
        if not self._maybe_unlock_ceelo_access(player):
            return ""
        self.player_manager.save_player(player)
        return (
            '\nSlick weighs your coin in his palm, then nods toward an Employees '
            'Only door. "You\'ve spent a lot of gold here. Want to see if you '
            'can get some more? West, if you\'ve got the nerve."'
        )

    def _decline_slick_worm_deal(self, player: Player) -> CommandResult:
        """Player turns down Slick's worm deal."""
        player.slick_deal_pending = False
        return CommandResult(
            message=(
                'Slick shrugs. "Your loss. Plenty of other suckers—er, '
                'customers—around."'
            ),
            broadcast=(
                f"ROOM:slick_store:{player.name} turns down Slick's worm deal."
            ),
        )

    def _handle_hacking_attempt(self, player: Player) -> CommandResult:
        """Warn once for weird characters, then send the player to jail."""
        player.hack_warnings += 1
        if player.hack_warnings <= 1:
            return CommandResult("no hacking please")
        return self.send_to_jail(
            player,
            "The sheriff appears out of nowhere, grabs you by the collar, "
            "and hauls you off to jail for hacking!"
        )

    def send_to_jail(self, player: Player, reason: str) -> CommandResult:
        """Move a player to jail and lock the exit based on visit count."""
        old_room = self.rooms.get(player.current_room)
        jail = self.rooms.get("jail")
        if not jail:
            return CommandResult("Something goes wrong... there's nowhere to send you.")

        if old_room:
            old_room.players.discard(player.name)
            if old_room.id in {"slick_store", CEELO_ROOM_ID}:
                player.clear_slick_visit()

        player.jail_visits += 1
        lock_seconds = 5 * player.jail_visits
        player.jail_release_at = time.time() + lock_seconds
        player.current_room = "jail"
        jail.players.add(player.name)

        leave_broadcast = ""
        if old_room and old_room.id != "jail":
            leave_broadcast = f"ROOM:{old_room.id}:{player.name} is dragged away by the sheriff!"

        message = (
            f"{reason}\n\n"
            f"*You've been locked in jail for {lock_seconds} seconds "
            f"(visit #{player.jail_visits}).*\n"
            f"{jail.get_description(current_player=player.name)}"
        )
        broadcast = leave_broadcast
        if leave_broadcast:
            broadcast += f"|ROOM:jail:{player.name} is thrown into a cell."
        else:
            broadcast = f"ROOM:jail:{player.name} is thrown into a cell."

        return CommandResult(message=message, broadcast=broadcast)
    
    def cmd_look(self, player: Player, args: str) -> CommandResult:
        """Look at the current room, a direction, or an object."""
        if args:
            cleaned = args.lower().strip()
            for prefix in ("to the ", "towards the ", "toward the ", "to ", "the "):
                if cleaned.startswith(prefix):
                    cleaned = cleaned[len(prefix):].strip()
                    break
            direction = DIRECTION_ALIASES.get(cleaned)
            if direction:
                return self._look_direction(player, direction)
            if self._is_truck_name(cleaned):
                return self.cmd_truck(player, "")
            return self.cmd_examine(player, args)
        
        room = self.rooms.get(player.current_room)
        if not room:
            return CommandResult("You're in a void... something went wrong!")

        self._maybe_unlock_ceelo_access(player)
        return CommandResult(self._room_description(player, room))

    def _maybe_unlock_ceelo_access(self, player: Player) -> bool:
        """Permanently unlock Slick's hidden west door when either gate passes."""
        if player.ceelo_access_unlocked:
            return False
        if (
            player.get_effective_attribute("charisma") >= CEELO_ACCESS_CHARISMA
            or player.slick_gold_spent >= CEELO_SPEND_UNLOCK
        ):
            player.ceelo_access_unlocked = True
            self.player_manager.save_player(player)
            return True
        return False

    def _room_description(self, player: Player, room: Room) -> str:
        """Render Slick's west door only for players who have unlocked it."""
        description = room.get_description(current_player=player.name)
        if self.garage:
            description += self.garage.describe_ships_here(room.id)
        if room.id == TRUCK_ROOM_ID:
            description += (
                "\nParked here:\n"
                "  - Your Truck"
            )
        if room.id != "slick_store" or not player.ceelo_access_unlocked:
            return description
        exits = list(room.exits.keys())
        description = description.replace(
            f"\nExits: [{', '.join(exits)}]",
            "\nA door marked EMPLOYEES ONLY stands open to the west."
            f"\n\nExits: [{', '.join(exits + ['west'])}]",
        )
        return description

    def _look_direction(self, player: Player, direction: str) -> CommandResult:
        """Peer into an adjacent room and report items, NPCs, and players."""
        room = self.rooms.get(player.current_room)
        if not room:
            return CommandResult("You're in a void... something went wrong!")

        if (
            room.id == "slick_store"
            and direction == "west"
            and player.ceelo_access_unlocked
        ):
            adj = self.rooms.get(CEELO_ROOM_ID)
            return CommandResult(
                message=f"You peer west toward {adj.name}.\nYou spot:\n  - Curt",
                broadcast=f"ROOM:{room.id}:{player.name} peers west.",
            )

        if direction not in room.exits:
            return CommandResult(f"You can't look {direction} from here.")

        adj = self.rooms.get(room.exits[direction])
        if not adj:
            return CommandResult(f"You peer {direction}, but see nothing.")

        lines = [f"You peer {direction} toward {adj.name}."]

        if adj.items:
            lines.append("You can make out:")
            for item in adj.items:
                lines.append(f"  - {item.display_name}")

        also_here = list(adj.npcs)
        also_here.extend(sorted(adj.players))
        if also_here:
            label = "Also there:" if adj.items else "You spot:"
            lines.append(label)
            for name in also_here:
                lines.append(f"  - {name}")

        if not adj.items and not also_here:
            lines.append("You don't see anyone or anything of note.")

        return CommandResult(
            message="\n".join(lines),
            broadcast=f"ROOM:{room.id}:{player.name} peers {direction}.",
        )
    
    def cmd_go(self, player: Player, direction: str) -> CommandResult:
        """Move in a direction."""
        direction = DIRECTION_ALIASES.get(direction.lower(), direction.lower())
        room = self.rooms.get(player.current_room)
        
        if not room:
            return CommandResult("You can't move - you're nowhere!")

        if room.id == "jail":
            if player.is_jail_locked():
                remaining = player.get_jail_remaining()
                return CommandResult(
                    f"The cell door won't budge. You still have {remaining} second"
                    f"{'s' if remaining != 1 else ''} left on your sentence."
                )
            new_room_id = room.exits.get("out")
            if not new_room_id:
                return CommandResult("The cell door is open, but there's nowhere to go.")
        elif room.id == "slick_store" and direction == "west":
            if not player.ceelo_access_unlocked:
                return CommandResult("You can't go west from here.")
            if player.ceelo_kicked_visit_id == player.slick_visit_id:
                return CommandResult(
                    'Slick blocks the Employees Only door. "Curt says you\'re '
                    'done spectating this visit. Take a walk."'
                )
            new_room_id = CEELO_ROOM_ID
        else:
            new_room_id = room.exits.get(direction)

            if direction not in room.exits:
                return CommandResult(f"You can't go {direction} from here.")

        # Check destination
        # Check if trying to enter Slick's while banned
        if new_room_id in {"slick_store", CEELO_ROOM_ID} and player.is_banned_from_slicks():
            remaining = player.get_slick_ban_remaining()
            minutes = remaining // 60
            seconds = remaining % 60
            
            slick_blocked = [
                f"You try to enter but Slick spots you through the window. \"Not so fast! Come back in {minutes}m {seconds}s!\"",
                f"The door won't budge. You hear Slick yell: \"You're still banned! {minutes}m {seconds}s left!\"",
                f"Slick appears at the door, blocking your way. \"Nope! Cool off for another {minutes}m {seconds}s!\"",
            ]
            return CommandResult(random.choice(slick_blocked))
        
        # Leave current room
        old_room = room
        old_room.players.discard(player.name)
        
        # Enter new room
        new_room = self.rooms.get(new_room_id)
        player.current_room = new_room_id
        new_room.players.add(player.name)

        slick_rooms = {"slick_store", CEELO_ROOM_ID}
        if old_room.id in slick_rooms and new_room_id not in slick_rooms:
            player.clear_slick_visit()
        elif new_room_id in slick_rooms and old_room.id not in slick_rooms:
            player.begin_slick_visit()

        invitation = ""
        if new_room_id == "slick_store" and self._maybe_unlock_ceelo_access(player):
            invitation = (
                '\n\nSlick eyes you, then jerks his thumb toward an Employees Only '
                'door to the west. "You look like you can handle Curt\'s table."'
            )
        
        # Build response
        leave_msg = f"{player.name} heads {direction}."
        arrive_msg = f"{player.name} arrives."
        
        result = CommandResult(
            message=self._room_description(player, new_room) + invitation,
            broadcast=f"ROOM:{old_room.id}:{leave_msg}|ROOM:{new_room_id}:{arrive_msg}"
        )
        return result
    
    def cmd_get(self, player: Player, item_name: str) -> CommandResult:
        """Pick up an item from the room, or all takeable items with 'all'."""
        if not item_name:
            return CommandResult("Get what?")
        
        room = self.rooms.get(player.current_room)
        item_name = item_name.lower().strip()

        if self._args_name_truck(item_name):
            return self.cmd_take_from_truck(player, item_name)

        if item_name in ("all", "*"):
            return self._get_all_items(player, room)
        
        # Find item in room
        for item in room.items:
            if item.matches(item_name):
                if not item.takeable:
                    return CommandResult(f"You can't take the {item.display_name}.")
                
                room.items.remove(item)
                msg = player.add_item(item)
                return CommandResult(
                    message=msg,
                    broadcast=f"ROOM:{room.id}:{player.name} picks up {item.display_name}."
                )
        
        return CommandResult(f"You don't see a '{item_name}' here.")

    def _get_all_items(self, player: Player, room) -> CommandResult:
        """Pick up every takeable item on the ground."""
        if not room.items:
            return CommandResult("There's nothing here to pick up.")

        taken = []
        left = []
        for item in list(room.items):
            if not item.takeable:
                left.append(item)
                continue
            room.items.remove(item)
            player.add_item(item)
            taken.append(item)

        if not taken:
            return CommandResult("You can't take anything here.")

        lines = ["You pick up everything you can:"]
        for item in taken:
            lines.append(f"  - {item.display_name}")
        if left:
            lines.append("Left behind:")
            for item in left:
                lines.append(f"  - {item.display_name}")

        if len(taken) == 1:
            broadcast = (
                f"ROOM:{room.id}:{player.name} picks up {taken[0].display_name}."
            )
        else:
            broadcast = (
                f"ROOM:{room.id}:{player.name} gathers up the items on the ground."
            )
        return CommandResult(message="\n".join(lines), broadcast=broadcast)
    
    def cmd_drop(self, player: Player, item_name: str) -> CommandResult:
        """Drop an item from inventory."""
        if not item_name:
            return CommandResult("Drop what?")
        
        item = player.find_item(item_name)
        if not item:
            return CommandResult(f"You're not carrying a '{item_name}'.")
        
        room = self.rooms.get(player.current_room)
        if is_ancient_fish_id(item.id):
            player.remove_item(item)
            if self.lake_state:
                self.lake_state.release(item.id)
            message = (
                f"The {item.name} slaps its tail wildly and flips itself "
                "toward the water. It's GONE!!"
            )
            return CommandResult(
                message=message,
                broadcast=f"ROOM:{room.id}:{message}",
            )

        player.remove_item(item)
        room.items.append(item)
        
        return CommandResult(
            message=f"You drop the {item.display_name}.",
            broadcast=f"ROOM:{room.id}:{player.name} drops {item.display_name}."
        )

    def _is_truck_name(self, text: str) -> bool:
        cleaned = (text or "").strip().lower()
        return cleaned in {
            "truck", "trucks", "pickup", "your truck", "the truck", "my truck",
        }

    def _args_name_truck(self, args: str) -> bool:
        tokens = (args or "").strip().lower().split()
        if not tokens:
            return False
        if tokens[-1] in ("truck", "trucks", "pickup"):
            return True
        if len(tokens) >= 2 and tokens[-2] in ("in", "into", "from", "to") and tokens[-1] in (
            "truck", "trucks", "pickup",
        ):
            return True
        return False

    def _item_query_from_truck_args(self, args: str) -> str:
        tokens = (args or "").strip().lower().split()
        if not tokens:
            return ""
        if len(tokens) >= 2 and tokens[-2] in ("in", "into", "from", "to") and tokens[-1] in (
            "truck", "trucks", "pickup",
        ):
            return " ".join(tokens[:-2]).strip()
        if tokens[-1] in ("truck", "trucks", "pickup"):
            return " ".join(tokens[:-1]).strip()
        return " ".join(tokens).strip()

    def _maybe_refuse_truck_board(self, player: Player, args: str) -> Optional[CommandResult]:
        if player.current_room != TRUCK_ROOM_ID:
            return None
        cleaned = (args or "").strip().lower()
        if not cleaned or self._is_truck_name(cleaned) or cleaned in ("it",):
            return CommandResult("You aren't quite ready to leave yet...")
        return None

    def _shard_combine_note(self, before: List[Item], after: List[Item]) -> str:
        before_gems = sum(1 for item in before if item.item_type == ItemType.GEM)
        after_gems = sum(1 for item in after if item.item_type == ItemType.GEM)
        if after_gems > before_gems:
            return " Same-color shards combine."
        if any(
            item.item_type == ItemType.SHARD for item in before
        ) and any(
            item.item_type == ItemType.SHARD for item in after
        ):
            before_shards = [
                (item.gem_attribute, item.shard_progress)
                for item in before if item.item_type == ItemType.SHARD
            ]
            after_shards = [
                (item.gem_attribute, item.shard_progress)
                for item in after if item.item_type == ItemType.SHARD
            ]
            if before_shards != after_shards:
                return " Same-color shards combine."
        return ""

    def cmd_truck(self, player: Player, args: str) -> CommandResult:
        """List the contents of the player's truck."""
        if player.current_room != TRUCK_ROOM_ID:
            return CommandResult("You don't see your truck here.")
        return CommandResult(player.get_truck_display())

    def _truck_put_candidates(self, player: Player) -> Tuple[List[Item], List[Item], List[Item]]:
        """Split inventory into storable items, worn/equipped, and ancients."""
        storable: List[Item] = []
        equipped: List[Item] = []
        ancients: List[Item] = []
        for item in list(player.inventory):
            if player.is_wearing_or_equipped(item):
                equipped.append(item)
            elif is_ancient_fish_id(item.id):
                ancients.append(item)
            else:
                storable.append(item)
        return storable, equipped, ancients

    def _put_all_in_truck(self, player: Player) -> CommandResult:
        """Store every eligible inventory item that fits after shard merge."""
        candidates, equipped, ancients = self._truck_put_candidates(player)
        if not candidates and not equipped and not ancients:
            return CommandResult("You're not carrying anything to store.")
        if not candidates:
            lines = []
            if equipped:
                lines.append("You should unequip that before storing it.")
            if ancients:
                lines.append("Those fish will not stay in the truck. They want the water.")
            return CommandResult("\n".join(lines))

        stored: List[Item] = []
        leftover: List[Item] = []
        truck = list(player.truck_storage)
        for item in candidates:
            preview = normalize_container_shards(truck + [item])
            if len(preview) > TRUCK_CAPACITY:
                leftover.append(item)
                continue
            truck = preview
            stored.append(item)

        if not stored:
            return CommandResult(
                f"The truck is full. ({len(player.truck_storage)}/{TRUCK_CAPACITY})"
            )

        before = list(player.truck_storage)
        moving = list(stored)
        for item in stored:
            player.remove_item(item)
        player.truck_storage = truck
        note = self._shard_combine_note(before + moving, truck)

        lines = ["You put everything you can in your truck:"]
        for item in stored:
            lines.append(f"  - {item.display_name}")
        if leftover:
            lines.append(
                f"The truck is full. ({len(player.truck_storage)}/{TRUCK_CAPACITY})"
            )
            lines.append("Left in your hands:")
            for item in leftover:
                lines.append(f"  - {item.display_name}")
        if equipped:
            lines.append("Left equipped:")
            for item in equipped:
                lines.append(f"  - {item.display_name}")
        if ancients:
            lines.append("Those fish will not stay in the truck:")
            for item in ancients:
                lines.append(f"  - {item.display_name}")
        if note:
            lines.append(note.strip())
        lines.append(player.get_truck_display())
        return CommandResult("\n".join(lines))

    def _take_all_from_truck(self, player: Player) -> CommandResult:
        """Move every stored truck item into inventory."""
        if not player.truck_storage:
            return CommandResult("Your truck is empty.")
        taken = list(player.truck_storage)
        before = list(player.inventory)
        player.truck_storage = []
        player.inventory.extend(taken)
        player.inventory = normalize_container_shards(player.inventory)
        note = self._shard_combine_note(before + taken, player.inventory)
        lines = ["You take everything from your truck:"]
        for item in taken:
            lines.append(f"  - {item.display_name}")
        if note:
            lines.append(note.strip())
        return CommandResult("\n".join(lines))

    def cmd_put(self, player: Player, args: str) -> CommandResult:
        """Store an item in the truck: put <item> truck / put all truck."""
        if not args.strip():
            return CommandResult("Put what where?")
        if not self._args_name_truck(args) and not self._is_truck_name(args):
            return CommandResult("Put it where? Try: put <item> truck")
        if player.current_room != TRUCK_ROOM_ID:
            return CommandResult("You don't see your truck here.")
        query = self._item_query_from_truck_args(args)
        if not query:
            return CommandResult("Put what in the truck?")
        if query in ("all", "*"):
            return self._put_all_in_truck(player)
        item = player.find_item(query)
        if not item:
            return CommandResult(f"You're not carrying a '{query}'.")
        if player.is_wearing_or_equipped(item):
            return CommandResult("You should unequip that before storing it.")
        if is_ancient_fish_id(item.id):
            return CommandResult(
                "That fish will not stay in the truck. It wants the water."
            )
        preview = normalize_container_shards(list(player.truck_storage) + [item])
        if len(preview) > TRUCK_CAPACITY:
            return CommandResult(
                f"The truck is full. ({len(player.truck_storage)}/{TRUCK_CAPACITY})"
            )
        before = list(player.truck_storage)
        player.remove_item(item)
        player.truck_storage = preview
        note = self._shard_combine_note(before + [item], preview)
        extra = player.get_truck_display()
        return CommandResult(
            f"You put the {item.display_name} in your truck.{note}\n{extra}"
        )

    def cmd_take_from_truck(self, player: Player, args: str) -> CommandResult:
        """Retrieve an item from the truck: take <item> truck / take all truck."""
        if player.current_room != TRUCK_ROOM_ID:
            return CommandResult("You don't see your truck here.")
        query = self._item_query_from_truck_args(args)
        if not query:
            return CommandResult("Take what from the truck?")
        if query in ("all", "*"):
            return self._take_all_from_truck(player)
        item = player.find_truck_item(query)
        if not item:
            return CommandResult(f"There's no '{query}' in your truck.")
        player.truck_storage.remove(item)
        before = list(player.inventory)
        player.inventory.append(item)
        player.inventory = normalize_container_shards(player.inventory)
        note = self._shard_combine_note(before + [item], player.inventory)
        return CommandResult(
            f"You take the {item.display_name} from your truck.{note}"
        )

    def cmd_use(self, player: Player, item_name: str) -> CommandResult:
        """Use a carried consumable, or feed a fish into a specialty lure."""
        if not item_name:
            return CommandResult("Use what?")
        lure, fish = self._find_lure_and_fish(player, item_name)
        if lure and fish:
            return self._feed_specialty_lure(player, lure, fish)
        item = player.find_item(item_name)
        if not item:
            return CommandResult(f"You're not carrying a '{item_name}'.")
        if item.id == "glowing_lure":
            return self._salvage_glowing_lure(player, item)
        if item.id == "bucket_of_chum":
            return self.cmd_chum(player, item_name)
        if item.is_specialty_lure():
            return CommandResult(
                "Hand Cliff a fish for this jig: use <jig> <fish> "
                "(or give <fish> to cliff)."
            )
        if item.item_type == ItemType.FISH:
            lure = self._first_specialty_lure(player)
            if lure:
                return self._feed_specialty_lure(player, lure, item)
            return CommandResult(
                "You need a custom jig before Cliff can dress a fish for you."
            )
        return CommandResult(f"You can't find a use for {item.display_name} here.")

    def cmd_salvage(self, player: Player, item_name: str) -> CommandResult:
        """Have Cliff salvage a mouth-found glowing lure into colored shards."""
        if not item_name:
            return CommandResult("Salvage what?")
        item = player.find_item(item_name)
        if not item:
            return CommandResult(f"You're not carrying a '{item_name}'.")
        return self._salvage_glowing_lure(player, item)

    def _salvage_glowing_lure(
        self, player: Player, lure: Item
    ) -> CommandResult:
        """Destroy a glowing lure and add 20% to each matching shard."""
        if player.current_room != "bubba_workshop":
            return CommandResult(
                "Cliff salvages glowing lures in the workshop west of "
                "Bubba's store porch."
            )
        if lure.id != "glowing_lure":
            return CommandResult(
                'Cliff shakes his head. "Only those glowing lures from a '
                'fish carry anything I can salvage."'
            )
        if player.is_wearing_or_equipped(lure):
            return CommandResult(
                "Remove the glowing lure from your line before Cliff salvages it."
            )

        attributes = list(dict.fromkeys(
            attr for attr, value in lure.modifiers
            if attr and value > 0
        ))
        if not attributes:
            return CommandResult(
                'Cliff turns the lure over. "No light left in this one."'
            )

        player.remove_item(lure)
        results = []
        for attribute in attributes:
            shard = next(
                (
                    item for item in player.inventory
                    if item.item_type == ItemType.SHARD
                    and item.gem_attribute == attribute
                ),
                None,
            )
            old_progress = shard.shard_progress if shard else 0
            new_progress = old_progress + 20
            color = attribute_color_name(attribute)
            if new_progress >= 100:
                if shard:
                    player.remove_item(shard)
                gem = create_gem(attribute)
                player.add_item(gem)
                results.append(
                    f"The {color} shard reaches 100% and hardens into "
                    f"a {gem.display_name}!"
                )
            else:
                if shard:
                    shard.shard_progress = new_progress
                    results.append(
                        f"The {color} shard grows to {new_progress}%."
                    )
                else:
                    shard = create_shard(attribute, new_progress)
                    player.add_item(shard)
                    results.append(f"A {shard.display_name} forms.")

        return CommandResult(
            message=(
                "Cliff cracks the glowing lure over a shallow metal dish. "
                "Its colored light splinters into crystal.\n"
                + "\n".join(results)
            ),
            broadcast=(
                f"ROOM:{player.current_room}:{player.name} hands Cliff a "
                "glowing lure. He cracks it over a metal dish."
            ),
        )

    def cmd_feed(self, player: Player, args: str) -> CommandResult:
        """Sacrifice a fish to attune or strengthen a specialty lure."""
        if not args:
            return CommandResult(
                "Feed what? Usage: feed <jig> <fish> (or give <fish> to cliff)"
            )
        lure, fish = self._find_lure_and_fish(player, args)
        if lure and fish:
            return self._feed_specialty_lure(player, lure, fish)
        fish = self._find_typed_item(player, args, item_type=ItemType.FISH)
        if fish and fish.item_type == ItemType.FISH:
            lure = self._first_specialty_lure(player)
            if lure:
                return self._feed_specialty_lure(player, lure, fish)
            return CommandResult(
                "You need a custom jig before Cliff can dress a fish for you."
            )
        lure = self._find_typed_item(player, args, specialty_lure=True)
        if lure and lure.is_specialty_lure():
            return CommandResult(
                "Hand Cliff a fish for this jig: feed <jig> <fish>."
            )
        return CommandResult(
            "Hand Cliff a fish for a custom jig: feed <jig> <fish>."
        )

    @staticmethod
    def _first_specialty_lure(player: Player) -> Optional[Item]:
        for item in player.inventory:
            if item.is_specialty_lure():
                return item
        return None

    @staticmethod
    def _find_typed_item(
        player: Player,
        query: str,
        item_type: Optional[ItemType] = None,
        specialty_lure: bool = False,
    ) -> Optional[Item]:
        """Find an inventory item by name/number, optionally restricted by type."""
        query = query.strip()
        if not query:
            return None
        numbered = query.rstrip(")")
        candidates = (
            [player.find_item(numbered)]
            if numbered.isdigit()
            else player.inventory
        )
        for item in candidates:
            if item is None:
                continue
            if not numbered.isdigit() and not item.matches(query):
                continue
            if specialty_lure and not item.is_specialty_lure():
                continue
            if item_type is not None and item.item_type != item_type:
                continue
            return item
        return None

    def _find_lure_and_fish(
        self, player: Player, args: str
    ) -> tuple[Optional[Item], Optional[Item]]:
        """Parse two inventory items and return (specialty lure, fish)."""
        text = args.strip()
        if not text:
            return None, None
        lowered = text.lower()
        pairs: List[tuple[str, str]] = []
        for sep in (" with ", " on ", " into ", " to "):
            if sep in lowered:
                idx = lowered.find(sep)
                pairs.append((text[:idx].strip(), text[idx + len(sep):].strip()))
        parts = text.split()
        for i in range(1, len(parts)):
            pairs.append((" ".join(parts[:i]), " ".join(parts[i:])))
        seen = set()
        for left, right in pairs:
            key = (left.lower(), right.lower())
            if not left or not right or key in seen:
                continue
            seen.add(key)
            first = self._find_typed_item(
                player, left, specialty_lure=True
            ) or self._find_typed_item(player, left, item_type=ItemType.FISH)
            second = self._find_typed_item(
                player, right, item_type=ItemType.FISH
            ) or self._find_typed_item(player, right, specialty_lure=True)
            if not first or not second or first is second:
                continue
            lure = first if first.is_specialty_lure() else second
            fish = second if first.is_specialty_lure() else first
            if lure.is_specialty_lure() and fish.item_type == ItemType.FISH:
                return lure, fish
        return None, None

    def _feed_specialty_lure(
        self, player: Player, lure: Item, fish: Item
    ) -> CommandResult:
        """Lock or strengthen a custom jig with Cliff's help."""
        if not lure.is_specialty_lure():
            return CommandResult(
                "Only a blank jig from Bubba's kit can be dressed here."
            )
        if player.current_room != "bubba_workshop":
            return CommandResult(
                "Cliff works jigs in the workshop west of the store porch. "
                "That's where you hand him a fish."
            )
        if fish.item_type != ItemType.FISH:
            return CommandResult("Cliff only wants a fish.")
        if fish.id in LURE_FORBIDDEN_SPECIES or is_ancient_fish_id(fish.id):
            return CommandResult(
                'Cliff backs up a step. "I don\'t put that one on a jig. '
                'You keep it."'
            )
        if lure.attracts_fish_id and fish.id != lure.attracts_fish_id:
            species = self._fish_species_by_id(lure.attracts_fish_id)
            wanted = species.name if species else lure.attracts_fish_id
            return CommandResult(
                f'Cliff turns the {fish.plain_display_name} in his hand and '
                f'shakes his head. "This jig\'s already a {wanted} pattern. '
                'Bring me that, or start a new blank."'
            )
        if lure.species_attraction_multiplier() >= SPECIALTY_LURE_MAX_MULT:
            return CommandResult(
                'Cliff glances at the jig. "That one\'s wearing all it can. '
                'You keep the fish."'
            )

        first_attune = lure.attracts_fish_id is None
        species = self._fish_species_by_id(fish.id) or fish
        gained = specialty_lure_essence_from_fish(fish, species)
        player.remove_item(fish)
        lure.attracts_fish_id = fish.id
        lure.lure_essence = round(lure.lure_essence + gained, 2)
        lure.name = attuned_lure_name(species)
        lure.description = attuned_lure_description(species)
        multiplier = lure.species_attraction_multiplier()
        component = random.choice(JIG_COMPONENTS)
        article = "an" if component[0].lower() in "aeiou" else "a"
        quote = random.choice(CLIFF_COMPONENT_LINES)
        if first_attune:
            finish = (
                f"You bind the {component} onto the blank jig. "
                f"It settles into a {species.name} pattern. "
                f"(Now {multiplier:.2f}× for that species.)"
            )
        else:
            finish = (
                f"You bind the {component} onto your {lure.plain_display_name}. "
                f"The {species.name} pattern looks sharper. "
                f"(Now {multiplier:.2f}×, {lure.lure_essence:.1f} typical "
                f"{species.name}.)"
            )
        return CommandResult(
            message=(
                f"You hand Cliff the {fish.plain_display_name}.\n"
                f"He turns it toward the window, then fishes {article} "
                f"{component} out of a cluttered tin.\n"
                f'Cliff says, "{quote}"\n'
                f"{finish}"
            ),
            broadcast=(
                f"ROOM:{player.current_room}:{player.name} hands Cliff a fish. "
                f"Cliff passes back {article} {component}."
            ),
        )

    def _fish_species_by_id(self, fish_id: str) -> Optional[Item]:
        for fish, _ in CATCHABLE_FISH:
            if fish.id == fish_id:
                return fish
        return None

    def cmd_chum(self, player: Player, item_name: str) -> CommandResult:
        """Dump one bucket of chum into the current fishing spot."""
        query = item_name.strip() or "chum"
        item = player.find_item(query)
        if not item or item.id != "bucket_of_chum":
            return CommandResult("You need a bucket of chum to do that.")

        room = self.rooms.get(player.current_room)
        if not room or not room.is_water:
            return CommandResult(
                "Save that for a fishing spot—dumping chum here would be a waste."
            )

        gain = room.apply_chum(10)
        if gain <= 0:
            return CommandResult(
                "This spot is already teeming with fish. You keep the chum sealed."
            )

        player.remove_item(item)
        return CommandResult(
            message=(
                "You dump the bucket of chum into the water. An oily slick "
                "spreads across the surface, drawing in more fish."
            ),
            broadcast=(
                f"ROOM:{room.id}:{player.name} dumps a bucket of chum into "
                "the water. An oily slick spreads across the surface."
            ),
        )

    _RELEASE_FORBIDDEN_IDS = frozenset({"legendary_carp", "ancient_whiskers"})

    @staticmethod
    def _release_population_gain(fish: Item) -> int:
        """10–20 from quality and weight. Weight above 10 lb does not help."""
        condition = max(0, min(9, fish.condition))
        weight = max(0.0, float(fish.weight or 0.0))
        return min(20, 10 + int(condition * 0.5 + min(weight, 10.0) * 0.5 + 0.5))

    @staticmethod
    def _release_constitution_needed(population: int) -> int:
        """Deeper, colder wades as the water gets livelier."""
        if population <= 50:
            return 0
        if population <= 60:
            return 3
        if population <= 70:
            return 5
        if population <= 80:
            return 7
        if population <= 90:
            return 9
        return 11

    def cmd_release(self, player: Player, item_name: str) -> CommandResult:
        """Pay the lake by releasing a fish; the commotion draws others."""
        if not item_name.strip():
            return CommandResult("Release what?")
        fish = self._find_typed_item(
            player, item_name, item_type=ItemType.FISH
        ) or player.find_item(item_name)
        if not fish or fish.item_type != ItemType.FISH:
            query = item_name.strip().rstrip(")")
            if query.isdigit():
                return CommandResult(
                    f"You don't have an item numbered {query} in your inventory."
                )
            return CommandResult(f"You're not carrying a '{item_name}'.")

        room = self.rooms.get(player.current_room)
        if not room or not room.is_water:
            return CommandResult("You need to be at the water to pay the lake.")

        if (
            fish.id in self._RELEASE_FORBIDDEN_IDS
            or is_ancient_fish_id(fish.id)
        ):
            return CommandResult(
                f"{fish.name} isn't yours to spend."
            )

        if player.last_fish_release_cycle == self.population_cycle:
            return CommandResult(
                "The water is still settling from your last release."
            )

        population = room.population or 0
        if population >= 100:
            return CommandResult(
                "This water is already teeming. You keep the fish."
            )

        needed = self._release_constitution_needed(population)
        constitution = player.get_effective_attribute("constitution")
        if constitution < needed:
            return CommandResult(
                "The water's too lively. You haven't the constitution to "
                f"wade out far enough for a real release. (Need {needed} CON.)"
            )

        gain = self._release_population_gain(fish)
        room.population = min(100, population + gain)
        player.remove_item(fish)
        player.last_fish_release_cycle = self.population_cycle

        if population <= 50:
            action = (
                f"You pay the lake with a {fish.display_name}. You wade the "
                "shallows and turn it loose. It tears off through the open "
                "water, and others come to look."
            )
        else:
            action = (
                f"You wade deeper into the cold, wrestle a {fish.display_name} "
                "through the boil, and turn it loose. The panic runs through "
                "the school."
            )
        if fish.id == "sting_puffer":
            action += " It puffs once and bolts."
        return CommandResult(
            message=action,
            broadcast=(
                f"ROOM:{room.id}:{player.name} releases a fish back into the "
                "water. It tears off in a panic."
            ),
        )
    
    def cmd_inventory(self, player: Player, args: str) -> CommandResult:
        """Show player inventory, optionally filtered (e.g. inv hat)."""
        query = (args or "").strip().lower()
        if query in ("sort fish", "sortfish"):
            message = player.sort_inventory_fish()
            if message.startswith("You aren't"):
                return CommandResult(message)
            return CommandResult(
                message + "\n" + player.get_inventory_display()
            )
        return CommandResult(player.get_inventory_display(args))

    def release_unique_fish(self, player: Player) -> bool:
        """Return all held or in-flight ancient fish to their catch pools."""
        held = [
            item for item in list(player.inventory) + list(player.truck_storage)
            if is_ancient_fish_id(item.id)
        ]
        reserved_ids = set(player.reserved_unique_fish_ids)
        if player.ancient_whiskers_reserved:
            reserved_ids.add("ancient_whiskers")
        for item in held:
            if item in player.inventory:
                player.remove_item(item)
            elif item in player.truck_storage:
                player.truck_storage.remove(item)
        player.ancient_whiskers_reserved = False
        player.reserved_unique_fish_ids.clear()
        if self.lake_state:
            for item_id in {item.id for item in held} | reserved_ids:
                self.lake_state.release(item_id)
        return bool(held or reserved_ids)

    def release_ancient_whiskers(self, player: Player) -> bool:
        """Backward-compatible name for returning any ancient fish."""
        return self.release_unique_fish(player)
    
    def cmd_examine(self, player: Player, target: str) -> CommandResult:
        """Examine an item or object."""
        if not target:
            return CommandResult("Examine what?")
        
        target = target.lower().strip()
        number_query = target.rstrip(")")

        # Inventory slot numbers: examine 1 / ex 2)
        if number_query.isdigit():
            item = player.find_item(number_query)
            if item:
                return CommandResult(self._format_item_examine(item))
            return CommandResult(
                f"You don't have an item numbered {number_query} in your inventory."
            )
        
        # Check inventory first
        item = player.find_item(target)
        if item:
            return CommandResult(self._format_item_examine(item))
        truck_item = player.find_truck_item(target)
        if truck_item and player.current_room == TRUCK_ROOM_ID:
            return CommandResult(self._format_item_examine(truck_item))
        if self._is_truck_name(target):
            return self.cmd_truck(player, "")
        
        # Check room items
        room = self.rooms.get(player.current_room)
        for item in room.items:
            if item.matches(target):
                return CommandResult(self._format_item_examine(item))
        
        # Check for other players
        for p in self.player_manager.get_players_in_room(player.current_room):
            if target == p.name.lower():
                desc = f"\n{p.name.upper()}\nA fellow angler."
                desc += p.get_equipment_display()
                desc += f"\nFish caught: {p.fish_caught}"
                return CommandResult(desc)

        # Check for NPCs in the room
        if self.fishermen:
            fisherman = self.fishermen.in_room(room.id)
            if fisherman and fisherman.matches_target(target):
                return CommandResult(
                    f"\n{fisherman.display.upper()}\n"
                    f"{fisherman.description}"
                )
        for npc in room.npcs:
            if target == npc.lower():
                desc = NPC_DESCRIPTIONS.get(
                    npc.lower(), f"\n{npc.upper()}\nA local."
                )
                if npc.lower() == "bubba" and self.market:
                    quest_lines = self.market.get_bubba_quest_status_lines()
                    if quest_lines:
                        desc += "\n\n" + "\n".join(quest_lines)
                return CommandResult(desc)
        
        return CommandResult(f"You don't see '{target}' here.")

    def _find_attack_target(self, player: Player, target: str):
        """
        Find a player or NPC in the room matching target.
        Returns ('player'|'npc', name) or None.
        """
        if not target:
            return None
        target = target.lower().strip()
        room = self.rooms.get(player.current_room)
        if not room:
            return None

        for p in self.player_manager.get_players_in_room(player.current_room):
            if p.name.lower() == target and p.name != player.name:
                return ("player", p.name)

        for npc in room.npcs:
            if npc.lower() == target:
                return ("npc", npc)

        if self.fishermen:
            local = self.fishermen.in_room(room.id)
            if local and local.matches_target(target):
                return ("npc", local.display)

        return None

    def cmd_punch(self, player: Player, target: str) -> CommandResult:
        """Secret violence command — attacking anyone gets you jailed."""
        if not target:
            return CommandResult("Punch who?")
        found = self._find_attack_target(player, target)
        if not found:
            return CommandResult(f"You don't see '{target}' here to punch.")
        kind, name = found
        return self.send_to_jail(
            player,
            f"You haul off and punch {name}! The sheriff tackles you mid-swing "
            f"and drags you straight to jail."
        )

    def cmd_kick(self, player: Player, target: str) -> CommandResult:
        """Secret violence command — attacking anyone gets you jailed."""
        if not target:
            return CommandResult("Kick who?")
        found = self._find_attack_target(player, target)
        if not found:
            return CommandResult(f"You don't see '{target}' here to kick.")
        kind, name = found
        return self.send_to_jail(
            player,
            f"You try to kick {name}! Before your boot connects, the sheriff "
            f"grabs you and hauls you off to jail."
        )

    def _format_item_examine(self, item: Item) -> str:
        """Format examine text for an item including condition and modifiers."""
        lines = [
            f"\n{item.plain_display_name.upper()}",
            item.description,
        ]
        if not is_ancient_fish_id(item.id):
            heading = "Quality" if item.item_type == ItemType.FISH else "Condition"
            lines.append(
                f"{heading}: {item.colored_condition_name} ({item.condition}/9)"
            )
        if item.modifiers:
            for attr, val in item.modifiers:
                lines.append(
                    f"Modifier: +{val} {colorize_attribute(attr, attribute_abbrev(attr))}"
                )
        if item.item_type == ItemType.GEM and item.gem_attribute:
            attr = item.gem_attribute
            lines.append(
                f"Gem power: {colorize_attribute(attr, attribute_abbrev(attr))} "
                f"({attr})"
            )
        if item.item_type == ItemType.SHARD and item.gem_attribute:
            attr = item.gem_attribute
            lines.append(
                f"Gem progress: {item.shard_progress}% "
                f"{colorize_attribute(attr, attribute_abbrev(attr))} ({attr})"
            )
        if item.wear_slot:
            lines.append(f"Wear slot: {item.wear_slot.value}")
        if item.item_type == ItemType.FISHING_POLE:
            lines.append(f"Fishing power: {item.fishing_power}")
        if item.item_type in (ItemType.LURE, ItemType.BAIT):
            lines.append(f"Attraction: {item.attraction}")
        if item.id == "glowing_lure":
            colors = ", ".join(
                attribute_color_name(attr)
                for attr, value in item.modifiers
                if value > 0
            )
            lines.append(
                f"Salvage glow: {colors}. Cliff can turn each color into "
                "20% of a shard."
            )
        if item.is_specialty_lure():
            if item.attracts_fish_id:
                species = self._fish_species_by_id(item.attracts_fish_id)
                lines.append(
                    f"Attuned to: {species.name if species else item.attracts_fish_id}"
                )
                lines.append(
                    f"Scent strength: {item.species_attraction_multiplier():.2f}× "
                    f"({item.lure_essence:.1f} typical fish on the pattern, "
                    f"cap {SPECIALTY_LURE_MAX_MULT:.0f}×)"
                )
            else:
                lines.append(
                    "Attuned to: nothing yet. In Bubba's workshop, hand Cliff "
                    "a fish with this jig. More of the same species will "
                    "improve it. Size relative to that species matters, "
                    "not raw pounds. Equip with: wear jig."
                )
        if item.item_type == ItemType.FISH:
            lines.append(f"Weight: {item.weight} lbs")
        if item.item_type == ItemType.TOOLKIT:
            lines.append("Use: repair <item> (each repair wears the toolkit down)")
        return "\n".join(lines)
    
    def cmd_equip(self, player: Player, item_name: str) -> CommandResult:
        """Equip or wear an item, or wear all clothing into empty slots."""
        if not item_name:
            return CommandResult(player.get_equipment_display())
        if item_name.lower().strip() in ("all", "*"):
            return CommandResult(player.wear_all())
        
        item = player.find_item(item_name)
        if not item:
            return CommandResult(f"You don't have a '{item_name}'.")
        
        return CommandResult(player.equip(item))
    
    def cmd_unequip(self, player: Player, item_type: str) -> CommandResult:
        """Unequip gear, or remove all worn clothing with no args / 'all'."""
        target = item_type.strip().lower() if item_type else ""
        if not target or target in ("all", "*"):
            return CommandResult(player.unequip_all_worn())
        attribute = ATTRIBUTE_ALIASES.get(target)
        if attribute:
            return CommandResult(player.unequip_by_attribute(attribute))
        return CommandResult(player.unequip(item_type))

    def cmd_stats(self, player: Player, args: str) -> CommandResult:
        """Show character attributes."""
        return CommandResult(player.get_attributes_display())

    def cmd_meditate(self, player: Player, args: str) -> CommandResult:
        """Secret: sit with your attributes and named knacks."""
        s = player.get_effective_attribute("strength")
        d = player.get_effective_attribute("dexterity")
        c = player.get_effective_attribute("constitution")
        i = player.get_effective_attribute("intelligence")
        w = player.get_effective_attribute("wisdom")
        h = player.get_effective_attribute("charisma")
        bite = (s - 1) * 0.8 + (d - 1) * 0.8
        reel = (s - 1) * 0.8 + (c - 1) * 0.8
        reflex = max(2, d + 2)
        mental = 2 * i + w
        surge = min(75.0, max(0.0, 1.25 * (mental - 3)))
        care = max(15, int(round(100 - (i - 1) * 7 - (w - 1) * 3.5)))
        hardiness = Player.clothing_wear_chance(c)
        mending = max(5, 60 - 3 * ((i - 1) + (d - 1)))
        reading = 3 * (max(0, i - 1) + max(0, w - 1))
        forecast = min(5, (i + w) // 4)
        if h < 6:
            rapport = "stay or leave"
        elif h < 9:
            rapport = "a landmark"
        elif h < 12:
            rapport = "the water's mood"
        else:
            rapport = "the exact spot"
        if w <= 2:
            insight = "polite guesses"
        elif w <= 4:
            insight = "the market's lean"
        elif w < 8:
            insight = "price arrows"
        elif w < 12:
            insight = "percent vs usual"
        else:
            insight = "percent vs usual, in green"
        appraise = (
            "a tight look at a fish"
            if i + w >= 4
            else "too cloudy to price a catch"
        )
        lines = [
            "You sit still and take stock of yourself.",
            player.get_attributes_display(),
            "",
            "  Patience     "
            + (f"{bite:.1f}s off the wait" if bite else "no hurry yet"),
            "  Fight        "
            + (f"{reel:.1f}s off the reel" if reel else "a long pull"),
            f"  Reflex       {reflex:.0f}s to answer a run",
            f"  Surge        {surge:.0f}% chance of a lucky pull",
            f"  Care         {care}% chance tackle scuffs this cast",
            f"  Hardiness    {hardiness}% chance worn clothes scuff",
            f"  Mending      {mending:.0f}s with a toolkit",
            f"  Reading      +{reading}% to consider the water",
            f"  Appraisal    {appraise}",
            f"  Forecast     {forecast} weather step"
            + ("s" if forecast != 1 else ""),
            f"  Insight      {insight}",
            f"  Charm        +{(h - 1) * 5}% selling fish to Bubba",
            f"  Hustle       +{(h - 1) * 0.5:.1f}% at Slick's",
            f"  Rapport      fishermen offer {rapport}",
        ]
        return CommandResult("\n".join(lines))

    def _match_catchable_species(self, query: str) -> Optional[Item]:
        """Resolve a player phrase to a catchable species template."""
        q = query.lower().strip()
        if not q:
            return None
        normalized = q.replace(" ", "_")
        exact = []
        partial = []
        for fish, _ in CATCHABLE_FISH:
            name = fish.name.lower()
            if q == fish.id or normalized == fish.id or q == name:
                exact.append(fish)
            elif q in name or q in fish.id.replace("_", " "):
                partial.append(fish)
        if exact:
            return exact[0]
        if partial:
            return partial[0]
        return None

    def _ancient_record_label(self, base_id: str) -> str:
        spec = UNIQUE_FISH_SPECS.get(base_id)
        if spec:
            return colorize_ancient_fish(spec.unique_id, spec.name)
        species = self._fish_species_by_id(base_id)
        name = species.name if species else base_id.replace("_", " ")
        return colorize_ancient_fish(f"ancient_{base_id}", f"Ancient {name}")

    def _format_record_line(self, label: str, weight: float) -> str:
        pad = max(1, 32 - len(strip_ansi(label)))
        return f"  {label}{' ' * pad}{weight:.1f} lbs"

    def cmd_ponder(self, player: Player, args: str) -> CommandResult:
        """Show this player's heaviest ordinary and ancient catches."""
        if not player.best_fish and not player.best_ancient:
            return CommandResult(
                "You sit still and ponder the ones that got away. "
                "You don't have a personal best yet."
            )

        lines = ["You sit still and ponder your personal bests.", ""]
        if player.best_fish:
            lines.append("  CATCHES")
            for fish, _ in CATCHABLE_FISH:
                weight = player.best_fish.get(fish.id)
                if weight:
                    label = colorize_fish_species(fish.id, fish.name)
                    lines.append(self._format_record_line(label, weight))
            extras = [
                fish_id for fish_id in player.best_fish
                if fish_id not in {fish.id for fish, _ in CATCHABLE_FISH}
            ]
            for fish_id in extras:
                lines.append(
                    self._format_record_line(
                        fish_id.replace("_", " "),
                        player.best_fish[fish_id],
                    )
                )
            lines.append("")
        if player.best_ancient:
            lines.append("  ANCIENTS")
            for fish, _ in CATCHABLE_FISH:
                weight = player.best_ancient.get(fish.id)
                if weight:
                    lines.append(
                        self._format_record_line(
                            self._ancient_record_label(fish.id),
                            weight,
                        )
                    )
            extras = [
                fish_id for fish_id in player.best_ancient
                if fish_id not in {fish.id for fish, _ in CATCHABLE_FISH}
            ]
            for fish_id in extras:
                lines.append(
                    self._format_record_line(
                        self._ancient_record_label(fish_id),
                        player.best_ancient[fish_id],
                    )
                )
            lines.append("")
        return CommandResult("\n".join(lines).rstrip())

    def cmd_brag(self, player: Player, args: str) -> CommandResult:
        """Boast about a personal-best weight to the current room."""
        query = args.strip()
        if not query:
            return CommandResult("Brag about which fish? Try 'brag trout'.")

        want_ancient = False
        species_query = query.lower()
        if species_query == "ancient":
            want_ancient = True
            species_query = ""
        elif species_query.startswith("ancient "):
            want_ancient = True
            species_query = species_query[8:].strip()
        elif species_query in {
            "whiskers", "old whiskers", "ancient whiskers",
        }:
            want_ancient = True
            species_query = "old whiskers"

        if want_ancient:
            if not player.best_ancient:
                return self._brag_embarrassed(player)
            if species_query:
                species = self._match_catchable_species(species_query)
                if not species or species.id not in player.best_ancient:
                    return self._brag_embarrassed(player)
                base_id = species.id
                weight = player.best_ancient[base_id]
            else:
                base_id = max(
                    player.best_ancient,
                    key=lambda fish_id: player.best_ancient[fish_id],
                )
                weight = player.best_ancient[base_id]
            spec = UNIQUE_FISH_SPECS.get(base_id)
            display = spec.name if spec else f"Ancient {base_id.replace('_', ' ')}"
            colored = self._ancient_record_label(base_id)
        else:
            species = self._match_catchable_species(species_query)
            if not species or species.id not in player.best_fish:
                return self._brag_embarrassed(player)
            weight = player.best_fish[species.id]
            display = species.name
            colored = colorize_fish_species(species.id, species.name)

        return CommandResult(
            message=f"You brag about your {weight:.1f} lb {colored}.",
            broadcast=(
                f"ROOM:{player.current_room}:{player.name} brags about a "
                f"{weight:.1f} lb {display}!"
            ),
        )

    def _brag_embarrassed(self, player: Player) -> CommandResult:
        """Failed brag: you look embarrassed, and the room sees it."""
        return CommandResult(
            message="You look embarrassed.",
            broadcast=(
                f"ROOM:{player.current_room}:{player.name} looks embarrassed."
            ),
        )

    def cmd_drink(self, player: Player, args: str) -> CommandResult:
        """Secret: drink an unopened beer to permanently improve an attribute."""
        if player.beer_awaiting_attr or player.beer_confirm_attr:
            return CommandResult(
                "You're still buzzing from the last beer. "
                "Choose an attribute (str/dex/con/int/wis/cha), then yes or no."
            )

        beer = None
        query = (args or "").strip().lower()
        if query and query not in ("beer", "unopened beer", "an unopened beer"):
            # Allow drink <item> only for beer
            candidate = player.find_item(query)
            if candidate and candidate.item_type == ItemType.BEER:
                beer = candidate
            elif candidate:
                return CommandResult("That's not something you should drink.")
            else:
                return CommandResult(f"You don't have a '{args}'.")
        else:
            for item in player.get_inventory_display_order():
                if item.item_type == ItemType.BEER:
                    beer = item
                    break

        if not beer:
            return CommandResult("You don't have any beer to drink.")

        player.remove_item(beer)
        player.beer_awaiting_attr = True
        player.beer_confirm_attr = None
        return CommandResult(
            "You crack open the beer and drink it down. Inspiration washes over you.\n"
            "Which attribute will you improve?\n"
            "(str / dex / con / int / wis / cha)"
        )

    def _choose_beer_attribute(self, player: Player, attribute: str) -> CommandResult:
        """After drinking, player names an attribute — ask for confirmation."""
        current = player.attributes.get(attribute, 1)
        player.beer_awaiting_attr = False
        player.beer_confirm_attr = attribute
        colored = colorize_attribute(attribute, attribute_abbrev(attribute))
        return CommandResult(
            f"Raise your {colored} ({attribute}) from {current} to {current + 1}?\n"
            "(yes / no)"
        )

    def _confirm_beer_improve(self, player: Player, accepted: bool) -> CommandResult:
        """Confirm or cancel the beer attribute improvement."""
        attribute = player.beer_confirm_attr
        player.beer_confirm_attr = None
        player.beer_awaiting_attr = False
        if not attribute:
            return CommandResult("The moment passes.")
        if not accepted:
            return CommandResult(
                "You change your mind. The inspiration fades with a burp."
            )
        return CommandResult(player.improve_attribute(attribute))

    def cmd_gem(self, player: Player, args: str) -> CommandResult:
        """Secret: socket a gem into equipment or clothing."""
        if not args:
            return CommandResult(
                "Usage: gem <gem> <item>  (or gem <item> <gem>)"
            )

        tokens = args.split()
        if len(tokens) < 2:
            return CommandResult(
                "Usage: gem <gem> <item>  (or gem <item> <gem>)"
            )

        carried = list(player.inventory)

        def find_gem(query: str) -> Optional[Item]:
            q = query.lower().strip()
            if not q:
                return None
            for item in carried:
                if item.item_type != ItemType.GEM:
                    continue
                if item.matches(q):
                    return item
                if item.gem_attribute and (
                    q == item.gem_attribute
                    or q == attribute_abbrev(item.gem_attribute).lower()
                    or q == attribute_color_name(item.gem_attribute)
                ):
                    return item
            return None

        def find_gear(query: str) -> Optional[Item]:
            q = query.lower().strip()
            if not q:
                return None
            item = player.find_item(q)
            if item and item.item_type in GEMMABLE_TYPES:
                return item
            return None

        gem = None
        target = None
        # Prefer gem-first splits (longest gem query first)
        for i in range(len(tokens) - 1, 0, -1):
            g = find_gem(" ".join(tokens[:i]))
            t = find_gear(" ".join(tokens[i:]))
            if g and t and g is not t:
                gem, target = g, t
                break
        # Also allow item-first
        if not gem or not target:
            for i in range(1, len(tokens)):
                t = find_gear(" ".join(tokens[:i]))
                g = find_gem(" ".join(tokens[i:]))
                if g and t and g is not t:
                    gem, target = g, t
                    break

        if not gem:
            return CommandResult("You need a gem for that.")
        if not target:
            return CommandResult(
                "You can only set gems into fishing gear or clothing."
            )

        attr, new_val = target.apply_gem(gem)
        player.remove_item(gem)
        colored_attr = colorize_attribute(attr, attribute_abbrev(attr))
        return CommandResult(
            message=(
                f"You somehow crush the {gem.display_name} with your hand and rub the powder on your "
                f"{target.display_name}.\n"
                f"It glows briefly — now +{new_val} {colored_attr}."
            ),
            broadcast=(
                f"ROOM:{player.current_room}:{player.name} works a "
                f"{attribute_color_name(attr)} gem into their gear."
            ),
        )

    def _repair_duration(self, player: Player, *, hand_repair: bool = False) -> float:
        """
        Base repair time is 60s, reduced by Intelligence and Dexterity.
        Floor is 5 seconds. Hand-repair in jail takes three times as long.
        """
        intelligence = player.get_effective_attribute("intelligence")
        dexterity = player.get_effective_attribute("dexterity")
        reduction = 3 * ((intelligence - 1) + (dexterity - 1))
        duration = float(max(5, 60 - reduction))
        if hand_repair:
            duration *= 3.0
        return duration

    def _find_usable_toolkit(self, player: Player, exclude: Item = None) -> Optional[Item]:
        """Find a non-broken toolkit in inventory, optionally excluding one item."""
        for item in player.inventory:
            if item is exclude:
                continue
            if item.item_type == ItemType.TOOLKIT and not item.is_broken():
                return item
        return None

    def _iter_repairable_items(self, player: Player):
        """Worn gear, then equipped pole/lure, then carried inventory order."""
        for slot in ("head", "neck", "chest", "hands", "fingers", "legs", "feet"):
            item = player.worn_items.get(slot)
            if item and item.can_be_repaired():
                yield item
        if player.equipped_pole and player.equipped_pole.can_be_repaired():
            yield player.equipped_pole
        if player.equipped_lure and player.equipped_lure.can_be_repaired():
            yield player.equipped_lure
        for item in player.get_inventory_display_order():
            if item.can_be_repaired():
                yield item

    def _format_repairable_list(self, player: Player) -> str:
        """Show worn and carried items a toolkit could restore to new."""
        equip_lines = []
        for slot in ("head", "neck", "chest", "hands", "fingers", "legs", "feet"):
            item = player.worn_items.get(slot)
            if item and item.can_be_repaired():
                equip_lines.append(f"  {slot:<10} {item.display_name}")
        if player.equipped_pole and player.equipped_pole.can_be_repaired():
            equip_lines.append(
                f"  {'pole':<10} {player.equipped_pole.display_name}"
            )
        if player.equipped_lure and player.equipped_lure.can_be_repaired():
            equip_lines.append(
                f"  {'lure':<10} {player.equipped_lure.display_name}"
            )

        carried_lines = []
        for number, item in enumerate(player.get_inventory_display_order(), start=1):
            if item.can_be_repaired():
                carried_lines.append(f"  {number}) {item.display_name}")

        if not equip_lines and not carried_lines:
            return "Nothing you carry needs repairing."

        lines = ["\n  REPAIRABLE ITEMS", "=" * 40]
        if equip_lines:
            lines.append("\nEquipment:")
            lines.extend(equip_lines)
        if carried_lines:
            lines.append("\nCarried:")
            lines.extend(carried_lines)
        lines.append("\nType 'repair <item>', 'repair <#>', or 'repair next'.")
        return "\n".join(lines)

    def cmd_repair(self, player: Player, item_name: str) -> CommandResult:
        """Repair an item using a toolkit. Takes time based on Int/Dex."""
        if not item_name:
            return CommandResult(self._format_repairable_list(player))

        query = item_name.strip().lower()
        if query == "next":
            target = next(self._iter_repairable_items(player), None)
            if not target:
                return CommandResult("Nothing you carry needs repairing.")
        else:
            target = player.find_item(item_name)
            if not target:
                return CommandResult(f"You don't have a '{item_name}'.")

        if target.item_type == ItemType.FISH:
            return CommandResult("You can't repair a fish.")

        if target.condition >= 9:
            return CommandResult(
                f"Your {target.display_name} is already in perfect condition."
            )

        toolkit = self._find_usable_toolkit(player, exclude=target)
        jail_hand_repair = (
            toolkit is None and player.current_room == "jail"
        )
        if not toolkit and not jail_hand_repair:
            if target.item_type == ItemType.TOOLKIT:
                return CommandResult(
                    "You need another working toolkit to repair this one."
                )
            return CommandResult(
                "You need a working toolkit to repair items. "
                "They're rare — check the stores."
            )

        duration = self._repair_duration(player, hand_repair=jail_hand_repair)
        target_ref = target
        toolkit_ref = toolkit
        old_condition = target.condition
        old_label = colorize_condition(
            old_condition,
            CONDITION_NAMES.get(max(0, min(9, old_condition)), "unknown"),
        )

        def finish_repair() -> str:
            if target_ref not in player.inventory:
                return "You lost the item before you could finish repairing it."
            if toolkit_ref is not None:
                if toolkit_ref not in player.inventory or toolkit_ref.is_broken():
                    return "Your toolkit failed before the repair was finished."

            target_ref.condition = 9
            lines = [
                f"You finish repairing your {target_ref.display_name}.",
                f"(Was {old_label}, now restored to {colorize_condition(9, 'new')}.)",
            ]
            if toolkit_ref is not None:
                degrade_msg = toolkit_ref.degrade(1)
                if degrade_msg:
                    lines.append(degrade_msg)
                else:
                    lines.append(
                        f"Your {toolkit_ref.display_name} shows a little more wear."
                    )
            else:
                lines.append("You didn't have a toolkit, so it took a while.")
            return "\n".join(lines)

        seconds = int(duration)
        if jail_hand_repair:
            start = (
                f"You've got time and no toolkit, so you sit on the jail bench "
                f"and work your {target.plain_display_name} by hand...\n"
            )
        else:
            start = (
                f"You set to work on your {target.plain_display_name} with your "
                f"{toolkit.plain_display_name}...\n"
            )
        return CommandResult(
            message="",
            immediate_message=(
                f"{start}"
                f"(Repairing — about {seconds} second{'s' if seconds != 1 else ''}. "
                f"Higher Intelligence and Dexterity speed this up.)"
            ),
            delay_seconds=duration,
            deferred=finish_repair,
        )
    
    def cmd_fish(self, player: Player, args: str) -> CommandResult:
        """
        Cast, wait for a bite, then reel in.
        Bite wait uses population + Strength/Dexterity.
        Reel time uses fish weight + Strength/Constitution.
        Base reel time is 5× the prior default (cap 200s).
        """
        room = self.rooms.get(player.current_room)
        
        if not room.is_water:
            return CommandResult("You can't fish here - there's no water!")
        
        can_fish, reason = player.can_fish()
        if not can_fish:
            return CommandResult(reason)

        population = room.population or 0
        fish_modifier = 1.0
        rare_modifier = 1.0
        weather_msg = ""
        if self.weather:
            weather = self.weather.get_current_weather()
            fish_modifier = weather.fish_modifier
            rare_modifier = weather.rare_fish_modifier
            weather_msg = f" ({weather.name} weather)"

        fishing_power = player.get_fishing_power()
        if population in (0, 100):
            catch_chance = population
        else:
            catch_chance = max(0, min(100, int(population * fish_modifier)))
        caught = random.randint(1, 100) <= catch_chance

        strength = player.get_effective_attribute("strength")
        dexterity = player.get_effective_attribute("dexterity")
        constitution = player.get_effective_attribute("constitution")
        intelligence = player.get_effective_attribute("intelligence")
        wisdom = player.get_effective_attribute("wisdom")

        # Bite wait: 2x old (100-pop)/10, reduced by Str and Dex
        base_bite = round(2 * (100 - population) / 10)
        bite_reduction = (strength - 1) * 0.8 + (dexterity - 1) * 0.8
        if base_bite <= 0:
            bite_seconds = 0.0
        else:
            bite_seconds = float(max(1, round(base_bite - bite_reduction)))

        cast_message = f"You cast your line and wait...{weather_msg}"

        if not caught:
            degrade_msgs = player.degrade_fishing_gear()
            degrade_text = ("\n" + "\n".join(degrade_msgs)) if degrade_msgs else ""
            miss = random.choice([
                "Nothing takes the bait.",
                "You feel no bites and reel in an empty line.",
                "The water remains still. No fish this time.",
            ]) + degrade_text
            return CommandResult(
                message="",
                immediate_message=cast_message,
                stages=[DelayStage(bite_seconds, miss)],
            )

        caught_fish = self._select_fish(
            fishing_power, rare_modifier, player=player
        )
        fish_copy = self._create_sized_fish(caught_fish)
        weight_hint = self._fish_weight_hint(fish_copy.weight)

        # Reel wait: 5x prior default; Str/Con now shave 0.8s per point.
        base_reel = 10 * (2 + fish_copy.weight * 0.4)
        reel_reduction = (strength - 1) * 0.8 + (constitution - 1) * 0.8
        reel_seconds = float(max(1, min(200, round(base_reel - reel_reduction))))
        response_seconds = float(max(2, dexterity + 2))

        reel_hint = " pay attention" if reel_seconds >= 40 else ""
        hook_lines = [
            "A fish takes the bait — you've hooked something!",
            weight_hint,
            f"You start reeling it in...{reel_hint}",
        ]
        if player.cut_reminders_shown < 2:
            hook_lines.append(
                "(Type CUT, or press Ctrl-G, to snap the line.)"
            )
            player.cut_reminders_shown += 1
        hook_message = "\n".join(hook_lines)
        hook_broadcast = f"ROOM:{room.id}:{player.name} hooks a fish!"
        excitement = self._catch_excitement(caught_fish, fish_copy)

        def finish_catch():
            degrade_msgs = player.degrade_fishing_gear()
            degrade_text = (
                "\n" + "\n".join(degrade_msgs) if degrade_msgs else ""
            )
            player.add_item(fish_copy)
            if is_ancient_fish_id(fish_copy.id):
                player.reserved_unique_fish_ids.discard(fish_copy.id)
            if fish_copy.id == "ancient_whiskers":
                player.ancient_whiskers_reserved = False
            level_lines = player.record_fish_catch(fish_copy.weight, fish_copy)
            level_progress = (
                f"[{player.total_weight_caught:.1f}/"
                f"{player.next_level_threshold():g}]"
            )
            lines = [
                f"*SPLASH*\nYou reel in a {fish_copy.display_name}! "
                f"({fish_copy.weight} lbs) {level_progress}"
                f"{excitement}{degrade_text}"
            ]

            if fish_copy.id == "sting_puffer":
                player.apply_temp_attributes(-1, 3 * 60)
                lines.append("")
                lines.append(
                    "The sting puffer jabs you with a spine! You feel weakened "
                    "(-1 to all attributes for 3 minutes)."
                )
            elif fish_copy.id == "zen_guppy":
                player.apply_temp_attributes(2, 2 * 60)
                lines.append("")
                lines.append("Your mood improves (+2 to all attributes for 2 minutes).")

            extra_broadcast = None
            if random.randint(1, GEM_CATCH_CHANCE) == 1:
                gem = create_gem()
                player.add_item(gem)
                color = attribute_color_name(gem.gem_attribute)
                lines.append("")
                lines.append(
                    f"Something glints in the fish's mouth — a {gem.display_name}!"
                )
                lines.append("You quietly slip it into your pocket.")
                extra_broadcast = (
                    f"ROOM:{room.id}:{player.name} quietly slips something "
                    f"{color} into their pocket."
                )
            if random.randint(1, MOUTH_LURE_CATCH_CHANCE) == 1:
                lure = create_glowing_lure()
                player.add_item(lure)
                lines.append("")
                lines.append(
                    "You notice this fish had a glowing "
                    f"{colorize_condition(2, 'lure')} already hooked in its "
                    "mouth. Three colors pulse beneath its golden surface."
                )
                lines.append(
                    "You carefully remove it. Someone is probably kicking "
                    "themselves for losing this lure..."
                )
            if level_lines:
                lines.append("")
                lines.extend(level_lines)
            return "\n".join(lines), extra_broadcast

        return CommandResult(
            message="",
            immediate_message=cast_message,
            stages=[
                DelayStage(
                    bite_seconds,
                    hook_message,
                    hook_broadcast,
                ),
            ],
            reel_challenge=ReelChallenge(
                total_seconds=reel_seconds,
                response_seconds=response_seconds,
                intelligence=intelligence,
                wisdom=wisdom,
            ),
            deferred=finish_catch,
            broadcast=(
                f"ROOM:{room.id}:{self._catch_broadcast(player.name, fish_copy, excitement)}"
            ),
        )

    @staticmethod
    def _catch_excitement(species: Item, caught: Item) -> str:
        """Room-visible flourish for a landed fish."""
        if species.value >= 100 or caught.fish_size == "trophy":
            return " What an extraordinary catch!"
        if species.value >= 40 or caught.fish_size == "large":
            return " What a catch!"
        if species.value >= 20:
            return " Nice one!"
        return ""

    @staticmethod
    def _hook_broadcast(actor: str) -> str:
        return f"{actor} hooks a fish!"

    @staticmethod
    def _catch_broadcast(actor: str, fish: Item, excitement: str) -> str:
        return f"{actor} catches a {fish.weight} lb {fish.display_name}!{excitement}"

    def try_npc_catch(self, fisherman: Fisherman, room_id: str):
        """
        Occasionally land a fish as a lake NPC.

        Returns (hook_message, catch_message, reel_seconds) or None on a miss.
        Never takes an ancient fish out of the pool.
        """
        room = self.rooms.get(room_id)
        if not room or not room.is_water:
            return None

        population = room.population or 0
        fish_modifier = 1.0
        rare_modifier = 1.0
        if self.weather:
            weather = self.weather.get_current_weather()
            fish_modifier = weather.fish_modifier
            rare_modifier = weather.rare_fish_modifier
        if population in (0, 100):
            catch_chance = population
        else:
            catch_chance = max(0, min(100, int(population * fish_modifier)))
        if random.randint(1, 100) > catch_chance:
            return None

        species = self._select_fish(
            fishing_power=5,
            rare_modifier=rare_modifier,
            allow_ancient=False,
        )
        caught = self._create_sized_fish(species)
        excitement = self._catch_excitement(species, caught)
        reel_seconds = float(max(4, min(NPC_CATCH_MAX_SECONDS, round(2 + caught.weight))))
        return (
            self._hook_broadcast(fisherman.display),
            self._catch_broadcast(fisherman.display, caught, excitement),
            reel_seconds,
        )

    def _fish_weight_hint(self, weight: float) -> str:
        """Describe how heavy the hooked fish feels, with colored descriptors."""
        # (quality 0-9, descriptor, template with {d})
        if weight < 1.0:
            quality, word = 0, "weightless"
            template = "It feels almost {d} on the line."
        elif weight < 2.0:
            quality, word = 2, "light"
            template = "It feels {d} — probably a small one."
        elif weight < 4.0:
            quality, word = 4, "medium"
            template = "There's a solid tug; it feels like {d} weight."
        elif weight < 7.0:
            quality, word = 5, "heavy"
            template = "It feels {d} — this one's putting up a fight."
        elif weight < 12.0:
            quality, word = 7, "big"
            template = "Your rod bends hard; this feels like a {d} fish."
        else:
            quality, word = 9, "enormous"
            template = (
                "The line screams under the strain — this feels {d}!"
            )
        return template.format(d=colorize_condition(quality, word))
    
    def _adjusted_catch_table(
        self,
        fishing_power: int,
        rare_modifier: float,
        player: Optional[Player] = None,
    ) -> List[tuple]:
        """Weighted species table after gear, weather, and specialty lures."""
        lure = player.equipped_lure if player else None
        lure_species = (
            lure.attracts_fish_id
            if lure and lure.is_specialty_lure() and not lure.is_broken()
            else None
        )
        lure_mult = (
            lure.species_attraction_multiplier() if lure_species else 1.0
        )
        adjusted_weights = []
        for fish, weight in CATCHABLE_FISH:
            adjusted_weight = weight
            if fish.value > 30:
                adjusted_weight += fishing_power
                adjusted_weight = int(adjusted_weight * rare_modifier)
            if lure_species and fish.id == lure_species:
                adjusted_weight = int(adjusted_weight * lure_mult)
            adjusted_weights.append((fish, max(1, adjusted_weight)))
        return adjusted_weights

    def _select_fish(
        self,
        fishing_power: int,
        rare_modifier: float,
        player: Optional[Player] = None,
        allow_ancient: bool = True,
    ) -> Item:
        """Select a fish species using rarity, gear, and weather."""
        adjusted_weights = self._adjusted_catch_table(
            fishing_power, rare_modifier, player
        )

        roll = random.randint(1, sum(weight for _, weight in adjusted_weights))
        cumulative = 0
        for fish, weight in adjusted_weights:
            cumulative += weight
            if roll <= cumulative:
                if (
                    allow_ancient
                    and self.lake_state
                    and self.lake_state.is_available(fish.id)
                    and random.randint(
                        1, self.lake_state.rarity(fish.id)
                    ) == 1
                    and self.lake_state.reserve(fish.id)
                ):
                    ancient = self.lake_state.create_catch(fish.id)
                    if player:
                        player.reserved_unique_fish_ids.add(ancient.id)
                        if ancient.id == "ancient_whiskers":
                            player.ancient_whiskers_reserved = True
                    return ancient
                return fish
        return CATCHABLE_FISH[0][0]

    def _create_sized_fish(self, fish: Item) -> Item:
        """Create a fish whose rarer size changes its weight and value."""
        if is_ancient_fish_id(fish.id):
            caught = create_item_copy(fish)
            caught.fish_size = None
            caught.weight = fish.weight
            caught.value = fish.value
            caught.description = fish.description
            return caught

        size, weight_multiplier, value_multiplier = random.choices(
            [
                ("tiny", 0.55, 0.5),
                ("small", 0.78, 0.75),
                ("average", 1.0, 1.0),
                ("large", 1.35, 1.5),
                ("trophy", 1.8, 2.25),
            ],
            weights=[30, 30, 25, 12, 3],
            k=1,
        )[0]
        caught = create_item_copy(fish)
        caught.fish_size = size
        caught.weight = round(fish.weight * weight_multiplier, 1)
        caught.value = max(1, round(fish.value * value_multiplier))
        caught.description = (
            f"{fish.description} This {colorize_fish_size(size)} specimen weighs "
            f"{caught.weight} lbs."
        )
        return caught

    def cmd_consider(self, player: Player, args: str) -> CommandResult:
        """Try to estimate the fish population at a water room."""
        room = self.rooms.get(player.current_room)
        if not room.is_water:
            return CommandResult(
                "You study your surroundings, but there is no water to consider."
            )

        population = room.population or 0
        intelligence = player.get_effective_attribute("intelligence")
        wisdom = player.get_effective_attribute("wisdom")
        bonus_points = max(0, intelligence - 1) + max(0, wisdom - 1)
        # Higher populations are easier to read — more fish to notice.
        population_bonus = population // 2
        success_chance = min(95, 5 + bonus_points * 3 + population_bonus)
        broadcast = (
            f"ROOM:{room.id}:{player.name} mumbles to themselves briefly."
        )

        if random.randint(1, 100) > success_chance:
            return CommandResult(
                "You study the water, but cannot judge how many fish are present."
            )

        return CommandResult(
            self._population_message(population),
            broadcast=broadcast,
        )

    @staticmethod
    def _sense_likelihood(probability: float) -> str:
        """Describe a species' share of the ordinary catch table."""
        if probability >= 0.30:
            return "one of the strongest possibilities"
        if probability >= 0.15:
            return "common in what might take the bait"
        if probability >= 0.07:
            return "a fair possibility"
        if probability >= 0.03:
            return "possible, though not common"
        if probability >= 0.01:
            return "unlikely"
        return "very unlikely"

    @staticmethod
    def _sense_weather_notes(weather, species) -> List[str]:
        """How today's sky changes bites and rarer fish, without ancient spoilers."""
        if weather is None:
            return []
        sky = weather.name.lower()
        notes = []
        if weather.fish_modifier > 1.0:
            notes.append(
                f"The {sky} weather has fish feeding more readily."
            )
        elif weather.fish_modifier < 1.0:
            notes.append(
                f"The {sky} weather is making bites harder to come by."
            )
        if species.value > 30:
            if weather.rare_fish_modifier > 1.0:
                notes.append(
                    f"The {sky} weather is stirring the less common fish."
                )
            elif weather.rare_fish_modifier < 1.0:
                notes.append(
                    f"The {sky} weather is keeping the less common fish down."
                )
        return notes

    def cmd_sense(self, player: Player, args: str) -> CommandResult:
        """Estimate one ordinary species' share of catches under current conditions."""
        room = self.rooms.get(player.current_room)
        if not room or not room.is_water:
            return CommandResult(
                "You listen for the lake, but there is no fishing water here to read."
            )

        query = args.strip()
        if not query:
            return CommandResult("Sense which fish? Try 'sense trout'.")

        intelligence = player.get_effective_attribute("intelligence")
        wisdom = player.get_effective_attribute("wisdom")
        perception = intelligence + wisdom
        if perception < 4:
            return CommandResult(
                "You study the water, but its fish are only shadows to you. "
                "You need more Intelligence and Wisdom to make sense of them."
            )

        # Only templates in the ordinary catch table can be sensed. Unique
        # ancient rolls deliberately remain invisible to this command.
        species = self._match_catchable_species(query)
        if not species or query.lower().strip().startswith("ancient "):
            return CommandResult("You can't get a feel for that kind of fish.")

        rare_modifier = 1.0
        weather = None
        if self.weather:
            weather = self.weather.get_current_weather()
            rare_modifier = weather.rare_fish_modifier

        table = self._adjusted_catch_table(
            player.get_fishing_power(),
            rare_modifier,
            player,
        )
        total_weight = sum(weight for _, weight in table)
        species_weight = next(
            weight for fish, weight in table if fish.id == species.id
        )
        probability = species_weight / total_weight
        colored_species = colorize_fish_species(species.id, species.name)
        if weather:
            feel = (
                f"You quiet your thoughts and feel for {colored_species} "
                f"in the {weather.name.lower()} weather."
            )
        else:
            feel = f"You quiet your thoughts and feel for {colored_species}."
        lines = [
            feel,
            (
                f"They seem {self._sense_likelihood(probability)} among the "
                "fish that might take your bait."
            ),
        ]
        lines.extend(self._sense_weather_notes(weather, species))

        lure = player.equipped_lure
        if (
            lure
            and not lure.is_broken()
            and lure.is_specialty_lure()
            and lure.attracts_fish_id == species.id
        ):
            lines.append(
                f"Your {lure.plain_display_name} is drawing them toward you."
            )

        if perception >= 8:
            # Better mental stats narrow a deliberately fuzzy estimate without
            # exposing the source weights or ancient odds.
            uncertainty = max(0.10, 0.50 - 0.05 * (perception - 8))
            low = max(0.0, probability * (1.0 - uncertainty) * 100)
            high = min(100.0, probability * (1.0 + uncertainty) * 100)
            lines.append(
                f"Your best guess is somewhere around {low:.1f}%–{high:.1f}% "
                "of catches, if something bites."
            )

        return CommandResult(
            "\n".join(lines),
            broadcast=(
                f"ROOM:{room.id}:{player.name} studies the water in silence."
            ),
        )

    @staticmethod
    def _appraisal_range(value: int, score: int, *, total: bool) -> tuple[int, int]:
        """Return an INT/WIS-scaled estimate around a true sale value."""
        floor = 0.20 if total else 0.10
        base = 0.85 if total else 0.60
        uncertainty = max(floor, base - 0.04 * score)
        low = max(1, math.floor(value * (1.0 - uncertainty)))
        high = max(low, math.ceil(value * (1.0 + uncertainty)))
        return low, high

    def cmd_appraise(self, player: Player, args: str) -> CommandResult:
        """Estimate one fish or all carried fish at Bubba's current prices."""
        intelligence = player.get_effective_attribute("intelligence")
        wisdom = player.get_effective_attribute("wisdom")
        if intelligence + wisdom < 4:
            return CommandResult(
                "You attempt to appraise the value of your fish, "
                "but it could be any number..."
            )

        score = intelligence + 2 * wisdom
        query = args.strip()
        if query:
            item = player.find_item(query)
            if not item:
                return CommandResult(f"You don't have a '{query}'.")
            if item.item_type != ItemType.FISH:
                return CommandResult(
                    f"Your {item.display_name} is not a fish to appraise."
                )
            value = self._estimate_sell_price(StoreType.BUBBA, item, player)
            if value is None:
                return CommandResult("You cannot get a useful appraisal for that fish.")
            low, high = self._appraisal_range(value, score, total=False)
            return CommandResult(
                f"You estimate Bubba would pay about ${low} - ${high} "
                f"for your {item.display_name}."
            )

        fish = [item for item in player.inventory if item.item_type == ItemType.FISH]
        if not fish:
            return CommandResult("You have no fish to appraise.")
        values = [
            self._estimate_sell_price(StoreType.BUBBA, item, player)
            for item in fish
        ]
        total_value = sum(value for value in values if value is not None)
        low, high = self._appraisal_range(total_value, score, total=True)
        return CommandResult(
            f"You estimate Bubba would pay about ${low} - ${high} "
            f"for all {len(fish)} fish."
        )

    def _population_message(self, population: int) -> str:
        """Describe population in ten-percent bands with colored descriptors."""
        # (descriptor, message template with {d} for the colored word)
        bands = [
            ("lifeless", "The water seems {d}. You detect virtually no fish."),
            ("faintest", "Only the {d} signs of fish disturb the water."),
            ("sparse", "The population looks very {d}."),
            ("thin", "There are a few fish here, though they are spread {d}."),
            ("modest", "The water holds a {d} fish population."),
            ("healthy", "The fish population appears {d} and balanced."),
            ("plenty", "There seem to be {d} of fish beneath the surface."),
            ("busy", "The water is {d} with frequent signs of fish."),
            ("teeming", "This spot is {d} with fish."),
            ("exceptional", "Fish are everywhere; this is an {d} fishing spot."),
            ("packed", "The water is absolutely {d} with fish."),
        ]
        index = 10 if population >= 100 else max(0, population // 10)
        word, template = bands[index]
        # Same palette as item condition (0 red … 9 teal)
        quality = 9 if index >= 9 else index
        return template.format(d=colorize_condition(quality, word))
    
    def cmd_say(self, player: Player, message: str) -> CommandResult:
        """Say something to the room."""
        if not message:
            return CommandResult("Say what?")
        
        room = self.rooms.get(player.current_room)
        lines = [f'You say, "{message}"']
        broadcasts = [f'ROOM:{room.id}:{player.name} says, "{message}"']

        greets_norm = (
            any(npc.lower() == "norm" for npc in room.npcs)
            and re.search(r"\b(?:hello|hi|hey)\b", message, re.IGNORECASE)
            and re.search(r"\bnorm\b", message, re.IGNORECASE)
        )
        if greets_norm:
            reply = self._start_or_resume_norm_round(player)
            lines.append(f'Norm says, "{reply}"')
            broadcasts.append(f'ROOM:{room.id}:Norm says, "{reply}"')

        if self.fishermen:
            local = self.fishermen.in_room(room.id)
            named = self.fishermen.named_in(message)
            if local and named:
                state = self.fishermen.begin_interaction(player.name, local)
                if state == "active":
                    if named != local:
                        reply = local.wrong_name
                    else:
                        reply = self._fisherman_fishing_hint(player, local)
                    lines.append(f"{local.display} says, {reply}")
                    broadcasts.append(
                        f"ROOM:{room.id}:{local.display} says, {reply}"
                    )
                elif state == "ignored":
                    reaction = (
                        f"{local.display} ignores you and watches the line."
                    )
                    lines.append(reaction)
                    broadcasts.append(f"ROOM:{room.id}:{reaction}")

        return CommandResult(
            message="\n".join(lines),
            broadcast="|".join(broadcasts),
        )

    def _start_or_resume_norm_round(self, player: Player) -> str:
        """Start Norm's private guessing round without resetting an active one."""
        key = player.name.lower()
        if key not in self.norm_rounds:
            self.norm_rounds[key] = NormRound(
                target=create_random_norm_fish_quest()
            )
            return (
                "Hello! I want to see a specific fish! "
                "Can you guess what it is?!"
            )
        return "I'm still thinking of the same fish! Show me your guess!"

    @staticmethod
    def _norm_target_name(target: BubbaFishQuest) -> str:
        quality = condition_word(target.condition, ItemType.FISH)
        return f"{quality} {target.fish_size} {target.fish_name}"

    def _give_to_norm(self, player: Player, item: Item) -> CommandResult:
        """Judge a fish against Norm's hidden species, size, and quality."""
        if item.item_type != ItemType.FISH:
            return CommandResult(
                f'Norm hands back your {item.display_name}. '
                '"Ha, that\'s not a fish!"'
            )

        key = player.name.lower()
        round_state = self.norm_rounds.get(key)
        if not round_state:
            return CommandResult(
                f"Norm hands back your {item.display_name}. "
                '"Say hello first! Then I\'ll think of a fish."'
            )

        skip_lines = {
            "sting_puffer": (
                "WHOA!! That looks dangerous, I wasn't thinking of that!"
            ),
            "bluegill": "No way, not that boring fish!",
        }
        skip_line = (
            "WHOA!! I've never seen anything that old! "
            "I wasn't thinking of that!"
            if is_ancient_fish_id(item.id)
            else skip_lines.get(item.id)
        )
        if skip_line:
            return CommandResult(
                f"Norm hands back your {item.display_name}.\n"
                f'"{skip_line}"'
            )

        target = round_state.target
        matches = sum((
            item.id == target.fish_id,
            (item.fish_size or "").lower() == target.fish_size,
            item.condition == target.condition,
        ))

        if matches == 3:
            reward = create_item_copy(SPECIALTY_LURE, roll_stats=False, condition=9)
            player.add_item(reward)
            self.norm_rounds[key] = NormRound(
                target=create_random_norm_fish_quest()
            )
            return CommandResult(
                f"Norm looks over your {item.display_name}, then hands it back.\n"
                '"Yay, that\'s what I was thinking of!"\n'
                f"He gives you a {reward.display_name}.\n"
                '"I wanna play again!"'
            )

        round_state.guesses += 1
        reactions = {
            0: "No, not this!",
            1: "Kinda, but not quite.",
            2: "Oh, this is close!",
        }
        lines = [
            f"Norm looks over your {item.display_name}, then hands it back.",
            f'"{reactions[matches]}"',
        ]
        remaining = NORM_MAX_GUESSES - round_state.guesses
        if remaining <= 0:
            answer = self._norm_target_name(target)
            lines.append(f'"I was thinking of a {answer}!"')
            lines.append('"Say hello if you want to play again!"')
            self.norm_rounds.pop(key, None)
        else:
            noun = "guess" if remaining == 1 else "guesses"
            lines.append(f"({remaining} {noun} left.)")
        return CommandResult("\n".join(lines))

    def cmd_nod(self, player: Player, target: str) -> CommandResult:
        """Nod at a player or NPC in the room."""
        if not target:
            return CommandResult("Nod at whom?")

        found = self._find_attack_target(player, target)
        room = self.rooms.get(player.current_room)
        local_fisherman = (
            self.fishermen.in_room(room.id) if self.fishermen else None
        )
        named_fisherman = (
            self.fishermen.target_named(target) if self.fishermen else None
        )
        if not found:
            return CommandResult(f"You don't see '{target}' here.")

        _, name = found
        lines = [f"You nod at {name}."]
        broadcasts = [f"ROOM:{room.id}:{player.name} nods at {name}."]

        if name.lower() == "bubba":
            lines.append("Bubba nods back at you.")
            broadcasts.append(f"ROOM:{room.id}:Bubba nods back at {player.name}.")
            if self.market:
                quest_lines = self.market.get_bubba_quest_status_lines()
                if quest_lines:
                    lines.extend(quest_lines)
        elif name.lower() == "cliff":
            lines.append("Cliff nods once, already looking back at the vise.")
            broadcasts.append(
                f"ROOM:{room.id}:Cliff nods once at {player.name}."
            )
        elif name.lower() == "slick":
            lines.append("Slick furrows his brow and seems sweatier.")
            broadcasts.append(
                f"ROOM:{room.id}:Slick furrows his brow and seems sweatier."
            )
        elif local_fisherman and name.lower() == local_fisherman.display.lower():
            state = self.fishermen.begin_interaction(
                player.name, local_fisherman
            )
            if state == "active":
                if named_fisherman and named_fisherman != local_fisherman:
                    reply = local_fisherman.wrong_name
                    lines.append(f"{local_fisherman.display} says, {reply}")
                    broadcasts.append(
                        f"ROOM:{room.id}:{local_fisherman.display} says, {reply}"
                    )
                elif player.get_effective_attribute("charisma") >= 6:
                    reply = local_fisherman.greeting
                    lines.append(f"{local_fisherman.display} says, {reply}")
                    broadcasts.append(
                        f"ROOM:{room.id}:{local_fisherman.display} says, {reply}"
                    )
                else:
                    reaction = (
                        f"{local_fisherman.display} gives you a guarded nod."
                    )
                    lines.append(reaction)
                    broadcasts.append(f"ROOM:{room.id}:{reaction}")
            elif state == "ignored":
                reaction = (
                    f"{local_fisherman.display} ignores you and watches the line."
                )
                lines.append(reaction)
                broadcasts.append(f"ROOM:{room.id}:{reaction}")

        return CommandResult(
            message="\n".join(lines),
            broadcast="|".join(broadcasts),
        )

    def _fisherman_fishing_hint(
        self, player: Player, fisherman: Fisherman
    ) -> str:
        """Give a charisma-scaled hint about the best current fishing water."""
        fishing_rooms = [
            room for room in self.rooms.values()
            if room.is_water and room.id not in FISHING_HOLE_IDS
        ]
        best_population = max((room.population or 0) for room in fishing_rooms)
        best_rooms = [
            room for room in fishing_rooms
            if (room.population or 0) == best_population
        ]
        current_is_best = any(
            room.id == player.current_room for room in best_rooms
        )
        charisma = player.get_effective_attribute("charisma")

        if charisma < 6:
            return fisherman.staying if current_is_best else fisherman.elsewhere

        # Ties are all best. If elsewhere, consistently describe the first
        # tied room in world order rather than changing the answer per request.
        target = (
            self.rooms[player.current_room] if current_is_best else best_rooms[0]
        )
        landmark = FISHING_SPOT_LANDMARKS.get(
            target.id, f"near {target.name.lower()}"
        )
        if charisma < 9:
            if current_is_best:
                return fisherman.staying
            return f'{fisherman.vague_prefix} {landmark}."'

        population_hint = self._population_message(target.population or 0)
        if charisma < 12:
            if current_is_best:
                return f'"{population_hint}"'
            return (
                f'{fisherman.vague_prefix} {landmark}. '
                f'{population_hint}"'
            )
        return (
            f'{fisherman.exact_prefix} {target.name}. '
            f'{population_hint}"'
        )

    def _format_duration(self, seconds: int) -> str:
        """Human-readable duration for shopkeeper tips."""
        seconds = max(0, int(seconds))
        minutes, secs = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        if minutes > 0:
            return f"{minutes}m {secs}s"
        return f"{secs}s"

    def cmd_tip(self, player: Player, args: str) -> CommandResult:
        """Give gold to a player or NPC in the room."""
        if not args:
            return CommandResult("Tip whom? Usage: tip <amount> <name>")

        parts = args.split()
        if len(parts) < 2:
            return CommandResult("Usage: tip <amount> <name> (or tip <name> <amount>)")

        if parts[0].isdigit():
            amount = int(parts[0])
            target_name = " ".join(parts[1:])
        elif parts[-1].isdigit():
            amount = int(parts[-1])
            target_name = " ".join(parts[:-1])
        else:
            return CommandResult("Usage: tip <amount> <name> (or tip <name> <amount>)")

        if amount <= 0:
            return CommandResult("You must tip at least 1 gold.")
        if player.gold < amount:
            return CommandResult(f"You only have {player.gold} gold.")

        found = self._find_attack_target(player, target_name)
        if not found:
            return CommandResult(f"You don't see '{target_name}' here.")

        kind, name = found
        room = self.rooms.get(player.current_room)
        if kind == "npc" and name.lower() == "curt":
            if not self.ceelo_tip_handler:
                return CommandResult("Curt taps the dice cup, but the table is closed.")
            return self.ceelo_tip_handler(player, amount)

        player.gold -= amount

        lines = [f"You tip {name} {amount} gold."]
        broadcasts = [
            f"ROOM:{room.id}:{player.name} tips {name} {amount} gold."
        ]

        if kind == "player":
            recipient = self.player_manager.find_online_player(name)
            if recipient:
                recipient.gold += amount
                recipient.total_gold_earned += amount
        elif name.lower() == "bubba":
            if amount >= 5 and self.market:
                remaining = self._format_duration(
                    self.market.get_clothing_rotation_remaining()
                )
                lines.append(
                    f'Bubba tips his hat. "New clothing stock comes in about '
                    f'{remaining}, partner."'
                )
                broadcasts.append(
                    f'ROOM:{room.id}:Bubba tips his hat and murmurs something '
                    f'to {player.name}.'
                )
            else:
                lines.append('Bubba grins. "Mighty kind of you."')
        elif name.lower() == "cliff":
            lines.append(
                'Cliff tucks the coins under a tin of beads. "It spends."'
            )
        elif name.lower() == "slick":
            if amount >= 10 and self.market:
                remaining = self._format_duration(
                    self.market.get_clothing_rotation_remaining()
                )
                lines.append(
                    f'Slick pockets the coins fast. "Clothes shipment\'s due '
                    f'in about {remaining}. Don\'t say I told you."'
                )
                broadcasts.append(
                    f"ROOM:{room.id}:Slick leans in and whispers something "
                    f"to {player.name}."
                )
            else:
                lines.append(
                    'Slick pockets it without looking. "Yeah, yeah. Thanks."'
                )
        else:
            lines.append(f"{name} accepts your tip.")

        lines.append(f"You now have {player.gold} gold.")
        return CommandResult(
            message="\n".join(lines),
            broadcast="|".join(broadcasts),
        )

    def cmd_roll(self, player: Player, args: str) -> CommandResult:
        """Take a Cee-lo turn, or roll your eyes away from Curt's table."""
        if player.current_room == CEELO_ROOM_ID:
            if not self.ceelo_roll_handler:
                return CommandResult('Curt taps the table. "Ante first."')
            return self.ceelo_roll_handler(player)
        return CommandResult(
            "You roll your eyes.",
            broadcast=f"ROOM:{player.current_room}:{player.name} rolls their eyes.",
        )

    def cmd_show(self, player: Player, args: str) -> CommandResult:
        """Hidden: same as give while a Norm guessing round is active."""
        if player.name.lower() not in self.norm_rounds:
            return CommandResult(
                "Unknown command: 'show'. Type 'help' for a list of commands."
            )
        return self.cmd_give(player, args)

    def cmd_give(self, player: Player, args: str) -> CommandResult:
        """Give an inventory item to another player in the room."""
        if not args:
            return CommandResult(
                "Give what? Usage: give <item> <player> "
                "(or give <item> to <player>)"
            )

        # Normalize "give X to Y"
        lowered = args.lower()
        if " to " in lowered:
            idx = lowered.rfind(" to ")
            item_query = args[:idx].strip()
            target_name = args[idx + 4:].strip()
        else:
            parts = args.split()
            if len(parts) < 2:
                return CommandResult(
                    "Usage: give <item> <player> (or give <item> to <player>)"
                )
            target_name = parts[-1]
            item_query = " ".join(parts[:-1])

        found = self._find_attack_target(player, target_name)
        if not found:
            return CommandResult(f"You don't see '{target_name}' here.")

        kind, name = found
        item = player.find_item(item_query)
        if not item:
            return CommandResult(f"You don't have a '{item_query}'.")

        if kind == "npc":
            if name.lower() == "norm":
                return self._give_to_norm(player, item)
            if name.lower() == "cliff" and item.id == "glowing_lure":
                return self._salvage_glowing_lure(player, item)
            local_fisherman = (
                self.fishermen.in_room(player.current_room)
                if self.fishermen else None
            )
            if name.lower() == "cliff" and item.item_type == ItemType.FISH:
                lure = self._first_specialty_lure(player)
                if not lure:
                    return CommandResult(
                        'Cliff eyes the fish, then the empty space on your '
                        'belt. "Bring a jig if you want that dressed."'
                    )
                return self._feed_specialty_lure(player, lure, item)
            if (
                local_fisherman
                and name.lower() == local_fisherman.display.lower()
                and is_ancient_fish_id(item.id)
            ):
                player.remove_item(item)
                if self.lake_state:
                    self.lake_state.release(item.id)
                return CommandResult(
                    (
                        f"You offer {item.display_name} to {local_fisherman.display}.\n"
                        "The fisherman recognizes the ancient fish immediately and "
                        "slips it gently back into the lake."
                    ),
                    broadcast=(
                        f"ROOM:{player.current_room}:{local_fisherman.display} "
                        f"returns {item.display_name} to the lake."
                    ),
                )
            return CommandResult(
                f"{name} shakes their head. They won't take your items."
            )

        if player.is_wearing_or_equipped(item):
            return CommandResult(
                f"You should unequip your {item.display_name} before giving it away."
            )

        recipient = self.player_manager.find_online_player(name)
        if not recipient:
            return CommandResult(f"{name} is no longer here.")

        room = self.rooms.get(player.current_room)
        player.remove_item(item)
        recipient.add_item(item)

        return CommandResult(
            message=(
                f"You give your {item.display_name} to {recipient.name}."
            ),
            broadcast=(
                f"ROOM:{room.id}:{player.name} gives {item.display_name} "
                f"to {recipient.name}."
            ),
        )
    
    def cmd_shout(self, player: Player, message: str) -> CommandResult:
        """Shout something to everyone."""
        if not message:
            return CommandResult("Shout what?")
        
        return CommandResult(
            message=f'You shout, "{message.upper()}!"',
            broadcast=f'GLOBAL:{player.name} shouts, "{message.upper()}!"'
        )
    
    def cmd_who(self, player: Player, args: str) -> CommandResult:
        """List all connected players."""
        players = list(self.player_manager.players.values())
        
        if not players:
            return CommandResult("No one else is playing right now.")
        
        lines = ["\n" + "="*40]
        lines.append("  PLAYERS ONLINE")
        lines.append("="*40)
        
        for p in players:
            room = self.rooms.get(p.current_room)
            room_name = room.name if room else "Unknown"
            you = " (you)" if p.name == player.name else ""
            lines.append(f"  {p.name}{you} - {room_name}")
        
        lines.append(f"\nTotal: {len(players)} player(s)")
        return CommandResult("\n".join(lines))
    
    def cmd_help(self, player: Player, args: str) -> CommandResult:
        """Show help information."""
        help_text = """
==========================================
  FISHING MUD - HELP
==========================================

MOVEMENT:
  north/n, south/s, east/e, west/w
  go <direction>
  look <direction>    - Peer into an adjacent room

ITEMS:
  look/l/ls/cd        - Look at your surroundings
  get/take/pick <item>  - Pick up an item
  get all               - Pick up everything on the ground
  drop <item>          - Drop an item
  put <item/#> truck   - Store an item in your truck (parking lot)
  put all truck        - Store everything you can in your truck
  take <item/#> truck  - Take an item from your truck
  take all truck       - Take everything from your truck
  truck                - Look in your truck
  use <item>           - Use a consumable item
  use/feed <jig> <fish>  - Hand Cliff a fish to dress or improve a jig
  inventory/inv/i [filter] - Show inventory (name or type/slot, e.g. inv hat, inv pole)
  inv gear              - Show all fishing poles and lures (equipped and carried)
  inv sort fish         - Keep gear in place; sort fish by species, quality, size
  examine/ex <item/#>  - Look closely at something (or inventory #)
  equip/eq/wear/don [item] - Show equipment, or equip/wear an item
  wear all              - Wear clothing into empty slots
  unequip/uneq/remove/rem [item] - Remove gear (bare removes all worn)
  remove <attr>         - Remove all gear boosting that attribute (e.g. rem con)
  repair/fix [item]     - List worn gear, or repair with a toolkit
  repair/fix next       - Repair the next item that isn't new
                          (in jail you can mend by hand, 3× slower)
  stats/attributes      - Show character attributes and level

FISHING:
  fish/cast           - Cast your line (need pole equipped!)
  ponder              - Review your heaviest catch of each kind
  brag <fish>         - Boast a personal-best weight to the room
  consider/con        - Estimate a fishing spot's population
  sense <fish>        - Feel how likely a species is
  release <fish/#>    - Pay the lake; the commotion draws fish
  appraise/app [fish/#] - Estimate one fish or all fish at Bubba's prices
  chum/dump chum       - Use chum to briefly improve this fishing spot
  weather             - Check weather
  (While reeling: type CUT or press Ctrl-G to snap the line)

SHOPPING (at Bubba's or Slick's):
  list                - See items for sale & prices
  buy <item/#>        - Purchase an item (name or list number)
  sell                - See what you can sell and for how much
  sell <item/#>       - Sell a fish or item (name or inventory #)
  (Bubba posts a double-pay fish request — first to sell it wins!
   After that, each extra fish sold to him brings the next request 30s sooner.)

SOCIAL:
  say <message>         - Talk to others in the room
  nod <player/npc>      - Nod at someone
  tip <amount> <name>   - Give gold to a player or NPC
  give <item> <player>  - Give an item to another player
  shout/yell <message>  - Yell to everyone on the server
  who/players         - See who's online

OTHER:
  help/?              - Show this help
  save                - Manually save your progress
  quit/exit           - Leave the game (auto-saves)

TIPS:
  - Start by buying a fishing pole and some bait!
  - Equip your pole before you can fish
  - Catch enough total weight to level up (100 lbs, then 200, 400, ...)
  - Weather affects fishing - check conditions!
  - Better gear = better chance at rare fish
  - Slick's prices fluctuate - watch for deals!
  - Try to catch the legendary 'Old Whiskers'!
"""
        return CommandResult(help_text)
    
    def _get_current_store(self, room_id: str) -> Optional[StoreType]:
        """Get the store type for a room, or None if not a store."""
        if room_id == "store":
            return StoreType.BUBBA
        elif room_id == "slick_store":
            return StoreType.SLICK
        return None
    
    def cmd_weather(self, player: Player, args: str) -> CommandResult:
        """Check the current weather, and upcoming patterns with Int+Wis."""
        if not self.weather:
            return CommandResult("Weather system not available.")
        intelligence = player.get_effective_attribute("intelligence")
        wisdom = player.get_effective_attribute("wisdom")
        upcoming = min(5, (intelligence + wisdom) // 4)
        return CommandResult(self.weather.get_weather_display(upcoming))
    
    def cmd_buy(self, player: Player, item_name: str) -> CommandResult:
        """Buy an item from a store by name or list number."""
        room = self.rooms.get(player.current_room)
        store_type = self._get_current_store(room.id)
        
        if not store_type:
            return CommandResult("You need to be in a store to buy items!")
        
        if not item_name:
            return CommandResult("Buy what? Type 'list' to see available items.")
        
        item_name = item_name.lower().strip().rstrip(")")
        item = None

        # Buy by store list number: buy 5
        if item_name.isdigit():
            number = int(item_name)
            if self.market:
                item = self.market.get_for_sale_item_by_number(
                    store_type, number, player_name=player.name
                )
            else:
                for_sale = [
                    entry for entry, _ in STORE_INVENTORY.values()
                    if entry.item_type != ItemType.WEARABLE
                ]
                if 1 <= number <= len(for_sale):
                    item = for_sale[number - 1]
            if not item:
                return CommandResult(
                    f"There's no item numbered {number} for sale. Type 'list' to see what's available."
                )
        else:
            # Find item in store inventory by name
            for item_id, (candidate, quantity) in STORE_INVENTORY.items():
                if item_name in candidate.name.lower() or item_name == item_id:
                    if self.market and not self.market.is_item_for_sale(store_type, item_id):
                        if candidate.item_type == ItemType.WEARABLE:
                            if store_type == StoreType.SLICK:
                                return CommandResult(
                                    "Slick shrugs. \"Don't got that in stock right now. "
                                    "Clothing shipment changes every hour.\""
                                )
                            return CommandResult(
                                f"Bubba shakes his head. \"We're out of {candidate.name} right now. "
                                "New clothing comes in every hour—check 'list' later.\""
                            )
                        if (
                            store_type == StoreType.BUBBA
                            and self.market.is_limited_bubba_gear(item_id)
                        ):
                            return CommandResult(
                                f'Bubba shakes his head. "I only had one {candidate.name} '
                                'this shipment. Come back after we restock."'
                            )
                        continue
                    if (
                        self.market
                        and candidate.item_type == ItemType.WEARABLE
                        and self.market.player_bought_clothing(
                            store_type, player.name, item_id
                        )
                    ):
                        if store_type == StoreType.SLICK:
                            return CommandResult(
                                "Slick smirks. \"Already sold you that one this shipment. "
                                "Wait for the next drop.\""
                            )
                        return CommandResult(
                            f'Bubba tips his cap. "You already picked up a {candidate.name} '
                            "this hour. Come back after we restock.\""
                        )
                    item = candidate
                    break

        if not item:
            if store_type == StoreType.SLICK:
                return CommandResult("Slick shakes his head. \"Never heard of it. Try 'list'.\"")
            return CommandResult(
                f"Bubba doesn't sell '{item_name}'. Type 'list' to see what's available."
            )

        # Get price from market if available
        if self.market:
            price = self.market.get_buy_price(store_type, item.id)
            if price is None or price == 0:
                if store_type == StoreType.SLICK:
                    return CommandResult(
                        "Slick squints at you. \"Don't got that right now. Check back later.\""
                    )
                if self.market.is_limited_bubba_gear(item.id):
                    return CommandResult(
                        f'Bubba shakes his head. "I only had one {item.name} '
                        'this shipment. Come back after we restock."'
                    )
                return CommandResult(
                    f"Bubba doesn't have {item.name} for sale right now."
                )
            if (
                item.item_type == ItemType.WEARABLE
                and self.market.player_bought_clothing(
                    store_type, player.name, item.id
                )
            ):
                if store_type == StoreType.SLICK:
                    return CommandResult(
                        "Slick smirks. \"Already sold you that one this shipment. "
                        "Wait for the next drop.\""
                    )
                return CommandResult(
                    f'Bubba tips his cap. "You already picked up a {item.name} '
                    "this hour. Come back after we restock.\""
                )
        else:
            price = item.value
        
        if player.gold < price:
            if store_type == StoreType.SLICK:
                return CommandResult(
                    f"Slick laughs. \"That's {price} gold, friend. You're short.\""
                )
            return CommandResult(
                f"You don't have enough gold! The {item.name} costs {price} gold."
            )

        if (
            self.market
            and store_type == StoreType.BUBBA
            and self.market.is_limited_bubba_gear(item.id)
            and not self.market.take_bubba_limited_gear(item.id)
        ):
            return CommandResult(
                f'Bubba shakes his head. "I only had one {item.name} '
                'this shipment. Come back after we restock."'
            )
        
        player.gold -= price
        slick_unlock = (
            self._record_slick_spend(player, price)
            if store_type == StoreType.SLICK
            else ""
        )
        bought_kit = item.id == "lure_kit"
        # Store-bought fishing gear is always brand new
        if store_type == StoreType.SLICK and item.id == CAMPER_KEY.id:
            new_item = create_item_copy(CAMPER_KEY, roll_stats=False, condition=4)
        elif bought_kit:
            new_item = create_item_copy(SPECIALTY_LURE, condition=9)
        elif item.item_type in DEGRADABLE_TYPES:
            new_item = create_item_copy(item, condition=9)
        else:
            new_item = create_item_copy(item)
        player.add_item(new_item)
        if self.market and item.item_type == ItemType.WEARABLE:
            self.market.mark_clothing_purchased(store_type, player.name, item.id)
        
        if bought_kit:
            return CommandResult(
                f"You buy a jig kit for {price} gold.\n"
                f"Inside is a {new_item.display_name} (wear jig to equip). "
                "Take it west of the porch to Cliff in the workshop, then "
                "hand him a fish. More of that species — especially "
                "oversized ones — will improve the jig.\n"
                f"You have {player.gold} gold remaining."
            )
        if store_type == StoreType.SLICK:
            return CommandResult(
                f"Slick grins and slides you a {new_item.display_name} for {price} gold.\n"
                f"\"Pleasure doing business.\" You have {player.gold} gold remaining."
                f"{slick_unlock}"
            )
        return CommandResult(
            f"You buy a {new_item.display_name} for {price} gold.\n"
            f"You have {player.gold} gold remaining."
        )
    
    def cmd_list(self, player: Player, args: str) -> CommandResult:
        """List items for sale in a store."""
        room = self.rooms.get(player.current_room)
        store_type = self._get_current_store(room.id)
        
        if not store_type:
            return CommandResult("You need to be in a store to see what's for sale!")

        wisdom = player.get_effective_attribute("wisdom")
        
        # Use market listing if available
        if self.market:
            listing = self.market.get_store_listing(
                store_type,
                wisdom=wisdom,
                worn_slots=set(player.worn_items.keys()),
                player_name=player.name,
            )
            return CommandResult(f"{listing}\n\nYour gold: {player.gold}")
        
        # Fallback to basic listing
        lines = ["\n" + "="*45]
        if store_type == StoreType.SLICK:
            lines.append("  SLICK'S SURPLUS - PRICE LIST")
        else:
            lines.append("  BUBBA'S BAIT & TACKLE - PRICE LIST")
        lines.append("="*45)
        lines.append(f"\nYour gold: {player.gold} coins\n")
        
        lines.append("FISHING POLES:")
        for item_id, (item, qty) in STORE_INVENTORY.items():
            if item.item_type == ItemType.FISHING_POLE:
                lines.append(f"  {item.name:<30} {item.value:>5} gold")
        
        lines.append("\nLURES & BAIT:")
        for item_id, (item, qty) in STORE_INVENTORY.items():
            if item.item_type in [
                ItemType.LURE, ItemType.BAIT, ItemType.CONSUMABLE, ItemType.MISC
            ]:
                lines.append(f"  {item.name:<30} {item.value:>5} gold")
        
        lines.append("\nType 'buy <item name>' to purchase.")
        return CommandResult("\n".join(lines))
    
    def cmd_sell(self, player: Player, item_name: str) -> CommandResult:
        """Sell an item at a store, or list sell values if no item given."""
        room = self.rooms.get(player.current_room)
        store_type = self._get_current_store(room.id)
        
        if not store_type:
            return CommandResult("You need to be in a store to sell items!")
        
        if not item_name:
            return CommandResult(self._format_sell_offer_list(player, store_type))
        
        item = player.find_item(item_name)
        if not item:
            query = item_name.strip().rstrip(")")
            if query.isdigit():
                return CommandResult(
                    f"You don't have an item numbered {query} in your inventory. "
                    "Type 'inv' or 'sell' to see numbers."
                )
            return CommandResult(f"You don't have a '{item_name}'.")

        if player.is_wearing_or_equipped(item):
            return CommandResult(self._refuse_equipped_sale(store_type, item))

        if is_ancient_fish_id(item.id):
            if store_type == StoreType.SLICK:
                return CommandResult(
                    'Slick recoils. "Get that out of here, I have a bad '
                    'feeling about that fish..."'
                )

            payout = (
                self.lake_state.payout(item.id, item.weight)
                if self.lake_state else item.value
            )
            player.remove_item(item)
            player.gold += payout
            player.total_gold_earned += payout
            if self.lake_state:
                self.lake_state.release(item.id, grow=True)
            release_message = (
                "Bubba quickly weighs the fish and tosses it out of the "
                "window! It lands in the nearby stream and disappears "
                "downstream as it makes its way back to the water."
            )
            return CommandResult(
                message=(
                    '"You\'ve caught the legend. I\'ll pay you well so we can '
                    'return it to the water."\n'
                    f"Bubba pays you {payout} gold. (Your gold: {player.gold})\n"
                    f"{release_message}"
                ),
                broadcast=f"ROOM:{room.id}:{release_message}",
            )

        if item.item_type in UNSELLABLE_TYPES or item.id == "glowing_lure":
            if store_type == StoreType.SLICK:
                return CommandResult(
                    f'Slick waves you off. "I don\'t deal in that kind of '
                    f'{item.name}. Keep it."'
                )
            return CommandResult(
                f'Bubba chuckles. "I ain\'t buying your {item.name}, partner."'
            )

        if store_type == StoreType.SLICK and item.is_specialty_lure():
            return CommandResult(
                'Slick eyes the jig and shakes his head. '
                '"I don\'t fence Cliff\'s work. Get it out of here."'
            )
        
        # Determine what this store will buy
        if store_type == StoreType.BUBBA:
            if item.item_type != ItemType.FISH:
                return CommandResult(
                    f"Bubba only buys fish. He's not interested in your "
                    f"{item.display_name}."
                )
        # Slick buys anything

        sell_price = self._estimate_sell_price(store_type, item, player)
        if sell_price is None:
            return CommandResult(
                f"This shop won't buy your {item.display_name}."
            )

        quest_bonus = False
        if (
            store_type == StoreType.BUBBA
            and self.market
            and self.market.try_claim_bubba_quest(item, player.name)
        ):
            sell_price *= 2
            quest_bonus = True
        
        player.remove_item(item)
        player.gold += sell_price
        player.total_gold_earned += sell_price
        
        if store_type == StoreType.SLICK:
            slick_responses = [
                f"Slick examines your {item.display_name} and slides you {sell_price} gold.",
                f"\"I can work with this.\" Slick hands you {sell_price} gold.",
                f"Slick tosses {sell_price} gold on the counter. \"Deal.\"",
            ]
            return CommandResult(
                f"{random.choice(slick_responses)} (Your gold: {player.gold})"
            )

        if quest_bonus:
            room = self.rooms.get(player.current_room)
            quest = self.market.get_bubba_quest() if self.market else None
            target = quest.colored_target() if quest else item.display_name
            gem = create_gem()
            player.add_item(gem)
            return CommandResult(
                message=(
                    f'Bubba\'s eyes light up. "That\'s the one!"\n'
                    f"He pays double for your {target}: {sell_price} gold. "
                    f"(Your gold: {player.gold})\n"
                    f'"You earned this, too." Bubba slides a {gem.display_name} '
                    f"across the counter."
                ),
                broadcast=(
                    f'ROOM:{room.id}:Bubba pays {player.name} double for '
                    f'the requested {strip_ansi(target)}!'
                ),
            )

        quest_nudge = ""
        if (
            store_type == StoreType.BUBBA
            and item.item_type == ItemType.FISH
            and self.market
            and self.market.apply_bubba_post_quest_fish_sale()
            and self.market.bubba_post_quest_sale_should_nudge()
        ):
            quest_nudge = (
                "\nBubba already has what he wanted — "
                "that sale gets the next request coming sooner."
            )

        gold_now = f"(Your gold: {player.gold})"
        cha = player.get_effective_attribute("charisma")
        if item.item_type == ItemType.FISH and cha > 1:
            return CommandResult(
                f"You sell your {item.display_name} for {sell_price} gold. "
                f"{gold_now}\n"
                f"Bubba tips his cap — your charm sweetened the deal."
                f"{quest_nudge}"
            )
        
        return CommandResult(
            f"You sell your {item.display_name} for {sell_price} gold. "
            f"{gold_now}"
            f"{quest_nudge}"
        )

    def _refuse_equipped_sale(self, store_type: StoreType, item: Item) -> str:
        """Shopkeeper refuses to buy something still worn or equipped."""
        name = item.display_name
        if store_type == StoreType.SLICK:
            responses = [
                f'Slick snorts. "Nice try. Take the {name} off first, then we talk."',
                f'Slick taps the counter. "I ain\'t buying what you\'re still wearing. Unequip that {name}."',
                f'"You planning to walk out naked?" Slick grins. "Take off the {name} first."',
                f'Slick waves you off. "Peel that {name} off before you push it across my counter."',
            ]
        else:
            responses = [
                f'Bubba chuckles. "Whoa there — take that {name} off first, then I\'ll have a look."',
                f'Bubba shakes his head. "Can\'t buy what you\'re still using. Unequip the {name}."',
                f'"Hang on, partner." Bubba points at you. "Take off that {name} before we deal."',
            ]
        return random.choice(responses)

    def _estimate_sell_price(
        self,
        store_type: StoreType,
        item: Item,
        player: Optional[Player] = None,
    ) -> Optional[int]:
        """Estimate sell value for an inventory item at a store."""
        if item.item_type in UNSELLABLE_TYPES or item.id == "glowing_lure":
            return None
        if is_ancient_fish_id(item.id):
            if store_type == StoreType.SLICK:
                return None
            return (
                self.lake_state.payout(item.id, item.weight)
                if self.lake_state else item.value
            )
        if store_type == StoreType.SLICK and item.is_specialty_lure():
            return None

        charisma = 1
        if player is not None:
            charisma = player.get_effective_attribute("charisma")

        if self.market:
            return self.market.estimate_sell_price(
                store_type, item, charisma=charisma
            )

        if store_type == StoreType.BUBBA and item.item_type != ItemType.FISH:
            return None
        if store_type == StoreType.SLICK:
            base = max(1, int(item.value * 0.2))
        else:
            base = max(1, int(item.value * 0.5))
        price = apply_condition_sell_price(base, item.condition)
        return apply_charisma_sell_bonus(store_type, item, price, charisma)

    def _format_sell_offer_list(self, player: Player, store_type: StoreType) -> str:
        """List inventory items this store will buy and what they pay."""
        store_name = (
            "Slick's Surplus" if store_type == StoreType.SLICK
            else "Bubba's Bait & Tackle"
        )
        wisdom = player.get_effective_attribute("wisdom")
        lines = [
            "\n" + "=" * 50,
            f"  SELL OFFERS - {store_name.upper()}",
            "=" * 50,
        ]

        if store_type == StoreType.BUBBA:
            lines.append("\nBubba only buys fish.\n")
            if self.market:
                for quest_line in self.market.get_bubba_quest_status_lines():
                    lines.append(quest_line)
                lines.append("")
        else:
            lines.append("\nSlick will buy almost anything.\n")

        offers = []
        for number, item in enumerate(player.get_inventory_display_order(), start=1):
            if is_ancient_fish_id(item.id):
                continue
            price = self._estimate_sell_price(store_type, item, player)
            if price is None:
                continue
            quest_match = (
                store_type == StoreType.BUBBA
                and self.market
                and self.market.bubba_quest_matches(item)
            )
            if quest_match:
                price *= 2
            offers.append((number, item, price, quest_match))

        if not offers:
            if store_type == StoreType.BUBBA:
                lines.append("You're not carrying any fish to sell.")
            else:
                lines.append("You're not carrying anything Slick wants.")
            lines.append("\nType 'sell <item>' or 'sell <#>'.")
            return "\n".join(lines)

        lines.append("Your sellable items:")
        for number, item, price, quest_match in offers:
            name = item.display_name
            pad = max(0, 40 - len(strip_ansi(name)))
            line = f"  {number}) {name}{' ' * pad} {price:>4}g"
            if quest_match:
                line += "  << DOUBLE BOUNTY"
            if wisdom >= 5 and self.market:
                info = self.market.get_price_info(store_type, item.id)
                if info:
                    line += f" {self.market.get_trend_symbol(info.trend)}"
            if wisdom >= 8 and self.market:
                info = self.market.get_price_info(store_type, item.id)
                if info and info.base_price > 0 and item.item_type == ItemType.FISH:
                    usual = info.current_sell_price
                    if usual > 0:
                        # Compare paid amount to usual species sell price
                        pct = round(((price - usual) / usual) * 100)
                        if pct != 0:
                            hint = f"({pct:+d}% vs usual)"
                            if wisdom >= 12:
                                hint = f"\033[32m{hint}\033[0m"
                            line += f" {hint}"
            lines.append(line)

        if wisdom <= 2:
            lines.append("\nThese look like fair offers to you.")
        elif wisdom <= 4:
            lines.append("\nYou get a vague sense of how the market is leaning.")
        elif wisdom < 8:
            lines.append("\n↑↓→ show whether this shop's prices are rising or falling.")
        else:
            lines.append(
                "\n↑↓→ show price trends. Percentages compare offers to the usual rate."
            )

        if wisdom < 5:
            lines.append("(Higher Wisdom reveals more about shifting prices.)")

        lines.append("\nType 'sell <item>' or 'sell <#>' to sell.")
        lines.append(f"Your gold: {player.gold}")
        return "\n".join(lines)
    
    def cmd_sing(self, player: Player, args: str) -> CommandResult:
        """Secret command: Sing for Bubba (or get kicked out by Slick)."""
        room = self.rooms.get(player.current_room)
        
        # Singing at Slick's gets you kicked out!
        if room.id == "slick_store":
            # Ban for 5 minutes
            player.slick_ban_until = time.time() + (5 * 60)
            
            # Move player to exterior
            room.players.discard(player.name)
            player.clear_slick_visit()
            exterior = self.rooms.get("slick_exterior")
            player.current_room = "slick_exterior"
            exterior.players.add(player.name)
            
            slick_responses = [
                "Slick's eye twitches. \"What do you think this is, a karaoke bar? GET OUT!\"",
                "Slick slams his fist on the counter. \"No singing! This ain't amateur hour! OUT!\"",
                "Slick grabs you by the collar. \"You wanna sing? Sing outside! SCRAM!\"",
                "\"Are you KIDDING me right now?\" Slick shoves you toward the door. \"Come back when you're serious!\"",
            ]
            
            return CommandResult(
                message=f"{random.choice(slick_responses)}\n\n*You've been thrown out of Slick's!*\n*You can't return for 5 minutes.*\n\n{exterior.get_description(current_player=player.name)}",
                broadcast=f"ROOM:slick_store:Slick throws {player.name} out of the store!"
            )
        
        # Only works in Bubba's store
        if room.id != "store":
            return CommandResult("You hum a little tune to yourself.")
        
        # Check cooldown (12 hours = 43200 seconds)
        cooldown_seconds = SING_COOLDOWN_HOURS * 60 * 60
        current_time = time.time()
        time_since_last_sing = current_time - player.last_sing_time
        
        if time_since_last_sing < cooldown_seconds:
            # Still on cooldown - Bubba says something to the room
            bubba_responses = [
                "Not yet, I'm still thinking about that last song.",
                "Hold on now, that last tune is still stuck in my head!",
                "Easy there, songbird. I'm still humming your last melody.",
                "Whoa, give an old man time to recover from that last performance!",
            ]
            bubba_says = random.choice(bubba_responses)
            
            return CommandResult(
                message=f'Bubba looks up and says, "{bubba_says}"',
                broadcast=f'ROOM:{room.id}:Bubba says, "{bubba_says}"'
            )
        
        # Sing successfully!
        player.last_sing_time = current_time
        player.gold += 10
        player.total_gold_earned += 10
        
        songs = [
            "You belt out a soulful fishing ballad about the one that got away.",
            "You sing an old sea shanty about sailors and their catches.",
            "You perform a heartfelt country song about life on the lake.",
            "You croon a bluesy tune about early morning fishing trips.",
        ]
        
        bubba_reactions = [
            "Bubba wipes a tear from his eye and slips you 10 gold.",
            "Bubba claps his weathered hands and tosses you 10 gold.",
            "Bubba grins wide and slides 10 gold across the counter to you.",
            "Bubba whistles appreciatively and hands you 10 gold as a tip.",
        ]
        
        song = random.choice(songs)
        reaction = random.choice(bubba_reactions)
        
        return CommandResult(
            message=f"{song}\n\n{reaction}\nYou now have {player.gold} gold.",
            broadcast=f'ROOM:{room.id}:{player.name} sings a song. Bubba tips them 10 gold!'
        )
    
    def cmd_save(self, player: Player, args: str) -> CommandResult:
        """Manually save player progress."""
        if self.player_manager.save_player(player):
            return CommandResult("Progress saved!")
        return CommandResult("Error saving progress. Please try again.")
    
    def cmd_quit(self, player: Player, args: str) -> CommandResult:
        """Quit the game (handled by server)."""
        return CommandResult("QUIT", success=True)
