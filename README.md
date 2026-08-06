# Fishing MUD

A Multi-User Dimension (MUD) text-based fishing game where players connect via SSH to fish, explore, and interact with each other.

## Features

- **Multi-player**: Multiple users can connect simultaneously via SSH
- **Persistent Progress**: Player data (inventory, gold, stats) saved automatically
- **Exploration**: Navigate through a fishing-themed world using cardinal directions
- **Fishing System**: Equip poles and lures to catch fish of varying rarity
- **Dynamic Weather**: Weather changes every 5-15 minutes and affects fishing
- **Two Stores**: Bubba's (fair prices) and Slick's Surplus (volatile market)
- **Fluctuating Market**: Fish and item prices change over time - buy low, sell high!
- **Item Management**: Get, drop, buy, sell, and equip items
- **Social**: Chat with other players in the same room or shout globally

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Start the Server

```bash
python mud_server.py
```

The server will:
- Generate SSH host keys automatically (first run)
- Listen on port 2222 by default

### 3. Connect as a Player

```bash
ssh -p 2222 anyusername@localhost
```

**First time connecting:**
- Enter your desired player name
- Set a password (minimum 4 characters)
- Your account is created and progress will be saved

**Returning players:**
- Enter your player name
- Enter your password
- Your progress is restored (inventory, location, stats)

## Server Options

```bash
python mud_server.py --host 0.0.0.0 --port 2222
```

- `--host`: IP address to bind to (default: 0.0.0.0)
- `--port`: Port number (default: 2222)

## Game Commands

### Movement
| Command | Description |
|---------|-------------|
| `north/n`, `south/s`, `east/e`, `west/w` | Move in a direction |
| `go <direction>` | Move in specified direction |

### Items
| Command | Description |
|---------|-------------|
| `look` or `l` | Look at surroundings |
| `get <item>` | Pick up an item |
| `drop <item>` | Drop an item |
| `inventory` or `i` | Show your inventory |
| `examine <item>` | Look closely at something |
| `equip/wear/don <item>` | Equip fishing gear or wear clothing |
| `unequip <item/slot>` | Remove equipped gear or clothing |
| `stats` or `attributes` | Show character attributes |

### Fishing
| Command | Description |
|---------|-------------|
| `fish` or `cast` | Cast your line (requires equipped pole) |
| `consider` or `con` | Try to estimate the local fish population |
| `weather` | Check current weather conditions |

### Shopping (at Bubba's or Slick's)
| Command | Description |
|---------|-------------|
| `list` | See items for sale and current prices |
| `buy <item>` | Purchase an item |
| `sell <item>` | Sell fish or items |

### Social
| Command | Description |
|---------|-------------|
| `say <message>` | Talk to others in the room |
| `shout <message>` | Yell to everyone on the server |
| `who` | See who's online |

### Other
| Command | Description |
|---------|-------------|
| `help` | Show help information |
| `save` | Manually save your progress |
| `quit` | Leave the game (auto-saves) |

## Save System

Player progress is automatically saved:
- Every 10 commands
- Every 5 minutes (server auto-save)
- When you quit or disconnect

**Authentication:**
- Each player has a password-protected account
- Passwords are securely hashed (PBKDF2-SHA256 with salt)
- 3 login attempts allowed before disconnection

Save files are stored in the `saves/` directory as JSON files. When you reconnect with the same name and password, your progress is restored.

## Characters and Wearable Equipment

Characters have the six standard D&D attributes—Strength, Dexterity,
Constitution, Intelligence, Wisdom, and Charisma. New and existing characters
default to 1 in every attribute.

Wearable equipment has one of seven slots: head, neck, chest, hands, fingers,
legs, or feet. Equipping another item in an occupied slot automatically removes
the previous item. Clothing, worn slots, and attributes persist between logins.

Store clothing stock rotates every hour. Each shop carries a random selection of
8–20 apparel items at a time—check `list` often, and if something is missing,
come back after the next restock.

## Item Condition and Modifiers

Every item has a **condition** from 0–9:

| Value | Label |
|-------|-------|
| 0 | broken |
| 1 | ruined |
| 2 | battered |
| 3 | disheveled |
| 4 | worn |
| 5 | bog-standard |
| 6 | decent |
| 7 | nice |
| 8 | fine |
| 9 | new |

Fishing poles, lures, and bait **degrade by 1** each time you cast. Clothing and
jewelry do not degrade. Broken fishing gear cannot be used.

Equipment (poles, lures, bait, wearables) also rolls a single attribute modifier
from **+0 to +10**. Higher bonuses are exponentially rarer (~90% +0, ~9% +1,
~0.9% +2, and so on). A +0 modifier is omitted from the name; otherwise items
read like `new wool cap of +2 charisma`.

## Weather System

Weather changes every 5-15 minutes and affects fishing:

| Weather | Catch Rate | Rare Fish | Notes |
|---------|------------|-----------|-------|
| Sunny | Normal | Normal | Standard conditions |
| Partly Cloudy | +10% | Normal | Good fishing |
| Cloudy | +20% | +10% | Fish are active |
| Overcast | +30% | +20% | Excellent! Fish feeding |
| Light Rain | +40% | +30% | Fish are biting! |
| Rain | +30% | +40% | Great for big fish |
| Thunderstorm | -30% | +100% | Dangerous but legendary fish stir... |
| Fog | +10% | +50% | Mysterious, rare fish surface |
| Windy | -20% | -10% | Tough conditions |

Use the `weather` command to check conditions before fishing!

### Fish Populations and Timed Fishing

Every fishing spot has a population from 0–100% which changes every two
minutes. Population controls both bite chance and waiting time: 100% guarantees
an immediate bite, while 0% cannot produce a fish.

Use `consider` or `con` at water to estimate the population. The attempt always
has at least a 5% success chance, improved by Intelligence and Wisdom bonuses
from equipped items. Successful estimates use a different description for each
10% population band.

Once a fish bites, reeling takes time based on fish size. Equipped Strength and
Constitution bonuses reduce that timer. Fish sizes are weighted by rarity:
tiny and small specimens are common, while large and trophy specimens are
rarer and worth more. Fish never receive attribute modifiers.

## Market System

**Two Stores:**

1. **Bubba's Bait & Tackle** (North) - Fair, stable prices
   - Honest dealings, consistent pricing
   - Buys fish at 80% value
   - Only buys fish (not equipment)

2. **Slick's Surplus** (South of lake) - Volatile, opportunistic
   - Prices fluctuate wildly (up to 50% swings!)
   - Sometimes has great deals, sometimes overpriced
   - Buys ANYTHING (fish and equipment)
   - Check back often for the best prices

**Price Indicators:**
- ↑ = Prices rising
- ↓ = Prices falling  
- → = Prices stable

Market prices update every 10 minutes. Watch for announcements!

## World Map

```
                    ┌─────────────────┐
                    │  Bubba's Bait   │
                    │   & Tackle  $   │ ◄── Start Here!
                    └────────┬────────┘
                             │ S
                    ┌────────▼────────┐
                    │  Store Porch    │
                    └────────┬────────┘
                             │ S
                    ┌────────▼────────┐
                    │  Shady Trail    │
                    │    (North)      │
                    └────────┬────────┘
                             │ S
         ┌───────────────────┼───────────────────┐
         │ W                 │                 E │
┌────────▼────────┐ ┌────────▼────────┐ ┌────────▼────────┐
│   Rocky Path    │ │    Forest       │ │   Overgrown     │
│                 │ │   Crossroads    │ │     Path        │
└────────┬────────┘ └────────┬────────┘ └────────┬────────┘
         │ W                 │ S                 │ E
┌────────▼────────┐ ┌────────▼────────┐ ┌────────▼────────┐
│   Old Wooden    │ │  Shady Trail    │ │   Quiet Cove    │
│     Pier  🎣    │ │    (South)      │ │       🎣        │
└────────┬────────┘ └────────┬────────┘ └─────────────────┘
         │ S                 │ S
┌────────▼────────┐ ┌────────▼────────┐
│   Rocky Point   │◄┼─►  Lake Shore   │
│       🎣        │ │       🎣        │
└────────┬────────┘ └─────────────────┘
         │ S
┌────────▼────────┐
│   Muddy Trail   │
└────────┬────────┘
         │ S
┌────────▼────────┐
│ Slick's Surplus │
│    Outside      │
└────────┬────────┘
         │ S
┌────────▼────────┐
│    Slick's   $  │ ◄── Shady dealer!
│    Surplus      │
└─────────────────┘

🎣 = Fishing spot    $ = Store
```

## Fish & Rarity

| Fish | Rarity | Base Value |
|------|--------|------------|
| Bluegill | Common | 5 gold |
| Largemouth Bass | Uncommon | 15 gold |
| Channel Catfish | Uncommon | 20 gold |
| Rainbow Trout | Rare | 25 gold |
| Northern Pike | Very Rare | 40 gold |
| Old Whiskers (Legendary) | Legendary | 500 gold |

## Tips

1. **Get started**: Buy a basic fishing pole and some bait from Bubba's store
2. **Equip first**: Use `equip <pole>` and `equip <lure>` before fishing
3. **Check weather**: Use `weather` - conditions affect your catch rate!
4. **Better gear**: Higher quality poles and lures increase your chances of catching rare fish
5. **Compare prices**: Check both stores - Slick's prices fluctuate wildly
6. **Thunderstorms**: Dangerous conditions, but legendary fish become more active...
7. **Legendary catch**: Try to catch "Old Whiskers" at Rocky Point!

## File Structure

```
├── mud_server.py    # Main SSH server and game loop
├── world.py         # Room definitions and world map (14 rooms)
├── player.py        # Player class and management
├── commands.py      # Command parser and handlers
├── items.py         # Item definitions (poles, lures, fish)
├── weather.py       # Weather system with fishing modifiers
├── market.py        # Dynamic market with fluctuating prices
├── requirements.txt # Python dependencies
├── README.md        # This file
├── saves/           # Player save files (auto-created)
│   └── *.json       # Individual player data
├── ssh_host_key     # SSH host key (auto-generated)
└── ssh_host_key.pub # SSH public key (auto-generated)
```

## Running as a Service (Linux)

Create a systemd service file at `/etc/systemd/system/fishing-mud.service`:

```ini
[Unit]
Description=Fishing MUD Server
After=network.target

[Service]
Type=simple
User=mud
WorkingDirectory=/path/to/Newshot
ExecStart=/usr/bin/python3 mud_server.py --port 2222
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then enable and start:

```bash
sudo systemctl enable fishing-mud
sudo systemctl start fishing-mud
```

## License

MIT License - Feel free to modify and share!
