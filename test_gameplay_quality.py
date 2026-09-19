import asyncio
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

from commands import (
    CATCHABLE_FISH,
    GEM_CATCH_CHANCE,
    GameCommands,
    MOUTH_LURE_CATCH_CHANCE,
    NORM_MAX_GUESSES,
    NormRound,
    ReelChallenge,
)
from fishermen import FISHERMEN, FishermanManager, NPC_CATCH_MAX_SECONDS
from items import (
    ANCIENT_WHISKERS,
    ATTRIBUTES,
    BASIC_POLE,
    BASS,
    BLUEGILL,
    BUCKET_OF_CHUM,
    FISHING_HAT,
    GOLDEN_LURE,
    LEGENDARY_CARP,
    MUD_CARP,
    NIGHTCRAWLERS,
    PLASTIC_WORM,
    PRO_ROD,
    SPECIALTY_LURE,
    TROUT,
    ItemType,
    STORE_INVENTORY,
    create_item_copy,
    create_mouth_hooked_golden_lure,
    specialty_lure_essence_from_fish,
    specialty_lure_multiplier,
)
from lake_state import LakeCycleState
from market import BubbaFishQuest, Market, StoreType
from mud_server import FishingMUD, MUDSession, handle_client
from player import Player, PlayerManager
from weather import FORECAST_DEPTH, WeatherSystem
from world import create_world, population_shift_message, POPULATION_RISE_MESSAGES, POPULATION_FALL_MESSAGES


class GameplayQualityTests(unittest.TestCase):
    def setUp(self):
        self.rooms = create_world()
        self.market = Market()
        self.commands = GameCommands(
            self.rooms, Mock(), weather=None, market=self.market
        )

    def test_appraisal_gate_and_specific_is_more_accurate(self):
        player = Player("angler")
        fish = create_item_copy(BLUEGILL, roll_stats=False, condition=9)
        player.add_item(fish)

        blocked = self.commands.cmd_appraise(player, "")
        self.assertEqual(
            blocked.message,
            "You attempt to appraise the value of your fish, "
            "but it could be any number...",
        )

        player.attributes["intelligence"] = 2
        player.attributes["wisdom"] = 2
        specific = self.commands._appraisal_range(100, 6, total=False)
        total = self.commands._appraisal_range(100, 6, total=True)
        self.assertLess(specific[1] - specific[0], total[1] - total[0])
        self.assertIn("Bubba would pay about $", self.commands.cmd_appraise(
            player, fish.name
        ).message)

    def test_failed_consider_does_not_broadcast_mutter(self):
        player = Player("angler", current_room="old_pier")
        player.attributes["intelligence"] = 1
        player.attributes["wisdom"] = 1
        self.rooms["old_pier"].population = 20

        with patch("commands.random.randint", return_value=100):
            failed = self.commands.cmd_consider(player, "")
        self.assertIn("cannot judge", failed.message)
        self.assertIsNone(failed.broadcast)

        with patch("commands.random.randint", return_value=1):
            succeeded = self.commands.cmd_consider(player, "")
        self.assertIsNotNone(succeeded.broadcast)
        self.assertIn("mumbles to themselves briefly", succeeded.broadcast)

    def test_slick_immediate_nonfish_buyback_always_loses_gold(self):
        for template, _ in STORE_INVENTORY.values():
            if template.item_type == ItemType.FISH:
                continue
            item = create_item_copy(template, condition=9)
            buy = self.market.get_buy_price(StoreType.SLICK, item.id)
            if buy is None:
                # Clothing can only be bought when it is in this rotation.
                continue
            offer = self.market.estimate_sell_price(
                StoreType.SLICK, item, charisma=20
            )
            self.assertLess(
                offer,
                buy,
                f"{item.id} could be resold for {offer} after buying for {buy}",
            )

        for _ in range(25):
            self.market.update_prices()
            for template, _ in STORE_INVENTORY.values():
                item = create_item_copy(template, condition=9)
                buy = self.market.get_buy_price(StoreType.SLICK, item.id)
                if buy is None:
                    continue
                offer = self.market.estimate_sell_price(
                    StoreType.SLICK, item, charisma=20
                )
                self.assertLess(offer, buy)

    def test_slick_refuses_to_buy_jigs(self):
        player = Player("angler", current_room="slick_store")
        jig = create_item_copy(SPECIALTY_LURE, roll_stats=False, condition=9)
        player.add_item(jig)

        listing = self.commands._format_sell_offer_list(player, StoreType.SLICK)
        self.assertNotIn("jig", listing.lower())
        self.assertIsNone(
            self.market.estimate_sell_price(StoreType.SLICK, jig, charisma=20)
        )

        result = self.commands.cmd_sell(player, "jig")
        self.assertIn("Cliff's work", result.message)
        self.assertIn(jig, player.inventory)

    def test_bare_eq_shows_equipment(self):
        result = self.commands.cmd_equip(Player("angler"), "")
        self.assertIn("EQUIPMENT", result.message)
        self.assertIn("pole", result.message)

    def test_cutting_the_line_never_wears_the_pole(self):
        player = Player("angler")
        pole = create_item_copy(BASIC_POLE, condition=9)
        lure = create_item_copy(PLASTIC_WORM, condition=9)
        player.inventory.extend([pole, lure])
        player.equipped_pole = pole
        player.equipped_lure = lure
        with patch("player.random.randint", return_value=1):
            player.degrade_fishing_gear(include_pole=False)
        self.assertEqual(pole.condition, 9)
        self.assertLess(lure.condition, 9)

    def test_water_room_colors_fish_light_blue(self):
        water = next(room for room in self.rooms.values() if room.is_water)
        self.assertIn(
            "(You can \033[96mFISH\033[0m here)",
            water.get_description(),
        )

    def test_catch_line_includes_post_catch_level_progress(self):
        player = Player("angler")
        player.level = 4
        player.total_weight_caught = 614.5
        player.equipped_pole = create_item_copy(BASIC_POLE, condition=9)
        player.inventory.append(player.equipped_pole)
        water = next(room for room in self.rooms.values() if room.is_water)
        player.current_room = water.id
        water.population = 100
        caught = create_item_copy(BLUEGILL, roll_stats=False, condition=9)
        caught.weight = 0.4
        caught.fish_size = "small"
        player.degrade_fishing_gear = Mock(return_value=[])

        with (
            patch.object(self.commands, "_select_fish", return_value=BLUEGILL),
            patch.object(self.commands, "_create_sized_fish", return_value=caught),
            patch("commands.random.randint", return_value=2),
        ):
            result = self.commands.cmd_fish(player, "")
            message, _ = result.deferred()

        self.assertIn("(0.4 lbs) [614.9/800]", message)

    def test_rare_catch_can_yield_a_triple_stat_golden_lure(self):
        player = Player("angler")
        player.equipped_pole = create_item_copy(BASIC_POLE, condition=9)
        player.inventory.append(player.equipped_pole)
        water = next(room for room in self.rooms.values() if room.is_water)
        player.current_room = water.id
        water.population = 100
        caught = create_item_copy(BLUEGILL, roll_stats=False, condition=9)
        caught.weight = 0.4
        caught.fish_size = "small"
        player.degrade_fishing_gear = Mock(return_value=[])

        def randint(low, high):
            if high == 100:
                return 1
            if high == GEM_CATCH_CHANCE:
                return 2
            if high == MOUTH_LURE_CATCH_CHANCE:
                return 1
            return 2

        with (
            patch.object(self.commands, "_select_fish", return_value=BLUEGILL),
            patch.object(self.commands, "_create_sized_fish", return_value=caught),
            patch("commands.random.randint", side_effect=randint),
        ):
            result = self.commands.cmd_fish(player, "")
            message, extra = result.deferred()

        self.assertIn(
            "You notice this fish had a lure already hooked in its mouth. "
            "You carefully remove it.",
            message,
        )
        self.assertIn(
            "Someone is probably kicking themselves for losing this lure...",
            message,
        )
        self.assertIsNone(extra)
        lures = [item for item in player.inventory if item.id == "golden_lure"]
        self.assertEqual(len(lures), 1)
        self.assertEqual(lures[0].condition, 9)
        self.assertEqual(len(lures[0].modifiers), 3)
        self.assertEqual({value for _, value in lures[0].modifiers}, {1})
        self.assertEqual(len({attr for attr, _ in lures[0].modifiers}), 3)
        for attr, _ in lures[0].modifiers:
            self.assertIn(attr, ATTRIBUTES)

    def test_mouth_hooked_golden_lure_has_three_distinct_plus_ones(self):
        lure = create_mouth_hooked_golden_lure()
        self.assertEqual(lure.id, GOLDEN_LURE.id)
        self.assertEqual(lure.condition, 9)
        self.assertEqual(sorted(lure.modifiers), sorted((a, 1) for a, _ in lure.modifiers))
        self.assertEqual(len(lure.modifiers), 3)
        self.assertEqual(len(set(attr for attr, _ in lure.modifiers)), 3)

    def test_cut_reminder_only_on_first_two_hooks_this_login(self):
        player = Player("angler")
        player.equipped_pole = create_item_copy(BASIC_POLE, condition=9)
        player.inventory.append(player.equipped_pole)
        water = next(room for room in self.rooms.values() if room.is_water)
        player.current_room = water.id
        water.population = 100
        caught = create_item_copy(BLUEGILL, roll_stats=False, condition=9)
        caught.weight = 0.4
        caught.fish_size = "small"
        player.degrade_fishing_gear = Mock(return_value=[])
        reminder = "(Type CUT, or press Ctrl-G, to snap the line.)"

        def hook_text():
            result = self.commands.cmd_fish(player, "")
            return result.stages[0].message

        with (
            patch.object(self.commands, "_select_fish", return_value=BLUEGILL),
            patch.object(self.commands, "_create_sized_fish", return_value=caught),
            patch("commands.random.randint", return_value=2),
        ):
            first = hook_text()
            second = hook_text()
            third = hook_text()

        self.assertIn(reminder, first)
        self.assertIn(reminder, second)
        self.assertNotIn(reminder, third)

    def test_bubba_quest_payout_slides_a_gem_without_advertising_it(self):
        player = Player("angler", current_room="store")
        fish = create_item_copy(BLUEGILL, roll_stats=False, condition=8)
        fish.fish_size = "average"
        fish.weight = 0.5
        player.add_item(fish)
        self.market.bubba_quest.fish_id = "bluegill"
        self.market.bubba_quest.fish_name = "bluegill"
        self.market.bubba_quest.fish_size = "average"
        self.market.bubba_quest.condition = 8
        self.market.bubba_quest.target_weight = 0.5
        self.market.bubba_quest.claimed = False

        listing = self.commands._format_sell_offer_list(player, StoreType.BUBBA)
        self.assertIn("DOUBLE BOUNTY", listing)
        self.assertNotRegex(listing.lower(), r"gem")
        status = "\n".join(self.market.get_bubba_quest_status_lines())
        self.assertNotRegex(status.lower(), r"gem")

        result = self.commands.cmd_sell(player, "bluegill")
        gems = [item for item in player.inventory if item.item_type == ItemType.GEM]
        self.assertEqual(len(gems), 1)
        self.assertIn("You earned this, too", result.message)
        self.assertIn(gems[0].display_name, result.message)
        self.assertIn(f"(Your gold: {player.gold})", result.message)
        self.assertNotIn("You now have", result.message)
        self.assertNotIn("gem", result.broadcast.lower())

    def test_bubba_quest_timer_is_independent_of_clothing_restock(self):
        quest = self.market.bubba_quest
        due = self.market.bubba_quest_due_at
        self.assertIsNotNone(quest)
        self.assertGreater(self.market.seconds_until_bubba_quest(), 3500)

        self.market.rotate_clothing_stock(announce=False)
        self.assertIs(self.market.bubba_quest, quest)
        self.assertEqual(self.market.bubba_quest_due_at, due)

    def test_post_quest_fish_sales_shave_thirty_seconds_each(self):
        player = Player("angler", current_room="store")
        player.attributes["charisma"] = 1
        bounty = create_item_copy(BLUEGILL, roll_stats=False, condition=8)
        bounty.fish_size = "average"
        bounty.weight = 0.5
        extra = create_item_copy(BASS, roll_stats=False, condition=7)
        extra.fish_size = "small"
        player.add_item(bounty)
        player.add_item(extra)

        self.market.bubba_quest.fish_id = "bluegill"
        self.market.bubba_quest.fish_name = "bluegill"
        self.market.bubba_quest.fish_size = "average"
        self.market.bubba_quest.condition = 8
        self.market.bubba_quest.target_weight = 0.5
        self.market.bubba_quest.claimed = False
        self.market.bubba_quest_due_at = 1_000_000.0

        with patch("market.time.time", return_value=100.0):
            before = self.market.bubba_quest_due_at
            self.assertFalse(self.market.apply_bubba_post_quest_fish_sale())
            self.assertEqual(self.market.bubba_quest_due_at, before)

            complete = self.commands.cmd_sell(player, "bluegill")
            self.assertIn("That's the one", complete.message)
            self.assertEqual(self.market.bubba_quest_due_at, before)

            extra_sale = self.commands.cmd_sell(player, extra.name)
            self.assertNotIn("next request coming sooner", extra_sale.message)
            self.assertRegex(
                extra_sale.message,
                rf"for \d+ gold\. \(Your gold: {player.gold}\)",
            )
            self.assertNotIn("You now have", extra_sale.message)
            self.assertEqual(
                self.market.bubba_quest_due_at,
                before - Market.BUBBA_QUEST_FISH_SALE_REDUCTION,
            )
            self.assertEqual(self.market.bubba_post_quest_fish_sales, 1)

            fifth = create_item_copy(BASS, roll_stats=False, condition=6)
            player.add_item(fifth)
            self.market.bubba_post_quest_fish_sales = 4
            fifth_sale = self.commands.cmd_sell(player, fifth.name)
            self.assertIn("next request coming sooner", fifth_sale.message)
            self.assertEqual(self.market.bubba_post_quest_fish_sales, 5)

            self.market.bubba_quest_due_at = 110.0
            self.assertTrue(self.market.apply_bubba_post_quest_fish_sale())
            self.assertEqual(self.market.bubba_quest_due_at, 100.0)
            self.assertFalse(self.market.bubba_post_quest_sale_should_nudge())

    def test_bare_sell_greens_percent_hints_at_wisdom_12(self):
        player = Player("angler", current_room="store")
        player.attributes["wisdom"] = 8
        player.attributes["charisma"] = 5
        fish = create_item_copy(BLUEGILL, roll_stats=False, condition=9)
        player.add_item(fish)

        mid = self.commands._format_sell_offer_list(player, StoreType.BUBBA)
        self.assertRegex(mid, r"\(\+\d+% vs usual\)")
        self.assertNotIn("\033[32m", mid)

        player.attributes["wisdom"] = 12
        high = self.commands._format_sell_offer_list(player, StoreType.BUBBA)
        self.assertRegex(high, r"\033\[32m\(\+\d+% vs usual\)\033\[0m")

    def test_bubba_limits_better_gear_to_one_until_restock(self):
        player = Player("angler", current_room="store")
        player.gold = 10000
        first = self.commands.cmd_buy(player, "professional")
        self.assertIn("You buy", first.message)
        second = self.commands.cmd_buy(player, "professional")
        self.assertIn("only had one", second.message)
        poles = [
            item for item in player.inventory
            if item.id == "pro_rod"
        ]
        self.assertEqual(len(poles), 1)

        listing = self.commands.cmd_list(player, "")
        self.assertNotIn("professional fishing rod", listing.message)
        self.assertIn("Basic poles are always in stock", listing.message)

        cheap = self.commands.cmd_buy(player, "basic")
        again = self.commands.cmd_buy(player, "basic")
        self.assertIn("You buy", cheap.message)
        self.assertIn("You buy", again.message)
        self.assertEqual(
            len([item for item in player.inventory if item.id == "basic_pole"]),
            2,
        )

        self.market.rotate_clothing_stock(announce=False)
        restocked = self.commands.cmd_buy(player, "professional")
        self.assertIn("You buy", restocked.message)

    def test_chum_temporarily_raises_a_water_room_one_band(self):
        room = self.rooms["old_pier"]
        room.population = 40
        player = Player("angler", current_room=room.id)
        player.add_item(create_item_copy(BUCKET_OF_CHUM))

        result = self.commands.cmd_chum(player, "")

        self.assertEqual(room.population, 50)
        self.assertEqual(room.chum_bonus, 10)
        self.assertFalse(any(item.id == "bucket_of_chum" for item in player.inventory))
        self.assertIn("Population +10", result.message)
        self.assertIn("dumps a bucket of chum", result.broadcast)

        with patch("world.random.choice", return_value=5):
            room.update_population()
        self.assertEqual(room.population, 45)
        self.assertEqual(room.chum_bonus, 0)

    def test_population_shift_message_splits_rise_and_fall(self):
        self.assertIn(population_shift_message(40, 55), POPULATION_RISE_MESSAGES)
        self.assertIn(population_shift_message(55, 40), POPULATION_FALL_MESSAGES)
        self.assertIsNone(population_shift_message(50, 50))

    def test_inv_sort_fish_keeps_gear_and_orders_species_quality_size(self):
        player = Player("angler")
        worm = create_item_copy(PLASTIC_WORM, condition=9)
        chum = create_item_copy(BUCKET_OF_CHUM)
        trophy_bass = create_item_copy(BASS, roll_stats=False, condition=7)
        trophy_bass.fish_size = "trophy"
        small_bass = create_item_copy(BASS, roll_stats=False, condition=9)
        small_bass.fish_size = "small"
        new_avg_bass = create_item_copy(BASS, roll_stats=False, condition=9)
        new_avg_bass.fish_size = "average"
        trout = create_item_copy(TROUT, roll_stats=False, condition=6)
        trout.fish_size = "large"
        bluegill = create_item_copy(BLUEGILL, roll_stats=False, condition=8)
        bluegill.fish_size = "tiny"
        player.inventory = [
            trophy_bass, worm, trout, chum, small_bass, bluegill, new_avg_bass
        ]

        result = self.commands.cmd_inventory(player, "sort fish")

        self.assertIn("sort your fish", result.message)
        carried = player.get_inventory_display_order()
        self.assertEqual(
            [item.id for item in carried],
            [
                "plastic_worm",
                "bucket_of_chum",
                "bluegill",
                "bass",
                "bass",
                "bass",
                "trout",
            ],
        )
        bass = [item for item in carried if item.id == "bass"]
        self.assertEqual(bass[0].condition, 9)
        self.assertEqual(bass[0].fish_size, "average")
        self.assertEqual(bass[1].condition, 9)
        self.assertEqual(bass[1].fish_size, "small")
        self.assertEqual(bass[2].condition, 7)
        self.assertEqual(bass[2].fish_size, "trophy")

    def test_inv_gear_lists_poles_and_lures_including_equipped(self):
        player = Player("angler")
        pole = create_item_copy(BASIC_POLE, condition=9)
        spare = create_item_copy(PRO_ROD, condition=8)
        worm = create_item_copy(PLASTIC_WORM, condition=9)
        jig = create_item_copy(SPECIALTY_LURE, condition=7)
        bait = create_item_copy(NIGHTCRAWLERS, condition=9)
        hat = create_item_copy(FISHING_HAT, condition=9)
        fish = create_item_copy(BLUEGILL, roll_stats=False, condition=8)
        player.inventory = [pole, spare, worm, jig, bait, hat, fish]
        player.equip(pole)
        player.equip(hat)

        result = self.commands.cmd_inventory(player, "gear")
        text = result.message

        self.assertIn("INVENTORY FILTER: 'gear'", text)
        self.assertIn("basic fishing pole", text)
        self.assertIn("professional fishing rod", text)
        self.assertIn("plastic worm", text)
        self.assertIn("blank jig", text)
        self.assertNotIn("nightcrawler", text.lower())
        self.assertNotIn("fishing hat", text)
        self.assertNotIn("bluegill", text)

        carried = player.get_inventory_display_order()
        spare_number = next(
            number for number, item in enumerate(carried, start=1)
            if item.id == "pro_rod"
        )
        self.assertIn(f"{spare_number}) ", text)

    def test_chum_is_not_wasted_at_a_teeming_spot(self):
        room = self.rooms["old_pier"]
        room.population = 100
        player = Player("angler", current_room=room.id)
        player.add_item(create_item_copy(BUCKET_OF_CHUM))

        result = self.commands.cmd_use(player, "chum")

        self.assertEqual(room.population, 100)
        self.assertTrue(any(item.id == "bucket_of_chum" for item in player.inventory))
        self.assertIn("keep the chum sealed", result.message)

    def test_chum_is_bubba_only_and_limited_until_restock(self):
        self.assertTrue(
            self.market.is_item_for_sale(StoreType.BUBBA, "bucket_of_chum")
        )
        self.assertFalse(
            self.market.is_item_for_sale(StoreType.SLICK, "bucket_of_chum")
        )

        player = Player("angler", current_room="store")
        player.gold = 100
        first = self.commands.cmd_buy(player, "chum")
        second = self.commands.cmd_buy(player, "chum")
        self.assertIn("You buy", first.message)
        self.assertIn("only had one", second.message)

    def test_lure_kit_is_bubba_only_and_unpacks_a_blank_lure(self):
        self.assertTrue(self.market.is_item_for_sale(StoreType.BUBBA, "lure_kit"))
        self.assertFalse(self.market.is_item_for_sale(StoreType.SLICK, "lure_kit"))
        self.assertEqual(self.market.get_buy_price(StoreType.BUBBA, "lure_kit"), 5000)

        player = Player("angler", current_room="store")
        player.gold = 5000
        result = self.commands.cmd_buy(player, "jig kit")
        self.assertIn("blank jig", result.message)
        lures = [item for item in player.inventory if item.id == "specialty_lure"]
        self.assertEqual(len(lures), 1)
        self.assertIsNone(lures[0].attracts_fish_id)
        self.assertEqual(player.gold, 0)
        self.assertIn("You attach the", player.equip(lures[0]))

        player.gold = 5000
        sold_out = self.commands.cmd_buy(player, "kit")
        self.assertIn("only had one", sold_out.message)

    def test_specialty_lure_attunes_then_only_accepts_that_species(self):
        player = Player("angler", current_room="bubba_workshop")
        lure = create_item_copy(SPECIALTY_LURE, condition=9)
        bass = create_item_copy(BASS, roll_stats=False, condition=9)
        bass.weight = 4.0
        bluegill = create_item_copy(BLUEGILL, roll_stats=False, condition=9)
        player.add_item(lure)
        player.add_item(bass)
        player.add_item(bluegill)

        first = self.commands.cmd_feed(player, "jig bass")
        self.assertIn("largemouth bass", first.message)
        self.assertIn("Cliff", first.message)
        self.assertIn("You hand Cliff", first.message)
        self.assertEqual(lure.attracts_fish_id, "bass")
        self.assertAlmostEqual(lure.lure_essence, 1.33)
        self.assertFalse(any(item.id == "bass" for item in player.inventory))

        refused = self.commands.cmd_use(player, "jig bluegill")
        self.assertIn("already a largemouth bass pattern", refused.message)
        self.assertTrue(any(item.id == "bluegill" for item in player.inventory))

        more = create_item_copy(BASS, roll_stats=False, condition=9)
        more.weight = 4.0
        player.add_item(more)
        boosted = self.commands.cmd_feed(player, "bass")
        self.assertAlmostEqual(lure.lure_essence, 2.66)
        self.assertIn("2.67×", boosted.message)

    def test_specialty_lure_work_requires_bubba_workshop(self):
        player = Player("angler", current_room="old_pier")
        lure = create_item_copy(SPECIALTY_LURE, condition=9)
        bass = create_item_copy(BASS, roll_stats=False, condition=9)
        player.add_item(lure)
        player.add_item(bass)
        result = self.commands.cmd_feed(player, "jig bass")
        self.assertIn("workshop west of the store porch", result.message)
        self.assertTrue(any(item.id == "bass" for item in player.inventory))
        self.assertIsNone(lure.attracts_fish_id)

    def test_giving_a_fish_to_cliff_dresses_the_jig(self):
        player = Player("angler", current_room="bubba_workshop")
        lure = create_item_copy(SPECIALTY_LURE, condition=9)
        bass = create_item_copy(BASS, roll_stats=False, condition=9)
        bass.weight = 3.0
        player.add_item(lure)
        player.add_item(bass)
        self.commands.player_manager.get_players_in_room.return_value = []
        result = self.commands.cmd_give(player, "bass cliff")
        self.assertIn("You hand Cliff", result.message)
        self.assertEqual(lure.attracts_fish_id, "bass")
        self.assertFalse(any(item.id == "bass" for item in player.inventory))

    @staticmethod
    def _norm_target():
        return BubbaFishQuest(
            fish_id="bass",
            fish_name="largemouth bass",
            fish_size="average",
            condition=8,
            target_weight=3.0,
        )

    def test_norm_stays_on_overgrown_path_and_starts_private_game(self):
        self.assertIn("Norm", self.rooms["east_path"].npcs)
        player = Player("angler", current_room="east_path")
        self.commands.player_manager.get_players_in_room.return_value = []

        with patch(
            "commands.create_random_bubba_fish_quest",
            return_value=self._norm_target(),
        ):
            greeting = self.commands.cmd_say(player, "hello Norm")
            original = self.commands.norm_rounds["angler"]
            repeated = self.commands.cmd_say(player, "hey Norm")

        self.assertIn("I want to see a specific fish", greeting.message)
        self.assertIn("same fish", repeated.message)
        self.assertIs(self.commands.norm_rounds["angler"], original)

    def test_norm_returns_nonfish_and_scores_three_hidden_traits(self):
        player = Player("angler", current_room="east_path")
        self.commands.player_manager.get_players_in_room.return_value = []
        self.commands.norm_rounds["angler"] = NormRound(self._norm_target())

        hat = create_item_copy(FISHING_HAT, condition=9)
        player.add_item(hat)
        not_fish = self.commands.cmd_give(player, "hat to Norm")
        self.assertIn("that's not a fish", not_fish.message)
        self.assertIn(hat, player.inventory)

        none = create_item_copy(TROUT, roll_stats=False, condition=7)
        none.fish_size = "large"
        one = create_item_copy(BASS, roll_stats=False, condition=7)
        one.fish_size = "large"
        two = create_item_copy(BASS, roll_stats=False, condition=7)
        two.fish_size = "average"

        self.assertIn("No, not this", self.commands._give_to_norm(player, none).message)
        self.assertIn("Kinda, but not quite", self.commands._give_to_norm(player, one).message)
        self.assertIn("Oh, this is close", self.commands._give_to_norm(player, two).message)
        self.assertEqual(self.commands.norm_rounds["angler"].guesses, 3)

    def test_norm_exact_guess_returns_fish_rewards_jig_and_restarts(self):
        player = Player("angler", current_room="east_path")
        self.commands.player_manager.get_players_in_room.return_value = []
        fish = create_item_copy(BASS, roll_stats=False, condition=8)
        fish.fish_size = "average"
        player.add_item(fish)
        self.commands.norm_rounds["angler"] = NormRound(self._norm_target())

        with patch(
            "commands.create_random_bubba_fish_quest",
            return_value=BubbaFishQuest(
                "trout", "rainbow trout", "large", 9, 3.4
            ),
        ):
            result = self.commands.cmd_give(player, "bass to Norm")

        self.assertIn("that's what I was thinking of", result.message)
        self.assertIn("I wanna play again", result.message)
        self.assertIn(fish, player.inventory)
        rewards = [item for item in player.inventory if item.id == "specialty_lure"]
        self.assertEqual(len(rewards), 1)
        self.assertIsNone(rewards[0].attracts_fish_id)
        self.assertEqual(self.commands.norm_rounds["angler"].target.fish_id, "trout")
        self.assertEqual(self.commands.norm_rounds["angler"].guesses, 0)

    def test_norm_reveals_answer_after_eight_misses(self):
        player = Player("angler", current_room="east_path")
        state = NormRound(self._norm_target(), guesses=NORM_MAX_GUESSES - 1)
        self.commands.norm_rounds["angler"] = state
        wrong = create_item_copy(TROUT, roll_stats=False, condition=7)
        wrong.fish_size = "large"

        result = self.commands._give_to_norm(player, wrong)

        self.assertIn("fine average largemouth bass", result.message)
        self.assertIn("Say hello", result.message)
        self.assertNotIn("angler", self.commands.norm_rounds)

    def test_specialty_lure_scores_size_relative_to_the_species(self):
        average_bass = specialty_lure_essence_from_fish(BASS, BASS)
        average_carp = specialty_lure_essence_from_fish(MUD_CARP, MUD_CARP)
        self.assertAlmostEqual(average_bass, 1.0)
        self.assertAlmostEqual(average_carp, 1.0)

        trophy_bass = create_item_copy(BASS, roll_stats=False)
        trophy_bass.weight = 3.0 * 1.8
        trophy_carp = create_item_copy(MUD_CARP, roll_stats=False)
        trophy_carp.weight = 14.0 * 1.8
        self.assertAlmostEqual(
            specialty_lure_essence_from_fish(trophy_bass, BASS),
            specialty_lure_essence_from_fish(trophy_carp, MUD_CARP),
        )

    def test_specialty_lure_rejects_legendaries_and_boosts_catch_table(self):
        player = Player("angler", current_room="bubba_workshop")
        lure = create_item_copy(SPECIALTY_LURE, condition=9)
        legend = create_item_copy(LEGENDARY_CARP, roll_stats=False, condition=9)
        player.add_item(lure)
        player.add_item(legend)
        blocked = self.commands.cmd_feed(player, "jig whiskers")
        self.assertIn("don't put that one on a jig", blocked.message)
        self.assertTrue(any(item.id == "legendary_carp" for item in player.inventory))

        lure.attracts_fish_id = "bass"
        lure.lure_essence = 4.0
        lure.name = "largemouth bass jig"
        player.equipped_lure = lure
        plain = dict(
            (fish.id, weight)
            for fish, weight in self.commands._adjusted_catch_table(0, 1.0)
        )
        boosted = dict(
            (fish.id, weight)
            for fish, weight in self.commands._adjusted_catch_table(
                0, 1.0, player
            )
        )
        self.assertEqual(boosted["bass"], int(plain["bass"] * 3))
        self.assertEqual(boosted["bluegill"], plain["bluegill"])

    def test_specialty_lure_quadratic_curve_caps_at_10x(self):
        self.assertEqual(specialty_lure_multiplier(0), 2.0)
        self.assertEqual(specialty_lure_multiplier(4), 3.0)
        self.assertEqual(specialty_lure_multiplier(12), 4.0)
        self.assertEqual(specialty_lure_multiplier(24), 5.0)
        self.assertEqual(specialty_lure_multiplier(40), 6.0)
        self.assertEqual(specialty_lure_multiplier(60), 7.0)
        self.assertEqual(specialty_lure_multiplier(84), 8.0)
        self.assertEqual(specialty_lure_multiplier(112), 9.0)
        self.assertEqual(specialty_lure_multiplier(144), 10.0)
        self.assertEqual(specialty_lure_multiplier(200), 10.0)
        self.assertEqual(specialty_lure_multiplier(8), 3.5)
        self.assertEqual(specialty_lure_multiplier(32), 5.5)


class ReelEventTests(unittest.IsolatedAsyncioTestCase):
    def test_helpful_reel_chance_is_halved_but_still_caps_at_75(self):
        chance = MUDSession._helpful_reel_chance
        self.assertEqual(chance(1, 1), 0.0)
        self.assertAlmostEqual(chance(5, 5), 15.0)
        self.assertAlmostEqual(chance(10, 10), 33.75)
        self.assertEqual(chance(26, 11), 75.0)
        self.assertEqual(chance(40, 40), 75.0)

    async def test_wrong_direction_adds_quarter_of_original_time(self):
        session = MUDSession(Mock(), Mock())
        session.send_message = AsyncMock()
        session.get_input = AsyncMock(return_value="wrong")
        challenge = ReelChallenge(
            total_seconds=40,
            response_seconds=3,
            intelligence=1,
            wisdom=1,
        )

        fake_sleep = AsyncMock()
        with patch("mud_server.asyncio.sleep", new=fake_sleep):
            await session._run_reel_challenge(challenge)

        output = "".join(call.args[0] for call in session.send_message.call_args_list)
        self.assertIn("You have more to reel in", output)
        self.assertAlmostEqual(
            sum(call.args[0] for call in fake_sleep.call_args_list),
            50.0,
        )

    async def test_helpful_event_removes_time_and_answer_time_counts(self):
        session = MUDSession(Mock(), Mock())
        session.send_message = AsyncMock()
        session.get_input = AsyncMock(return_value="reel")
        challenge = ReelChallenge(
            total_seconds=20,
            response_seconds=3,
            intelligence=10,
            wisdom=10,
        )

        fake_sleep = AsyncMock()
        rolls = iter([0.0, *[1.0] * 50])

        def fake_choice(seq):
            seq = tuple(seq)
            if seq == ("reel", "pull", "slack", "yank"):
                return "reel"
            return "left"

        with (
            patch("mud_server.asyncio.sleep", new=fake_sleep),
            patch("mud_server.random.random", side_effect=lambda: next(rolls)),
            patch("mud_server.random.choice", side_effect=fake_choice),
            patch("mud_server.monotonic", side_effect=[100.0, 103.0]),
        ):
            await session._run_reel_challenge(challenge)

        output = "".join(call.args[0] for call in session.send_message.call_args_list)
        self.assertIn("Solid reel. The fish comes in easier.", output)
        self.assertAlmostEqual(
            sum(call.args[0] for call in fake_sleep.call_args_list),
            8.0,
        )

    def test_helpful_reel_bonus_rounds_reaction_time_up(self):
        bonus = MUDSession._helpful_reel_bonus
        self.assertEqual(bonus(0.0), 15.0)
        self.assertEqual(bonus(0.2), 15.0)
        self.assertEqual(bonus(1.0), 15.0)
        self.assertEqual(bonus(1.01), 12.0)
        self.assertEqual(bonus(2.0), 12.0)
        self.assertEqual(bonus(3.0), 9.0)
        self.assertEqual(bonus(4.0), 6.0)
        self.assertEqual(bonus(5.0), 3.0)

    def test_helpful_reel_success_copy_has_five_yank_tiers(self):
        message = MUDSession._helpful_reel_success_message
        self.assertEqual(
            message("yank", 0.4),
            "Perfect yank! The line sings and the fish surges in.",
        )
        self.assertEqual(
            message("yank", 2.0),
            "Sharp yank! You steal a long pull of line.",
        )
        self.assertEqual(
            message("yank", 2.2),
            "Solid yank. The fish comes in easier.",
        )
        self.assertEqual(
            message("yank", 4.0),
            "A late yank, but you still gain ground.",
        )
        self.assertEqual(
            message("yank", 5.0),
            "You yank just in time and take a little slack.",
        )

    def test_cut_words_are_reel_aborts(self):
        self.assertTrue(MUDSession._is_reel_abort("cut"))
        self.assertTrue(MUDSession._is_reel_abort("SNAP"))
        self.assertFalse(MUDSession._is_reel_abort("reel"))
        self.assertFalse(MUDSession._is_reel_abort("left"))

    async def test_cut_at_prompt_abandons_the_reel(self):
        session = MUDSession(Mock(), Mock())
        session.send_message = AsyncMock()
        session.get_input = AsyncMock(return_value="cut")
        challenge = ReelChallenge(
            total_seconds=40,
            response_seconds=3,
            intelligence=1,
            wisdom=1,
        )
        fake_sleep = AsyncMock()
        with patch("mud_server.asyncio.sleep", new=fake_sleep):
            landed = await session._run_reel_challenge(challenge)
        self.assertFalse(landed)


class AncientWhiskersTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        path = Path(self.temp_dir.name) / ".water_cycle.json"
        self.state = LakeCycleState(path)
        self.rooms = create_world()
        self.commands = GameCommands(
            self.rooms,
            Mock(),
            weather=None,
            market=Market(),
            lake_state=self.state,
        )

    def test_second_roll_reserves_only_one_fixed_size_catch(self):
        total = sum(weight for _, weight in CATCHABLE_FISH)
        player = Player("angler")
        with patch("commands.random.randint", side_effect=[total, 1, 9, total]):
            ancient = self.commands._select_fish(0, 1.0, player)
            ordinary = self.commands._select_fish(0, 1.0, Player("other"))

        self.assertEqual(ancient.id, "ancient_whiskers")
        self.assertEqual(ancient.weight, 46.0)
        self.assertTrue(player.ancient_whiskers_reserved)
        self.assertFalse(self.state.available)
        self.assertEqual(ordinary.id, "legendary_carp")

        sized = self.commands._create_sized_fish(ancient)
        self.assertEqual(sized.weight, 46.0)
        self.assertIsNone(sized.fish_size)

    def test_drop_returns_without_growth_and_logout_is_not_saved(self):
        player = Player("angler", current_room="store_porch")
        ancient = self.state.create_catch()
        player.add_item(ancient)
        self.state.reserve()

        result = self.commands.cmd_drop(player, "ancient")
        self.assertIn("It's GONE!!", result.message)
        self.assertTrue(self.state.available)
        self.assertEqual(self.state.weight, 46.0)
        self.assertNotIn(ancient, self.rooms["store_porch"].items)

        player.add_item(ancient)
        self.assertFalse(any(
            item["id"] == "ancient_whiskers"
            for item in player.to_dict()["inventory"]
        ))

    def test_slick_refuses_and_bubba_pays_then_grows(self):
        player = Player("angler", current_room="slick_store")
        ancient = self.state.create_catch()
        player.add_item(ancient)
        self.state.reserve()

        slick = self.commands.cmd_sell(player, "ancient")
        self.assertIn("Get that out of here", slick.message)
        self.assertIn(ancient, player.inventory)
        self.assertNotIn(
            "Ancient Whiskers",
            self.commands._format_sell_offer_list(player, StoreType.SLICK),
        )

        player.current_room = "store"
        bubba = self.commands.cmd_sell(player, "ancient")
        self.assertIn("Bubba pays you 510 gold. (Your gold: 560)", bubba.message)
        self.assertIn("tosses it out of the window", bubba.message)
        self.assertEqual(player.gold, 560)
        self.assertEqual(self.state.weight, 47.0)
        self.assertTrue(self.state.available)
        self.assertNotIn(ancient, player.inventory)


class WeatherForecastTests(unittest.TestCase):
    def setUp(self):
        self.rooms = create_world()
        self.weather = WeatherSystem()
        self.commands = GameCommands(
            self.rooms, Mock(), weather=self.weather, market=Market()
        )

    def test_forecast_is_banked_and_consumed_in_order(self):
        self.assertEqual(len(self.weather.forecast), FORECAST_DEPTH)
        expected = list(self.weather.peek_forecast(FORECAST_DEPTH))
        for next_weather in expected:
            _, new = self.weather.change_weather()
            self.assertEqual(new, next_weather)
            self.assertEqual(len(self.weather.forecast), FORECAST_DEPTH)

    def test_weather_command_gates_forecast_by_int_wis(self):
        player = Player("angler")
        player.attributes["intelligence"] = 1
        player.attributes["wisdom"] = 1
        low = self.commands.cmd_weather(player, "")
        self.assertNotIn("You get a sense of the upcoming weather:", low.message)
        self.assertIn("Higher Intelligence and Wisdom", low.message)

        names = [w.name for w in self.weather.peek_forecast(5)]
        player.attributes["intelligence"] = 2
        player.attributes["wisdom"] = 2
        one = self.commands.cmd_weather(player, "")
        self.assertIn("You get a sense of the upcoming weather:", one.message)
        after_one = one.message.split("You get a sense of the upcoming weather:")[1]
        self.assertEqual(after_one.strip(), names[0])

        player.attributes["intelligence"] = 10
        player.attributes["wisdom"] = 10
        five = self.commands.cmd_weather(player, "")
        self.assertIn(
            "You can feel the pending weather changes in your bones:",
            five.message,
        )
        after_five = five.message.split(
            "You can feel the pending weather changes in your bones:"
        )[1]
        self.assertEqual(
            [line.strip() for line in after_five.strip().splitlines()],
            names,
        )


class FishermanTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.rooms = create_world()
        self.state = LakeCycleState(
            Path(self.temp_dir.name) / ".water_cycle.json"
        )
        self.fishermen = FishermanManager(self.rooms)
        self.commands = GameCommands(
            self.rooms,
            PlayerManager(),
            weather=None,
            market=Market(),
            lake_state=self.state,
            fishermen=self.fishermen,
        )
        for room in self.rooms.values():
            if room.is_water:
                room.population = 20
        self.rooms["old_pier"].population = 90

    def _player_with_local_fisherman(
        self, name: str, charisma: int
    ) -> tuple[Player, object]:
        room_id = self.fishermen.assignments[FISHERMEN[0].name]
        player = Player(name, current_room=room_id)
        player.attributes["charisma"] = charisma
        return player, self.fishermen.in_room(room_id)

    def test_each_spot_shows_one_generic_but_unique_fisherman(self):
        identities = []
        displays = []
        for room in self.rooms.values():
            if not room.is_water:
                continue
            local = self.fishermen.in_room(room.id)
            self.assertEqual(room.npcs.count(local.display), 1)
            self.assertFalse(any(npc.name in room.npcs for npc in FISHERMEN))
            identities.append(local.name)
            displays.append(local.display)
        self.assertCountEqual(identities, [npc.name for npc in FISHERMEN])
        self.assertCountEqual(displays, [npc.display for npc in FISHERMEN])
        self.assertFalse(any("Tall" in display for display in displays))

    def test_charisma_six_nod_reveals_hidden_name(self):
        player, local = self._player_with_local_fisherman("charmer", 6)
        result = self.commands.cmd_nod(player, "fisherman")
        self.assertIn(local.name, result.message)

    def test_say_hint_scales_and_ties_count_as_best(self):
        low, local = self._player_with_local_fisherman("low", 1)
        low_result = self.commands.cmd_say(low, local.name)
        if low.current_room == "old_pier":
            self.assertIn(local.staying, low_result.message)
        else:
            self.assertIn(local.elsewhere, low_result.message)

        high, high_local = self._player_with_local_fisherman("high", 12)
        high_result = self.commands.cmd_say(high, high_local.name)
        self.assertIn("Old Wooden Pier", high_result.message)
        self.assertIn("exceptional", high_result.message)

        tied, tied_local = self._player_with_local_fisherman("tied", 1)
        self.rooms[tied.current_room].population = 90
        tied_result = self.commands.cmd_say(tied, tied_local.name)
        self.assertIn(tied_local.staying, tied_result.message)

    def test_wrong_name_starts_shared_nod_and_say_cooldown(self):
        player, local = self._player_with_local_fisherman("spammer", 12)
        wrong = next(npc for npc in FISHERMEN if npc != local)

        first = self.commands.cmd_say(player, wrong.name)
        self.assertIn(local.wrong_name, first.message)
        second = self.commands.cmd_nod(player, "fisherman")
        self.assertIn("ignores you", second.message)
        third = self.commands.cmd_say(player, local.name)
        self.assertNotIn(f"{local.display} says", third.message)
        self.assertNotIn("ignores you", third.message)

    def test_giving_ancient_whiskers_returns_it_without_growth(self):
        player, _ = self._player_with_local_fisherman("holder", 1)
        ancient = self.state.create_catch()
        player.add_item(ancient)
        self.state.reserve()

        result = self.commands.cmd_give(player, "ancient to fisherman")
        self.assertIn("back into the lake", result.message)
        self.assertNotIn(ancient, player.inventory)
        self.assertTrue(self.state.available)
        self.assertEqual(self.state.weight, 46.0)

    def test_relocation_moves_every_identity_to_a_different_spot(self):
        before = dict(self.fishermen.assignments)
        moves = self.fishermen.relocate()
        self.assertEqual(len(moves), len(FISHERMEN))
        for npc in FISHERMEN:
            self.assertNotEqual(
                before[npc.name], self.fishermen.assignments[npc.name]
            )
        for room in self.rooms.values():
            if room.is_water:
                local = self.fishermen.in_room(room.id)
                self.assertEqual(room.npcs.count(local.display), 1)

    def test_npc_catch_uses_player_room_broadcasts_and_skips_ancient(self):
        npc = self.fishermen.in_room("old_pier")
        self.rooms["old_pier"].population = 40
        with patch("commands.random.randint", return_value=100):
            miss = self.commands.try_npc_catch(npc, "old_pier")
        self.assertIsNone(miss)

        self.rooms["old_pier"].population = 100
        with patch("commands.random.randint", side_effect=[1, 1, 8]):
            with patch(
                "commands.random.choices",
                return_value=[("average", 1.0, 1.0)],
            ):
                result = self.commands.try_npc_catch(npc, "old_pier")

        hook, catch, delay = result
        self.assertEqual(hook, f"{npc.display} hooks a fish!")
        self.assertRegex(catch, rf"^{re.escape(npc.display)} catches a .+ lb .+!")
        self.assertNotIn("Ancient Whiskers", catch)
        self.assertGreaterEqual(delay, 4)
        self.assertTrue(self.state.available)


class FishermanBroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def test_restock_relocation_broadcasts_departures_and_arrivals(self):
        game = FishingMUD.__new__(FishingMUD)
        rooms = create_world()
        game.fishermen = FishermanManager(rooms)
        game.broadcast_to_room = AsyncMock()
        game._npc_fishing = set()
        game._fishermen_relocating = False

        await game.relocate_fishermen()

        self.assertEqual(
            game.broadcast_to_room.await_count,
            len(FISHERMEN) * 2,
        )
        messages = [
            call.args[1]
            for call in game.broadcast_to_room.await_args_list
        ]
        self.assertTrue(any("walks away" in message for message in messages))
        self.assertTrue(any("arrives" in message for message in messages))

    async def test_relocate_waits_until_active_fights_finish(self):
        game = FishingMUD.__new__(FishingMUD)
        rooms = create_world()
        game.fishermen = FishermanManager(rooms)
        game.broadcast_to_room = AsyncMock()
        game._npc_fishing = {"Walt"}
        game._fishermen_relocating = False

        original = game.fishermen.relocate

        def relocate_when_idle():
            self.assertEqual(game._npc_fishing, set())
            return original()

        game.fishermen.relocate = relocate_when_idle

        async def finish_fight():
            await asyncio.sleep(0.05)
            game._npc_fishing.clear()

        asyncio.create_task(finish_fight())
        await game.relocate_fishermen()
        self.assertFalse(game._fishermen_relocating)
        self.assertGreater(game.broadcast_to_room.await_count, 0)

    def test_restock_blocks_new_fights_when_imminent(self):
        game = FishingMUD.__new__(FishingMUD)
        game._fishermen_relocating = False
        game.market = Mock()
        game.market.get_clothing_rotation_remaining.return_value = (
            NPC_CATCH_MAX_SECONDS
        )
        self.assertTrue(game._npc_restock_imminent())
        game.market.get_clothing_rotation_remaining.return_value = (
            NPC_CATCH_MAX_SECONDS + 1
        )
        self.assertFalse(game._npc_restock_imminent())
        game._fishermen_relocating = True
        self.assertTrue(game._npc_restock_imminent(4))

    async def test_npc_catch_broadcasts_hook_then_landing(self):
        game = FishingMUD.__new__(FishingMUD)
        game.fishermen = Mock()
        game.fishermen.assignments = {"Walt": "old_pier"}
        game.broadcast_to_room = AsyncMock()
        game._npc_fishing = set()

        with patch("mud_server.asyncio.sleep", new=AsyncMock()):
            await game._play_npc_catch(
                "Walt",
                "old_pier",
                "Rangy Fisherman hooks a fish!",
                "Rangy Fisherman catches a 1.2 lb bluegill!",
                4,
            )

        messages = [call.args[1] for call in game.broadcast_to_room.await_args_list]
        self.assertEqual(
            messages,
            [
                "Rangy Fisherman hooks a fish!",
                "Rangy Fisherman catches a 1.2 lb bluegill!",
            ],
        )


class LoginSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_abandoned_character_creation_does_not_raise(self):
        session = MUDSession.__new__(MUDSession)
        session.game = Mock()
        session.process = Mock()
        session.process.stdout = Mock()
        session.process.stdout.drain = AsyncMock()
        session.player = None
        session.running = True
        session.command_history = []
        session.history_index = 0
        session._slick_deal_task = None
        session.send_message = AsyncMock()
        session.cleanup = AsyncMock()

        async def abandon(prompt="", hidden=False):
            raise EOFError()

        session.get_input = abandon
        await session.run()
        session.cleanup.assert_awaited()

    async def test_ctrl_c_during_login_does_not_raise(self):
        session = MUDSession.__new__(MUDSession)
        session.game = Mock()
        session.process = Mock()
        session.player = None
        session.running = True
        session.command_history = []
        session.history_index = 0
        session._slick_deal_task = None
        session.send_message = AsyncMock()
        session.cleanup = AsyncMock()

        async def interrupt(prompt="", hidden=False):
            raise KeyboardInterrupt()

        session.get_input = interrupt
        await session.run()
        session.cleanup.assert_awaited()

    async def test_handle_client_swallows_login_interrupt(self):
        process = Mock()
        process.channel = Mock()
        process.exit = Mock()
        with patch("mud_server.MUDSession") as session_cls:
            inst = Mock()
            inst.run = AsyncMock(side_effect=KeyboardInterrupt())
            session_cls.return_value = inst
            await handle_client(process, Mock())
        process.exit.assert_called_once_with(0)


if __name__ == "__main__":
    unittest.main()
