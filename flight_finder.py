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
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import product
from typing import Callable, Optional

import requests

# Windows consoles default to a codepage (e.g. cp1252) that can't print
# airport names with diacritics (Bucharest's "Henri Coandă", "Malmö", ...).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

from flyan import RyanAir, FlightSearchParams
from flyan.misc import Network as RyanairNetwork
from flywizz import WizzAir, TimetableSearch
from flywizz.misc import Network as WizzNetwork

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

# --- 1-stop (self-connect) search --------------------------------------
# Ryanair/Wizz don't interline: a "1-stop" here means booking two separate
# one-way tickets that happen to connect. No missed-connection protection,
# no baggage transfer -- you clear the terminal and re-check in.

ALLOW_ONE_STOP = True

# Minimum gap between leg-1 arrival and leg-2 departure. Below this there's
# no realistic chance of making the connection self-connecting.
MIN_LAYOVER_HOURS = 2.5

# Maximum gap before it's just a very long, unpleasant wait.
MAX_LAYOVER_HOURS = 8.0

# A 1-stop must land the same calendar day it takes off for leg 2 -- no
# overnight layovers.
#
# Assumed average block speed (cruise + taxi/climb/descent overhead) used to
# estimate a leg's arrival time when the airline's API doesn't return one
# (Wizz Air's timetable endpoint has no arrival time or flight duration).
# Ryanair's API does return a real arrival time, so this is only a fallback.
ESTIMATED_CRUISE_KMH = 750.0
ESTIMATED_OVERHEAD_HOURS = 0.75

# Pause between stopover candidates so a wide search (many stopovers) doesn't
# fire a burst of requests large enough to trip Wizz Air's rate limiting --
# seen in testing to 503 even unrelated later requests (e.g. the return
# direction's own direct-flight search) for a while afterwards.
ONE_STOP_THROTTLE_SECONDS = 0.4

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
    arrival: Optional[datetime] = None
    # True if `arrival` is a distance-based estimate rather than data the
    # airline's API actually returned (see ESTIMATED_CRUISE_KMH above).
    arrival_estimated: bool = False


@dataclass
class Journey:
    """One or two Legs from an ultimate origin to an ultimate destination.

    Two legs means a self-connect through the middle airport -- no
    interlining, so the layover was already checked against
    MIN/MAX_LAYOVER_HOURS and the same-day rule when this was built.
    """

    legs: list[Leg] = field(default_factory=list)

    @property
    def origin(self) -> str:
        return self.legs[0].origin

    @property
    def destination(self) -> str:
        return self.legs[-1].destination

    @property
    def departure(self) -> datetime:
        return self.legs[0].departure

    @property
    def arrival(self) -> Optional[datetime]:
        return self.legs[-1].arrival

    @property
    def price_eur(self) -> float:
        return sum(l.price_eur for l in self.legs)

    @property
    def is_direct(self) -> bool:
        return len(self.legs) == 1

    @property
    def stop_codes(self) -> list[str]:
        return [l.destination for l in self.legs[:-1]]

    @property
    def route_str(self) -> str:
        codes = [self.legs[0].origin] + [l.destination for l in self.legs]
        return "->".join(codes)

    @property
    def airlines_str(self) -> str:
        seen: list[str] = []
        for l in self.legs:
            if l.airline not in seen:
                seen.append(l.airline)
        return "+".join(seen)

    @property
    def layover(self) -> Optional[timedelta]:
        # Journeys with 2 legs are only ever built with a known arrival time
        # for leg 1 (see build_journeys), so this is safe despite the field
        # being Optional on Leg in general.
        if self.is_direct or self.legs[0].arrival is None:
            return None
        return self.legs[1].departure - self.legs[0].arrival

    @property
    def has_estimated_time(self) -> bool:
        return any(l.arrival_estimated for l in self.legs)

    @property
    def note(self) -> str:
        if self.is_direct:
            return "direct"
        hours = self.layover.total_seconds() / 3600  # type: ignore[union-attr]
        est = ", est. layover" if self.has_estimated_time else ""
        return f"via {self.stop_codes[0]} ({hours:.1f}h{est})"


@dataclass
class RoundTrip:
    outbound: Journey
    inbound: Journey

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


Networks = tuple[Optional[RyanairNetwork], Optional[WizzNetwork]]


def fetch_networks() -> Networks:
    """Fetch Ryanair's and Wizz Air's live route networks once.

    Returns (ryanair_network, wizz_network); either is None if that airline's
    network couldn't be fetched.
    """
    ryanair_net: Optional[RyanairNetwork] = None
    wizz_net: Optional[WizzNetwork] = None
    try:
        ryanair_net = RyanAir().get_network()
    except Exception as e:
        print(f"  [warning] couldn't fetch Ryanair's network ({e}); Ryanair routes/airports may be missed")
    try:
        wizz_net = WizzAir().get_network()
    except Exception as e:
        print(f"  [warning] couldn't fetch Wizz Air's network ({e}); Wizz routes/airports may be missed")
    return ryanair_net, wizz_net


def get_served_airports(networks: Networks) -> set[str]:
    """IATA codes of every airport Ryanair or Wizz Air currently flies to."""
    ryanair_net, wizz_net = networks
    codes: set[str] = set()
    if ryanair_net is not None:
        codes |= {a.iata_code for a in ryanair_net.airports}
    if wizz_net is not None:
        codes |= {s.iata for s in wizz_net.stations}
    return codes


def build_route_graph(networks: Networks) -> dict[str, set[str]]:
    """Adjacency map: iata -> set of iata codes with a direct Ryanair/Wizz route.

    Routes are treated as symmetric (X->Y implies Y->X), which holds for
    almost all Ryanair/Wizz routes.
    """
    ryanair_net, wizz_net = networks
    adj: dict[str, set[str]] = {}
    if ryanair_net is not None:
        for a in ryanair_net.airports:
            adj.setdefault(a.iata_code, set()).update(a.airport_routes())
    if wizz_net is not None:
        for s in wizz_net.stations:
            adj.setdefault(s.iata, set()).update(s.destinations())
    return adj


def find_stopovers(route_graph: dict[str, set[str]], origin: str, destination: str) -> set[str]:
    """Airports with a direct route from origin AND a direct route to destination."""
    return route_graph.get(origin, set()) & route_graph.get(destination, set())


def estimate_flight_duration(origin: str, destination: str) -> Optional[timedelta]:
    """Rough block-time estimate from great-circle distance.

    Only used as a fallback when the airline's API doesn't return an arrival
    time -- Wizz Air's timetable endpoint has none. Ryanair's does, and that
    real value is always preferred over this estimate.
    """
    index = airports.all_airports()
    o, d = index.get(origin), index.get(destination)
    if o is None or d is None:
        return None
    distance_km = airports.haversine_km(o.lat, o.lon, d.lat, d.lon)
    return timedelta(hours=distance_km / ESTIMATED_CRUISE_KMH + ESTIMATED_OVERHEAD_HOURS)


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
            arrival=f.arrival_date,
            arrival_estimated=False,
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

    duration = estimate_flight_duration(origin, destination)

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
            arrival=(entry.departure_date + duration) if duration else None,
            arrival_estimated=duration is not None,
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


def build_journeys(
    origin_codes: list[str],
    dest_codes: list[str],
    date_from: datetime,
    date_to: datetime,
    rates: dict,
    route_graph: dict[str, set[str]],
    allow_one_stop: bool,
    label: str,
    on_step: Optional[Callable[[str], None]] = None,
) -> list[Journey]:
    """Direct + (optionally) 1-stop journeys for every origin/dest combo.

    `on_step`, if given, is called with a short status string before each
    search -- lets a caller (e.g. a Streamlit progress bar) show liveness
    beyond the console prints this also does.

    A 1-stop candidate is kept only if: same-day layover, layover within
    [MIN_LAYOVER_HOURS, MAX_LAYOVER_HOURS], and its total price beats the
    cheapest direct flight on that same specific departure date (if one
    exists that day -- if there's no direct that day, the 1-stop is kept
    regardless, since there's nothing to compare it against).
    """
    leg_cache: dict[tuple[str, str, datetime, datetime], list[Leg]] = {}

    def cached_legs(o: str, d: str, df: datetime, dt: datetime, throttle: bool = False) -> list[Leg]:
        key = (o, d, df, dt)
        if key not in leg_cache:
            if throttle:
                # Space out stopover requests -- a wide search can otherwise
                # fire dozens of rapid requests and trip Wizz Air's rate
                # limiting for a while afterwards, even for unrelated calls.
                time.sleep(ONE_STOP_THROTTLE_SECONDS)
            leg_cache[key] = search_legs(o, d, df, dt, rates)
        return leg_cache[key]

    journeys: list[Journey] = []
    direct_price_by_date: dict[tuple[str, str], dict] = {}

    for origin, destination in product(origin_codes, dest_codes):
        msg = f"{label}: {origin} -> {destination}"
        print(msg)
        if on_step:
            on_step(msg)
        for leg in cached_legs(origin, destination, date_from, date_to):
            journeys.append(Journey(legs=[leg]))
            by_date = direct_price_by_date.setdefault((origin, destination), {})
            d = leg.departure.date()
            if d not in by_date or leg.price_eur < by_date[d]:
                by_date[d] = leg.price_eur

    if not allow_one_stop:
        return journeys

    for origin, destination in product(origin_codes, dest_codes):
        stopovers = find_stopovers(route_graph, origin, destination) - {origin, destination}
        for stop in stopovers:
            msg = f"{label} via {stop}: {origin} -> {stop} -> {destination}"
            print(msg)
            if on_step:
                on_step(msg)
            leg1_list = cached_legs(origin, stop, date_from, date_to, throttle=True)
            if not leg1_list:
                continue
            # Leg 2 can depart up to a day after the window closes, if leg 1
            # departs right at the edge of the window and arrives the next day.
            leg2_list = cached_legs(stop, destination, date_from, date_to + timedelta(days=1), throttle=True)
            if not leg2_list:
                continue

            direct_by_date = direct_price_by_date.get((origin, destination), {})
            for l1, l2 in product(leg1_list, leg2_list):
                if l1.arrival is None:
                    continue
                layover = l2.departure - l1.arrival
                layover_hours = layover.total_seconds() / 3600
                if not (MIN_LAYOVER_HOURS <= layover_hours <= MAX_LAYOVER_HOURS):
                    continue
                if l1.arrival.date() != l2.departure.date():
                    continue

                total_price = l1.price_eur + l2.price_eur
                direct_price = direct_by_date.get(l1.departure.date())
                if direct_price is not None and total_price > direct_price:
                    continue  # no cheaper than just flying direct that day

                journeys.append(Journey(legs=[l1, l2]))

    return journeys


def build_round_trips(outbound_journeys: list[Journey], inbound_journeys: list[Journey], stay_min: int, stay_max: int) -> list[RoundTrip]:
    trips = []
    for out_j, in_j in product(outbound_journeys, inbound_journeys):
        stay = (in_j.departure.date() - out_j.departure.date()).days
        if stay_min <= stay <= stay_max:
            trips.append(RoundTrip(outbound=out_j, inbound=in_j))
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


def prompt_airport_selection(label: str, lat: float, lon: float, served: set[str]) -> list[str]:
    found = airports.nearby_airports(lat, lon, NEARBY_RADIUS_KM)
    if served:
        found = [a for a in found if a.iata in served]
    if not found:
        raise SystemExit(
            f"No airport within {NEARBY_RADIUS_KM} km of {label} is served by Ryanair or Wizz Air."
        )

    print(f"\nRyanair/Wizz Air airports within {NEARBY_RADIUS_KM} km of {label}:")
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


def prompt_yes_no(label: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    raw = input(f"{label} ({hint}): ").strip().lower()
    if not raw:
        return default
    return raw.startswith("y")


# ============================================================================
# Main
# ============================================================================


def main():
    print("Fetching Ryanair and Wizz Air's current route networks...")
    networks = fetch_networks()
    served = get_served_airports(networks)
    route_graph = build_route_graph(networks)

    start_lat, start_lon = prompt_location("Start location")
    origin_airports = prompt_airport_selection("start", start_lat, start_lon, served)

    dest_lat, dest_lon = prompt_location("Destination location")
    dest_airports = prompt_airport_selection("destination", dest_lat, dest_lon, served)

    outbound_from, outbound_to = prompt_date_range("Outbound flight window")
    return_from, return_to = prompt_date_range("Return flight window")
    stay_min, stay_max = prompt_stay_range()
    allow_one_stop = prompt_yes_no(
        "Include 1-stop (self-connect) options? Slower -- many more searches", ALLOW_ONE_STOP
    )

    print(
        f"\nSearching {len(origin_airports)} origin(s) x {len(dest_airports)} destination(s), "
        f"outbound {outbound_from:%Y-%m-%d}-{outbound_to:%Y-%m-%d}, "
        f"return {return_from:%Y-%m-%d}-{return_to:%Y-%m-%d}, "
        f"stay {stay_min}-{stay_max} days, 1-stop {'on' if allow_one_stop else 'off'}...\n"
    )

    rates = get_eur_rates()

    # Search outbound and inbound independently across every airport combo,
    # so an open-jaw trip (e.g. fly into OTP, home from CLJ) can win over a
    # same-airport round trip if it's cheaper.
    outbound_journeys = build_journeys(
        origin_airports, dest_airports, outbound_from, outbound_to, rates, route_graph, allow_one_stop, "Outbound"
    )
    inbound_journeys = build_journeys(
        dest_airports, origin_airports, return_from, return_to, rates, route_graph, allow_one_stop, "Return"
    )

    all_trips = build_round_trips(outbound_journeys, inbound_journeys, stay_min, stay_max)

    if not all_trips:
        print("\nNo round trips found. Try widening the date windows, the stay interval, "
              "raising MAX_LEG_PRICE_EUR, or picking more airports.")
        return

    all_trips.sort(key=lambda t: t.total_price_eur)
    top_trips = all_trips[:TOP_N]

    def leg_detail(leg: Leg) -> str:
        extra = f" ({leg.price_original:.0f} {leg.currency})" if leg.currency != "EUR" else ""
        return f"{leg.airline} {leg.origin}->{leg.destination} {leg.departure:%Y-%m-%d %H:%M} {leg.price_eur:.0f} EUR{extra}"

    print(f"\n{'='*100}\nTOP {TOP_N} CHEAPEST ROUND TRIPS\n{'='*100}")
    header = f"{'Route':<24}{'Out':<18}{'In':<18}{'Stay':<6}{'Out Price':<12}{'In Price':<12}{'Total':<10}"
    print(header)
    for t in top_trips:
        route = f"{t.outbound.route_str} / {t.inbound.route_str}"
        print(
            f"{route:<24}"
            f"{t.outbound.departure:%Y-%m-%d %H:%M}  "
            f"{t.inbound.departure:%Y-%m-%d %H:%M}  "
            f"{t.stay_days:<6}"
            f"{t.outbound.price_eur:<11.0f} "
            f"{t.inbound.price_eur:<11.0f} "
            f"{t.total_price_eur:.0f} EUR"
        )
        print(f"    out: {t.outbound.note} -- " + " | ".join(leg_detail(l) for l in t.outbound.legs))
        print(f"    in:  {t.inbound.note} -- " + " | ".join(leg_detail(l) for l in t.inbound.legs))

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "stay_days",
            "out_route", "out_departure", "out_price_eur", "out_notes", "out_detail",
            "in_route", "in_departure", "in_price_eur", "in_notes", "in_detail",
            "total_price_eur",
        ])
        for t in all_trips:
            writer.writerow([
                t.stay_days,
                t.outbound.route_str, t.outbound.departure.isoformat(), f"{t.outbound.price_eur:.2f}",
                t.outbound.note, " | ".join(leg_detail(l) for l in t.outbound.legs),
                t.inbound.route_str, t.inbound.departure.isoformat(), f"{t.inbound.price_eur:.2f}",
                t.inbound.note, " | ".join(leg_detail(l) for l in t.inbound.legs),
                f"{t.total_price_eur:.2f}",
            ])

    print(f"\nFull results ({len(all_trips)} round trips) saved to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
