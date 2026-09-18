"""
Command parser and handlers for the MUD Fishing Game
"""

import random
import re
import time
import math
from typing import Optional, Callable, Dict, List, TYPE_CHECKING
from dataclasses import dataclass, field

from player import Player, PlayerManager
from world import Room, DIRECTION_ALIASES
from items import (
    Item, ItemType, STORE_INVENTORY, CATCHABLE_FISH, create_item_copy,
    DEGRADABLE_TYPES, strip_ansi, CONDITION_NAMES, colorize_condition,
    colorize_fish_size, attribute_abbrev, colorize_attribute,
    attribute_color_name, PLASTIC_WORM, create_beer, create_gem,
    UNSELLABLE_TYPES, GEMMABLE_TYPES, SPECIALTY_LURE, LURE_FORBIDDEN_SPECIES,
    SPECIALTY_LURE_MAX_MULT, attuned_lure_name, attuned_lure_description,
    specialty_lure_essence_from_fish,
)
from market import Market, StoreType, apply_condition_sell_price, apply_charisma_sell_bonus
from lake_state import LakeCycleState
from fishermen import Fisherman, FishermanManager, NPC_CATCH_MAX_SECONDS

if TYPE_CHECKING:
    from weather import WeatherSystem

SING_COOLDOWN_HOURS = 12
SLICK_WORM_DEAL_COST = 10
SLICK_WORM_DEAL_COUNT = 3
SLICK_DEAL_YES = frozenset({"yes", "y", "sure", "deal", "ok", "okay", "yeah", "yep"})
SLICK_DEAL_NO = frozenset({"no", "n", "nah", "nope", "pass"})
GEM_CATCH_CHANCE = 200  # 1 in 200

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
}

FISHING_SPOT_LANDMARKS = {
    "lake_shore": "along the sandy shore",
    "shallow_cove": "among the reeds in the quiet cove",
    "old_pier": "beside the old wooden boards",
    "rocky_point": "out by the flat rocks",
}


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
            "drop": self.cmd_drop,
            "use": self.cmd_use,
            "dump": self.cmd_chum,
            "chum": self.cmd_chum,
            "feed": self.cmd_feed,
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
            "fish": self.cmd_fish,
            "cast": self.cmd_fish,
            "repair": self.cmd_repair,
            "fix": self.cmd_repair,
            "consider": self.cmd_consider,
            "con": self.cmd_consider,
            "appraise": self.cmd_appraise,
            "app": self.cmd_appraise,
            "drink": self.cmd_drink,  # Secret — beer level-up
            "gem": self.cmd_gem,  # Secret — socket gems into gear
            "say": self.cmd_say,
            "nod": self.cmd_nod,
            "tip": self.cmd_tip,
            "give": self.cmd_give,
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
        
        if command in self.commands:
            return self.commands[command](player, args)
        
        # Check for direction shortcuts
        if command in DIRECTION_ALIASES:
            return self.cmd_go(player, DIRECTION_ALIASES[command])
        
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
            ),
            broadcast=(
                f"ROOM:slick_store:Slick sells {player.name} a special "
                f"three-worm deal."
            ),
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
            if old_room.id == "slick_store":
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
            return self.cmd_examine(player, args)
        
        room = self.rooms.get(player.current_room)
        if not room:
            return CommandResult("You're in a void... something went wrong!")
        
        return CommandResult(room.get_description(current_player=player.name))

    def _look_direction(self, player: Player, direction: str) -> CommandResult:
        """Peer into an adjacent room and report items, NPCs, and players."""
        room = self.rooms.get(player.current_room)
        if not room:
            return CommandResult("You're in a void... something went wrong!")

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
        
        if direction not in room.exits:
            return CommandResult(f"You can't go {direction} from here.")

        # Jail exit stays locked until the sentence is up
        if room.id == "jail" and player.is_jail_locked():
            remaining = player.get_jail_remaining()
            return CommandResult(
                f"The cell door won't budge. You still have {remaining} second"
                f"{'s' if remaining != 1 else ''} left on your sentence."
            )
        
        # Check destination
        new_room_id = room.exits[direction]
        
        # Check if trying to enter Slick's while banned
        if new_room_id == "slick_store" and player.is_banned_from_slicks():
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

        if old_room.id == "slick_store" and new_room_id != "slick_store":
            player.clear_slick_visit()
        elif new_room_id == "slick_store" and old_room.id != "slick_store":
            player.begin_slick_visit()
        
        # Build response
        leave_msg = f"{player.name} heads {direction}."
        arrive_msg = f"{player.name} arrives."
        
        result = CommandResult(
            message=new_room.get_description(current_player=player.name),
            broadcast=f"ROOM:{old_room.id}:{leave_msg}|ROOM:{new_room_id}:{arrive_msg}"
        )
        return result
    
    def cmd_get(self, player: Player, item_name: str) -> CommandResult:
        """Pick up an item from the room, or all takeable items with 'all'."""
        if not item_name:
            return CommandResult("Get what?")
        
        room = self.rooms.get(player.current_room)
        item_name = item_name.lower().strip()

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
        if item.id == "ancient_whiskers":
            player.remove_item(item)
            if self.lake_state:
                self.lake_state.release()
            message = (
                "The Ancient Whiskers slaps its tail wildly and flips itself "
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
        if item.id == "bucket_of_chum":
            return self.cmd_chum(player, item_name)
        if item.is_specialty_lure():
            return CommandResult(
                "Sacrifice a fish to this jig: use <jig> <fish> "
                "(or feed <fish>)."
            )
        if item.item_type == ItemType.FISH:
            lure = self._first_specialty_lure(player)
            if lure:
                return self._feed_specialty_lure(player, lure, item)
            return CommandResult(
                "You need a custom jig to sacrifice that fish into."
            )
        return CommandResult(f"You can't find a use for {item.display_name} here.")

    def cmd_feed(self, player: Player, args: str) -> CommandResult:
        """Sacrifice a fish to attune or strengthen a specialty lure."""
        if not args:
            return CommandResult(
                "Feed what? Usage: feed <jig> <fish> (or feed <fish>)"
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
                "You need a custom jig to sacrifice that fish into."
            )
        lure = self._find_typed_item(player, args, specialty_lure=True)
        if lure and lure.is_specialty_lure():
            return CommandResult(
                "Sacrifice a fish to this jig: feed <jig> <fish>."
            )
        return CommandResult(
            "Feed a fish into a custom jig: feed <jig> <fish>."
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
        """Find an inventory item, optionally restricted by type."""
        query = query.strip()
        if not query:
            return None
        for item in player.inventory:
            if not item.matches(query):
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
        """Lock or strengthen a specialty lure by sacrificing a fish."""
        if not lure.is_specialty_lure():
            return CommandResult(
                "Only a blank jig from Bubba's kit can be attuned."
            )
        if player.current_room != "bubba_workshop":
            return CommandResult(
                "Bubba keeps his vise, scent pans, and lure press in the "
                "workshop west of the store porch. That's the only place "
                "he'll let you work a custom jig."
            )
        if fish.item_type != ItemType.FISH:
            return CommandResult("You can only sacrifice a fish to that lure.")
        if fish.id in LURE_FORBIDDEN_SPECIES:
            return CommandResult(
                f"The {fish.plain_display_name} is too rare and wild to bottle "
                "into a lure."
            )
        if lure.attracts_fish_id and fish.id != lure.attracts_fish_id:
            species = self._fish_species_by_id(lure.attracts_fish_id)
            wanted = species.name if species else lure.attracts_fish_id
            return CommandResult(
                f"This lure already hungers for {wanted}. "
                "Only that species will strengthen it."
            )
        if lure.species_attraction_multiplier() >= SPECIALTY_LURE_MAX_MULT:
            return CommandResult(
                "The lure is already saturated with that scent. "
                "You keep the fish."
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
        if first_attune:
            return CommandResult(
                message=(
                    f"You clamp the blank jig in Bubba's bench vise and work "
                    f"the {fish.plain_display_name} into the paint and belly. "
                    f"The shed fills with the scent of {species.name}. "
                    f"(Now {multiplier:.2f}× for that species.)"
                ),
                broadcast=(
                    f"ROOM:{player.current_room}:{player.name} attunes a "
                    "jig at Bubba's workbench."
                ),
            )
        return CommandResult(
            message=(
                f"You render the {fish.plain_display_name} down in Bubba's "
                f"iron pan and steep the jig in the oil. The {species.name} "
                f"scent grows stronger. "
                f"(Now {multiplier:.2f}×, {lure.lure_essence:.1f} typical "
                f"{species.name} sacrificed.)"
            ),
            broadcast=(
                f"ROOM:{player.current_room}:{player.name} feeds a fish "
                "into a jig at the workbench."
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
                f"spreads across the surface, drawing in more fish. "
                f"(Population +{gain} until the lake shifts.)"
            ),
            broadcast=(
                f"ROOM:{room.id}:{player.name} dumps a bucket of chum into "
                "the water. An oily slick spreads across the surface."
            ),
        )
    
    def cmd_inventory(self, player: Player, args: str) -> CommandResult:
        """Show player inventory, optionally filtered (e.g. inv hat)."""
        return CommandResult(player.get_inventory_display(args))

    def release_ancient_whiskers(self, player: Player) -> bool:
        """Return a held or in-flight Ancient Whiskers to the catch pool."""
        held = [
            item for item in player.inventory
            if item.id == "ancient_whiskers"
        ]
        reserved = player.ancient_whiskers_reserved
        for item in held:
            player.remove_item(item)
        player.ancient_whiskers_reserved = False
        if (held or reserved) and self.lake_state:
            self.lake_state.release()
        return bool(held or reserved)
    
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
            f"Condition: {item.colored_condition_name} ({item.condition}/9)",
        ]
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
        if item.wear_slot:
            lines.append(f"Wear slot: {item.wear_slot.value}")
        if item.item_type == ItemType.FISHING_POLE:
            lines.append(f"Fishing power: {item.fishing_power}")
        if item.item_type in (ItemType.LURE, ItemType.BAIT):
            lines.append(f"Attraction: {item.attraction}")
        if item.is_specialty_lure():
            if item.attracts_fish_id:
                species = self._fish_species_by_id(item.attracts_fish_id)
                lines.append(
                    f"Attuned to: {species.name if species else item.attracts_fish_id}"
                )
                lines.append(
                    f"Scent strength: {item.species_attraction_multiplier():.2f}× "
                    f"({item.lure_essence:.1f} typical fish sacrificed, "
                    f"cap {SPECIALTY_LURE_MAX_MULT:.0f}×)"
                )
            else:
                lines.append(
                    "Attuned to: nothing yet. In Bubba's workshop, use this "
                    "jig with a fish to lock its species, then feed it more "
                    "of the same. Size relative to that species matters, "
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

    def _repair_duration(self, player: Player) -> float:
        """
        Base repair time is 60s, reduced by Intelligence and Dexterity.
        Floor is 5 seconds.
        """
        intelligence = player.get_effective_attribute("intelligence")
        dexterity = player.get_effective_attribute("dexterity")
        reduction = 3 * ((intelligence - 1) + (dexterity - 1))
        return float(max(5, 60 - reduction))

    def _find_usable_toolkit(self, player: Player, exclude: Item = None) -> Optional[Item]:
        """Find a non-broken toolkit in inventory, optionally excluding one item."""
        for item in player.inventory:
            if item is exclude:
                continue
            if item.item_type == ItemType.TOOLKIT and not item.is_broken():
                return item
        return None

    def cmd_repair(self, player: Player, item_name: str) -> CommandResult:
        """Repair an item using a toolkit. Takes time based on Int/Dex."""
        if not item_name:
            return CommandResult("Repair what? Usage: repair <item>")

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
        if not toolkit:
            if target.item_type == ItemType.TOOLKIT:
                return CommandResult(
                    "You need another working toolkit to repair this one."
                )
            return CommandResult(
                "You need a working toolkit to repair items. "
                "They're rare — check the stores."
            )

        duration = self._repair_duration(player)
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
            if toolkit_ref not in player.inventory or toolkit_ref.is_broken():
                return "Your toolkit failed before the repair was finished."

            target_ref.condition = 9
            degrade_msg = toolkit_ref.degrade(1)
            lines = [
                f"You finish repairing your {target_ref.display_name}.",
                f"(Was {old_label}, now restored to {colorize_condition(9, 'new')}.)",
            ]
            if degrade_msg:
                lines.append(degrade_msg)
            else:
                lines.append(
                    f"Your {toolkit_ref.display_name} shows a little more wear."
                )
            return "\n".join(lines)

        seconds = int(duration)
        return CommandResult(
            message="",
            immediate_message=(
                f"You set to work on your {target.plain_display_name} with your "
                f"{toolkit.plain_display_name}...\n"
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

        # Bite wait: 2x old (100-pop)/12.5, reduced by Str and Dex
        base_bite = round(2 * (100 - population) / 12.5)
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
            if fish_copy.id == "ancient_whiskers":
                player.ancient_whiskers_reserved = False
            level_lines = player.record_fish_catch(fish_copy.weight)
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
        Never takes Ancient Whiskers out of the pool.
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
                    and fish.id == "legendary_carp"
                    and self.lake_state
                    and self.lake_state.available
                    and random.randint(1, 100) == 1
                    and self.lake_state.reserve()
                ):
                    if player:
                        player.ancient_whiskers_reserved = True
                    return self.lake_state.create_catch()
                return fish
        return CATCHABLE_FISH[0][0]

    def _create_sized_fish(self, fish: Item) -> Item:
        """Create a fish whose rarer size changes its weight and value."""
        if fish.id == "ancient_whiskers":
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
                "You study the water, but cannot judge how many fish are present.",
                broadcast=broadcast,
            )

        return CommandResult(
            self._population_message(population),
            broadcast=broadcast,
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
        # Same scheme as item condition: 0 red, 1-4 yellow, 5-8 green, 9 teal
        quality = 9 if index >= 9 else index
        return template.format(d=colorize_condition(quality, word))
    
    def cmd_say(self, player: Player, message: str) -> CommandResult:
        """Say something to the room."""
        if not message:
            return CommandResult("Say what?")
        
        room = self.rooms.get(player.current_room)
        lines = [f'You say, "{message}"']
        broadcasts = [f'ROOM:{room.id}:{player.name} says, "{message}"']

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
        fishing_rooms = [room for room in self.rooms.values() if room.is_water]
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
            local_fisherman = (
                self.fishermen.in_room(player.current_room)
                if self.fishermen else None
            )
            if (
                local_fisherman
                and name.lower() == local_fisherman.display.lower()
                and item.id == "ancient_whiskers"
            ):
                player.remove_item(item)
                if self.lake_state:
                    self.lake_state.release()
                return CommandResult(
                    (
                        f"You offer {item.display_name} to {local_fisherman.display}.\n"
                        "The fisherman recognizes the old carp immediately and "
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
  use <item>           - Use a consumable item
  use/feed <jig> <fish>  - Attune or strengthen a custom jig (Bubba's Workshop)
  inventory/inv/i [filter] - Show inventory (name or type/slot, e.g. inv hat, inv pole)
  examine/ex <item/#>  - Look closely at something (or inventory #)
  equip/eq/wear/don [item] - Show equipment, or equip/wear an item
  wear all              - Wear clothing into empty slots
  unequip/uneq/remove/rem [item] - Remove gear (bare removes all worn)
  remove <attr>         - Remove all gear boosting that attribute (e.g. rem con)
  repair/fix <item>     - Repair gear with a toolkit (uses Int/Dex)
  stats/attributes      - Show character attributes and level

FISHING:
  fish/cast           - Cast your line (need pole equipped!)
  consider/con        - Estimate a fishing spot's population
  appraise/app [fish/#] - Estimate one fish or all fish at Bubba's prices
  chum/dump chum       - Use chum to briefly improve this fishing spot
  weather             - Check weather (Int+Wis reveals coming patterns)
  (While reeling: type CUT or press Ctrl-G to snap the line)

SHOPPING (at Bubba's or Slick's):
  list                - See items for sale & prices
  buy <item/#>        - Purchase an item (name or list number)
  sell                - See what you can sell and for how much
  sell <item/#>       - Sell a fish or item (name or inventory #)
  (Bubba posts an hourly double-pay fish request — first to sell it wins!)

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
        bought_kit = item.id == "lure_kit"
        # Store-bought fishing gear is always brand new
        if bought_kit:
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
                "Take it west of the porch to Bubba's workshop, then "
                "sacrifice a fish to attune it. More of that species — "
                "especially oversized ones — will strengthen it.\n"
                f"You have {player.gold} gold remaining."
            )
        if store_type == StoreType.SLICK:
            return CommandResult(
                f"Slick grins and slides you a {new_item.display_name} for {price} gold.\n"
                f"\"Pleasure doing business.\" You have {player.gold} gold remaining."
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

        if item.id == "ancient_whiskers":
            if store_type == StoreType.SLICK:
                return CommandResult(
                    'Slick recoils. "Get that out of here, I have a bad '
                    'feeling about that fish..."'
                )

            payout = (
                self.lake_state.payout(item.weight)
                if self.lake_state else item.value
            )
            player.remove_item(item)
            player.gold += payout
            player.total_gold_earned += payout
            if self.lake_state:
                self.lake_state.release(grow=True)
            release_message = (
                "Bubba quickly weighs the fish and tosses it out of the "
                "window! It lands in the nearby stream and disappears "
                "downstream as it makes its way back to the water."
            )
            return CommandResult(
                message=(
                    '"You\'ve caught the legend. I\'ll pay you well so we can '
                    'return him to the water."\n'
                    f"Bubba pays you {payout} gold.\n"
                    f"{release_message}\n"
                    f"You now have {player.gold} gold."
                ),
                broadcast=f"ROOM:{room.id}:{release_message}",
            )

        if item.item_type in UNSELLABLE_TYPES:
            if store_type == StoreType.SLICK:
                return CommandResult(
                    f'Slick waves you off. "I don\'t deal in that kind of '
                    f'{item.name}. Keep it."'
                )
            return CommandResult(
                f'Bubba chuckles. "I ain\'t buying your {item.name}, partner."'
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
                f"{random.choice(slick_responses)}\nYou now have {player.gold} gold."
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
                    f"He pays double for your {target}: {sell_price} gold.\n"
                    f'"You earned this, too." Bubba slides a {gem.display_name} '
                    f"across the counter.\n"
                    f"You now have {player.gold} gold."
                ),
                broadcast=(
                    f'ROOM:{room.id}:Bubba pays {player.name} double for '
                    f'the requested {strip_ansi(target)}!'
                ),
            )

        cha = player.get_effective_attribute("charisma")
        if item.item_type == ItemType.FISH and cha > 1:
            return CommandResult(
                f"You sell your {item.display_name} for {sell_price} gold.\n"
                f"Bubba tips his cap — your charm sweetened the deal.\n"
                f"You now have {player.gold} gold."
            )
        
        return CommandResult(
            f"You sell your {item.display_name} for {sell_price} gold.\n"
            f"You now have {player.gold} gold."
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
        if item.item_type in UNSELLABLE_TYPES:
            return None
        if item.id == "ancient_whiskers":
            if store_type == StoreType.SLICK:
                return None
            return (
                self.lake_state.payout(item.weight)
                if self.lake_state else item.value
            )

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
            if item.id == "ancient_whiskers":
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
