#!/usr/bin/env python3
"""
Flight Finder -- Streamlit UI

USAGE:
    streamlit run app.py
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from itertools import product

import pandas as pd
import streamlit as st

import airports
import flight_finder as ff

st.set_page_config(page_title="Flight Finder", page_icon="✈️", layout="wide")
st.title("✈️ Flight Finder")
st.caption("Round-trip search across every nearby airport, open-jaw included.")


@st.cache_data(show_spinner=False)
def cached_geocode(place: str):
    return airports.geocode(place)


@st.cache_data(show_spinner=False)
def cached_nearby(lat: float, lon: float, radius_km: float):
    return airports.nearby_airports(lat, lon, radius_km)


@st.cache_data(show_spinner=False, ttl=3600)
def cached_rates():
    return ff.get_eur_rates()


@st.cache_data(show_spinner=False, ttl=3600)
def cached_networks():
    return ff.fetch_networks()


def fmt_airport(a: airports.Airport) -> str:
    return f"{a.iata} — {a.name} ({a.distance_km:.0f} km)"


# ---------------------------------------------------------------------------
# Step 1: locations
# ---------------------------------------------------------------------------

col1, col2 = st.columns(2)
with col1:
    start_place = st.text_input("Start location", "Copenhagen")
with col2:
    dest_place = st.text_input("Destination location", "Bucharest")

radius_km = st.slider("Search radius for nearby airports (km)", 50, 500, 200, step=10)

if st.button("Find nearby airports"):
    with st.spinner("Looking up locations and airline networks..."):
        try:
            s_lat, s_lon, s_disp = cached_geocode(start_place)
            d_lat, d_lon, d_disp = cached_geocode(dest_place)
        except Exception as e:
            st.error(str(e))
        else:
            networks = cached_networks()
            served = ff.get_served_airports(networks)
            st.session_state.start_disp = s_disp
            st.session_state.dest_disp = d_disp
            origin_nearby = cached_nearby(s_lat, s_lon, radius_km)
            dest_nearby = cached_nearby(d_lat, d_lon, radius_km)
            if served:
                origin_nearby = [a for a in origin_nearby if a.iata in served]
                dest_nearby = [a for a in dest_nearby if a.iata in served]
            st.session_state.origin_airports = origin_nearby
            st.session_state.dest_airports = dest_nearby

# ---------------------------------------------------------------------------
# Step 2: pick airports, dates, run search
# ---------------------------------------------------------------------------

if st.session_state.get("origin_airports"):
    st.success(f"Start: {st.session_state.start_disp}  |  Destination: {st.session_state.dest_disp}")

    if not st.session_state.origin_airports:
        st.warning("No commercial airports found near the start location within this radius.")
    if not st.session_state.dest_airports:
        st.warning("No commercial airports found near the destination within this radius.")

    def airport_checklist(label: str, airport_list: list[airports.Airport], key_prefix: str) -> list[airports.Airport]:
        st.markdown(f"**{label}**")
        box = st.container(height=250, border=True)
        chosen = []
        for a in airport_list:
            checked = box.checkbox(fmt_airport(a), value=True, key=f"{key_prefix}_{a.iata}")
            if checked:
                chosen.append(a)
        return chosen

    c1, c2 = st.columns(2)
    with c1:
        origin_choices = airport_checklist("Origin airports", st.session_state.origin_airports, "orig")
    with c2:
        dest_choices = airport_checklist("Destination airports", st.session_state.dest_airports, "dest")

    st.subheader("Dates")
    c3, c4 = st.columns(2)
    with c3:
        outbound_range = st.date_input(
            "Outbound window",
            value=(date.today() + timedelta(days=30), date.today() + timedelta(days=60)),
        )
    with c4:
        return_range = st.date_input(
            "Return window",
            value=(date.today() + timedelta(days=35), date.today() + timedelta(days=65)),
        )

    stay_min, stay_max = st.slider("Length of stay (days)", 1, 30, (5, 8))

    c5, c6 = st.columns(2)
    with c5:
        max_price = st.number_input("Max price per leg (EUR, 0 = no limit)", min_value=0, value=300, step=10)
    with c6:
        top_n = st.number_input("Number of results to show", min_value=1, value=20, step=1)

    search_clicked = st.button("Search flights", type="primary")

    if search_clicked:
        if not origin_choices or not dest_choices:
            st.error("Pick at least one origin and one destination airport.")
        elif len(outbound_range) != 2 or len(return_range) != 2:
            st.error("Pick a full start and end date for both windows.")
        else:
            ff.MAX_LEG_PRICE_EUR = max_price if max_price > 0 else None

            outbound_from, outbound_to = (datetime.combine(d, datetime.min.time()) for d in outbound_range)
            return_from, return_to = (datetime.combine(d, datetime.min.time()) for d in return_range)

            origin_codes = [a.iata for a in origin_choices]
            dest_codes = [a.iata for a in dest_choices]

            rates = cached_rates()

            out_pairs = list(product(origin_codes, dest_codes))
            in_pairs = list(product(dest_codes, origin_codes))
            total_steps = len(out_pairs) + len(in_pairs)

            progress = st.progress(0.0)
            step = 0
            outbound_legs: list[ff.Leg] = []
            for o, d in out_pairs:
                progress.progress(step / total_steps, text=f"Searching outbound {o} → {d}")
                outbound_legs += ff.search_legs(o, d, outbound_from, outbound_to, rates)
                step += 1

            inbound_legs: list[ff.Leg] = []
            for d, o in in_pairs:
                progress.progress(step / total_steps, text=f"Searching return {d} → {o}")
                inbound_legs += ff.search_legs(d, o, return_from, return_to, rates)
                step += 1
            progress.empty()

            trips = ff.build_round_trips(outbound_legs, inbound_legs, stay_min, stay_max)

            if not trips:
                st.warning(
                    "No round trips found for this combination. Try widening the date windows, "
                    "the stay range, raising the price cap, or picking more airports."
                )
            else:
                trips.sort(key=lambda t: t.total_price_eur)
                top_trips = trips[:top_n]

                def trip_row(t: ff.RoundTrip) -> dict:
                    return {
                        "Out route": f"{t.outbound.origin}→{t.outbound.destination}",
                        "Out date": t.outbound.departure.strftime("%Y-%m-%d %H:%M"),
                        "Out airline": t.outbound.airline,
                        "Out price (EUR)": round(t.outbound.price_eur),
                        "In route": f"{t.inbound.origin}→{t.inbound.destination}",
                        "In date": t.inbound.departure.strftime("%Y-%m-%d %H:%M"),
                        "In airline": t.inbound.airline,
                        "In price (EUR)": round(t.inbound.price_eur),
                        "Stay (days)": t.stay_days,
                        "Total (EUR)": round(t.total_price_eur),
                    }

                st.subheader(f"Top {len(top_trips)} cheapest round trips")
                st.dataframe(pd.DataFrame(trip_row(t) for t in top_trips), use_container_width=True, hide_index=True)

                full_df = pd.DataFrame(trip_row(t) for t in trips)
                st.download_button(
                    f"Download all {len(trips)} results as CSV",
                    full_df.to_csv(index=False),
                    file_name="flight_options.csv",
                    mime="text/csv",
                )
