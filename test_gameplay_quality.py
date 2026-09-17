import asyncio
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

from commands import CATCHABLE_FISH, GameCommands, ReelChallenge
from fishermen import FISHERMEN, FishermanManager, NPC_CATCH_MAX_SECONDS
from items import (
    ANCIENT_WHISKERS,
    BASIC_POLE,
    BLUEGILL,
    ItemType,
    STORE_INVENTORY,
    create_item_copy,
)
from lake_state import LakeCycleState
from market import Market, StoreType
from mud_server import FishingMUD, MUDSession
from player import Player, PlayerManager
from weather import FORECAST_DEPTH, WeatherSystem
from world import create_world


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

    def test_bare_eq_shows_equipment(self):
        result = self.commands.cmd_equip(Player("angler"), "")
        self.assertIn("EQUIPMENT", result.message)
        self.assertIn("pole", result.message)

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
        self.assertNotIn("gem", result.broadcast.lower())

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
        self.assertIn("You bring the fish in faster", output)
        self.assertAlmostEqual(
            sum(call.args[0] for call in fake_sleep.call_args_list),
            11.0,
        )

    def test_helpful_reel_bonus_scales_with_leftover_reaction_time(self):
        bonus = MUDSession._helpful_reel_bonus
        self.assertEqual(bonus(5.0), 15.0)
        self.assertEqual(bonus(1.0), 3.0)
        self.assertEqual(bonus(0.0), 3.0)
        self.assertEqual(bonus(4.0), 12.0)


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
        self.assertIn("Bubba pays you 510 gold", bubba.message)
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


if __name__ == "__main__":
    unittest.main()
