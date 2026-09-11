import unittest
from unittest.mock import AsyncMock, Mock, patch

from commands import GameCommands, ReelChallenge
from items import (
    BASIC_POLE,
    BLUEGILL,
    ItemType,
    STORE_INVENTORY,
    create_item_copy,
)
from market import Market, StoreType
from mud_server import MUDSession
from player import Player
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


class ReelEventTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertIn("10 seconds have been added", output)
        self.assertAlmostEqual(
            sum(call.args[0] for call in fake_sleep.call_args_list),
            50.0,
        )

    async def test_helpful_event_removes_time_and_answer_time_counts(self):
        session = MUDSession(Mock(), Mock())
        session.send_message = AsyncMock()
        session.get_input = AsyncMock(return_value="reel")
        challenge = ReelChallenge(
            total_seconds=50,
            response_seconds=3,
            intelligence=10,
            wisdom=10,
        )

        fake_sleep = AsyncMock()
        with (
            patch("mud_server.asyncio.sleep", new=fake_sleep),
            patch("mud_server.random.randint", return_value=1),
            patch("mud_server.random.choice", return_value="reel"),
            patch("mud_server.monotonic", side_effect=[100.0, 103.0]),
        ):
            await session._run_reel_challenge(challenge)

        output = "".join(call.args[0] for call in session.send_message.call_args_list)
        self.assertIn("15 seconds faster", output)
        # 32 seconds in normal sleeps + 3 seconds spent answering = 35;
        # the other 15 seconds were removed by the successful event.
        self.assertAlmostEqual(
            sum(call.args[0] for call in fake_sleep.call_args_list),
            32.0,
        )


if __name__ == "__main__":
    unittest.main()
