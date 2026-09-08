#!/usr/bin/env python3
"""
Flight Finder: multi-airport one-way search
=============================================

Searches Ryanair (via Flyan) and Wizz Air (via Flywizz) across every
combination of starting airport -> arrival airport, for every one-way
flight inside a given date window. Prices are normalized to EUR. Prints
the top N cheapest options.

SETUP (run these once on your own machine):
    pip install Flyan Flywizz requests

USAGE:
    python flight_finder.py

Everything you'd want to tweak lives in the "SETTINGS" block below.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from itertools import product
from typing import Optional

import requests

from flyan import RyanAir, FlightSearchParams
from flywizz import WizzAir, TimetableSearch


# ============================================================================
# SETTINGS -- edit this block for your own search
# ============================================================================

# Every airport you could fly out from.
ORIGIN_AIRPORTS = ["CPH", "MMX"]

# Every airport you'd be willing to fly into.
DESTINATION_AIRPORTS = ["OTP", "TGM", "CLJ", "GHV", "BUD"]

# Search window: earliest possible departure to latest possible departure.
SEARCH_FROM = datetime(2026, 12, 20)
SEARCH_TO = datetime(2027, 1, 10)

# Drop flights priced above this (EUR) before ranking, just to cut noise.
# Set to None to disable.
MAX_PRICE_EUR: Optional[float] = 300

# How many results to print/save at the end.
TOP_N = 20

OUTPUT_CSV = "flight_options.csv"


# ============================================================================
# Internals -- shouldn't need to touch below this line
# ============================================================================


@dataclass
class FlightOption:
    airline: str
    origin: str
    destination: str
    departure: datetime
    price_original: float
    currency: str
    price_eur: float
    flight_number: str = ""


def get_eur_rates() -> dict:
    """Fetch live exchange rates (free, no API key) so DKK/SEK/EUR are comparable."""
    try:
        resp = requests.get("https://api.frankfurter.app/latest?from=EUR", timeout=10)
        resp.raise_for_status()
        rates = resp.json()["rates"]
        rates["EUR"] = 1.0
        return rates
    except Exception as e:
        print(f"  [warning] couldn't fetch live exchange rates ({e}); assuming 1:1 to EUR")
        return {"EUR": 1.0, "DKK": 7.46, "SEK": 11.2, "RON": 5.0, "HUF": 400.0}


def to_eur(amount: float, currency: str, rates: dict) -> float:
    rate = rates.get(currency)
    if not rate:
        return amount  # unknown currency, leave as-is (will show clearly in output)
    return amount / rate


def search_ryanair(origin: str, destination: str, rates: dict) -> list[FlightOption]:
    client = RyanAir(currency="EUR")
    params = FlightSearchParams(
        from_airport=origin,
        to_airport=destination,
        from_date=SEARCH_FROM,
        to_date=SEARCH_TO,
    )
    try:
        flights = client.get_oneways(params)
    except Exception as e:
        print(f"  [Ryanair] {origin} -> {destination}: no results / error ({e})")
        return []

    out = []
    for f in flights:
        out.append(FlightOption(
            airline="Ryanair",
            origin=origin,
            destination=destination,
            departure=f.departure_date,
            price_original=f.price,
            currency=f.currency,
            price_eur=to_eur(f.price, f.currency, rates),
            flight_number=f.flight_number,
        ))
    return out


def search_wizzair(origin: str, destination: str, rates: dict) -> list[FlightOption]:
    client = WizzAir()
    params = TimetableSearch(
        origin=origin,
        destination=destination,
        date_from=SEARCH_FROM,
        date_to=SEARCH_TO,
    )
    try:
        entries = client.get_timetable(params)
    except Exception as e:
        print(f"  [Wizz Air] {origin} -> {destination}: no results / error ({e})")
        return []

    out = []
    for entry in entries:
        if entry.price is None:
            continue
        out.append(FlightOption(
            airline="Wizz Air",
            origin=origin,
            destination=destination,
            departure=entry.departure_date,
            price_original=entry.price.amount,
            currency=entry.price.currency,
            price_eur=to_eur(entry.price.amount, entry.price.currency, rates),
        ))
    return out


def main():
    print(
        f"Searching {len(ORIGIN_AIRPORTS)} origin(s) x {len(DESTINATION_AIRPORTS)} destination(s) "
        f"between {SEARCH_FROM:%Y-%m-%d} and {SEARCH_TO:%Y-%m-%d}...\n"
    )

    rates = get_eur_rates()

    all_options: list[FlightOption] = []
    for origin, destination in product(ORIGIN_AIRPORTS, DESTINATION_AIRPORTS):
        print(f"Pair: {origin} -> {destination}")
        all_options += search_ryanair(origin, destination, rates)
        all_options += search_wizzair(origin, destination, rates)

    if MAX_PRICE_EUR is not None:
        all_options = [o for o in all_options if o.price_eur <= MAX_PRICE_EUR]

    if not all_options:
        print("\nNo flights found. Try widening the date range, raising MAX_PRICE_EUR, "
              "or double-check the airport codes.")
        return

    all_options.sort(key=lambda o: o.price_eur)
    top_options = all_options[:TOP_N]

    def price_str(o: FlightOption) -> str:
        extra = f" ({o.price_original:.0f} {o.currency})" if o.currency != "EUR" else ""
        return f"{o.price_eur:.0f} EUR{extra}"

    print(f"\n{'='*90}\nTOP {TOP_N} CHEAPEST ONE-WAY OPTIONS\n{'='*90}")
    header = f"{'Route':<10}{'Airline':<10}{'Departure':<20}{'Price':<20}"
    print(header)
    for o in top_options:
        route = f"{o.origin}->{o.destination}"
        dep_str = o.departure.strftime("%Y-%m-%d %H:%M")
        print(f"{route:<10}{o.airline:<10}{dep_str:<20}{price_str(o):<20}")

    # Save everything (not just top N) to CSV for your own filtering/sorting
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["origin", "destination", "airline", "flight_number", "departure_date",
                          "departure_time", "price_eur", "price_original", "currency"])
        for o in all_options:
            writer.writerow([o.origin, o.destination, o.airline, o.flight_number,
                              o.departure.strftime("%Y-%m-%d"), o.departure.strftime("%H:%M"),
                              f"{o.price_eur:.2f}", o.price_original, o.currency])

    print(f"\nFull results ({len(all_options)} flights) saved to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
