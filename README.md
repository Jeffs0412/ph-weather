# ph-weather MCP server

An MCP server that lets Claude Desktop answer questions about current and forecast
weather anywhere in the Philippines. Data comes from [Open-Meteo](https://open-meteo.com),
which is free and needs no API key. All times are Philippine Standard Time (UTC+8).

## Tools

| Tool | Use it for |
| --- | --- |
| `get_current_weather(location)` | Conditions right now, plus today's high/low and rain chance |
| `get_hourly_forecast(location, date, start_hour, end_hour)` | A specific time or part of a day, up to 16 days ahead (or 92 days back) |
| `get_daily_forecast(location, days)` | A day-by-day outlook for 1–16 days |
| `search_ph_locations(query, limit)` | Disambiguating common names such as "San Jose" |

`location` accepts a city, municipality, island, or landmark (`"Cebu City"`, `"Boracay"`),
optionally with a province or region (`"San Jose, Batangas"`, `"Makati, Metro Manila"`),
or raw coordinates (`"14.5995,120.9842"`).

## Run locally

```sh
uv sync
uv run weather          # starts the server on stdio (it waits for an MCP client)
uv run mcp dev src/weather/server.py   # optional: inspect it in the MCP Inspector
```

## Connect to Claude Desktop

Add this to `claude_desktop_config.json`, then fully quit and reopen Claude Desktop:

```json
{
  "mcpServers": {
    "ph-weather": {
      "command": "C:\Users\manal\.local\bin\uv.exe",
      "args": ["--directory", "C:\JEFF_FILES\JEFF_PROJECTS\AI\mcp_project\weather", "run", "weather"]
    }
  }
}
```

The config file is at `%APPDATA%\Claude\claude_desktop_config.json` for the standard
installer, or `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json`
for the Microsoft Store version. Server logs are in the `logs` folder next to it (`mcp-server-ph-weather.log`).

## Limitations

These are numerical model forecasts, not official advisories. For typhoon signals,
rainfall warnings, and other bulletins, check [PAGASA](https://pagasa.dost.gov.ph).
