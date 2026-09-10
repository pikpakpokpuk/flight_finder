# ✈️ Flight Finder

[![Lint](https://github.com/pikpakpokpuk/flight_finder/actions/workflows/lint.yml/badge.svg)](https://github.com/pikpakpokpuk/flight_finder/actions/workflows/lint.yml)

Multi-airport Ryanair / Wizz Air flight search. Give it a start and
destination city (not a specific airport), a date window, and it searches
every commercial airport within range of each, on both airlines, and ranks
the cheapest outbound and return flights independently -- including
self-connect 1-stop options where no direct route exists.

## Disclaimer

This is an **unofficial, personal-use tool**. It is **not affiliated with,
endorsed by, or connected to Ryanair or Wizz Air** in any way. It queries
those airlines' own (undocumented, internal) APIs via the third-party
wrapper libraries [Flyan](https://pypi.org/project/Flyan/) and
[Flywizz](https://pypi.org/project/Flywizz/), not a public/official API.

Automated querying like this is typically against the letter of an airline's
terms of service, even when done at low volume for personal trip planning.
Use at your own risk -- no warranty, no guarantee prices/availability shown
here match what you'd get booking directly, and no guarantee this keeps
working if an airline changes its API. Don't use this for high-volume or
commercial scraping.

## Features

- Resolves a city name to nearby airports (OurAirports data, live geocoding)
- Searches only airports Ryanair or Wizz Air actually serve
- Direct + optional 1-stop (self-connect) journeys, with layover-time and
  same-day sanity checks
- Outbound and return flights ranked independently (supports open-jaw trips)
- Prices normalized to EUR using live exchange rates
- CLI and Streamlit web UI

## Setup

```bash
pip install -r requirements.txt
```

## Usage

**CLI** (interactive prompts for locations, dates, airport picks):

```bash
python flight_finder.py
```

**Web UI** (city autocomplete, checkboxes, results table, CSV download):

```bash
streamlit run app.py
```

## How it works

1. `airports.py` geocodes your start/destination city and finds nearby
   commercial airports (within a configurable radius) that Ryanair or Wizz
   Air actually fly from/to.
2. `flight_finder.py` searches every selected origin/destination airport
   pair on both airlines across your date window, and -- if enabled --
   1-stop self-connect options through any airport with a direct route from
   the origin and to the destination.
3. Results are deduplicated, priced in EUR, and the cheapest outbound and
   return flights are printed/shown, independently of each other.

## Notes

- A "1-stop" journey is two separate one-way tickets that happen to connect
  through a third airport -- Ryanair/Wizz don't interline, so there's no
  missed-connection protection and no baggage transfer between legs.
- Config knobs (search radius, price cap, layover bounds, retry behavior,
  etc.) live at the top of [flight_finder.py](flight_finder.py).

## License

[MIT](LICENSE)
