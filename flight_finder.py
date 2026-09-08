#!/usr/bin/env python3
"""
Flight Finder: interactive multi-airport one-way / round-trip search
=======================================================================

Asks for your start and destination locations (plain place names),
finds commercial airports within range of each using OurAirports data,
lets you pick which ones to consider, then asks for an outbound date
window, a return date window, and an acceptable length-of-stay range.
Searches Ryanair (via Flyan) and Wizz Air (via Flywizz) across every
selected origin/destination airport pair, pairs up outbound and return
flights that fit the stay-length interval, and prints the top N
cheapest round trips.

SETUP (run these once on your own machine):
    pip install Flyan Flywizz requests

USAGE:
    python flight_finder.py
"""

from __future__ import annotations

import csv
import sys
from dataclasses import dataclass
from datetime import datetime
from itertools import product
from typing import Optional

import requests

# Windows consoles default to a codepage (e.g. cp1252) that can't print
# airport names with diacritics (Bucharest's "Henri Coandă", "Malmö", ...).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

from flyan import RyanAir, FlightSearchParams
from flywizz import WizzAir, TimetableSearch

import airports

# ============================================================================
# SETTINGS
# ============================================================================

NEARBY_RADIUS_KM = 200

# Drop legs priced above this (EUR) before pairing, just to cut noise.
# Set to None to disable.
MAX_LEG_PRICE_EUR: Optional[float] = 300

# How many round-trip results to print/save at the end.
TOP_N = 20

OUTPUT_CSV = "flight_options.csv"


# ============================================================================
# Flight search
# ============================================================================


@dataclass
class Leg:
    airline: str
    origin: str
    destination: str
    departure: datetime
    price_original: float
    currency: str
    price_eur: float
    flight_number: str = ""


@dataclass
class RoundTrip:
    outbound: Leg
    inbound: Leg

    @property
    def total_price_eur(self) -> float:
        return self.outbound.price_eur + self.inbound.price_eur

    @property
    def stay_days(self) -> int:
        return (self.inbound.departure.date() - self.outbound.departure.date()).days


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


def search_ryanair(origin: str, destination: str, date_from: datetime, date_to: datetime, rates: dict) -> list[Leg]:
    client = RyanAir(currency="EUR")
    params = FlightSearchParams(
        from_airport=origin,
        to_airport=destination,
        from_date=date_from,
        to_date=date_to,
    )
    try:
        flights = client.get_oneways(params)
    except Exception as e:
        print(f"  [Ryanair] {origin} -> {destination}: no results / error ({e})")
        return []

    out = []
    for f in flights:
        out.append(Leg(
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


def search_wizzair(origin: str, destination: str, date_from: datetime, date_to: datetime, rates: dict) -> list[Leg]:
    client = WizzAir()
    params = TimetableSearch(
        origin=origin,
        destination=destination,
        date_from=date_from,
        date_to=date_to,
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
        out.append(Leg(
            airline="Wizz Air",
            origin=origin,
            destination=destination,
            departure=entry.departure_date,
            price_original=entry.price.amount,
            currency=entry.price.currency,
            price_eur=to_eur(entry.price.amount, entry.price.currency, rates),
        ))
    return out


def search_legs(origin: str, destination: str, date_from: datetime, date_to: datetime, rates: dict) -> list[Leg]:
    legs = (
        search_ryanair(origin, destination, date_from, date_to, rates)
        + search_wizzair(origin, destination, date_from, date_to, rates)
    )
    if MAX_LEG_PRICE_EUR is not None:
        legs = [l for l in legs if l.price_eur <= MAX_LEG_PRICE_EUR]
    return legs


def build_round_trips(outbound_legs: list[Leg], inbound_legs: list[Leg], stay_min: int, stay_max: int) -> list[RoundTrip]:
    trips = []
    for out_leg, in_leg in product(outbound_legs, inbound_legs):
        stay = (in_leg.departure.date() - out_leg.departure.date()).days
        if stay_min <= stay <= stay_max:
            trips.append(RoundTrip(outbound=out_leg, inbound=in_leg))
    return trips


# ============================================================================
# Interactive prompts
# ============================================================================


def prompt_location(label: str) -> tuple[float, float]:
    while True:
        place = input(f"{label} (city/address): ").strip()
        if not place:
            continue
        try:
            lat, lon, display = airports.geocode(place)
        except Exception as e:
            print(f"  [error] {e}. Try again.")
            continue
        print(f"  -> resolved to {display} ({lat:.3f}, {lon:.3f})")
        return lat, lon


def prompt_airport_selection(label: str, lat: float, lon: float) -> list[str]:
    found = airports.nearby_airports(lat, lon, NEARBY_RADIUS_KM)
    if not found:
        raise SystemExit(f"No commercial airports found within {NEARBY_RADIUS_KM} km of {label}.")

    print(f"\nAirports within {NEARBY_RADIUS_KM} km of {label}:")
    for i, a in enumerate(found, 1):
        print(f"  {i:>2}. {a.iata}  {a.name} ({a.municipality}, {a.country}) -- {a.distance_km:.0f} km")

    while True:
        raw = input("Select airports (comma-separated numbers, or 'all'): ").strip().lower()
        if raw == "all":
            return [a.iata for a in found]
        try:
            idxs = [int(x.strip()) for x in raw.split(",") if x.strip()]
            chosen = [found[i - 1].iata for i in idxs]
        except (ValueError, IndexError):
            print("  [error] invalid selection, try again.")
            continue
        if chosen:
            return chosen
        print("  [error] pick at least one.")


def prompt_date(label: str) -> datetime:
    while True:
        raw = input(f"{label} (YYYY-MM-DD): ").strip()
        try:
            return datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            print("  [error] use format YYYY-MM-DD, try again.")


def prompt_date_range(label: str) -> tuple[datetime, datetime]:
    d_from = prompt_date(f"{label} -- earliest date")
    d_to = prompt_date(f"{label} -- latest date")
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    return d_from, d_to


def prompt_stay_range() -> tuple[int, int]:
    while True:
        try:
            lo = int(input("Minimum length of stay (days): ").strip())
            hi = int(input("Maximum length of stay (days): ").strip())
        except ValueError:
            print("  [error] enter whole numbers, try again.")
            continue
        if lo > hi:
            lo, hi = hi, lo
        return lo, hi


# ============================================================================
# Main
# ============================================================================


def main():
    start_lat, start_lon = prompt_location("Start location")
    origin_airports = prompt_airport_selection("start", start_lat, start_lon)

    dest_lat, dest_lon = prompt_location("Destination location")
    dest_airports = prompt_airport_selection("destination", dest_lat, dest_lon)

    outbound_from, outbound_to = prompt_date_range("Outbound flight window")
    return_from, return_to = prompt_date_range("Return flight window")
    stay_min, stay_max = prompt_stay_range()

    print(
        f"\nSearching {len(origin_airports)} origin(s) x {len(dest_airports)} destination(s), "
        f"outbound {outbound_from:%Y-%m-%d}-{outbound_to:%Y-%m-%d}, "
        f"return {return_from:%Y-%m-%d}-{return_to:%Y-%m-%d}, "
        f"stay {stay_min}-{stay_max} days...\n"
    )

    rates = get_eur_rates()

    # Search outbound and inbound legs independently across every airport
    # combo, so an open-jaw trip (e.g. fly into OTP, home from CLJ) can win
    # over a same-airport round trip if it's cheaper.
    outbound_legs: list[Leg] = []
    for origin, destination in product(origin_airports, dest_airports):
        print(f"Outbound: {origin} -> {destination}")
        outbound_legs += search_legs(origin, destination, outbound_from, outbound_to, rates)

    inbound_legs: list[Leg] = []
    for destination, origin in product(dest_airports, origin_airports):
        print(f"Return:   {destination} -> {origin}")
        inbound_legs += search_legs(destination, origin, return_from, return_to, rates)

    all_trips = build_round_trips(outbound_legs, inbound_legs, stay_min, stay_max)

    if not all_trips:
        print("\nNo round trips found. Try widening the date windows, the stay interval, "
              "raising MAX_LEG_PRICE_EUR, or picking more airports.")
        return

    all_trips.sort(key=lambda t: t.total_price_eur)
    top_trips = all_trips[:TOP_N]

    def price_str(leg: Leg) -> str:
        extra = f" ({leg.price_original:.0f} {leg.currency})" if leg.currency != "EUR" else ""
        return f"{leg.price_eur:.0f} EUR{extra}"

    print(f"\n{'='*100}\nTOP {TOP_N} CHEAPEST ROUND TRIPS\n{'='*100}")
    header = f"{'Route':<20}{'Out':<18}{'In':<18}{'Stay':<6}{'Airlines':<20}{'Out Price':<18}{'In Price':<18}{'Total':<10}"
    print(header)
    for t in top_trips:
        route = f"{t.outbound.origin}->{t.outbound.destination} / {t.inbound.origin}->{t.inbound.destination}"
        airlines = f"{t.outbound.airline}/{t.inbound.airline}"
        print(
            f"{route:<20}"
            f"{t.outbound.departure:%Y-%m-%d %H:%M}  "
            f"{t.inbound.departure:%Y-%m-%d %H:%M}  "
            f"{t.stay_days:<6}"
            f"{airlines:<20}"
            f"{price_str(t.outbound):<18}"
            f"{price_str(t.inbound):<18}"
            f"{t.total_price_eur:.0f} EUR"
        )

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "stay_days",
            "out_origin", "out_destination", "out_airline", "out_flight_number", "out_departure",
            "out_price_eur", "out_price_original", "out_currency",
            "in_origin", "in_destination", "in_airline", "in_flight_number", "in_departure",
            "in_price_eur", "in_price_original", "in_currency",
            "total_price_eur",
        ])
        for t in all_trips:
            writer.writerow([
                t.stay_days,
                t.outbound.origin, t.outbound.destination, t.outbound.airline, t.outbound.flight_number,
                t.outbound.departure.isoformat(), f"{t.outbound.price_eur:.2f}", t.outbound.price_original, t.outbound.currency,
                t.inbound.origin, t.inbound.destination, t.inbound.airline, t.inbound.flight_number,
                t.inbound.departure.isoformat(), f"{t.inbound.price_eur:.2f}", t.inbound.price_original, t.inbound.currency,
                f"{t.total_price_eur:.2f}",
            ])

    print(f"\nFull results ({len(all_trips)} round trips) saved to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
