"""Persistent state for the lake's unique roaming catch."""

import json
from pathlib import Path

from items import ANCIENT_WHISKERS, create_item_copy


STATE_PATH = Path("saves/.water_cycle.json")
ANCIENT_START_WEIGHT = 46.0
OLD_WHISKERS_MAX_WEIGHT = 45.0


class LakeCycleState:
    """Track whether Ancient Whiskers is catchable and its current weight."""

    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        self.weight = ANCIENT_START_WEIGHT
        self.available = True
        self._load()

        # A restart disconnects every holder. Treat any in-flight reservation
        # as returned to the lake.
        self.available = True
        self.save()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
            self.weight = max(
                ANCIENT_START_WEIGHT,
                float(data.get("weight", ANCIENT_START_WEIGHT)),
            )
            self.available = bool(data.get("available", True))
        except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError):
            self.weight = ANCIENT_START_WEIGHT
            self.available = True

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "weight": self.weight,
            "available": self.available,
        }))
        temporary.replace(self.path)

    def reserve(self) -> bool:
        """Remove the fish from the catch pool if it is currently available."""
        if not self.available:
            return False
        self.available = False
        self.save()
        return True

    def release(self, *, grow: bool = False) -> None:
        """Return the fish to the catch pool, optionally one pound heavier."""
        if grow:
            self.weight = round(self.weight + 1.0, 1)
        self.available = True
        self.save()

    def payout(self, weight: float = None) -> int:
        """Bubba pays 500g plus 10g per pound above Old Whiskers' maximum."""
        current = self.weight if weight is None else weight
        extra = max(0.0, current - OLD_WHISKERS_MAX_WEIGHT)
        return 500 + round(extra * 10)

    def create_catch(self):
        """Create the fixed-size fish at its persisted weight and payout."""
        fish = create_item_copy(ANCIENT_WHISKERS)
        fish.weight = self.weight
        fish.value = self.payout(self.weight)
        fish.fish_size = None
        fish.description = (
            f"{ANCIENT_WHISKERS.description} It weighs {fish.weight:.1f} lbs."
        )
        return fish
