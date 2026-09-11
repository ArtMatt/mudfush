#!/usr/bin/env python3
"""
Fishing MUD Server
A Multi-User Dimension text-based fishing game
Users connect via SSH to play together
"""

import asyncio
import asyncssh
import random
import sys
import os
import time
import logging
from typing import Dict, Optional, Set
from pathlib import Path

from world import create_world, Room, reset_ground_items
from player import Player, PlayerManager
from commands import GameCommands, CommandResult
from weather import WeatherSystem, WeatherType, WEATHER_DATA
from market import Market
from lake_state import LakeCycleState

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
monotonic = time.monotonic


class FishingMUD:
    """Main game server class."""
    
    def __init__(self):
        self.rooms = create_world()
        self.player_manager = PlayerManager()
        self.weather = WeatherSystem()
        self.market = Market()
        self.lake_state = LakeCycleState()
        self.commands = GameCommands(
            self.rooms, 
            self.player_manager,
            weather=self.weather,
            market=self.market,
            lake_state=self.lake_state,
        )
        self.sessions: Dict[str, 'MUDSession'] = {}  # player_name -> session
        self._autosave_task = None
        self._population_task = None
        self._ground_loot_task = None
        self._scavenger_task = None
        self._clothing_degrade_task = None
        self.shutdown_event = asyncio.Event()
        self.started_at = time.time()
        
        logger.info("Fishing MUD initialized")
        logger.info(f"Loaded {len(self.rooms)} rooms")
        logger.info(f"Weather: {self.weather.get_current_weather().name}")
        
        # Log existing saved players
        saved_players = self.player_manager.get_all_saved_players()
        if saved_players:
            logger.info(f"Found {len(saved_players)} saved player(s)")
    
    async def start_autosave(self, interval: int = 300):
        """Start periodic auto-save task (default every 5 minutes)."""
        async def autosave_loop():
            while True:
                await asyncio.sleep(interval)
                if self.player_manager.players:
                    self.player_manager.save_all_players()
                    logger.info(f"Auto-saved {len(self.player_manager.players)} player(s)")
        
        self._autosave_task = asyncio.create_task(autosave_loop())
        logger.info(f"Auto-save enabled (every {interval} seconds)")
    
    async def start_weather(self, min_interval: int = 300, max_interval: int = 900):
        """Weather now changes with fish population updates (no separate timer)."""
        logger.info(
            "Weather system ready (changes with fish population updates)"
        )
    
    async def start_market(self, interval: int = 600):
        """Start the market system (updates every 10 minutes)."""
        self.market.set_broadcast_callback(self.broadcast_global)
        await self.market.start(interval)
        logger.info(f"Market system started (updates every {interval//60} minutes)")

    async def start_population_updates(self, interval: int = 120):
        """Update fish populations and weather together."""
        async def population_loop():
            while True:
                await asyncio.sleep(interval)
                for room in self.rooms.values():
                    room.update_population()
                logger.info("Updated fish populations")

                old, new = self.weather.change_weather()
                if old.weather_type != new.weather_type:
                    message = self.weather.get_weather_change_message(old, new)
                    await self.broadcast_to_fishing_rooms(message)
                    logger.info(f"Weather changed: {old.name} -> {new.name}")

        self._population_task = asyncio.create_task(population_loop())
        logger.info(
            f"Fish population & weather updates started "
            f"(every {interval} seconds)"
        )

    async def start_ground_loot_resets(self, interval: int = 3600):
        """Clear and respawn ground items every hour."""
        async def ground_loot_loop():
            while True:
                await asyncio.sleep(interval)
                counts = reset_ground_items(self.rooms)
                logger.info(
                    f"Ground loot reset "
                    f"(common={counts['common']}, clothing={counts['clothing']})"
                )
                await self.broadcast_global(
                    "*A breeze stirs the trails — scattered finds on the ground have changed.*"
                )

        self._ground_loot_task = asyncio.create_task(ground_loot_loop())
        logger.info(
            f"Ground loot reset started (every {interval // 60} minutes)"
        )

    async def start_scavenger_cleanup(self, interval: int = 10800):
        """Every 3 hours, clear all ground items (scavenger flavor for witnesses)."""
        async def scavenger_loop():
            while True:
                await asyncio.sleep(interval)
                cleared_rooms = await self.run_scavenger_cleanup()
                logger.info(
                    f"Scavenger cleaned ground items from {cleared_rooms} room(s)"
                )

        self._scavenger_task = asyncio.create_task(scavenger_loop())
        logger.info(
            f"Scavenger cleanup started (every {interval // 3600} hour(s))"
        )

    async def start_clothing_degrade(self, interval: int = 1800):
        """Every 30 minutes, wear down clothing worn since the last tick."""
        async def clothing_degrade_loop():
            while True:
                await asyncio.sleep(interval)
                await self.run_clothing_degrade()

        self._clothing_degrade_task = asyncio.create_task(clothing_degrade_loop())
        logger.info(
            f"Clothing degrade started (every {interval // 60} minutes)"
        )

    async def run_clothing_degrade(self) -> int:
        """Degrade marked worn clothes for all online players."""
        affected = 0
        for name, session in list(self.sessions.items()):
            player = session.player
            if not player:
                continue
            messages = player.degrade_worn_clothes()
            if not messages:
                continue
            affected += 1
            text = "\n".join(messages)
            try:
                await session.send_message(
                    f"\n*Your clothes show a little more wear...*\n{text}\n> "
                )
            except Exception:
                pass
        if affected:
            logger.info(f"Clothing degrade affected {affected} player(s)")
        return affected

    async def run_scavenger_cleanup(self) -> int:
        """
        Remove all items from the ground.
        Players in a room that had items see the scavenger arrive and leave.
        """
        message = (
            "A mysterious scavenger wanders by and takes the items on the ground, "
            "then as mysteriously as they arrived, they disappear."
        )
        cleared = 0
        for room in self.rooms.values():
            if not room.items:
                continue
            had_items = True
            if room.players:
                await self.broadcast_to_room(room.id, message)
            room.items.clear()
            if had_items:
                cleared += 1
        return cleared
    
    def stop_autosave(self):
        """Stop the auto-save task."""
        if self._autosave_task:
            self._autosave_task.cancel()
            self._autosave_task = None
    
    def stop_systems(self):
        """Stop all background systems."""
        self.stop_autosave()
        self.weather.stop()
        self.market.stop()
        if self._population_task:
            self._population_task.cancel()
            self._population_task = None
        if self._ground_loot_task:
            self._ground_loot_task.cancel()
            self._ground_loot_task = None
        if self._scavenger_task:
            self._scavenger_task.cancel()
            self._scavenger_task = None
        if self._clothing_degrade_task:
            self._clothing_degrade_task.cancel()
            self._clothing_degrade_task = None
    
    def register_session(self, player_name: str, session: 'MUDSession'):
        """Register a session for a player."""
        self.sessions[player_name] = session
    
    def unregister_session(self, player_name: str):
        """Unregister a session."""
        self.sessions.pop(player_name, None)
    
    async def broadcast_to_room(self, room_id: str, message: str, exclude: str = None):
        """Send a message to all players in a room."""
        for player_name, session in list(self.sessions.items()):
            player = self.player_manager.get_player(player_name)
            if player and player.current_room == room_id and player_name != exclude:
                try:
                    await session.send_message(f"\n{message}\n> ")
                except Exception as e:
                    logger.error(f"Error sending to {player_name}: {e}")

    async def broadcast_to_fishing_rooms(self, message: str):
        """Announce a message in every room where fishing is possible."""
        for room in self.rooms.values():
            if room.is_water:
                await self.broadcast_to_room(room.id, message)
    
    async def broadcast_global(self, message: str, exclude: str = None):
        """Send a message to all players."""
        for player_name, session in list(self.sessions.items()):
            if player_name != exclude:
                try:
                    await session.send_message(f"\n{message}\n> ")
                except Exception as e:
                    logger.error(f"Error sending to {player_name}: {e}")
    
    async def handle_broadcast(self, broadcast_str: str, sender: str):
        """Handle broadcast messages from command results."""
        if not broadcast_str:
            return
        
        # Parse broadcast format: ROOM:room_id:message or GLOBAL:message
        # Can have multiple broadcasts separated by |
        broadcasts = broadcast_str.split("|")
        
        for broadcast in broadcasts:
            parts = broadcast.split(":", 2)
            if len(parts) >= 2:
                broadcast_type = parts[0]
                
                if broadcast_type == "ROOM":
                    room_id = parts[1]
                    message = parts[2] if len(parts) > 2 else ""
                    await self.broadcast_to_room(room_id, message, exclude=sender)
                    
                elif broadcast_type == "GLOBAL":
                    message = parts[1]
                    await self.broadcast_global(message, exclude=sender)

    def admin_help(self) -> str:
        """Help text for the server admin console."""
        return """
Admin console commands:
  help                         Show this help
  who                          List online players
  uptime                       Show how long the server has been running
  saves                        List saved player characters
  save                         Force-save all online players
  broadcast <message>          Send a message to everyone
  kick <name>                  Disconnect a player
  unjail <name>                Clear a player's jail lock

  reset world                  Recreate rooms/items (players stay online)
  reset loot                   Respawn ground items now
  reset market                 Reset store prices and clothing stock
  reset weather                Set weather back to sunny
  reset player <name>          Reset one character (keeps password)
  reset players confirm        Reset ALL saved characters (keeps passwords)
  delete player <name>         Delete a save and kick if online
  shutdown                     Save everyone and stop the server
""".strip()

    async def handle_admin_command(self, line: str) -> str:
        """Parse and run an admin console command. Returns text to print."""
        parts = line.strip().split(maxsplit=1)
        if not parts:
            return ""
        cmd = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("help", "?"):
            return self.admin_help()

        if cmd in ("who", "players"):
            return self._admin_who()

        if cmd == "uptime":
            return self._admin_uptime()

        if cmd == "saves":
            saved = self.player_manager.get_all_saved_players()
            if not saved:
                return "No saved players."
            return "Saved players:\n  " + "\n  ".join(sorted(saved))

        if cmd == "save":
            self.player_manager.save_all_players()
            count = len(self.player_manager.players)
            return f"Saved {count} online player(s)."

        if cmd in ("broadcast", "say"):
            if not args:
                return "Usage: broadcast <message>"
            await self.broadcast_global(f"[ADMIN] {args}")
            return f"Broadcast sent: {args}"

        if cmd == "kick":
            return await self._admin_kick(args)

        if cmd == "unjail":
            return await self._admin_unjail(args)

        if cmd == "reset":
            return await self._admin_reset(args)

        if cmd == "delete":
            return await self._admin_delete(args)

        if cmd in ("shutdown", "stop"):
            return await self.shutdown()

        return f"Unknown admin command: '{cmd}'. Type 'help' for a list."

    async def shutdown(self) -> str:
        """Save players, disconnect sessions, and signal the server to stop."""
        if self.shutdown_event.is_set():
            return "Shutdown already in progress."

        logger.info("Admin requested shutdown")
        await self.broadcast_global(
            "*The server is shutting down. Your progress is being saved.*"
        )
        self.player_manager.save_all_players()
        saved = len(self.player_manager.players)

        for name, session in list(self.sessions.items()):
            session.running = False
            try:
                await session.send_message(
                    "\n*Server shutting down. Goodbye!*\n"
                )
            except Exception:
                pass
            try:
                session.process.exit(0)
            except Exception:
                pass

        self.shutdown_event.set()
        return f"Saved {saved} player(s). Shutting down..."

    def _admin_who(self) -> str:
        players = list(self.player_manager.players.values())
        if not players:
            return "No players online."
        lines = ["Online players:"]
        for player in sorted(players, key=lambda p: p.name.lower()):
            room = self.rooms.get(player.current_room)
            room_name = room.name if room else player.current_room
            lines.append(f"  {player.name} - {room_name} ({player.gold}g)")
        lines.append(f"Total: {len(players)}")
        return "\n".join(lines)

    def _admin_uptime(self) -> str:
        """Format how long the server process has been running."""
        elapsed = max(0, int(time.time() - self.started_at))
        days, rem = divmod(elapsed, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours or days:
            parts.append(f"{hours}h")
        if minutes or hours or days:
            parts.append(f"{minutes}m")
        parts.append(f"{seconds}s")
        started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started_at))
        return f"Uptime: {' '.join(parts)} (since {started})"

    async def _admin_kick(self, name: str) -> str:
        if not name:
            return "Usage: kick <name>"
        player = self.player_manager.find_online_player(name)
        if not player:
            return f"No online player named '{name}'."
        session = self.sessions.get(player.name)
        if session:
            try:
                await session.send_message(
                    "\n*You have been kicked by an admin.*\n"
                )
            except Exception:
                pass
            session.running = False
            try:
                session.process.exit(0)
            except Exception:
                pass
        return f"Kicked {player.name}."

    async def _admin_unjail(self, name: str) -> str:
        if not name:
            return "Usage: unjail <name>"
        player = self.player_manager.find_online_player(name)
        if player:
            player.jail_release_at = 0.0
            self.player_manager.save_player(player)
            session = self.sessions.get(player.name)
            if session:
                await session.send_message(
                    "\n*The jail door clicks open. An admin has released you.*\n> "
                )
            return f"Released {player.name} from jail lock."

        saved_name = self.player_manager.find_saved_player_name(name)
        if not saved_name:
            return f"No player named '{name}'."
        offline = self.player_manager.load_player(saved_name)
        if not offline:
            return f"Could not load '{saved_name}'."
        offline.jail_release_at = 0.0
        self.player_manager.save_player(offline)
        return f"Cleared jail lock for offline player {offline.name}."

    async def _admin_reset(self, args: str) -> str:
        if not args:
            return (
                "Usage: reset world|loot|market|weather|player <name>|"
                "players confirm"
            )

        parts = args.split(maxsplit=1)
        target = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if target == "world":
            return await self.reset_world()
        if target in ("loot", "ground", "items"):
            return await self.reset_loot()
        if target == "market":
            return await self.reset_market()
        if target == "weather":
            return await self.reset_weather()
        if target == "player":
            return await self.reset_player(rest)
        if target == "players":
            if rest.lower() != "confirm":
                return (
                    "This resets ALL characters. "
                    "Type: reset players confirm"
                )
            return await self.reset_all_players()

        return f"Unknown reset target '{target}'. Type 'help'."

    async def _admin_delete(self, args: str) -> str:
        parts = args.split(maxsplit=1)
        if not parts or parts[0].lower() != "player" or len(parts) < 2:
            return "Usage: delete player <name>"
        return await self.delete_player(parts[1].strip())

    async def reset_world(self) -> str:
        """Recreate the world map while keeping players online."""
        placements = []
        for player in self.player_manager.players.values():
            placements.append((player, player.current_room))
            old_room = self.rooms.get(player.current_room)
            if old_room:
                old_room.players.discard(player.name)

        self.rooms = create_world()
        self.commands.rooms = self.rooms

        moved = []
        for player, room_id in placements:
            if room_id not in self.rooms:
                room_id = "store"
                player.current_room = room_id
                moved.append(player.name)
            self.rooms[room_id].players.add(player.name)

        await self.broadcast_global(
            "*The world shimmers and resets around you.*"
        )
        for name in moved:
            session = self.sessions.get(name)
            if session and session.player:
                room = self.rooms.get(session.player.current_room)
                if room:
                    await session.send_message(
                        "\n*Your location no longer existed — "
                        "you were moved to Bubba's.*\n"
                        f"{room.get_description(current_player=name)}\n> "
                    )

        logger.info("Admin reset world")
        msg = f"World reset ({len(self.rooms)} rooms)."
        if moved:
            msg += f" Moved to store: {', '.join(moved)}"
        return msg

    async def reset_loot(self) -> str:
        """Respawn ground items immediately."""
        counts = reset_ground_items(self.rooms)
        await self.broadcast_global(
            "*A breeze stirs the trails — scattered finds on the ground have changed.*"
        )
        logger.info(
            f"Admin reset ground loot "
            f"(common={counts['common']}, clothing={counts['clothing']})"
        )
        return (
            f"Ground loot reset: {counts['common']} common, "
            f"{counts['clothing']} clothing."
        )

    async def reset_market(self) -> str:
        """Reinitialize market prices, clothing stock, and Bubba's fish quest."""
        self.market._initialize_prices()
        self.market.rotate_clothing_stock(announce=False)
        quest = self.market.rotate_bubba_quest(announce=False)
        await self.broadcast_global(
            "*Market prices and clothing stock have been reset.*\n"
            f'*Bubba hollers, "{quest.offer_phrase()}!"*'
        )
        logger.info("Admin reset market")
        return (
            f"Market prices and clothing stock reset. "
            f'Bubba quest: "{quest.offer_phrase()}"'
        )

    async def reset_weather(self) -> str:
        """Reset weather to sunny."""
        old = self.weather.current_weather
        self.weather.current_weather = WEATHER_DATA[WeatherType.SUNNY]
        self.weather.last_change = time.time()
        new = self.weather.current_weather
        if old.weather_type != new.weather_type:
            await self.broadcast_to_fishing_rooms(
                self.weather.get_weather_change_message(old, new)
            )
        else:
            await self.broadcast_to_fishing_rooms(
                f"*** The weather holds at {new.name.lower()}. ***\n"
                f"*** {new.affects_message} ***"
            )
        logger.info("Admin reset weather")
        return (
            f"Weather reset: {old.name} -> "
            f"{self.weather.current_weather.name}."
        )

    async def reset_player(self, name: str) -> str:
        """Reset one character to defaults, keeping password."""
        if not name:
            return "Usage: reset player <name>"

        online = self.player_manager.find_online_player(name)
        if online:
            return await self._apply_player_reset(online, online=True)

        saved_name = self.player_manager.find_saved_player_name(name)
        if not saved_name:
            return f"No player named '{name}'."
        offline = self.player_manager.load_player(saved_name)
        if not offline:
            return f"Could not load '{saved_name}'."
        return await self._apply_player_reset(offline, online=False)

    async def _apply_player_reset(self, source: Player, online: bool) -> str:
        self.commands.release_ancient_whiskers(source)
        fresh = self.player_manager.create_reset_player(source)

        if online:
            old_room = self.rooms.get(source.current_room)
            if old_room:
                old_room.players.discard(source.name)

            self.player_manager.players[source.name] = fresh
            store = self.rooms.get("store")
            if store:
                store.players.add(fresh.name)

            session = self.sessions.get(source.name)
            if session:
                session.player = fresh
                await session.send_message(
                    "\n*An admin has reset your character.*\n"
                    f"{self.rooms['store'].get_description(current_player=fresh.name)}\n> "
                )

        self.player_manager.save_player(fresh)
        logger.info(f"Admin reset player {fresh.name}")
        state = "online" if online else "offline"
        return f"Reset {state} player {fresh.name} (password kept)."

    async def reset_all_players(self) -> str:
        """Reset every saved character, including online ones."""
        names = set(self.player_manager.get_all_saved_players())
        names.update(self.player_manager.players.keys())
        reset_count = 0
        for name in sorted(names, key=str.lower):
            online = self.player_manager.find_online_player(name)
            if online:
                await self._apply_player_reset(online, online=True)
                reset_count += 1
                continue
            offline = self.player_manager.load_player(name)
            if offline:
                await self._apply_player_reset(offline, online=False)
                reset_count += 1
        return f"Reset {reset_count} player character(s)."

    async def delete_player(self, name: str) -> str:
        """Delete a save file and kick the player if online."""
        if not name:
            return "Usage: delete player <name>"

        online = self.player_manager.find_online_player(name)
        canonical = (
            online.name if online
            else self.player_manager.find_saved_player_name(name)
        )
        if not canonical and not online:
            return f"No player named '{name}'."

        display = canonical or name
        if online:
            self.commands.release_ancient_whiskers(online)
            session = self.sessions.get(online.name)
            room = self.rooms.get(online.current_room)
            if room:
                room.players.discard(online.name)
            self.unregister_session(online.name)
            self.player_manager.remove_player(online.name, save=False)
            if session:
                session.player = None  # prevent disconnect autosave
                session.running = False
                try:
                    await session.send_message(
                        "\n*Your character has been deleted by an admin.*\n"
                    )
                except Exception:
                    pass
                try:
                    session.process.exit(0)
                except Exception:
                    pass

        deleted = self.player_manager.delete_player_save(display)
        logger.info(f"Admin deleted player {display}")
        if deleted:
            return f"Deleted save for {display}."
        return f"Removed {display}, but no save file was found."


class MUDSession:
    """Handles an individual player's SSH session."""

    HISTORY_LIMIT = 20
    
    def __init__(self, game: FishingMUD, process: asyncssh.SSHServerProcess):
        self.game = game
        self.process = process
        self.player: Optional[Player] = None
        self.running = True
        self.command_history: list = []
        self.history_index: int = 0  # Points just past newest entry when at blank prompt
        self._slick_deal_task: Optional[asyncio.Task] = None
        
    async def send_message(self, message: str):
        """Send a message to the player."""
        try:
            self.process.stdout.write(message)
            await self.process.stdout.drain()
        except Exception as e:
            logger.error(f"Error sending message: {e}")

    def remember_command(self, command: str):
        """Store a submitted command in history (max HISTORY_LIMIT)."""
        command = command.strip()
        if not command:
            return
        if self.command_history and self.command_history[-1] == command:
            self.history_index = len(self.command_history)
            return
        self.command_history.append(command)
        if len(self.command_history) > self.HISTORY_LIMIT:
            self.command_history = self.command_history[-self.HISTORY_LIMIT:]
        self.history_index = len(self.command_history)

    async def _redraw_input_line(self, prompt: str, line: str):
        """Replace the current input line on the terminal."""
        # Carriage return, rewrite prompt + text, clear any leftover characters
        await self.send_message(f"\r{prompt}{line}\x1b[K")

    async def _read_escape_sequence(self) -> Optional[str]:
        """
        Read an ANSI escape sequence after ESC was received.
        Returns 'up', 'down', or None for unrecognized sequences.
        """
        try:
            first = await asyncio.wait_for(self.process.stdin.read(1), timeout=0.05)
        except asyncio.TimeoutError:
            return None
        if not first:
            return None

        # CSI sequences: ESC [ A/B  or  ESC O A/B (application cursor keys)
        if first in ('[', 'O'):
            try:
                second = await asyncio.wait_for(self.process.stdin.read(1), timeout=0.05)
            except asyncio.TimeoutError:
                return None
            if second == 'A':
                return 'up'
            if second == 'B':
                return 'down'
        return None
    
    async def get_input(self, prompt: str = "", hidden: bool = False) -> str:
        """Get input from the player. If hidden=True, don't echo characters (for passwords)."""
        if prompt:
            await self.send_message(prompt)
        
        line = ""
        # Draft line saved when browsing history so Down can restore it
        draft = ""
        # Index into history while browsing; len(history) means "on draft/empty"
        if not hidden:
            self.history_index = len(self.command_history)

        while self.running:
            try:
                char = await asyncio.wait_for(
                    self.process.stdin.read(1),
                    timeout=600  # 10 minute timeout
                )
                
                if not char:
                    raise EOFError()
                
                # Handle special characters
                if char in ('\r', '\n'):
                    await self.send_message('\r\n')
                    return line.strip()
                elif char == '\x1b' and not hidden:
                    direction = await self._read_escape_sequence()
                    if direction == 'up':
                        if not self.command_history:
                            continue
                        if self.history_index == len(self.command_history):
                            draft = line
                        if self.history_index > 0:
                            self.history_index -= 1
                            line = self.command_history[self.history_index]
                            await self._redraw_input_line(prompt, line)
                    elif direction == 'down':
                        if not self.command_history:
                            continue
                        if self.history_index < len(self.command_history):
                            self.history_index += 1
                            if self.history_index == len(self.command_history):
                                line = draft
                            else:
                                line = self.command_history[self.history_index]
                            await self._redraw_input_line(prompt, line)
                    # Ignore other escape sequences
                elif char == '\x7f' or char == '\x08':  # Backspace
                    if line:
                        line = line[:-1]
                        if not hidden:
                            await self.send_message('\x08 \x08')
                        # Editing leaves history browse mode relative to draft
                        if not hidden:
                            self.history_index = len(self.command_history)
                            draft = line
                elif char == '\x03':  # Ctrl+C
                    raise KeyboardInterrupt()
                elif char == '\x04':  # Ctrl+D
                    raise EOFError()
                elif char.isprintable():
                    line += char
                    if not hidden:
                        await self.send_message(char)
                        self.history_index = len(self.command_history)
                        draft = line
                    else:
                        await self.send_message('*')  # Show asterisks for passwords
                    
            except asyncio.TimeoutError:
                await self.send_message("\nConnection timed out due to inactivity.\n")
                raise EOFError()
    
    async def run(self):
        """Main session loop."""
        try:
            # Welcome screen
            welcome = """
╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║     🎣  WELCOME TO FISHING MUD  🎣                           ║
║                                                              ║
║     A Multi-User Fishing Adventure                          ║
║                                                              ║
║     Catch fish, meet friends, and hunt for                  ║
║     the legendary 'Old Whiskers'!                           ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝

"""
            await self.send_message(welcome)
            
            # Get player name
            name = None
            while True:
                name = await self.get_input("Enter your name: ")
                name = name.strip()
                
                if not name:
                    await self.send_message("Please enter a valid name.\n")
                    continue
                
                if len(name) > 20:
                    await self.send_message("Name too long (max 20 characters).\n")
                    continue
                
                if not name.isalnum():
                    await self.send_message("Name must be alphanumeric.\n")
                    continue
                
                # Check if already playing
                if self.game.player_manager.is_player_online(name):
                    await self.send_message(f"'{name}' is already playing. Choose another name.\n")
                    continue
                
                break
            
            # Check if player exists (returning) or new
            is_returning = self.game.player_manager.player_exists(name)
            
            if is_returning:
                # Returning player - authenticate
                await self.send_message(f"\nWelcome back, {name}!\n")
                
                max_attempts = 3
                for attempt in range(max_attempts):
                    password = await self.get_input("Enter your password: ", hidden=True)
                    
                    success, error = self.game.player_manager.authenticate_player(name, password)
                    
                    if success:
                        if error == "NEEDS_PASSWORD":
                            # Legacy player without password
                            await self.send_message("\nYour account needs a password for security.\n")
                            while True:
                                new_pass = await self.get_input("Set a password (min 4 chars): ", hidden=True)
                                if len(new_pass) < 4:
                                    await self.send_message("Password too short. Try again.\n")
                                    continue
                                confirm = await self.get_input("Confirm password: ", hidden=True)
                                if new_pass != confirm:
                                    await self.send_message("Passwords don't match. Try again.\n")
                                    continue
                                break
                            
                            self.player = self.game.player_manager.load_player(name)
                            self.player.set_password(new_pass)
                            self.game.player_manager.save_player(self.player)
                            await self.send_message("Password set!\n")
                        else:
                            # Load the player
                            self.player = self.game.player_manager.load_player(name)
                        break
                    else:
                        remaining = max_attempts - attempt - 1
                        if remaining > 0:
                            await self.send_message(f"Incorrect password. {remaining} attempts remaining.\n")
                        else:
                            await self.send_message("Too many failed attempts. Goodbye!\n")
                            return
                else:
                    return  # Failed all attempts
                    
            else:
                # New player - register
                await self.send_message(f"\nNew player! Let's set up your account, {name}.\n")
                
                while True:
                    password = await self.get_input("Choose a password (min 4 chars): ", hidden=True)
                    
                    if len(password) < 4:
                        await self.send_message("Password too short. Try again.\n")
                        continue
                    
                    confirm = await self.get_input("Confirm password: ", hidden=True)
                    
                    if password != confirm:
                        await self.send_message("Passwords don't match. Try again.\n")
                        continue
                    
                    break
                
                self.player, error = self.game.player_manager.register_player(name, password)
                if error:
                    await self.send_message(f"Error: {error}\n")
                    return
                
                await self.send_message("\nAccount created! Your progress will be saved.\n")
            
            # Add player to the game
            self.game.player_manager.add_player_to_game(self.player)
            self.game.register_session(name, self)
            
            # Add player to their room (might be different if returning)
            current_room = self.game.rooms.get(self.player.current_room)
            current_room.players.add(name)
            
            # Announce arrival
            if is_returning:
                await self.game.broadcast_global(f"{name} has returned!", exclude=name)
                await self.send_message(f"\nWelcome back, {name}!\n")
            else:
                await self.game.broadcast_global(f"{name} has joined the game!", exclude=name)
                await self.send_message(f"\nWelcome, {name}! You have 50 gold to start.\n")

            # Equipment overview first
            await self.send_message(self.player.get_equipment_display() + "\n")

            if is_returning:
                await self.send_message(
                    f"\nYou have {self.player.gold} gold and "
                    f"{len(self.player.inventory)} items.\n"
                )
                await self.send_message(f"Fish caught: {self.player.fish_caught}")
                if self.player.biggest_catch > 0:
                    await self.send_message(
                        f" | Biggest catch: {self.player.biggest_catch:.1f} lbs"
                    )
                await self.send_message(
                    f"\nLevel {self.player.level} — "
                    f"{self.player.total_weight_caught:.1f} lbs caught lifetime"
                )
                await self.send_message("\n")
            
            await self.send_message("Type 'help' for commands, or 'look' to see where you are.\n")
            
            # Show initial room
            result = self.game.commands.cmd_look(self.player, "")
            await self.send_message(result.message + "\n")

            # If logging in already inside Slick's, start the visit timer
            if self.player.current_room == "slick_store":
                self.player.begin_slick_visit()
            self._sync_slick_deal_timer()
            
            # Track commands for auto-save
            command_count = 0
            
            # Main game loop
            while self.running:
                try:
                    command = await self.get_input("> ")
                    
                    if not command:
                        continue

                    self.remember_command(command)
                    
                    result = self.game.commands.parse_and_execute(self.player, command)
                    
                    if result.message == "QUIT":
                        # Save before quitting
                        self.game.player_manager.save_player(self.player)
                        await self.send_message("\nProgress saved. Thanks for playing! Goodbye!\n")
                        break

                    if result.stages:
                        if result.immediate_message:
                            await self.send_message(
                                result.immediate_message + "\n"
                            )
                        for stage in result.stages:
                            if stage.delay_seconds > 0:
                                await asyncio.sleep(stage.delay_seconds)
                            if stage.message:
                                await self.send_message(stage.message + "\n")
                            if stage.broadcast:
                                await self.game.handle_broadcast(
                                    stage.broadcast, self.player.name
                                )

                        if result.reel_challenge:
                            await self._run_reel_challenge(
                                result.reel_challenge
                            )

                        if result.deferred:
                            deferred_out = result.deferred()
                            if isinstance(deferred_out, tuple):
                                result.message = deferred_out[0] or ""
                                extra_bc = deferred_out[1] if len(deferred_out) > 1 else None
                                if extra_bc:
                                    if result.broadcast:
                                        result.broadcast = (
                                            f"{result.broadcast}|{extra_bc}"
                                        )
                                    else:
                                        result.broadcast = extra_bc
                            else:
                                result.message = deferred_out
                    elif result.delay_seconds > 0:
                        if result.immediate_message:
                            await self.send_message(
                                result.immediate_message + "\n"
                            )
                        await asyncio.sleep(result.delay_seconds)
                        if result.deferred:
                            deferred_out = result.deferred()
                            if isinstance(deferred_out, tuple):
                                result.message = deferred_out[0] or ""
                                extra_bc = deferred_out[1] if len(deferred_out) > 1 else None
                                if extra_bc:
                                    if result.broadcast:
                                        result.broadcast = (
                                            f"{result.broadcast}|{extra_bc}"
                                        )
                                    else:
                                        result.broadcast = extra_bc
                            else:
                                result.message = deferred_out

                    if result.message:
                        await self.send_message(result.message + "\n")
                    
                    # Handle broadcasts
                    if result.broadcast:
                        await self.game.handle_broadcast(result.broadcast, self.player.name)

                    self._sync_slick_deal_timer()
                    
                    # Auto-save every 10 commands
                    command_count += 1
                    if command_count >= 10:
                        self.game.player_manager.save_player(self.player)
                        command_count = 0
                        
                except EOFError:
                    break
                except KeyboardInterrupt:
                    await self.send_message("\nUse 'quit' to exit.\n")
                except Exception as e:
                    logger.error(f"Error in session loop: {e}")
                    await self.send_message(f"\nError: {e}\n")
        
        finally:
            await self.cleanup()

    async def _run_reel_challenge(self, challenge) -> None:
        """
        Run a dynamic reel timer.

        Direction mistakes add 25% of the original duration. Helpful mental
        events can remove 15 seconds, and time spent answering those events
        continues to count down the reel.
        """
        original = float(challenge.total_seconds)
        remaining = original
        response_seconds = max(2, int(challenge.response_seconds))
        direction_in = 40.0
        helpful_in = 5.0
        mental_score = 2 * challenge.intelligence + challenge.wisdom
        helpful_chance = min(75.0, max(0.0, 2.5 * (mental_score - 3)))
        helpful_actions = ("reel", "pull", "slack", "yank")

        progress = [
            [original * 0.8, "You still have a lot of line to reel in", False],
            [original * 0.6, "You are almost halfway done", False],
            [original * 0.4, "You are more than halfway done", False],
            [original * 0.2, "You almost have the fish!", False],
        ]

        def show_progress() -> list[str]:
            messages = []
            for entry in progress:
                threshold, message, shown = entry
                if not shown and remaining <= threshold:
                    messages.append(message)
                    entry[2] = True
            return messages

        async def pass_reel_time(seconds: float) -> None:
            """Count down normal reel time and both recurring event clocks."""
            nonlocal remaining, direction_in, helpful_in
            if seconds <= 0:
                return
            await asyncio.sleep(seconds)
            remaining = max(0.0, remaining - seconds)
            direction_in -= seconds
            helpful_in -= seconds

        while remaining > 0:
            next_progress = min(
                (
                    remaining - threshold
                    for threshold, _, shown in progress
                    if not shown and remaining > threshold
                ),
                default=remaining,
            )
            wait = min(remaining, direction_in, helpful_in, next_progress)
            await pass_reel_time(max(0.0, wait))

            for message in show_progress():
                await self.send_message(f"\n{message}\n")

            # Direction checks pause the reel while the player answers.
            if direction_in <= 0:
                direction_in += 40.0
                pulling = random.choice(("left", "right"))
                correct = "right" if pulling == "left" else "left"
                await self.send_message(
                    f"\nThe fish is pulling to the {pulling}, "
                    "pull the other way!\n"
                )
                try:
                    response = await asyncio.wait_for(
                        self.get_input(
                            f"(You have {response_seconds} seconds) > "
                        ),
                        timeout=response_seconds,
                    )
                    succeeded = response.strip().lower() in {correct, correct[0]}
                except asyncio.TimeoutError:
                    succeeded = False

                if succeeded:
                    await self.send_message(
                        f"You pull {correct} and keep the line tight!\n"
                    )
                else:
                    penalty = original * 0.25
                    remaining += penalty
                    await self.send_message(
                        "The fish takes more line! You have more to reel in.\n"
                    )

            # Helpful opportunities are rolled every 5 seconds of reel time.
            if helpful_in <= 0 and remaining > 0:
                helpful_in += 5.0
                if random.random() * 100.0 < helpful_chance:
                    action = random.choice(helpful_actions)
                    window = min(5.0, remaining)
                    await self.send_message(
                        f"\nYou see an opening! Type {action.upper()} now!\n"
                    )
                    started = monotonic()
                    answer = ""
                    try:
                        answer = await asyncio.wait_for(
                            self.get_input("(You have 5 seconds) > "),
                            timeout=window,
                        )
                    except asyncio.TimeoutError:
                        pass
                    elapsed = min(window, monotonic() - started)
                    remaining = max(0.0, remaining - elapsed)
                    direction_in -= elapsed
                    helpful_in -= elapsed

                    if answer.strip().lower() == action:
                        saved = min(15.0, remaining)
                        remaining -= saved
                        await self.send_message(
                            f"Perfect {action}! You bring the fish in faster.\n"
                        )
                    elif remaining > 0:
                        await self.send_message(
                            "The opening passes, but you keep reeling.\n"
                        )

                    for message in show_progress():
                        await self.send_message(f"\n{message}\n")

    def _cancel_slick_deal_task(self):
        """Cancel any pending Slick worm-deal timer."""
        if self._slick_deal_task and not self._slick_deal_task.done():
            self._slick_deal_task.cancel()
        self._slick_deal_task = None

    def _sync_slick_deal_timer(self):
        """
        Start a once-per-visit 60s timer while inside Slick's.
        Cancel it when the player leaves.
        """
        if not self.player:
            self._cancel_slick_deal_task()
            return
        if self.player.current_room != "slick_store":
            self._cancel_slick_deal_task()
            return
        if self.player.slick_deal_offered:
            return
        if self._slick_deal_task and not self._slick_deal_task.done():
            return
        visit_id = self.player.slick_visit_id
        self._slick_deal_task = asyncio.create_task(
            self._slick_worm_deal_after_delay(visit_id)
        )

    async def _slick_worm_deal_after_delay(self, visit_id: int):
        """After 1 minute in Slick's, pitch the worm deal once this visit."""
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            return

        if not self.running or not self.player:
            return
        if self.player.current_room != "slick_store":
            return
        if self.player.slick_visit_id != visit_id:
            return
        if self.player.slick_deal_offered:
            return

        self.player.slick_deal_offered = True
        self.player.slick_deal_pending = True
        await self.send_message(
            '\nSlick leans over the counter.\n'
            '"You seem like you are passionate about fishing. I normally sell '
            'these worms at $3 for each, but for you, ill make you a deal and '
            'give you 3 for $10. What do you say?"\n'
            '(Type yes or no)\n'
        )
        # Nudge so the prompt isn't buried under the pitch
        try:
            self.process.stdout.write("> ")
            await self.process.stdout.drain()
        except Exception:
            pass
    
    async def cleanup(self):
        """Clean up when player disconnects."""
        self.running = False
        self._cancel_slick_deal_task()
        
        if self.player:
            player_name = self.player.name
            self.player.clear_slick_visit()
            self.game.commands.release_ancient_whiskers(self.player)
            
            # Remove from room
            room = self.game.rooms.get(self.player.current_room)
            if room:
                room.players.discard(player_name)
            
            # Announce departure
            await self.game.broadcast_global(f"{player_name} has left the game.", exclude=player_name)
            
            # Unregister and save player progress
            self.game.unregister_session(player_name)
            self.game.player_manager.remove_player(player_name, save=True)  # Save on disconnect
            
            logger.info(f"Player {player_name} disconnected (progress saved)")


class MUDSSHServer(asyncssh.SSHServer):
    """SSH Server that handles authentication."""
    
    def __init__(self, game: FishingMUD):
        self.game = game
    
    def connection_made(self, conn):
        logger.info(f"New SSH connection from {conn.get_extra_info('peername')}")
    
    def connection_lost(self, exc):
        if exc:
            logger.error(f"Connection lost: {exc}")
    
    def begin_auth(self, username: str) -> bool:
        # Allow any username, no password required for the game
        return False  # No auth required
    
    def password_auth_supported(self) -> bool:
        return True
    
    def validate_password(self, username: str, password: str) -> bool:
        # Accept any password (or empty)
        return True


async def handle_client(process: asyncssh.SSHServerProcess, game: FishingMUD):
    """Handle a new SSH client connection."""
    # Set up terminal
    process.channel.set_line_mode(False)
    process.channel.set_echo(False)
    
    session = MUDSession(game, process)
    
    try:
        await session.run()
    except Exception as e:
        logger.error(f"Session error: {e}")
    finally:
        process.exit(0)


async def run_admin_console(game: FishingMUD):
    """
    Read admin commands from the server process stdin while the MUD runs.
    Type 'help' in the terminal that started mud_server.py.
    """
    print("\nAdmin console ready. Type 'help' for commands.\n", flush=True)
    while True:
        try:
            line = await asyncio.to_thread(sys.stdin.readline)
        except Exception as e:
            logger.error(f"Admin console error: {e}")
            break

        if line == "":
            logger.info("Admin console closed (stdin EOF)")
            break

        line = line.strip()
        if not line:
            continue

        try:
            result = await game.handle_admin_command(line)
            if result:
                print(result, flush=True)
            if game.shutdown_event.is_set():
                break
        except Exception as e:
            print(f"Admin error: {e}", flush=True)
            logger.exception("Admin command failed")


async def start_server(host: str = '0.0.0.0', port: int = 2222):
    """Start the SSH server."""
    game = FishingMUD()
    
    # Generate host keys if they don't exist
    key_path = Path('ssh_host_key')
    if not key_path.exists():
        logger.info("Generating SSH host key...")
        key = asyncssh.generate_private_key('ssh-rsa', key_size=2048)
        key.write_private_key('ssh_host_key')
        key.write_public_key('ssh_host_key.pub')
        logger.info("SSH host key generated")
    
    def server_factory():
        return MUDSSHServer(game)
    
    async def process_factory(process):
        await handle_client(process, game)
    
    server = await asyncssh.create_server(
        server_factory,
        host,
        port,
        server_host_keys=['ssh_host_key'],
        process_factory=process_factory
    )
    
    # Start background systems
    await game.start_autosave(interval=300)  # Save every 5 minutes
    await game.start_weather()  # Changes with population updates
    await game.start_market(interval=600)  # Market updates every 10 min
    await game.start_population_updates(interval=120)  # Fish + weather every 2 min
    await game.start_ground_loot_resets(interval=3600)  # Ground items every hour
    await game.start_scavenger_cleanup(interval=10800)  # Clear clutter every 3 hours
    await game.start_clothing_degrade(interval=1800)  # Worn clothes every 30 min
    admin_task = asyncio.create_task(run_admin_console(game))
    
    logger.info(f"")
    logger.info(f"╔════════════════════════════════════════════════════════════╗")
    logger.info(f"║  FISHING MUD SERVER STARTED                                ║")
    logger.info(f"║                                                            ║")
    logger.info(f"║  Connect with: ssh -p {port} <username>@{host:<21} ║")
    logger.info(f"║                                                            ║")
    logger.info(f"║  Example: ssh -p 2222 fisher@localhost                     ║")
    logger.info(f"║                                                            ║")
    logger.info(f"║  Admin: type commands in this terminal (help / shutdown)   ║")
    logger.info(f"╚════════════════════════════════════════════════════════════╝")
    logger.info(f"")
    
    try:
        await game.shutdown_event.wait()
    finally:
        admin_task.cancel()
        game.stop_systems()
        server.close()
        await server.wait_closed()
        logger.info("Server shut down.")


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Fishing MUD Server')
    parser.add_argument('--host', default='0.0.0.0', help='Host to bind to')
    parser.add_argument('--port', type=int, default=2222, help='Port to listen on')
    args = parser.parse_args()
    
    try:
        asyncio.run(start_server(args.host, args.port))
    except KeyboardInterrupt:
        logger.info("Server shutting down...")
    except Exception as e:
        logger.error(f"Server error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
