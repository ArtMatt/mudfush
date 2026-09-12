"""Roaming fishing NPCs with hidden identities and per-player cooldowns."""

import random
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from world import Room


FISHERMAN_DISPLAY_NAME = "Tall Fisherman"
FISHERMAN_COOLDOWN_SECONDS = 60


@dataclass(frozen=True)
class Fisherman:
    name: str
    description: str
    greeting: str
    wrong_name: str
    elsewhere: str
    staying: str
    vague_prefix: str
    exact_prefix: str


FISHERMEN = (
    Fisherman(
        name="Walt",
        description="A rangy older fisherman in a patched canvas coat watches his line without blinking.",
        greeting='"Hello. Name\'s Walt."',
        wrong_name='"That ain\'t my name."',
        elsewhere='"Water\'s too quiet here. I\'d try somewhere else."',
        staying='"I wouldn\'t be in a hurry to leave this water."',
        vague_prefix='"If I were moving, I\'d look',
        exact_prefix='"Best water right now is',
    ),
    Fisherman(
        name="June",
        description="A tall fisherman in a broad straw hat studies every ripple between patient casts.",
        greeting='"Hello, name\'s June."',
        wrong_name='"You have me confused with somebody else."',
        elsewhere='"The birds are feeding better over another stretch of water."',
        staying='"The little signs all say to stay right here."',
        vague_prefix='"Watch the water',
        exact_prefix='"The strongest signs are at',
    ),
    Fisherman(
        name="Otis",
        description="A lanky fisherman hung with old charms mutters to the bobber as if it can answer.",
        greeting='"Howdy. Otis is the name."',
        wrong_name='"Wrong soul, wrong name."',
        elsewhere='"This spot\'s luck has wandered off someplace else."',
        staying='"Luck\'s sitting beside us today. Best not chase it off."',
        vague_prefix='"The lucky water lies',
        exact_prefix='"Luck has settled at',
    ),
    Fisherman(
        name="Mara",
        description="A tall fisherman in a red scarf keeps immaculate tackle and a guarded eye on the lake.",
        greeting='"Hello. I\'m Mara."',
        wrong_name='"Try the right name next time."',
        elsewhere='"I\'ve seen better action somewhere else, but I\'m not doing all your work."',
        staying='"You could leave, but you\'d be giving up the best water."',
        vague_prefix='"I\'d put my next cast',
        exact_prefix='"The best spot is',
    ),
)


class FishermanManager:
    """Assign one unique fisherman to every fishing room and manage interactions."""

    def __init__(self, rooms: Dict[str, Room]):
        self.rooms = rooms
        self.fishing_room_ids = [
            room.id for room in rooms.values() if room.is_water
        ]
        if len(self.fishing_room_ids) != len(FISHERMEN):
            raise ValueError(
                "The number of unique fishermen must match the number of fishing rooms"
            )

        shuffled = list(FISHERMEN)
        random.shuffle(shuffled)
        self.assignments: Dict[str, str] = {
            npc.name: room_id
            for npc, room_id in zip(shuffled, self.fishing_room_ids)
        }
        self._cooldowns: Dict[Tuple[str, str], float] = {}
        self._ignored_once: set[Tuple[str, str]] = set()
        self._sync_room_displays()

    def _sync_room_displays(self) -> None:
        for room_id in self.fishing_room_ids:
            room = self.rooms[room_id]
            room.npcs = [
                name for name in room.npcs
                if name.lower() != FISHERMAN_DISPLAY_NAME.lower()
            ]
        for room_id in self.assignments.values():
            self.rooms[room_id].npcs.append(FISHERMAN_DISPLAY_NAME)

    def in_room(self, room_id: str) -> Optional[Fisherman]:
        for npc in FISHERMEN:
            if self.assignments.get(npc.name) == room_id:
                return npc
        return None

    def named_in(self, message: str) -> Optional[Fisherman]:
        """Return the first fisherman whose secret name appears as a word."""
        for npc in FISHERMEN:
            if re.search(rf"\b{re.escape(npc.name)}\b", message, re.IGNORECASE):
                return npc
        return None

    def target_named(self, target: str) -> Optional[Fisherman]:
        cleaned = target.strip().lower()
        return next((npc for npc in FISHERMEN if npc.name.lower() == cleaned), None)

    @staticmethod
    def is_display_target(target: str) -> bool:
        return target.strip().lower() in {
            "tall fisherman", "fisherman", "tall", "fisher",
        }

    def begin_interaction(
        self,
        player_name: str,
        npc: Fisherman,
        now: Optional[float] = None,
    ) -> str:
        """
        Return active, ignored, or silent.

        Nod and say share one cooldown. The first repeat gets an ignore reaction;
        further repeats during that cooldown get no NPC reaction.
        """
        current = time.time() if now is None else now
        key = (player_name.lower(), npc.name.lower())
        if current >= self._cooldowns.get(key, 0):
            self._cooldowns[key] = current + FISHERMAN_COOLDOWN_SECONDS
            self._ignored_once.discard(key)
            return "active"
        if key not in self._ignored_once:
            self._ignored_once.add(key)
            return "ignored"
        return "silent"

    def relocate(self) -> List[Tuple[Fisherman, str, str]]:
        """Move every fisherman to a different fishing room."""
        if len(FISHERMEN) < 2:
            return []
        old_rooms = [self.assignments[npc.name] for npc in FISHERMEN]
        offset = random.randint(1, len(FISHERMEN) - 1)
        moves = []
        for index, npc in enumerate(FISHERMEN):
            old_room = old_rooms[index]
            new_room = old_rooms[(index + offset) % len(old_rooms)]
            self.assignments[npc.name] = new_room
            moves.append((npc, old_room, new_room))
        self._sync_room_displays()
        return moves
