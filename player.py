"""
Player management for the MUD Fishing Game
"""

import json
import os
import time
import hashlib
import secrets
import random
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Set
from pathlib import Path
from items import Item, ItemType, WearSlot


SAVE_DIR = Path("saves")

# Level 2 at 100 lbs total caught, then 200, 400, 800, ...
LEVEL_WEIGHT_BASE = 100.0


def weight_threshold_for_level(level: int) -> float:
    """
    Lifetime lbs of fish required to reach the given level.
    Level 1 requires 0; level 2 = 100; level 3 = 200; level 4 = 400; etc.
    """
    if level <= 1:
        return 0.0
    return LEVEL_WEIGHT_BASE * (2 ** (level - 2))


def level_from_total_weight(total_weight: float) -> int:
    """Highest level earned for a lifetime catch weight."""
    level = 1
    while total_weight >= weight_threshold_for_level(level + 1):
        level += 1
    return level


def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    """Hash a password with a salt. Returns (hash, salt)."""
    if salt is None:
        salt = secrets.token_hex(16)
    password_hash = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt.encode('utf-8'),
        100000  # iterations
    ).hex()
    return password_hash, salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    """Verify a password against a stored hash."""
    computed_hash, _ = hash_password(password, salt)
    return secrets.compare_digest(computed_hash, stored_hash)


@dataclass
class Player:
    name: str
    password_hash: str = ""  # Hashed password
    password_salt: str = ""  # Salt for password hashing
    current_room: str = "store"  # Start at the fishing store
    inventory: List[Item] = field(default_factory=list)
    gold: int = 50  # Starting money
    equipped_pole: Optional[Item] = None
    equipped_lure: Optional[Item] = None
    worn_items: Dict[str, Item] = field(default_factory=dict)
    attributes: Dict[str, int] = field(default_factory=lambda: {
        "strength": 1,
        "dexterity": 1,
        "constitution": 1,
        "intelligence": 1,
        "wisdom": 1,
        "charisma": 1,
    })
    fish_caught: int = 0
    biggest_catch: float = 0.0  # Weight of biggest fish
    total_weight_caught: float = 0.0  # Lifetime lbs of fish caught
    level: int = 1
    pending_level_ups: int = 0  # Unspent permanent attribute improvements
    total_gold_earned: int = 0  # Lifetime earnings
    play_time_seconds: int = 0  # Total play time
    last_sing_time: float = 0.0  # Unix timestamp of last sing for Bubba
    slick_ban_until: float = 0.0  # Unix timestamp when Slick's ban expires
    hack_warnings: int = 0  # Non-standard character offenses
    jail_visits: int = 0  # Times sent to jail
    jail_release_at: float = 0.0  # Unix timestamp when jail exit unlocks
    # Temporary all-attribute modifier from special fish (not from equipment)
    temp_attr_mod: int = 0
    temp_attr_expires_at: float = 0.0
    # Runtime-only Slick visit deal state (not saved)
    slick_visit_id: int = 0
    slick_deal_offered: bool = False
    slick_deal_pending: bool = False
    # Runtime-only beer drink confirmation (not saved)
    beer_awaiting_attr: bool = False
    beer_confirm_attr: Optional[str] = None
    # Wearables worn since the last timed clothing-degrade tick
    clothes_worn_since_degrade: Set[int] = field(default_factory=set)

    def mark_clothing_worn(self, item: Item) -> None:
        """Note that this wearable was put on since the last clothing degrade."""
        if item.item_type == ItemType.WEARABLE:
            self.clothes_worn_since_degrade.add(id(item))

    def begin_slick_visit(self):
        """Start a fresh visit to Slick's (resets the once-per-visit deal)."""
        self.slick_visit_id += 1
        self.slick_deal_offered = False
        self.slick_deal_pending = False

    def clear_slick_visit(self):
        """Invalidate any pending Slick deal when leaving the store."""
        self.slick_visit_id += 1
        self.slick_deal_offered = False
        self.slick_deal_pending = False

    def next_level_threshold(self) -> float:
        """Lifetime lbs needed to reach the next level."""
        return weight_threshold_for_level(self.level + 1)

    def record_fish_catch(self, weight: float) -> List[str]:
        """
        Record a landed fish's weight toward leveling.
        Returns level-up notification lines (may be empty).
        Grants one unopened beer per level gained.
        """
        from items import create_beer

        self.total_weight_caught = round(self.total_weight_caught + weight, 1)
        if weight > self.biggest_catch:
            self.biggest_catch = weight
        self.fish_caught += 1

        earned = level_from_total_weight(self.total_weight_caught)
        if earned <= self.level:
            return []

        gained = earned - self.level
        self.level = earned
        for _ in range(gained):
            self.add_item(create_beer())

        lines = [
            f"*** LEVEL UP! You are now level {self.level}! ***",
        ]
        if gained == 1:
            lines.append(
                "You find an unopened beer on the ground and tuck it into "
                "your pack."
            )
        else:
            lines.append(
                f"You find {gained} unopened beers on the ground and tuck "
                f"them into your pack."
            )
        return lines

    def improve_attribute(self, attribute: str) -> str:
        """Permanently raise a base attribute by 1 (from drinking beer)."""
        if attribute not in self.attributes:
            return (
                "Unknown attribute. Choose: strength, dexterity, constitution, "
                "intelligence, wisdom, or charisma (or str/dex/con/int/wis/cha)."
            )
        self.attributes[attribute] = self.attributes.get(attribute, 1) + 1
        new_val = self.attributes[attribute]
        return (
            f"You permanently improve your {attribute}! "
            f"Base {attribute} is now {new_val}."
        )

    def get_level_progress_lines(self) -> List[str]:
        """Status lines for level / catch progress."""
        next_at = self.next_level_threshold()
        remaining = max(0.0, next_at - self.total_weight_caught)
        return [
            f"Level: {self.level}",
            f"Lifetime catch weight: {self.total_weight_caught:.1f} lbs",
            f"Next level at: {next_at:.0f} lbs ({remaining:.1f} lbs to go)",
        ]
    
    def set_password(self, password: str):
        """Set the player's password."""
        self.password_hash, self.password_salt = hash_password(password)
    
    def check_password(self, password: str) -> bool:
        """Check if a password is correct."""
        if not self.password_hash or not self.password_salt:
            return False
        return verify_password(password, self.password_hash, self.password_salt)
    
    def is_banned_from_slicks(self) -> bool:
        """Check if player is currently banned from Slick's store."""
        return time.time() < self.slick_ban_until
    
    def get_slick_ban_remaining(self) -> int:
        """Get seconds remaining on Slick's ban."""
        remaining = self.slick_ban_until - time.time()
        return max(0, int(remaining))

    def is_jail_locked(self) -> bool:
        """True if the player is still locked in jail."""
        return time.time() < self.jail_release_at

    def get_jail_remaining(self) -> int:
        """Seconds remaining until the jail exit unlocks."""
        return max(0, int(self.jail_release_at - time.time()))
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert player to dictionary for saving."""
        def item_to_dict(item: Item) -> Dict[str, Any]:
            return {
                "id": item.id,
                "name": item.name,
                "description": item.description,
                "item_type": item.item_type.value,
                "takeable": item.takeable,
                "value": item.value,
                "fishing_power": item.fishing_power,
                "attraction": item.attraction,
                "weight": item.weight,
                "wear_slot": item.wear_slot.value if item.wear_slot else None,
                "condition": item.condition,
                "modifiers": [
                    {"attribute": attr, "value": val}
                    for attr, val in item.modifiers
                ],
                # Legacy fields for older readers
                "modifier_attribute": item.modifier_attribute,
                "modifier_value": item.modifier_value,
                "fish_size": item.fish_size,
                "gem_attribute": item.gem_attribute,
            }
        
        equipped_pole_idx = None
        equipped_lure_idx = None
        
        if self.equipped_pole and self.equipped_pole in self.inventory:
            equipped_pole_idx = self.inventory.index(self.equipped_pole)
        if self.equipped_lure and self.equipped_lure in self.inventory:
            equipped_lure_idx = self.inventory.index(self.equipped_lure)
        worn_item_indices = {
            slot: self.inventory.index(item)
            for slot, item in self.worn_items.items()
            if item in self.inventory
        }
        
        return {
            "name": self.name,
            "password_hash": self.password_hash,
            "password_salt": self.password_salt,
            "current_room": self.current_room,
            "inventory": [item_to_dict(item) for item in self.inventory],
            "gold": self.gold,
            "equipped_pole_idx": equipped_pole_idx,
            "equipped_lure_idx": equipped_lure_idx,
            "worn_item_indices": worn_item_indices,
            "attributes": self.attributes,
            "fish_caught": self.fish_caught,
            "biggest_catch": self.biggest_catch,
            "total_weight_caught": self.total_weight_caught,
            "level": self.level,
            "pending_level_ups": self.pending_level_ups,
            "total_gold_earned": self.total_gold_earned,
            "play_time_seconds": self.play_time_seconds,
            "last_sing_time": self.last_sing_time,
            "slick_ban_until": self.slick_ban_until,
            "hack_warnings": self.hack_warnings,
            "jail_visits": self.jail_visits,
            "jail_release_at": self.jail_release_at,
            "temp_attr_mod": self.temp_attr_mod,
            "temp_attr_expires_at": self.temp_attr_expires_at,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Player':
        """Create player from saved dictionary."""
        def dict_to_item(d: Dict[str, Any]) -> Item:
            mods = []
            if d.get("modifiers"):
                for entry in d["modifiers"]:
                    attr = entry.get("attribute")
                    val = int(entry.get("value", 0))
                    if attr and val > 0:
                        mods.append((attr, val))
            elif d.get("modifier_attribute") and d.get("modifier_value", 0) > 0:
                mods = [(d["modifier_attribute"], int(d["modifier_value"]))]
            return Item(
                id=d["id"],
                name=d["name"],
                description=d["description"],
                item_type=ItemType(d["item_type"]),
                takeable=d.get("takeable", True),
                value=d.get("value", 0),
                fishing_power=d.get("fishing_power", 0),
                attraction=d.get("attraction", 0),
                weight=d.get("weight", 0.0),
                wear_slot=WearSlot(d["wear_slot"]) if d.get("wear_slot") else None,
                condition=d.get("condition", 5),
                modifiers=mods,
                fish_size=d.get("fish_size"),
                gem_attribute=d.get("gem_attribute"),
            )
        
        inventory = [dict_to_item(item_data) for item_data in data.get("inventory", [])]
        
        player = cls(
            name=data["name"],
            password_hash=data.get("password_hash", ""),
            password_salt=data.get("password_salt", ""),
            current_room=data.get("current_room", "store"),
            inventory=inventory,
            attributes={
                attribute: data.get("attributes", {}).get(attribute, 1)
                for attribute in (
                    "strength", "dexterity", "constitution",
                    "intelligence", "wisdom", "charisma"
                )
            },
            gold=data.get("gold", 50),
            fish_caught=data.get("fish_caught", 0),
            biggest_catch=data.get("biggest_catch", 0.0),
            total_weight_caught=float(data.get("total_weight_caught", 0.0)),
            level=int(data.get("level", 1)),
            pending_level_ups=int(data.get("pending_level_ups", 0)),
            total_gold_earned=data.get("total_gold_earned", 0),
            play_time_seconds=data.get("play_time_seconds", 0),
            last_sing_time=data.get("last_sing_time", 0.0),
            slick_ban_until=data.get("slick_ban_until", 0.0),
            hack_warnings=data.get("hack_warnings", 0),
            jail_visits=data.get("jail_visits", 0),
            jail_release_at=data.get("jail_release_at", 0.0),
            temp_attr_mod=int(data.get("temp_attr_mod", 0)),
            temp_attr_expires_at=float(data.get("temp_attr_expires_at", 0.0)),
        )
        
        # Restore equipped items by index
        equipped_pole_idx = data.get("equipped_pole_idx")
        equipped_lure_idx = data.get("equipped_lure_idx")
        
        if equipped_pole_idx is not None and equipped_pole_idx < len(inventory):
            player.equipped_pole = inventory[equipped_pole_idx]
        if equipped_lure_idx is not None and equipped_lure_idx < len(inventory):
            player.equipped_lure = inventory[equipped_lure_idx]
        for slot, item_idx in data.get("worn_item_indices", {}).items():
            if 0 <= item_idx < len(inventory):
                player.worn_items[slot] = inventory[item_idx]

        # Migrate old unspent level-ups into beers
        if player.pending_level_ups > 0:
            from items import create_beer
            for _ in range(player.pending_level_ups):
                player.inventory.append(create_beer())
            player.pending_level_ups = 0
        
        return player
    
    def add_item(self, item: Item) -> str:
        """Add an item to the player's inventory."""
        self.inventory.append(item)
        return f"You pick up the {item.display_name}."
    
    def remove_item(self, item: Item) -> str:
        """Remove an item from the player's inventory."""
        if item in self.inventory:
            self.inventory.remove(item)
            
            # Unequip if it was equipped
            if self.equipped_pole == item:
                self.equipped_pole = None
            if self.equipped_lure == item:
                self.equipped_lure = None
            for slot, worn_item in list(self.worn_items.items()):
                if worn_item == item:
                    del self.worn_items[slot]
                
            return f"You drop the {item.display_name}."
        return f"You don't have a {item.display_name}."

    def is_wearing_or_equipped(self, item: Item) -> bool:
        """True if the item is currently worn or equipped for fishing."""
        if item == self.equipped_pole or item == self.equipped_lure:
            return True
        return item in self.worn_items.values()
    
    def get_inventory_display_order(self) -> List[Item]:
        """
        Unequipped inventory items for the Items: list and numbered commands.
        Worn/equipped gear is shown only under EQUIPMENT.
        """
        return [
            item for item in self.inventory
            if not self.is_wearing_or_equipped(item)
        ]

    def find_item(self, item_name: str) -> Optional[Item]:
        """Find an item in inventory by display number or name."""
        query = item_name.strip().lower().rstrip(")")
        carried = self.get_inventory_display_order()
        if query.isdigit():
            index = int(query) - 1
            if 0 <= index < len(carried):
                return carried[index]
            return None

        for item in carried:
            if item.matches(item_name):
                return item
        # Allow finding equipped/worn gear by name (e.g. examine, unequip)
        for item in self.inventory:
            if item.matches(item_name):
                return item
        return None
    
    def get_inventory_display(self, filter_text: str = "") -> str:
        """
        Get a formatted inventory display.
        If filter_text is set, only show equipment and items matching that text.
        Filtered item numbers stay the same as in the full inventory list.
        """
        query = filter_text.strip().lower()
        lines = []

        if query:
            lines.append(f"\n  INVENTORY FILTER: '{filter_text.strip()}'")
            lines.append("=" * 40)

            # Matching worn/equipped gear
            equip_matches = []
            for slot in ("head", "neck", "chest", "hands", "fingers", "legs", "feet"):
                item = self.worn_items.get(slot)
                if item and item.matches_inventory_filter(query):
                    equip_matches.append(f"  {slot:<10} {item.display_name}")
            if self.equipped_pole and self.equipped_pole.matches_inventory_filter(query):
                equip_matches.append(
                    f"  {'pole':<10} {self.equipped_pole.display_name}"
                )
            if self.equipped_lure and self.equipped_lure.matches_inventory_filter(query):
                equip_matches.append(
                    f"  {'lure':<10} {self.equipped_lure.display_name}"
                )

            if equip_matches:
                lines.append("\nEquipment matches:")
                lines.extend(equip_matches)

            carried = self.get_inventory_display_order()
            item_matches = [
                (number, item)
                for number, item in enumerate(carried, start=1)
                if item.matches_inventory_filter(query)
            ]
            if item_matches:
                lines.append("\nItem matches:")
                for number, item in item_matches:
                    lines.append(f"  {number}) {item.display_name}")

            if not equip_matches and not item_matches:
                lines.append(f"\nNo inventory matches for '{filter_text.strip()}'.")

            return "\n".join(lines)

        lines = [self.get_equipment_display()]
        lines.append("")
        lines.append("  YOUR INVENTORY")
        lines.append("=" * 40)
        lines.append(f"Gold: {self.gold} coins")

        carried = self.get_inventory_display_order()
        if not carried:
            if self.inventory:
                lines.append("\nItems:\n  (Nothing unequipped — see EQUIPMENT above.)")
            else:
                lines.append("\nYou're not carrying anything.")
        else:
            lines.append("\nItems:")
            for number, item in enumerate(carried, start=1):
                lines.append(f"  {number}) {item.display_name}")
        
        lines.append(f"\nFish caught: {self.fish_caught}")
        if self.biggest_catch > 0:
            lines.append(f"Biggest catch: {self.biggest_catch:.1f} lbs")
        lines.append("")
        lines.extend(self.get_level_progress_lines())
        
        return "\n".join(lines)
    
    def equip(self, item: Item) -> str:
        """Equip a fishing pole, lure, bait, or wearable item."""
        if item not in self.inventory:
            return f"You don't have a {item.display_name}."

        if item.is_broken() and item.item_type in (
            ItemType.FISHING_POLE, ItemType.LURE, ItemType.BAIT
        ):
            return f"Your {item.display_name} can't be used."
        
        if item.item_type == ItemType.FISHING_POLE:
            self.equipped_pole = item
            return f"You ready your {item.display_name}."
        elif item.item_type == ItemType.LURE:
            self.equipped_lure = item
            return f"You attach the {item.display_name} to your line."
        elif item.item_type == ItemType.BAIT:
            self.equipped_lure = item
            return f"You bait your hook with {item.display_name}."
        elif item.item_type == ItemType.WEARABLE and item.wear_slot:
            slot = item.wear_slot.value
            previous = self.worn_items.get(slot)
            self.worn_items[slot] = item
            self.mark_clothing_worn(item)
            if previous:
                return (
                    f"You remove your {previous.display_name} and wear the "
                    f"{item.display_name} on your {slot}."
                )
            return f"You wear the {item.display_name} on your {slot}."
        else:
            return f"You can't equip a {item.display_name}."

    def wear_all(self) -> str:
        """Wear inventory clothing into any currently empty body slots."""
        slots = ("head", "neck", "chest", "hands", "fingers", "legs", "feet")
        newly_worn = []
        for slot in slots:
            if slot in self.worn_items:
                continue
            for item in self.inventory:
                if item.item_type != ItemType.WEARABLE or not item.wear_slot:
                    continue
                if item.wear_slot.value != slot:
                    continue
                if item in self.worn_items.values():
                    continue
                self.worn_items[slot] = item
                self.mark_clothing_worn(item)
                newly_worn.append((slot, item))
                break

        if not newly_worn:
            return "You have no clothing that fits in an unused slot."

        lines = ["You dress yourself:"]
        for slot, item in newly_worn:
            lines.append(f"  - {item.display_name} on your {slot}")
        return "\n".join(lines)
    
    def unequip(self, item_type: str) -> str:
        """Unequip an item by type, body slot, or item name."""
        if item_type.lower() in ["pole", "rod", "fishing pole"]:
            if self.equipped_pole:
                name = self.equipped_pole.display_name
                self.equipped_pole = None
                return f"You put away your {name}."
            return "You don't have a pole equipped."
        elif item_type.lower() in ["lure", "bait"]:
            if self.equipped_lure:
                name = self.equipped_lure.display_name
                self.equipped_lure = None
                return f"You remove the {name}."
            return "You don't have a lure equipped."
        target = item_type.lower()
        if target in self.worn_items:
            item = self.worn_items.pop(target)
            return f"You remove the {item.display_name} from your {target}."
        for slot, item in list(self.worn_items.items()):
            if item.matches(target):
                del self.worn_items[slot]
                return f"You remove the {item.display_name} from your {slot}."
        return "You don't have that equipped or worn."

    def unequip_all_worn(self) -> str:
        """Remove all worn clothing and accessories."""
        if not self.worn_items:
            return "You're not wearing anything."
        names = [item.display_name for item in self.worn_items.values()]
        self.worn_items.clear()
        if len(names) == 1:
            return f"You remove your {names[0]}."
        return "You remove everything you're wearing:\n  - " + "\n  - ".join(names)

    def unequip_by_attribute(self, attribute: str) -> str:
        """Remove all worn/equipped items that boost the given attribute."""
        removed = []

        for slot, item in list(self.worn_items.items()):
            if item.boosts_attribute(attribute):
                del self.worn_items[slot]
                removed.append(item)

        if self.equipped_pole and self.equipped_pole.boosts_attribute(attribute):
            removed.append(self.equipped_pole)
            self.equipped_pole = None

        if self.equipped_lure and self.equipped_lure.boosts_attribute(attribute):
            removed.append(self.equipped_lure)
            self.equipped_lure = None

        if not removed:
            return f"You're not using any gear that boosts {attribute}."

        lines = [f"You remove your {attribute}-boosting gear:"]
        for item in removed:
            lines.append(f"  - {item.display_name}")
        return "\n".join(lines)

    def get_equipment_display(self) -> str:
        """List every equipment slot and what is worn/equipped, or 'none'."""
        lines = ["\n  EQUIPMENT", "=" * 40]

        for slot in ("head", "neck", "chest", "hands", "fingers", "legs", "feet"):
            item = self.worn_items.get(slot)
            value = item.display_name if item else "none"
            lines.append(f"  {slot:<10} {value}")

        pole = (
            self.equipped_pole.display_name if self.equipped_pole else "none"
        )
        lure = (
            self.equipped_lure.display_name if self.equipped_lure else "none"
        )
        lines.append(f"  {'pole':<10} {pole}")
        lines.append(f"  {'lure':<10} {lure}")
        return "\n".join(lines)

    def get_attributes_display(self) -> str:
        """Return the character's D&D-style attributes."""
        from items import colorize_attribute, attribute_abbrev

        lines = ["\n" + "=" * 40, "  CHARACTER ATTRIBUTES", "=" * 40]
        lines.extend(self.get_level_progress_lines())
        lines.append("")
        for name, base_value in self.attributes.items():
            current_value = self.get_effective_attribute(name)
            bonus = current_value - base_value
            if bonus > 0:
                suffix = f" (+{bonus})"
            elif bonus < 0:
                suffix = f" ({bonus})"
            else:
                suffix = ""
            label = colorize_attribute(name, f"{attribute_abbrev(name):<3} {name.title()}")
            lines.append(f"  {label:<28} {current_value}{suffix}")

        temp = self.get_temp_attribute_bonus()
        if temp:
            remaining = max(0, int(self.temp_attr_expires_at - time.time()))
            minutes = remaining // 60
            seconds = remaining % 60
            sign = f"+{temp}" if temp > 0 else str(temp)
            lines.append("")
            lines.append(
                f"  Temporary: {sign} to all attributes "
                f"({minutes}m {seconds}s remaining)"
            )
        return "\n".join(lines)

    def get_equipped_items(self) -> list:
        """Return all currently active equipment."""
        equipped = list(self.worn_items.values())
        for item in (self.equipped_pole, self.equipped_lure):
            if item and not item.is_broken():
                equipped.append(item)
        return equipped

    def clear_expired_temp_attributes(self) -> None:
        """Clear temporary attribute effects that have timed out."""
        if self.temp_attr_expires_at and time.time() >= self.temp_attr_expires_at:
            self.temp_attr_mod = 0
            self.temp_attr_expires_at = 0.0

    def get_temp_attribute_bonus(self) -> int:
        """Active all-attribute temporary modifier, or 0 if expired."""
        self.clear_expired_temp_attributes()
        if self.temp_attr_expires_at <= 0:
            return 0
        return self.temp_attr_mod

    def apply_temp_attributes(self, amount: int, duration_seconds: float) -> None:
        """Replace any current temp effect with a new all-attribute modifier."""
        self.temp_attr_mod = amount
        self.temp_attr_expires_at = time.time() + duration_seconds

    def get_effective_attribute(self, attribute: str) -> int:
        """Base attribute plus equipment modifiers and temporary fish effects."""
        value = self.attributes.get(attribute, 1)
        for item in self.get_equipped_items():
            value += item.bonus_for(attribute)
        value += self.get_temp_attribute_bonus()
        return max(0, value)    
    def get_fishing_power(self) -> int:
        """Calculate total fishing power from equipment."""
        power = 0
        if self.equipped_pole and not self.equipped_pole.is_broken():
            power += self.equipped_pole.fishing_power
        if self.equipped_lure and not self.equipped_lure.is_broken():
            power += self.equipped_lure.attraction
        return power

    def degrade_fishing_gear(self) -> list:
        """
        Possibly degrade equipped fishing pole/lure after a cast.
        Intelligence (full weight) and Wisdom (half) reduce wear chance.
        Dexterity does not affect gear wear. Clothing is unaffected.
        """
        messages = []
        wisdom = self.get_effective_attribute("wisdom")
        intelligence = self.get_effective_attribute("intelligence")

        # Int carries the former Dex weight; Wis is half of that.
        reduction = (
            (intelligence - 1) * 7
            + (wisdom - 1) * 3.5
        )
        wear_chance = max(15, int(round(100 - reduction)))

        helpers = []
        if intelligence > 1:
            helpers.append("intelligence")
        if wisdom > 1:
            helpers.append("wisdom")

        def _helped_phrase() -> str:
            if not helpers:
                return "your skill helped keep its condition"
            if len(helpers) == 1:
                return f"your {helpers[0]} helped keep its condition"
            return (
                f"your {helpers[0]} and {helpers[1]} "
                f"helped keep its condition"
            )

        for gear in (self.equipped_pole, self.equipped_lure):
            if not gear:
                continue
            roll = random.randint(1, 100)
            if roll > wear_chance:
                # Stats prevented wear this cast
                if helpers:
                    messages.append(
                        f"Your {gear.name} looks {gear.colored_condition_name} "
                        f"({_helped_phrase()})."
                    )
                continue
            msg = gear.degrade(1)
            if msg:
                messages.append(msg)
            if gear.is_broken():
                if gear == self.equipped_pole:
                    self.equipped_pole = None
                    messages.append(
                        f"You put away the {gear.colored_condition_name} {gear.name}."
                    )
                elif gear == self.equipped_lure:
                    self.equipped_lure = None
                    messages.append(
                        f"You remove the {gear.colored_condition_name} {gear.name}."
                    )
        return messages

    @staticmethod
    def clothing_wear_chance(constitution: int) -> int:
        """
        Chance (1-100) that worn clothing degrades on a timed tick.
        Threshold gates by Constitution; floor of 10% at CON 20+.
        """
        if constitution >= 20:
            return 10
        if constitution >= 15:
            return 25
        if constitution >= 12:
            return 40
        if constitution >= 8:
            return 55
        if constitution >= 5:
            return 70
        if constitution >= 3:
            return 85
        return 100

    def degrade_worn_clothes(self) -> list:
        """
        Degrade currently worn clothing that was put on since the last tick.
        Constitution gives threshold-based chances to skip wear (floor 10% at 20).
        Clears the worn-since-degrade marks afterward.
        """
        messages = []
        pending = self.clothes_worn_since_degrade
        if not pending:
            return messages

        constitution = self.get_effective_attribute("constitution")
        wear_chance = self.clothing_wear_chance(constitution)

        for slot, item in list(self.worn_items.items()):
            if id(item) not in pending:
                continue
            if item.item_type != ItemType.WEARABLE:
                continue

            if random.randint(1, 100) > wear_chance:
                if constitution > 1:
                    messages.append(
                        f"Your {item.name} looks {item.colored_condition_name} "
                        f"(your constitution helped keep its condition)."
                    )
                continue

            msg = item.degrade(1, allow_wearable=True)
            if msg:
                messages.append(msg)
            if item.is_broken():
                del self.worn_items[slot]
                messages.append(
                    f"You take off the {item.colored_condition_name} {item.name}."
                )

        self.clothes_worn_since_degrade.clear()
        return messages
    
    def can_fish(self) -> tuple[bool, str]:
        """Check if player can fish."""
        if not self.equipped_pole:
            return False, "You need to equip a fishing pole first! Use 'equip <pole>'."
        if self.equipped_pole.is_broken():
            return False, f"Your {self.equipped_pole.display_name} can't be used. Equip another pole."
        return True, ""


class PlayerManager:
    """Manages all connected players."""
    
    def __init__(self):
        self.players: Dict[str, Player] = {}  # Currently online players
        self._ensure_save_dir()
    
    def _ensure_save_dir(self):
        """Create saves directory if it doesn't exist."""
        SAVE_DIR.mkdir(exist_ok=True)
    
    def _get_save_path(self, name: str) -> Path:
        """Get the save file path for a player."""
        safe_name = "".join(c for c in name.lower() if c.isalnum())
        return SAVE_DIR / f"{safe_name}.json"
    
    def save_player(self, player: Player) -> bool:
        """Save a player's progress to disk."""
        try:
            save_path = self._get_save_path(player.name)
            with open(save_path, 'w') as f:
                json.dump(player.to_dict(), f, indent=2)
            return True
        except Exception as e:
            print(f"Error saving player {player.name}: {e}")
            return False
    
    def load_player(self, name: str) -> Optional[Player]:
        """Load a player from disk if they exist."""
        save_path = self._get_save_path(name)
        if not save_path.exists():
            return None
        
        try:
            with open(save_path, 'r') as f:
                data = json.load(f)
            return Player.from_dict(data)
        except Exception as e:
            print(f"Error loading player {name}: {e}")
            return None
    
    def player_exists(self, name: str) -> bool:
        """Check if a player save file exists."""
        return self._get_save_path(name).exists()
    
    def is_player_online(self, name: str) -> bool:
        """Check if a player is currently online."""
        # Case-insensitive check
        return name.lower() in [p.lower() for p in self.players.keys()]
    
    def authenticate_player(self, name: str, password: str) -> tuple[bool, str]:
        """Authenticate a returning player. Returns (success, error_message)."""
        player = self.load_player(name)
        if player is None:
            return False, "Player not found."
        
        if not player.password_hash:
            # Legacy player without password - let them set one
            return True, "NEEDS_PASSWORD"
        
        if player.check_password(password):
            return True, ""
        return False, "Incorrect password."
    
    def register_player(self, name: str, password: str) -> tuple[Player, str]:
        """Register a new player with password. Returns (player, error_message)."""
        if self.player_exists(name):
            return None, "A player with that name already exists."
        
        if len(password) < 4:
            return None, "Password must be at least 4 characters."
        
        player = Player(name=name)
        player.set_password(password)
        self.save_player(player)
        return player, ""
    
    def add_player_to_game(self, player: Player):
        """Add an authenticated player to the active game."""
        self.players[player.name] = player
    
    def add_player(self, name: str) -> tuple[Player, bool]:
        """Add a player to the game. Returns (player, is_returning).
        DEPRECATED: Use authenticate/register flow instead."""
        if name in self.players:
            return self.players[name], True
        
        # Try to load existing player
        player = self.load_player(name)
        is_returning = player is not None
        
        if player is None:
            # Create new player
            player = Player(name=name)
        
        self.players[name] = player
        return player, is_returning
    
    def remove_player(self, name: str, save: bool = True) -> Optional[Player]:
        """Remove a player from the game, optionally saving first."""
        player = self.players.pop(name, None)
        if player and save:
            self.save_player(player)
        return player
    
    def get_player(self, name: str) -> Optional[Player]:
        """Get a player by name."""
        return self.players.get(name)
    
    def get_players_in_room(self, room_id: str) -> List[Player]:
        """Get all players in a specific room."""
        return [p for p in self.players.values() if p.current_room == room_id]
    
    def broadcast_to_room(self, room_id: str, message: str, exclude: str = None) -> List[str]:
        """Return list of player names in room (excluding one) to receive a message."""
        return [p.name for p in self.players.values() 
                if p.current_room == room_id and p.name != exclude]
    
    def save_all_players(self):
        """Save all currently online players."""
        for player in self.players.values():
            self.save_player(player)
    
    def get_all_saved_players(self) -> List[str]:
        """Get list of all saved player names."""
        players = []
        for save_file in SAVE_DIR.glob("*.json"):
            try:
                with open(save_file, 'r') as f:
                    data = json.load(f)
                    players.append(data.get("name", save_file.stem))
            except:
                pass
        return players

    def find_online_player(self, name: str) -> Optional[Player]:
        """Find an online player by case-insensitive name."""
        needle = name.lower()
        for player in self.players.values():
            if player.name.lower() == needle:
                return player
        return None

    def find_saved_player_name(self, name: str) -> Optional[str]:
        """Find a saved player's canonical name (case-insensitive)."""
        needle = name.lower()
        for saved in self.get_all_saved_players():
            if saved.lower() == needle:
                return saved
        return None

    def delete_player_save(self, name: str) -> bool:
        """Delete a player's save file. Returns True if a file was removed."""
        path = self._get_save_path(name)
        # Also try canonical saved name
        canonical = self.find_saved_player_name(name)
        if canonical:
            path = self._get_save_path(canonical)
        if path.exists():
            path.unlink()
            return True
        return False

    def create_reset_player(self, source: Player) -> Player:
        """Build a fresh character keeping name and password credentials."""
        fresh = Player(
            name=source.name,
            password_hash=source.password_hash,
            password_salt=source.password_salt,
        )
        return fresh
