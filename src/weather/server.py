"""MCP server that gives Claude weather data for locations in the Philippines.

Data comes from Open-Meteo (https://open-meteo.com), which is free and needs no
API key. Place names are resolved with Open-Meteo's geocoding API, restricted to
the Philippines, and every timestamp is reported in Philippine time (UTC+8).

The server talks to Claude Desktop over stdio, so nothing here may write to
stdout.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "ph-weather-mcp/0.1"
TIMEZONE = "Asia/Manila"
# The Philippines has no daylight saving time, so a fixed offset avoids needing tzdata on Windows.
PH_TZ = timezone(timedelta(hours=8), "PHT")

# Open-Meteo serves up to 16 days of forecast and up to 92 days of recent history.
MAX_FORECAST_DAYS = 16
MAX_PAST_DAYS = 92

# Rough bounding box of the Philippine archipelago, used to sanity-check coordinates.
PH_LAT_RANGE = (4.0, 21.5)
PH_LON_RANGE = (116.0, 127.5)

WMO_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snowfall",
    73: "Moderate snowfall",
    75: "Heavy snowfall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}

# Lower rank = more likely to be the place a person means by a bare name.
FEATURE_RANK = {"PPLC": 0, "PPLA": 1, "PPLA2": 2, "PPLA3": 3, "PPLA4": 4, "PPL": 5}

# Everyday names for regions that the geocoder records under their official names.
REGION_ALIASES = {
    "metro manila": "national capital region",
    "ncr": "national capital region",
    "mm": "national capital region",
    "car": "cordillera",
    "barmm": "bangsamoro",
    "armm": "bangsamoro",
}

COORD_PATTERN = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")

INSTRUCTIONS = f"""\
Weather data for the Philippines (source: Open-Meteo, no API key needed).

Use these tools whenever the user asks about current, upcoming, or recent weather
for any place in the Philippines: cities, municipalities, provinces, islands, or
landmarks.

- get_current_weather: conditions right now.
- get_hourly_forecast: a specific time or part of a day ("at 3pm tomorrow",
  "this evening", "Saturday morning"). Pass the date as YYYY-MM-DD and an hour range.
- get_daily_forecast: day-by-day outlook, up to {MAX_FORECAST_DAYS} days.
- search_ph_locations: check which place a name resolves to when it is ambiguous.

All times are Philippine Standard Time (PHT, UTC+8). Each tool's output states
the current PHT date and time, so resolve relative dates such as "tomorrow" or
"this weekend" against that. Locations may also be given as "latitude,longitude".

These are model forecasts, not official advisories. For typhoon signals, rainfall
warnings, or other official bulletins, point the user to PAGASA (pagasa.dost.gov.ph).
"""

mcp = MCPServer(name="ph-weather", instructions=INSTRUCTIONS)


@dataclass
class Place:
    name: str
    latitude: float
    longitude: float
    region: str = ""

    @property
    def label(self) -> str:
        return f"{self.name}, {self.region}" if self.region else self.name


def _now_ph() -> datetime:
    return datetime.now(PH_TZ)


async def _get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        async with httpx2.AsyncClient(timeout=20.0, headers={"User-Agent": USER_AGENT}) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
    except httpx2.HTTPStatusError as exc:
        reason = ""
        try:
            reason = exc.response.json().get("reason", "")
        except ValueError:
            pass
        raise ToolError(f"Weather service returned HTTP {exc.response.status_code}. {reason}".strip()) from exc
    except httpx2.HTTPError as exc:
        raise ToolError(f"Could not reach the weather service: {exc}") from exc


def _region_of(result: dict[str, Any]) -> str:
    parts = [result.get(key) for key in ("admin2", "admin1")]
    return ", ".join(p for p in parts if p and p != result.get("name"))


def _name_variants(name: str) -> list[str]:
    """Spellings to try, since geocoding matches names like "Makati City" but not "Makati City, NCR"."""
    variants = [name]
    stripped = re.sub(r"^(city|municipality) of\s+|\s+city$", "", name, flags=re.IGNORECASE).strip()
    if stripped and stripped.lower() != name.lower():
        variants.append(stripped)
    if not name.lower().endswith(" city"):
        variants.append(f"{name} City")
    return variants


async def _search(query: str, count: int = 10) -> list[dict[str, Any]]:
    """Geocode a place name within the Philippines, best match first."""
    parts = [p.strip() for p in query.split(",") if p.strip()]
    parts = [p for p in parts if p.lower() not in {"philippines", "ph", "phl"}]
    if not parts:
        raise ToolError("Please give a place name in the Philippines, e.g. 'Cebu City' or 'Baguio'.")
    name = parts[0]
    hints = [REGION_ALIASES.get(h.lower(), h.lower()) for h in parts[1:]]

    seen: dict[int, dict[str, Any]] = {}
    for variant in _name_variants(name):
        data = await _get_json(
            GEOCODING_URL,
            {"name": variant, "count": count, "countryCode": "PH", "language": "en", "format": "json"},
        )
        for result in data.get("results", []):
            seen.setdefault(result["id"], result)

    def score(result: dict[str, Any]) -> tuple:
        admin = " ".join(str(result.get(k, "")) for k in ("admin1", "admin2", "admin3", "admin4")).lower()
        hint_misses = sum(1 for h in hints if h not in admin)
        exact = result.get("name", "").lower() in {v.lower() for v in _name_variants(name)}
        return (
            hint_misses,
            0 if exact else 1,
            FEATURE_RANK.get(result.get("feature_code", ""), 6),
            -(result.get("population") or 0),
        )

    return sorted(seen.values(), key=score)


async def _resolve(location: str) -> Place:
    match = COORD_PATTERN.match(location)
    if match:
        lat, lon = float(match.group(1)), float(match.group(2))
        if not (PH_LAT_RANGE[0] <= lat <= PH_LAT_RANGE[1] and PH_LON_RANGE[0] <= lon <= PH_LON_RANGE[1]):
            raise ToolError(f"Coordinates {lat}, {lon} are outside the Philippines.")
        return Place(name=f"{lat:.4f}, {lon:.4f}", latitude=lat, longitude=lon)

    results = await _search(location)
    if not results:
        raise ToolError(
            f"No place in the Philippines matched '{location}'. Try the city or municipality name, "
            "add the province (e.g. 'San Jose, Batangas'), or pass 'latitude,longitude'."
        )
    best = results[0]
    return Place(
        name=best["name"],
        latitude=best["latitude"],
        longitude=best["longitude"],
        region=_region_of(best),
    )


def _describe(code: int | None) -> str:
    if code is None:
        return "Unknown"
    return WMO_CODES.get(int(code), f"Weather code {code}")


def _compass(degrees: float | None) -> str:
    if degrees is None:
        return "?"
    points = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return points[round(degrees / 22.5) % 16]


def _fmt(value: Any, unit: str = "", digits: int = 1) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        value = round(value, digits)
    return f"{value}{unit}"


def _header(place: Place) -> str:
    now = _now_ph()
    return (
        f"Location: {place.label} ({place.latitude:.4f}, {place.longitude:.4f})\n"
        f"Current time in the Philippines: {now:%A, %d %B %Y %I:%M %p} PHT"
    )


def _parse_date(value: str | None) -> date:
    today = _now_ph().date()
    if not value:
        return today
    text = value.strip().lower()
    relative = {"today": 0, "tomorrow": 1, "yesterday": -1}
    if text in relative:
        return today + timedelta(days=relative[text])
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ToolError(f"Invalid date '{value}'. Use YYYY-MM-DD, 'today', or 'tomorrow'.") from exc


@mcp.tool()
async def search_ph_locations(query: str, limit: int = 5) -> str:
    """Find places in the Philippines matching a name, best match first.

    Use this to disambiguate common names (e.g. "San Jose", "Santa Cruz") before
    asking for weather, or to confirm which place a name resolves to.

    Args:
        query: Place name, optionally followed by province/region,
            e.g. "San Jose, Occidental Mindoro" or "Tagaytay".
        limit: Maximum number of matches to return (1-20).
    """
    limit = max(1, min(limit, 20))
    results = (await _search(query, count=max(limit, 10)))[:limit]
    if not results:
        return f"No places in the Philippines matched '{query}'."
    lines = [f"Matches for '{query}':"]
    for i, r in enumerate(results, 1):
        population = f", pop. {r['population']:,}" if r.get("population") else ""
        region = _region_of(r)
        lines.append(
            f"{i}. {r['name']}{', ' + region if region else ''} "
            f"({r['latitude']:.4f}, {r['longitude']:.4f}{population})"
        )
    lines.append("Pass the name with its province, or the coordinates, to the weather tools for an exact match.")
    return "\n".join(lines)


@mcp.tool()
async def get_current_weather(location: str) -> str:
    """Get the weather right now at a place in the Philippines.

    Args:
        location: City, municipality, province, or landmark in the Philippines
            (e.g. "Quezon City", "Davao City", "Boracay", "Baguio, Benguet"),
            or "latitude,longitude".
    """
    place = await _resolve(location)
    data = await _get_json(
        FORECAST_URL,
        {
            "latitude": place.latitude,
            "longitude": place.longitude,
            "timezone": TIMEZONE,
            "current": ",".join(
                [
                    "temperature_2m",
                    "apparent_temperature",
                    "relative_humidity_2m",
                    "is_day",
                    "precipitation",
                    "weather_code",
                    "cloud_cover",
                    "pressure_msl",
                    "wind_speed_10m",
                    "wind_direction_10m",
                    "wind_gusts_10m",
                ]
            ),
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,uv_index_max,sunrise,sunset",
            "forecast_days": 1,
        },
    )
    c = data["current"]
    d = data.get("daily", {})
    observed = datetime.fromisoformat(c["time"])

    def today(key: str) -> Any:
        values = d.get(key) or [None]
        return values[0]

    return "\n".join(
        [
            _header(place),
            f"Observed at: {observed:%I:%M %p} PHT ({'day' if c.get('is_day') else 'night'})",
            "",
            f"Conditions: {_describe(c.get('weather_code'))}",
            f"Temperature: {_fmt(c.get('temperature_2m'), '°C')} (feels like {_fmt(c.get('apparent_temperature'), '°C')})",
            f"Humidity: {_fmt(c.get('relative_humidity_2m'), '%')}",
            f"Precipitation (last 15 min): {_fmt(c.get('precipitation'), ' mm')}",
            f"Cloud cover: {_fmt(c.get('cloud_cover'), '%')}",
            f"Wind: {_fmt(c.get('wind_speed_10m'), ' km/h')} from {_compass(c.get('wind_direction_10m'))}, "
            f"gusts {_fmt(c.get('wind_gusts_10m'), ' km/h')}",
            f"Pressure: {_fmt(c.get('pressure_msl'), ' hPa')}",
            "",
            f"Today: high {_fmt(today('temperature_2m_max'), '°C')}, low {_fmt(today('temperature_2m_min'), '°C')}, "
            f"max chance of rain {_fmt(today('precipitation_probability_max'), '%')}, "
            f"max UV index {_fmt(today('uv_index_max'))}",
            f"Sunrise {_fmt(today('sunrise'))[-5:]}, sunset {_fmt(today('sunset'))[-5:]}",
        ]
    )


@mcp.tool()
async def get_hourly_forecast(
    location: str,
    date: str | None = None,
    start_hour: int = 0,
    end_hour: int = 23,
) -> str:
    """Get the hour-by-hour weather for one day at a place in the Philippines.

    Use this for questions about a specific time or part of the day, e.g.
    "Will it rain in Makati at 5pm?" or "What's the weather in Cebu tomorrow morning?".
    Works for up to 16 days ahead and up to 92 days in the past.

    Args:
        location: City, municipality, province, or landmark in the Philippines,
            or "latitude,longitude".
        date: Day in Philippine time as YYYY-MM-DD, or "today"/"tomorrow".
            Defaults to today.
        start_hour: First hour to include, 0-23 in Philippine time (e.g. 15 for 3 PM).
        end_hour: Last hour to include, 0-23 in Philippine time.
    """
    day = _parse_date(date)
    today = _now_ph().date()
    if day > today + timedelta(days=MAX_FORECAST_DAYS - 1):
        raise ToolError(f"Forecasts only go {MAX_FORECAST_DAYS} days ahead (until {today + timedelta(days=MAX_FORECAST_DAYS - 1)}).")
    if day < today - timedelta(days=MAX_PAST_DAYS):
        raise ToolError(f"Hourly history only goes back {MAX_PAST_DAYS} days.")
    if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23) or start_hour > end_hour:
        raise ToolError("start_hour and end_hour must be 0-23 with start_hour <= end_hour.")

    place = await _resolve(location)
    data = await _get_json(
        FORECAST_URL,
        {
            "latitude": place.latitude,
            "longitude": place.longitude,
            "timezone": TIMEZONE,
            "start_date": day.isoformat(),
            "end_date": day.isoformat(),
            "hourly": ",".join(
                [
                    "temperature_2m",
                    "apparent_temperature",
                    "relative_humidity_2m",
                    "precipitation_probability",
                    "precipitation",
                    "weather_code",
                    "cloud_cover",
                    "wind_speed_10m",
                    "wind_direction_10m",
                    "wind_gusts_10m",
                    "uv_index",
                ]
            ),
        },
    )
    h = data["hourly"]
    lines = [
        _header(place),
        f"Hourly forecast for {day:%A, %d %B %Y} (PHT):",
        "",
        "Time     | Conditions              | Temp (feels) | Humidity | Rain chance / amount | Wind (gusts)            | UV",
    ]
    for i, stamp in enumerate(h["time"]):
        t = datetime.fromisoformat(stamp)
        if not (start_hour <= t.hour <= end_hour):
            continue
        lines.append(
            f"{t:%I:%M %p} | {_describe(h['weather_code'][i]):<23} | "
            f"{_fmt(h['temperature_2m'][i], '°C')} ({_fmt(h['apparent_temperature'][i], '°C')}) | "
            f"{_fmt(h['relative_humidity_2m'][i], '%')} | "
            f"{_fmt(h['precipitation_probability'][i], '%')} / {_fmt(h['precipitation'][i], ' mm')} | "
            f"{_fmt(h['wind_speed_10m'][i], ' km/h')} {_compass(h['wind_direction_10m'][i])} "
            f"({_fmt(h['wind_gusts_10m'][i], ' km/h')}) | {_fmt(h['uv_index'][i])}"
        )
    if day < today:
        lines.append("\nNote: this date is in the past, so values are modelled observations, not a forecast.")
    return "\n".join(lines)


@mcp.tool()
async def get_daily_forecast(location: str, days: int = 7) -> str:
    """Get a day-by-day weather forecast for a place in the Philippines.

    Use this for multi-day outlooks, e.g. "What's the weather in Palawan this week?"
    or "Will it rain in Manila this weekend?". Day 1 is today in Philippine time.

    Args:
        location: City, municipality, province, or landmark in the Philippines,
            or "latitude,longitude".
        days: Number of days to forecast, 1-16.
    """
    days = max(1, min(days, MAX_FORECAST_DAYS))
    place = await _resolve(location)
    data = await _get_json(
        FORECAST_URL,
        {
            "latitude": place.latitude,
            "longitude": place.longitude,
            "timezone": TIMEZONE,
            "forecast_days": days,
            "daily": ",".join(
                [
                    "weather_code",
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "apparent_temperature_max",
                    "precipitation_sum",
                    "precipitation_probability_max",
                    "precipitation_hours",
                    "wind_speed_10m_max",
                    "wind_gusts_10m_max",
                    "wind_direction_10m_dominant",
                    "uv_index_max",
                    "sunrise",
                    "sunset",
                ]
            ),
        },
    )
    d = data["daily"]
    lines = [_header(place), f"{days}-day forecast (PHT):", ""]
    for i, stamp in enumerate(d["time"]):
        day = date.fromisoformat(stamp)
        lines.extend(
            [
                f"{day:%a %d %b %Y}: {_describe(d['weather_code'][i])}",
                f"  Temp {_fmt(d['temperature_2m_min'][i], '°C')} to {_fmt(d['temperature_2m_max'][i], '°C')} "
                f"(feels up to {_fmt(d['apparent_temperature_max'][i], '°C')})",
                f"  Rain: {_fmt(d['precipitation_probability_max'][i], '%')} chance, "
                f"{_fmt(d['precipitation_sum'][i], ' mm')} over {_fmt(d['precipitation_hours'][i], ' h')}",
                f"  Wind up to {_fmt(d['wind_speed_10m_max'][i], ' km/h')} from "
                f"{_compass(d['wind_direction_10m_dominant'][i])}, gusts {_fmt(d['wind_gusts_10m_max'][i], ' km/h')}",
                f"  UV index max {_fmt(d['uv_index_max'][i])}; sunrise {d['sunrise'][i][-5:]}, sunset {d['sunset'][i][-5:]}",
            ]
        )
    return "\n".join(lines)


def run() -> None:
    mcp.run(transport="stdio")
