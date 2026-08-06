"""
Weather system for the MUD Fishing Game
Weather changes periodically and affects fishing
"""

import random
import asyncio
import time
from dataclasses import dataclass
from typing import Dict, List, Callable, Optional
from enum import Enum


class WeatherType(Enum):
    SUNNY = "sunny"
    PARTLY_CLOUDY = "partly_cloudy"
    CLOUDY = "cloudy"
    OVERCAST = "overcast"
    LIGHT_RAIN = "light_rain"
    RAIN = "rain"
    THUNDERSTORM = "thunderstorm"
    FOG = "fog"
    WINDY = "windy"


@dataclass
class Weather:
    weather_type: WeatherType
    name: str
    description: str
    fish_modifier: float  # Multiplier for catch chance
    rare_fish_modifier: float  # Bonus for rare fish
    affects_message: str  # What players see about fishing conditions


WEATHER_DATA: Dict[WeatherType, Weather] = {
    WeatherType.SUNNY: Weather(
        weather_type=WeatherType.SUNNY,
        name="Sunny",
        description="The sun shines brightly overhead. Perfect day to be outside.",
        fish_modifier=1.0,
        rare_fish_modifier=1.0,
        affects_message="Normal fishing conditions."
    ),
    WeatherType.PARTLY_CLOUDY: Weather(
        weather_type=WeatherType.PARTLY_CLOUDY,
        name="Partly Cloudy",
        description="White clouds drift lazily across a blue sky.",
        fish_modifier=1.1,
        rare_fish_modifier=1.0,
        affects_message="Good fishing conditions."
    ),
    WeatherType.CLOUDY: Weather(
        weather_type=WeatherType.CLOUDY,
        name="Cloudy",
        description="Gray clouds blanket the sky, blocking the sun.",
        fish_modifier=1.2,
        rare_fish_modifier=1.1,
        affects_message="Fish are more active in the shade."
    ),
    WeatherType.OVERCAST: Weather(
        weather_type=WeatherType.OVERCAST,
        name="Overcast",
        description="Heavy clouds hang low, threatening rain.",
        fish_modifier=1.3,
        rare_fish_modifier=1.2,
        affects_message="Excellent fishing! Fish are feeding before the storm."
    ),
    WeatherType.LIGHT_RAIN: Weather(
        weather_type=WeatherType.LIGHT_RAIN,
        name="Light Rain",
        description="A gentle drizzle falls from the sky.",
        fish_modifier=1.4,
        rare_fish_modifier=1.3,
        affects_message="Rain stirs up the water. Fish are biting!"
    ),
    WeatherType.RAIN: Weather(
        weather_type=WeatherType.RAIN,
        name="Rainy",
        description="Steady rain patters on the water's surface.",
        fish_modifier=1.3,
        rare_fish_modifier=1.4,
        affects_message="Great conditions for catching big fish!"
    ),
    WeatherType.THUNDERSTORM: Weather(
        weather_type=WeatherType.THUNDERSTORM,
        name="Thunderstorm",
        description="Lightning flashes and thunder rumbles across the lake. Dangerous!",
        fish_modifier=0.7,
        rare_fish_modifier=2.0,
        affects_message="Dangerous conditions, but legendary fish stir in the depths..."
    ),
    WeatherType.FOG: Weather(
        weather_type=WeatherType.FOG,
        name="Foggy",
        description="Thick fog rolls across the lake, visibility is poor.",
        fish_modifier=1.1,
        rare_fish_modifier=1.5,
        affects_message="Mysterious conditions. Rare fish seem drawn to the surface."
    ),
    WeatherType.WINDY: Weather(
        weather_type=WeatherType.WINDY,
        name="Windy",
        description="Strong winds whip across the water, creating choppy waves.",
        fish_modifier=0.8,
        rare_fish_modifier=0.9,
        affects_message="Tough casting conditions. Fish are staying deep."
    ),
}

# Weather transition probabilities (what weather can follow what)
WEATHER_TRANSITIONS: Dict[WeatherType, List[tuple[WeatherType, int]]] = {
    WeatherType.SUNNY: [
        (WeatherType.SUNNY, 40),
        (WeatherType.PARTLY_CLOUDY, 35),
        (WeatherType.WINDY, 15),
        (WeatherType.FOG, 10),
    ],
    WeatherType.PARTLY_CLOUDY: [
        (WeatherType.SUNNY, 25),
        (WeatherType.PARTLY_CLOUDY, 30),
        (WeatherType.CLOUDY, 30),
        (WeatherType.WINDY, 15),
    ],
    WeatherType.CLOUDY: [
        (WeatherType.PARTLY_CLOUDY, 20),
        (WeatherType.CLOUDY, 25),
        (WeatherType.OVERCAST, 30),
        (WeatherType.LIGHT_RAIN, 15),
        (WeatherType.FOG, 10),
    ],
    WeatherType.OVERCAST: [
        (WeatherType.CLOUDY, 20),
        (WeatherType.OVERCAST, 20),
        (WeatherType.LIGHT_RAIN, 35),
        (WeatherType.RAIN, 20),
        (WeatherType.THUNDERSTORM, 5),
    ],
    WeatherType.LIGHT_RAIN: [
        (WeatherType.OVERCAST, 25),
        (WeatherType.LIGHT_RAIN, 30),
        (WeatherType.RAIN, 30),
        (WeatherType.CLOUDY, 15),
    ],
    WeatherType.RAIN: [
        (WeatherType.LIGHT_RAIN, 25),
        (WeatherType.RAIN, 30),
        (WeatherType.THUNDERSTORM, 20),
        (WeatherType.OVERCAST, 25),
    ],
    WeatherType.THUNDERSTORM: [
        (WeatherType.RAIN, 40),
        (WeatherType.THUNDERSTORM, 20),
        (WeatherType.OVERCAST, 30),
        (WeatherType.CLOUDY, 10),
    ],
    WeatherType.FOG: [
        (WeatherType.FOG, 30),
        (WeatherType.CLOUDY, 30),
        (WeatherType.PARTLY_CLOUDY, 25),
        (WeatherType.SUNNY, 15),
    ],
    WeatherType.WINDY: [
        (WeatherType.WINDY, 30),
        (WeatherType.PARTLY_CLOUDY, 30),
        (WeatherType.CLOUDY, 25),
        (WeatherType.SUNNY, 15),
    ],
}


class WeatherSystem:
    """Manages weather state and transitions."""
    
    def __init__(self):
        self.current_weather: Weather = WEATHER_DATA[WeatherType.SUNNY]
        self.last_change: float = time.time()
        self._task: Optional[asyncio.Task] = None
        self._broadcast_callback: Optional[Callable] = None
    
    def get_current_weather(self) -> Weather:
        """Get the current weather."""
        return self.current_weather
    
    def get_weather_display(self) -> str:
        """Get a formatted weather display."""
        w = self.current_weather
        lines = [
            f"\nWeather: {w.name}",
            f"{w.description}",
            f"Fishing: {w.affects_message}",
        ]
        return "\n".join(lines)
    
    def _select_next_weather(self) -> Weather:
        """Select the next weather based on transition probabilities."""
        transitions = WEATHER_TRANSITIONS.get(
            self.current_weather.weather_type,
            [(WeatherType.SUNNY, 100)]
        )
        
        total = sum(weight for _, weight in transitions)
        roll = random.randint(1, total)
        
        cumulative = 0
        for weather_type, weight in transitions:
            cumulative += weight
            if roll <= cumulative:
                return WEATHER_DATA[weather_type]
        
        return WEATHER_DATA[WeatherType.SUNNY]
    
    def change_weather(self) -> tuple[Weather, Weather]:
        """Change to new weather. Returns (old_weather, new_weather)."""
        old_weather = self.current_weather
        self.current_weather = self._select_next_weather()
        self.last_change = time.time()
        return old_weather, self.current_weather
    
    def set_broadcast_callback(self, callback: Callable):
        """Set callback for broadcasting weather changes."""
        self._broadcast_callback = callback
    
    async def start(self, min_interval: int = 300, max_interval: int = 900):
        """Start the weather change loop (5-15 minutes by default)."""
        async def weather_loop():
            while True:
                # Wait random interval between changes
                wait_time = random.randint(min_interval, max_interval)
                await asyncio.sleep(wait_time)
                
                old, new = self.change_weather()
                
                if old.weather_type != new.weather_type:
                    # Broadcast weather change
                    if self._broadcast_callback:
                        change_msg = self.get_weather_change_message(old, new)
                        await self._broadcast_callback(change_msg)
        
        self._task = asyncio.create_task(weather_loop())
    
    def get_weather_change_message(self, old: Weather, new: Weather) -> str:
        """Generate a message for weather transitions."""
        transitions = {
            (WeatherType.SUNNY, WeatherType.PARTLY_CLOUDY): "Clouds begin to drift across the sky.",
            (WeatherType.SUNNY, WeatherType.WINDY): "A strong wind picks up suddenly.",
            (WeatherType.SUNNY, WeatherType.FOG): "A mysterious fog rolls in from the lake.",
            (WeatherType.PARTLY_CLOUDY, WeatherType.SUNNY): "The clouds part and the sun shines through.",
            (WeatherType.PARTLY_CLOUDY, WeatherType.CLOUDY): "More clouds gather overhead.",
            (WeatherType.CLOUDY, WeatherType.OVERCAST): "The sky grows dark with heavy clouds.",
            (WeatherType.CLOUDY, WeatherType.LIGHT_RAIN): "Light rain begins to fall.",
            (WeatherType.OVERCAST, WeatherType.LIGHT_RAIN): "Raindrops begin to fall from the dark sky.",
            (WeatherType.OVERCAST, WeatherType.RAIN): "Rain starts pouring down.",
            (WeatherType.OVERCAST, WeatherType.THUNDERSTORM): "Thunder rumbles in the distance as a storm approaches!",
            (WeatherType.LIGHT_RAIN, WeatherType.RAIN): "The rain intensifies.",
            (WeatherType.RAIN, WeatherType.THUNDERSTORM): "Lightning flashes! A thunderstorm has arrived!",
            (WeatherType.RAIN, WeatherType.LIGHT_RAIN): "The rain begins to let up.",
            (WeatherType.THUNDERSTORM, WeatherType.RAIN): "The thunder fades, but rain continues.",
            (WeatherType.FOG, WeatherType.SUNNY): "The fog lifts, revealing a sunny sky.",
            (WeatherType.FOG, WeatherType.CLOUDY): "The fog thins into low clouds.",
            (WeatherType.WINDY, WeatherType.SUNNY): "The wind dies down to a gentle breeze.",
        }
        
        key = (old.weather_type, new.weather_type)
        if key in transitions:
            lead = transitions[key]
        else:
            lead = f"The weather changes to {new.name.lower()}."

        return (
            f"*** {lead} ***\n"
            f"*** The weather is now {new.name.lower()}. ***\n"
            f"*** {new.affects_message} ***"
        )
    
    def stop(self):
        """Stop the weather loop."""
        if self._task:
            self._task.cancel()
            self._task = None
