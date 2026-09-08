"""
Airport lookup: geocode a place name and find nearby commercial airports.

Data sources (both free, no API key):
  - Open-Meteo Geocoding API -- turns a place name into lat/lon.
  - OurAirports CSV dump     -- full list of the world's airports, with
    coordinates, type (large/medium/small), and IATA codes. Downloaded
    once and cached locally.
"""

from __future__ import annotations

import csv
import math
import os
from dataclasses import dataclass

import requests

AIRPORTS_CSV_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "airports_cache.csv")

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

# Only these count as "commercial enough to bother with".
COMMERCIAL_TYPES = {"large_airport", "medium_airport"}


@dataclass
class Airport:
    iata: str
    name: str
    municipality: str
    country: str
    lat: float
    lon: float
    distance_km: float = 0.0


def geocode(place: str) -> tuple[float, float, str]:
    """Look up a place name, return (lat, lon, display_name)."""
    resp = requests.get(GEOCODE_URL, params={"name": place, "count": 1, "language": "en"}, timeout=10)
    resp.raise_for_status()
    results = resp.json().get("results")
    if not results:
        raise ValueError(f"couldn't find a location for {place!r}")
    r = results[0]
    display = ", ".join(x for x in [r.get("name"), r.get("admin1"), r.get("country")] if x)
    return r["latitude"], r["longitude"], display


def _ensure_airports_csv() -> str:
    if not os.path.exists(CACHE_PATH):
        print("Downloading airport database (one-time, ~7 MB)...")
        resp = requests.get(AIRPORTS_CSV_URL, timeout=30)
        resp.raise_for_status()
        with open(CACHE_PATH, "wb") as f:
            f.write(resp.content)
    return CACHE_PATH


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearby_airports(lat: float, lon: float, radius_km: float = 300) -> list[Airport]:
    """Commercial airports with an IATA code within radius_km of (lat, lon), nearest first."""
    path = _ensure_airports_csv()
    found = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["type"] not in COMMERCIAL_TYPES:
                continue
            iata = row.get("iata_code", "").strip()
            if not iata:
                continue
            try:
                a_lat, a_lon = float(row["latitude_deg"]), float(row["longitude_deg"])
            except (TypeError, ValueError):
                continue
            dist = _haversine_km(lat, lon, a_lat, a_lon)
            if dist <= radius_km:
                found.append(Airport(
                    iata=iata,
                    name=row["name"],
                    municipality=row.get("municipality", ""),
                    country=row.get("iso_country", ""),
                    lat=a_lat,
                    lon=a_lon,
                    distance_km=dist,
                ))
    found.sort(key=lambda a: a.distance_km)
    return found
