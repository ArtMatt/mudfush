"""Roaming fishing NPCs with hidden identities and per-player cooldowns."""

import random
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from camper import FISHING_HOLE_IDS
from world import Room


FISHERMAN_COOLDOWN_SECONDS = 60
NPC_CATCH_MAX_SECONDS = 25
SHARE_CHARISMA = 12
SHARE_SECONDS = 15 * 60


@dataclass(frozen=True)
class Fisherman:
    name: str
    display: str
    description: str
    greeting: str
    wrong_name: str
    elsewhere: str
    staying: str
    vague_prefix: str
    exact_prefix: str

    def matches_target(self, target: str) -> bool:
        cleaned = target.strip().lower()
        adjective = self.display.replace(" Fisherman", "").lower()
        return cleaned in {
            self.display.lower(),
            adjective,
            f"{adjective} fisherman",
            "fisherman",
            "fisher",
            self.name.lower(),
        }


FISHERMEN = (
    Fisherman(
        name="Walt",
        display="Rangy Fisherman",
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
        display="Hatted Fisherman",
        description="A patient fisherman in a broad straw hat studies every ripple between casts.",
        greeting='"Hello, name\'s June."',
        wrong_name='"You have me confused with somebody else."',
        elsewhere='"The birds are feeding better over another stretch of water."',
        staying='"The little signs all say to stay right here."',
        vague_prefix='"Watch the water',
        exact_prefix='"The strongest signs are at',
    ),
    Fisherman(
        name="Otis",
        display="Beaded Fisherman",
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
        display="Scarfed Fisherman",
        description="A precise fisherman in a red scarf keeps immaculate tackle and a guarded eye on the lake.",
        greeting='"Hello. I\'m Mara."',
        wrong_name='"Try the right name next time."',
        elsewhere='"I\'ve seen better action somewhere else, but I\'m not doing all your work."',
        staying='"You could leave, but you\'d be giving up the best water."',
        vague_prefix='"I\'d put my next cast',
        exact_prefix='"The best spot is',
    ),
)


@dataclass
class FishShare:
    """One patron hiring one fisherman to hand over catches."""

    player_name: str
    fisherman_name: str
    room_id: str
    expires_at: float


class FishermanManager:
    """Assign one unique fisherman to every fishing room and manage interactions."""

    def __init__(self, rooms: Dict[str, Room]):
        self.rooms = rooms
        self.fishing_room_ids = [
            room.id for room in rooms.values()
            if room.is_water and room.id not in FISHING_HOLE_IDS
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
        self._shares_by_player: Dict[str, FishShare] = {}
        self._shares_by_fisher: Dict[str, FishShare] = {}
        self._sync_room_displays()

    def _sync_room_displays(self) -> None:
        displays = {npc.display.lower() for npc in FISHERMEN}
        for room_id in self.fishing_room_ids:
            room = self.rooms[room_id]
            room.npcs = [
                name for name in room.npcs
                if name.lower() not in displays
            ]
        for npc in FISHERMEN:
            self.rooms[self.assignments[npc.name]].npcs.append(npc.display)

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

    def start_share(
        self,
        player_name: str,
        fisherman_name: str,
        room_id: str,
        now: Optional[float] = None,
    ) -> str:
        """
        Hire this fisherman to hand their catches to one player.

        Returns started, renewed, or busy. An expired deal is dropped first.
        Renewing keeps the same session, so a fish already on the line
        still belongs to this player.
        """
        current = time.time() if now is None else now
        existing = self._shares_by_fisher.get(fisherman_name)
        if existing is not None and existing.expires_at <= current:
            self._drop_share(existing)
            existing = None
        if (
            existing is not None
            and existing.player_name.lower() != player_name.lower()
        ):
            return "busy"

        mine = self._shares_by_player.get(player_name.lower())
        if mine is not None and mine is not existing:
            self._drop_share(mine)
        if existing is not None:
            existing.expires_at = current + SHARE_SECONDS
            existing.room_id = room_id
            return "renewed"

        share = FishShare(
            player_name=player_name,
            fisherman_name=fisherman_name,
            room_id=room_id,
            expires_at=current + SHARE_SECONDS,
        )
        self._shares_by_player[player_name.lower()] = share
        self._shares_by_fisher[fisherman_name] = share
        return "started"

    def active_share_for(
        self,
        fisherman_name: str,
        now: Optional[float] = None,
    ) -> Optional["FishShare"]:
        """The live deal for this fisherman, if it has not run out."""
        share = self._shares_by_fisher.get(fisherman_name)
        if share is None:
            return None
        current = time.time() if now is None else now
        if share.expires_at <= current:
            return None
        return share

    def hooked_share(
        self,
        fisherman_name: str,
        room_id: str,
        now: Optional[float] = None,
    ) -> Optional["FishShare"]:
        """The deal a fish on the line belongs to, if the patron is still here."""
        share = self.active_share_for(fisherman_name, now)
        if share is None or share.room_id != room_id:
            return None
        if self.assignments.get(fisherman_name) != room_id:
            return None
        if not self.patron_in_room(share):
            return None
        return share

    def still_hooked(self, share: "FishShare") -> bool:
        """True if this same deal is still the fisherman's current one."""
        return self._shares_by_fisher.get(share.fisherman_name) is share

    def patron_in_room(self, share: "FishShare") -> bool:
        room = self.rooms.get(share.room_id)
        return room is not None and share.player_name in room.players

    def expire_share(self, share: "FishShare", now: Optional[float] = None) -> bool:
        """Drop this deal if it is still current and its time is up."""
        if self._shares_by_fisher.get(share.fisherman_name) is not share:
            return False
        current = time.time() if now is None else now
        if current < share.expires_at:
            return False
        self._drop_share(share)
        return True

    def end_share_for_player(self, player_name: str) -> None:
        share = self._shares_by_player.get(player_name.lower())
        if share is not None:
            self._drop_share(share)

    def end_share_if_left(self, player_name: str, room_id: str) -> None:
        """End the deal when the patron is no longer in the hired room."""
        share = self._shares_by_player.get(player_name.lower())
        if share is not None and share.room_id != room_id:
            self._drop_share(share)

    def _drop_share(self, share: "FishShare") -> None:
        if self._shares_by_fisher.get(share.fisherman_name) is share:
            del self._shares_by_fisher[share.fisherman_name]
        if self._shares_by_player.get(share.player_name.lower()) is share:
            del self._shares_by_player[share.player_name.lower()]

    def relocate(self) -> List[Tuple[Fisherman, str, str]]:
        """Move every fisherman to a different fishing room."""
        self._shares_by_player.clear()
        self._shares_by_fisher.clear()
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
