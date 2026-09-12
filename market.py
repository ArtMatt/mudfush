"""
Market system for the MUD Fishing Game
Handles fluctuating prices for buying and selling
"""

import random
import asyncio
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Callable, Set
from enum import Enum

from items import (
    Item, ItemType, STORE_INVENTORY, WEARABLE_ITEMS, CONDITION_NAMES,
    colorize_condition, colorize_fish_size, UNSELLABLE_TYPES,
    BLUEGILL, BASS, CATFISH, TROUT, PIKE, LEGENDARY_CARP,
    MUD_CARP, PEBBLE_PERCH, MOON_DARTER, WALLEYE,
    STING_PUFFER, ZEN_GUPPY,
)


class StoreType(Enum):
    BUBBA = "bubba"  # Honest, fair prices
    SLICK = "slick"  # Shady, volatile prices


# Fish Bubba may request on his hourly bounty (no legendaries)
_BUBBA_QUEST_FISH = [
    BLUEGILL, PEBBLE_PERCH, BASS, MUD_CARP, WALLEYE, CATFISH, TROUT, PIKE,
    MOON_DARTER, STING_PUFFER, ZEN_GUPPY,
]

# Same size → weight multipliers as catch generation
_FISH_SIZE_WEIGHT = {
    "tiny": 0.55,
    "small": 0.78,
    "average": 1.0,
    "large": 1.35,
    "trophy": 1.8,
}


@dataclass
class PriceInfo:
    """Current price information for an item."""
    base_price: int
    current_buy_price: int  # What the store sells for
    current_sell_price: int  # What the store buys for
    trend: int  # -1 falling, 0 stable, 1 rising
    volatility: float  # How much prices can swing


@dataclass 
class StoreInventoryItem:
    """An item in a store's inventory."""
    item: Item
    quantity: int  # -1 for unlimited
    price_info: PriceInfo


@dataclass
class BubbaFishQuest:
    """Bubba's once-per-hour double-pay fish request."""
    fish_id: str
    fish_name: str
    fish_size: str
    condition: int
    target_weight: float
    claimed: bool = False
    claimed_by: Optional[str] = None

    def matches(self, item: Item) -> bool:
        """True if this fish fulfills the current request."""
        if item.item_type != ItemType.FISH:
            return False
        if item.id != self.fish_id:
            return False
        if (item.fish_size or "").lower() != self.fish_size:
            return False
        if item.condition != self.condition:
            return False
        # Weight is determined by species+size; allow tiny float drift
        return abs(item.weight - self.target_weight) < 0.15

    def offer_phrase(self) -> str:
        """Plain speech: I'll pay double for a nice average northern pike."""
        quality = CONDITION_NAMES.get(self.condition, "decent")
        return (
            f"I'll pay double for a {quality} {self.fish_size} {self.fish_name}"
        )

    def colored_target(self) -> str:
        """Colored quality + size + name for UI listings."""
        quality = colorize_condition(
            self.condition,
            CONDITION_NAMES.get(self.condition, "decent"),
        )
        size = colorize_fish_size(self.fish_size)
        return f"{quality} {size} {self.fish_name}"


class Market:
    """Manages market prices for all stores."""

    CLOTHING_MIN = 8
    CLOTHING_MAX = 20
    SLICK_CLOTHING_MIN = 2
    SLICK_CLOTHING_MAX = 4
    CLOTHING_ROTATION_SECONDS = 3600  # 1 hour
    
    def __init__(self):
        self.prices: Dict[str, Dict[str, PriceInfo]] = {
            StoreType.BUBBA.value: {},
            StoreType.SLICK.value: {},
        }
        self.clothing_stock: Dict[str, List[str]] = {
            StoreType.BUBBA.value: [],
            StoreType.SLICK.value: [],
        }
        # store -> player_name -> set of clothing item ids bought this rotation
        self.clothing_purchases: Dict[str, Dict[str, Set[str]]] = {
            StoreType.BUBBA.value: {},
            StoreType.SLICK.value: {},
        }
        self.last_update: float = time.time()
        self.last_clothing_rotation: float = time.time()
        self.bubba_quest: Optional[BubbaFishQuest] = None
        self._task: Optional[asyncio.Task] = None
        self._clothing_task: Optional[asyncio.Task] = None
        self._broadcast_callback: Optional[Callable] = None
        self._restock_callback: Optional[Callable] = None
        
        self._initialize_prices()
        self.rotate_clothing_stock(announce=False)
        self.rotate_bubba_quest(announce=False)
    
    def _initialize_prices(self):
        """Set up initial prices for both stores."""
        # Items sold at stores
        store_items = [item for item, _ in STORE_INVENTORY.values()]
        
        # Fish (only bought by stores, not sold)
        fish_items = [
            BLUEGILL, PEBBLE_PERCH, BASS, MUD_CARP, WALLEYE,
            CATFISH, TROUT, PIKE, MOON_DARTER, STING_PUFFER, ZEN_GUPPY,
            LEGENDARY_CARP,
        ]
        
        # Bubba's prices - fair and stable
        for item in store_items:
            self.prices[StoreType.BUBBA.value][item.id] = PriceInfo(
                base_price=item.value,
                current_buy_price=item.value,
                current_sell_price=int(item.value * 0.5),  # Bubba buys at 50%
                trend=0,
                volatility=0.1  # 10% max swing
            )
        
        for fish in fish_items:
            self.prices[StoreType.BUBBA.value][fish.id] = PriceInfo(
                base_price=fish.value,
                current_buy_price=0,  # Doesn't sell fish
                current_sell_price=int(fish.value * 0.8),  # Bubba pays 80% for fish
                trend=0,
                volatility=0.15
            )
        
        # Slick's prices - volatile and opportunistic  
        for item in store_items:
            # Slick charges more but sometimes has deals
            markup = random.uniform(1.1, 1.4)
            self.prices[StoreType.SLICK.value][item.id] = PriceInfo(
                base_price=item.value,
                current_buy_price=int(item.value * markup),
                current_sell_price=int(item.value * 0.4),  # Lowballs on buyback
                trend=random.choice([-1, 0, 1]),
                volatility=0.35  # 35% swings - very volatile
            )
        
        for fish in fish_items:
            # Slick's fish prices are all over the place
            self.prices[StoreType.SLICK.value][fish.id] = PriceInfo(
                base_price=fish.value,
                current_buy_price=0,
                current_sell_price=int(fish.value * random.uniform(0.5, 1.2)),
                trend=random.choice([-1, 0, 1]),
                volatility=0.5  # 50% swings on fish!
            )
    
    def rotate_clothing_stock(self, announce: bool = True) -> Dict[str, int]:
        """
        Pick a fresh unique clothing selection for each store.
        Bubba: 8-20 items. Slick: 2-4 items.
        Clears per-player clothing purchase locks for the new rotation.
        """
        all_wearable_ids = list(WEARABLE_ITEMS.keys())
        counts = {}

        for store_type in (StoreType.BUBBA, StoreType.SLICK):
            if store_type == StoreType.SLICK:
                lo, hi = self.SLICK_CLOTHING_MIN, self.SLICK_CLOTHING_MAX
            else:
                lo, hi = self.CLOTHING_MIN, self.CLOTHING_MAX
            count = random.randint(lo, hi)
            count = min(count, len(all_wearable_ids))
            # random.sample guarantees unique item ids in the listing
            selected = random.sample(all_wearable_ids, count)
            self.clothing_stock[store_type.value] = selected
            self.clothing_purchases[store_type.value] = {}
            counts[store_type.value] = count

        self.last_clothing_rotation = time.time()
        return counts

    def get_clothing_rotation_remaining(self) -> int:
        """Seconds until the next clothing stock rotation."""
        elapsed = time.time() - self.last_clothing_rotation
        remaining = self.CLOTHING_ROTATION_SECONDS - elapsed
        return max(0, int(remaining))

    def player_bought_clothing(
        self, store: StoreType, player_name: str, item_id: str
    ) -> bool:
        """True if this player already bought this clothing item this rotation."""
        bought = self.clothing_purchases.get(store.value, {}).get(player_name, set())
        return item_id in bought

    def mark_clothing_purchased(
        self, store: StoreType, player_name: str, item_id: str
    ) -> None:
        """Record that a player bought this clothing item this rotation."""
        by_player = self.clothing_purchases.setdefault(store.value, {})
        bought = by_player.setdefault(player_name, set())
        bought.add(item_id)

    def can_player_buy_clothing(
        self, store: StoreType, player_name: str, item_id: str
    ) -> bool:
        """True if clothing is stocked and this player hasn't bought it yet."""
        if item_id not in self.clothing_stock.get(store.value, []):
            return False
        return not self.player_bought_clothing(store, player_name, item_id)

    def rotate_bubba_quest(self, announce: bool = True) -> BubbaFishQuest:
        """
        Pick a new hourly fish bounty for Bubba.
        Requests a catchable species at a size/weight and quality (6-9).
        Only the first matching sale that hour gets double pay.
        """
        fish = random.choice(_BUBBA_QUEST_FISH)
        size = random.choice(list(_FISH_SIZE_WEIGHT.keys()))
        # Caught fish roll condition 6-9 — only request fulfillable qualities
        condition = random.choice([6, 7, 8, 9])
        target_weight = round(fish.weight * _FISH_SIZE_WEIGHT[size], 1)
        self.bubba_quest = BubbaFishQuest(
            fish_id=fish.id,
            fish_name=fish.name,
            fish_size=size,
            condition=condition,
            target_weight=target_weight,
        )
        return self.bubba_quest

    def get_bubba_quest(self) -> Optional[BubbaFishQuest]:
        return self.bubba_quest

    def bubba_quest_matches(self, item: Item) -> bool:
        """True if item matches the open (unclaimed) bounty."""
        quest = self.bubba_quest
        return bool(quest and not quest.claimed and quest.matches(item))

    def try_claim_bubba_quest(self, item: Item, player_name: str) -> bool:
        """
        Claim the bounty if this fish matches and it is still open.
        Returns True if this sale earns the double payout.
        """
        quest = self.bubba_quest
        if not quest or quest.claimed or not quest.matches(item):
            return False
        quest.claimed = True
        quest.claimed_by = player_name
        return True

    def get_bubba_quest_status_lines(self) -> List[str]:
        """Status lines for list/sell/examine UI."""
        quest = self.bubba_quest
        if not quest:
            return []
        target = quest.colored_target()
        if quest.claimed:
            who = quest.claimed_by or "someone"
            return [
                f'Bubba already got his {target} from {who} this hour.',
                "Next request when clothing rotates.",
            ]
        return [
            f'Bubba says, "{quest.offer_phrase()}."',
            f"(About {quest.target_weight} lb — first matching catch gets double!)",
        ]

    def is_item_for_sale(self, store: StoreType, item_id: str) -> bool:
        """True if the store currently sells this item."""
        if item_id not in STORE_INVENTORY:
            return False
        item, _ = STORE_INVENTORY[item_id]
        if item.item_type == ItemType.WEARABLE:
            return item_id in self.clothing_stock.get(store.value, [])
        # Fishing gear is always available
        return True

    def get_stocked_clothing(
        self, store: StoreType, player_name: Optional[str] = None
    ) -> List[Item]:
        """
        Return wearable items currently stocked at a store.
        If player_name is given, omit items that player already bought this rotation.
        """
        items = []
        for item_id in self.clothing_stock.get(store.value, []):
            if item_id not in WEARABLE_ITEMS:
                continue
            if player_name and self.player_bought_clothing(store, player_name, item_id):
                continue
            items.append(WEARABLE_ITEMS[item_id])
        return items

    def get_buy_price(self, store: StoreType, item_id: str) -> Optional[int]:
        """Get the price to BUY an item FROM a store."""
        if not self.is_item_for_sale(store, item_id):
            return None
        prices = self.prices.get(store.value, {})
        if item_id in prices:
            return prices[item_id].current_buy_price
        return None
    
    def get_sell_price(self, store: StoreType, item_id: str) -> Optional[int]:
        """Get the price a store will PAY for an item."""
        prices = self.prices.get(store.value, {})
        if item_id in prices:
            return prices[item_id].current_sell_price
        return None
    
    def get_price_info(self, store: StoreType, item_id: str) -> Optional[PriceInfo]:
        """Get full price info for an item at a store."""
        return self.prices.get(store.value, {}).get(item_id)
    
    def get_trend_symbol(self, trend: int) -> str:
        """Get a symbol representing price trend."""
        if trend > 0:
            return "↑"
        elif trend < 0:
            return "↓"
        return "→"

    def estimate_sell_price(
        self,
        store: StoreType,
        item: Item,
        charisma: int = 1,
    ) -> Optional[int]:
        """Estimate what a store will pay for a player's item."""
        if item.item_type in UNSELLABLE_TYPES:
            return None
        if store == StoreType.BUBBA and item.item_type != ItemType.FISH:
            return None

        sell_price = self.get_sell_price(store, item.id)
        if sell_price is None:
            if store == StoreType.SLICK:
                sell_price = int(item.value * 0.4)
            else:
                sell_price = int(item.value * 0.5)

        if item.item_type == ItemType.FISH:
            price_info = self.get_price_info(store, item.id)
            if price_info and price_info.base_price > 0:
                size_multiplier = item.value / price_info.base_price
                sell_price = max(1, round(sell_price * size_multiplier))
        elif store == StoreType.SLICK:
            # Slick's immediate buyback offer is deliberately much lower than
            # his shelf price. Market movement can still create a later profit.
            sell_price = max(1, round(sell_price * 0.5))

        sell_price = apply_condition_sell_price(sell_price, item.condition)
        offer = apply_charisma_sell_bonus(store, item, sell_price, charisma)
        if store == StoreType.SLICK and item.item_type != ItemType.FISH:
            current_buy = self.get_buy_price(store, item.id)
            if current_buy is not None and current_buy > 0:
                # Even one-gold goods cannot be bought and immediately sold
                # back without a loss. A future higher market can still beat
                # the price the player originally paid.
                offer = min(offer, max(0, current_buy - 1))
        return offer

    def _market_insight(self, store: StoreType, wisdom: int) -> List[str]:
        """Wisdom-gated market flavor text."""
        if wisdom <= 2:
            return ["Prices look about the same as always."]
        if wisdom <= 4:
            rising = falling = 0
            for info in self.prices.get(store.value, {}).values():
                if info.current_buy_price > 0 or info.current_sell_price > 0:
                    if info.trend > 0:
                        rising += 1
                    elif info.trend < 0:
                        falling += 1
            if rising > falling + 2:
                return ["You sense prices are drifting upward."]
            if falling > rising + 2:
                return ["You sense prices are softening a little."]
            return ["The market feels fairly steady."]
        return []
    
    def update_prices(self) -> Dict[str, List[str]]:
        """Update all prices. Returns dict of notable changes per store."""
        changes: Dict[str, List[str]] = {
            StoreType.BUBBA.value: [],
            StoreType.SLICK.value: [],
        }
        
        for store_type in [StoreType.BUBBA, StoreType.SLICK]:
            store_prices = self.prices[store_type.value]
            
            for item_id, price_info in store_prices.items():
                old_sell = price_info.current_sell_price
                old_buy = price_info.current_buy_price
                
                # Update trend randomly
                trend_change = random.choices(
                    [-1, 0, 1],
                    weights=[30, 40, 30]
                )[0]
                price_info.trend = max(-1, min(1, price_info.trend + trend_change))
                
                # Calculate price change
                volatility = price_info.volatility
                if store_type == StoreType.SLICK:
                    volatility *= 1.5  # Slick is extra volatile
                
                # Base change on trend
                trend_factor = price_info.trend * 0.05  # 5% per trend point
                random_factor = random.uniform(-volatility, volatility)
                total_change = 1 + trend_factor + random_factor
                
                # Apply to sell price
                new_sell = int(price_info.base_price * total_change)
                new_sell = max(1, min(new_sell, price_info.base_price * 2))  # Cap at 200%
                price_info.current_sell_price = new_sell
                
                # Apply to buy price (if store sells this item)
                if price_info.current_buy_price > 0:
                    buy_change = 1 + (trend_factor * 0.5) + (random_factor * 0.7)
                    new_buy = int(price_info.base_price * buy_change)
                    new_buy = max(
                        int(price_info.base_price * 0.7),
                        min(new_buy, int(price_info.base_price * 1.5))
                    )
                    price_info.current_buy_price = new_buy
                
                # Track significant changes (>15%)
                if old_sell > 0:
                    pct_change = abs(new_sell - old_sell) / old_sell
                    if pct_change > 0.15:
                        direction = "up" if new_sell > old_sell else "down"
                        changes[store_type.value].append(
                            f"{item_id} prices moved {direction}!"
                        )
        
        self.last_update = time.time()
        return changes
    
    def set_broadcast_callback(self, callback: Callable):
        """Set callback for broadcasting market changes."""
        self._broadcast_callback = callback

    def set_restock_callback(self, callback: Callable):
        """Set an async callback run after each hourly Bubba restock."""
        self._restock_callback = callback
    
    async def start(self, interval: int = 600):
        """Start the market update and clothing rotation loops."""
        async def market_loop():
            while True:
                await asyncio.sleep(interval)
                self.update_prices()

        async def clothing_loop():
            while True:
                await asyncio.sleep(self.CLOTHING_ROTATION_SECONDS)
                counts = self.rotate_clothing_stock()
                quest = self.rotate_bubba_quest()
                if self._broadcast_callback:
                    bubba_count = counts.get(StoreType.BUBBA.value, 0)
                    msg = (
                        "\n*** Clothing Restock: Bubba's has new apparel! "
                        f"({bubba_count} items) ***\n"
                        f'*** Bubba hollers, "{quest.offer_phrase()}!" ***'
                    )
                    await self._broadcast_callback(msg)
                if self._restock_callback:
                    await self._restock_callback()
        
        self._task = asyncio.create_task(market_loop())
        self._clothing_task = asyncio.create_task(clothing_loop())
    
    def stop(self):
        """Stop the market loops."""
        if self._task:
            self._task.cancel()
            self._task = None
        if self._clothing_task:
            self._clothing_task.cancel()
            self._clothing_task = None
    
    def get_for_sale_items(
        self, store: StoreType, player_name: Optional[str] = None
    ) -> List[Item]:
        """
        Return currently buyable store items in listing order.
        Fishing gear first, then stocked clothing (sorted by name).
        Clothing already bought by player_name this rotation is omitted.
        """
        items: List[Item] = []
        for item_id, (item, _) in STORE_INVENTORY.items():
            if item.item_type == ItemType.WEARABLE:
                continue
            if self.is_item_for_sale(store, item_id):
                info = self.prices.get(store.value, {}).get(item_id)
                if info and info.current_buy_price > 0:
                    items.append(item)

        for item in sorted(
            self.get_stocked_clothing(store, player_name=player_name),
            key=lambda i: i.name,
        ):
            info = self.prices.get(store.value, {}).get(item.id)
            if info and info.current_buy_price > 0:
                items.append(item)
        return items

    def get_for_sale_item_by_number(
        self,
        store: StoreType,
        number: int,
        player_name: Optional[str] = None,
    ) -> Optional[Item]:
        """Resolve a 1-based list number to a currently for-sale item."""
        items = self.get_for_sale_items(store, player_name=player_name)
        index = number - 1
        if 0 <= index < len(items):
            return items[index]
        return None

    def get_store_listing(
        self,
        store: StoreType,
        wisdom: int = 1,
        worn_slots: Optional[Set[str]] = None,
        player_name: Optional[str] = None,
    ) -> str:
        """Get formatted price listing. Wisdom controls market insight detail."""
        lines = []
        store_prices = self.prices[store.value]
        show_trends = wisdom >= 5
        show_relative = wisdom >= 8
        worn_slots = worn_slots or set()
        pink_star = "\033[95m*\033[0m"
        
        if store == StoreType.BUBBA:
            lines.append("\n" + "="*50)
            lines.append("  BUBBA'S BAIT & TACKLE - PRICES")
            lines.append("="*50)
            lines.append("\nFair prices, honest deals!\n")
            for quest_line in self.get_bubba_quest_status_lines():
                lines.append(quest_line)
            lines.append("")
        else:
            lines.append("\n" + "="*50)
            lines.append("  SLICK'S SURPLUS - TODAY'S PRICES")
            lines.append("="*50)
            if wisdom <= 2:
                lines.append("\nSlick grins. \"Best prices you'll find.\"\n")
            else:
                lines.append("\n*Prices subject to change*\n")

        insight = self._market_insight(store, wisdom)
        if insight:
            lines.extend(insight)
            lines.append("")

        for_sale = self.get_for_sale_items(store, player_name=player_name)
        gear_items = [i for i in for_sale if i.item_type != ItemType.WEARABLE]
        clothing_items = [i for i in for_sale if i.item_type == ItemType.WEARABLE]
        
        # Fishing equipment for sale
        lines.append("FISHING GEAR (for sale):")
        number = 1
        if not gear_items:
            lines.append("  (None)")
        else:
            for item in gear_items:
                info = store_prices[item.id]
                lines.append(
                    self._format_buy_line(
                        number, item, info, show_trends, show_relative
                    )
                )
                number += 1

        stocked_total = len(self.clothing_stock.get(store.value, []))
        lines.append(
            f"\nCLOTHING & ACCESSORIES "
            f"({len(clothing_items)} available to you / {stocked_total} stocked, "
            f"one each until restock):"
        )
        if not clothing_items:
            if stocked_total > 0:
                lines.append(
                    "  (You've already bought everything available this hour. "
                    "Wait for restock!)"
                )
            else:
                lines.append("  (Nothing in stock right now. Check back later!)")
        else:
            for item in clothing_items:
                info = store_prices[item.id]
                slot = item.wear_slot.value if item.wear_slot else "unknown"
                extra = f"[{slot}]"
                if slot not in worn_slots:
                    extra += f" {pink_star}"
                lines.append(
                    self._format_buy_line(
                        number, item, info, show_trends, show_relative,
                        extra=extra,
                    )
                )
                number += 1
        
        # Fish buying prices (not numbered — these are what the store pays)
        lines.append("\nFISH PRICES (we buy):")
        fish_ids = [
            "bluegill", "pebble_perch", "bass", "mud_carp", "walleye",
            "catfish", "trout", "pike", "moon_darter", "sting_puffer",
            "zen_guppy", "legendary_carp",
        ]
        fish_names = {
            "bluegill": "Bluegill",
            "pebble_perch": "Pebble Perch",
            "bass": "Largemouth Bass",
            "mud_carp": "Mud Carp",
            "walleye": "Walleye",
            "catfish": "Sleepy Catfish",
            "trout": "Rainbow Trout",
            "pike": "Northern Pike",
            "moon_darter": "Moon Darter",
            "sting_puffer": "Sting Puffer",
            "zen_guppy": "Zen Guppy",
            "legendary_carp": "Old Whiskers",
        }
        
        for fish_id in fish_ids:
            info = store_prices.get(fish_id)
            if not info:
                continue
            name = fish_names.get(fish_id, fish_id)
            line = f"  {name:<25} {info.current_sell_price:>4}g"
            if show_trends:
                line += f" {self.get_trend_symbol(info.trend)}"
            if show_relative and info.base_price > 0:
                pct = round(
                    ((info.current_sell_price - info.base_price) / info.base_price) * 100
                )
                if pct != 0:
                    line += f" ({pct:+d}% vs usual)"
            lines.append(line)

        lines.append("\nType 'buy <item>' or 'buy <#>' to purchase.")
        lines.append(f"{pink_star} = empty wear slot on you")
        if show_trends:
            lines.append("↑ = Rising  ↓ = Falling  → = Stable")
        if show_relative:
            lines.append("Percentages show how far prices sit from their usual value.")
        if wisdom < 5:
            lines.append("(Higher Wisdom reveals more about shifting prices.)")
        lines.append("Clothing selection changes every hour — one of each per player until then.")
        if store == StoreType.SLICK and wisdom >= 3:
            lines.append("Check back often—Slick's prices move a lot.")
        
        return "\n".join(lines)

    def _format_buy_line(
        self,
        number: int,
        item: Item,
        info: PriceInfo,
        show_trends: bool,
        show_relative: bool,
        extra: str = "",
    ) -> str:
        """Format one for-sale price line with wisdom-gated details."""
        line = f"  {number}) {item.name:<23} {info.current_buy_price:>4}g"
        if show_trends:
            line += f" {self.get_trend_symbol(info.trend)}"
        if show_relative and info.base_price > 0:
            pct = round(
                ((info.current_buy_price - info.base_price) / info.base_price) * 100
            )
            if pct != 0:
                line += f" ({pct:+d}% vs usual)"
        if extra:
            line += f" {extra}"
        return line


def apply_condition_sell_price(base_price: int, condition: int) -> int:
    """
    Scale a sell offer by item condition.
    Broken (0) always pays 1 gold; new (9) pays full price.
    """
    if condition <= 0:
        return 1
    return max(1, round(base_price * condition / 9))


def apply_charisma_sell_bonus(
    store: StoreType,
    item: Item,
    price: int,
    charisma: int,
) -> int:
    """
    Charisma helps a lot when selling fish to Bubba.
    Slick barely cares about charm.
    """
    cha = max(1, charisma)
    if store == StoreType.BUBBA and item.item_type == ItemType.FISH:
        # +5% per Charisma point above 1
        multiplier = 1.0 + (cha - 1) * 0.05
    elif store == StoreType.SLICK:
        # +0.5% per Charisma point above 1
        multiplier = 1.0 + (cha - 1) * 0.005
    else:
        return max(1, price)
    return max(1, round(price * multiplier))


# Global market instance (will be initialized by server)
market: Optional[Market] = None
