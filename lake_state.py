"""Persistent state for the lake's world-unique ancient fish."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from items import CATCHABLE_FISH, Item, ItemType


STATE_PATH = Path("saves/.water_cycle.json")
ANCIENT_START_WEIGHT = 46.0
OLD_WHISKERS_MAX_WEIGHT = 45.0


@dataclass(frozen=True)
class UniqueFishSpec:
    base_id: str
    unique_id: str
    name: str
    description: str
    start_weight: float
    start_rarity: int
    base_payout: int


def unique_fish_id(base_id: str) -> str:
    if base_id == "legendary_carp":
        return "ancient_whiskers"
    return f"ancient_{base_id}"


def base_fish_id(unique_id: str) -> str:
    if unique_id == "ancient_whiskers":
        return "legendary_carp"
    if unique_id.startswith("ancient_"):
        return unique_id[len("ancient_"):]
    return unique_id


def is_unique_fish_id(item_id: str) -> bool:
    return base_fish_id(item_id) in UNIQUE_FISH_SPECS and item_id != base_fish_id(item_id)


def _build_unique_specs() -> Dict[str, UniqueFishSpec]:
    specs = {}
    for fish, table_weight in CATCHABLE_FISH:
        if fish.id == "legendary_carp":
            name = "Ancient Whiskers"
            description = (
                "An impossibly old carp marked by deep scars and long silver "
                "whiskers. The lake itself seems quieter around it."
            )
            start_weight = ANCIENT_START_WEIGHT
            base_payout = 500
        else:
            name = f"Ancient {fish.name}"
            description = (
                f"An impossibly old {fish.name}, larger than any ordinary "
                "member of its species. Scars and age mark its body."
            )
            # One tenth of a pound above the normal trophy weight.
            start_weight = round(fish.weight * 1.8 + 0.1, 1)
            base_payout = max(100, fish.value * 10)
        specs[fish.id] = UniqueFishSpec(
            base_id=fish.id,
            unique_id=unique_fish_id(fish.id),
            name=name,
            description=description,
            start_weight=start_weight,
            start_rarity=max(100, table_weight),
            base_payout=base_payout,
        )
    return specs


UNIQUE_FISH_SPECS = _build_unique_specs()


class LakeCycleState:
    """Track availability, growing weight, and growing rarity per ancient."""

    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        self.fish = {
            base_id: {
                "weight": spec.start_weight,
                "rarity": spec.start_rarity,
                "available": True,
            }
            for base_id, spec in UNIQUE_FISH_SPECS.items()
        }
        self._load()

        # A restart disconnects every holder. Return all held/in-flight uniques.
        for state in self.fish.values():
            state["available"] = True
        self.save()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
            saved_fish = data.get("fish")
            if isinstance(saved_fish, dict):
                for base_id, spec in UNIQUE_FISH_SPECS.items():
                    saved = saved_fish.get(base_id, {})
                    state = self.fish[base_id]
                    state["weight"] = max(
                        spec.start_weight,
                        float(saved.get("weight", spec.start_weight)),
                    )
                    state["rarity"] = max(
                        spec.start_rarity,
                        int(saved.get("rarity", spec.start_rarity)),
                    )
                    state["available"] = bool(saved.get("available", True))
            else:
                # Migrate the original single-Ancient-Whiskers save.
                spec = UNIQUE_FISH_SPECS["legendary_carp"]
                state = self.fish["legendary_carp"]
                state["weight"] = max(
                    spec.start_weight,
                    float(data.get("weight", spec.start_weight)),
                )
                state["available"] = bool(data.get("available", True))
        except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"fish": self.fish}))
        temporary.replace(self.path)

    @property
    def weight(self) -> float:
        """Backward-compatible Ancient Whiskers weight."""
        return float(self.fish["legendary_carp"]["weight"])

    @property
    def available(self) -> bool:
        """Backward-compatible Ancient Whiskers availability."""
        return bool(self.fish["legendary_carp"]["available"])

    def _base_id(self, identifier: str) -> str:
        return base_fish_id(identifier)

    def is_available(self, identifier: str) -> bool:
        state = self.fish.get(self._base_id(identifier))
        return bool(state and state["available"])

    def rarity(self, identifier: str) -> int:
        state = self.fish[self._base_id(identifier)]
        return int(state["rarity"])

    def reserve(self, identifier: str = "legendary_carp") -> bool:
        """Remove one ancient from the catch pool while it is in flight/held."""
        base_id = self._base_id(identifier)
        state = self.fish.get(base_id)
        if not state or not state["available"]:
            return False
        state["available"] = False
        self.save()
        return True

    def release(
        self, identifier: str = "ancient_whiskers", *, grow: bool = False
    ) -> None:
        """Return an ancient, growing weight and rarity after Bubba buys it."""
        base_id = self._base_id(identifier)
        state = self.fish.get(base_id)
        if not state:
            return
        if grow:
            state["weight"] = round(float(state["weight"]) + 1.0, 1)
            state["rarity"] = int(state["rarity"]) + 1
        state["available"] = True
        self.save()

    def payout(self, identifier="ancient_whiskers", weight: float = None) -> int:
        """Bubba pays a species base plus 10g per pound of ancient growth."""
        if isinstance(identifier, (int, float)):
            weight = float(identifier)
            identifier = "ancient_whiskers"
        base_id = self._base_id(identifier)
        spec = UNIQUE_FISH_SPECS[base_id]
        current = (
            float(self.fish[base_id]["weight"])
            if weight is None else float(weight)
        )
        growth_floor = (
            OLD_WHISKERS_MAX_WEIGHT
            if base_id == "legendary_carp"
            else spec.start_weight
        )
        extra = max(0.0, current - growth_floor)
        return spec.base_payout + round(extra * 10)

    def create_catch(self, identifier: str = "legendary_carp") -> Item:
        """Create one fixed-size ancient from its persisted state."""
        base_id = self._base_id(identifier)
        spec = UNIQUE_FISH_SPECS[base_id]
        weight = float(self.fish[base_id]["weight"])
        return Item(
            id=spec.unique_id,
            name=spec.name,
            description=f"{spec.description} It weighs {weight:.1f} lbs.",
            item_type=ItemType.FISH,
            takeable=True,
            value=self.payout(base_id, weight),
            weight=weight,
            condition=9,
            fish_size=None,
        )
