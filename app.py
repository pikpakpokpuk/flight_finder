#!/usr/bin/env python3
"""
Flight Finder -- Streamlit UI

USAGE:
    streamlit run app.py
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st
from streamlit_searchbox import st_searchbox

import airports
import flight_finder as ff

st.set_page_config(page_title="Flight Finder", page_icon="✈️", layout="wide")
st.title("✈️ Flight Finder")
st.caption("Outbound and return flights found and ranked independently across every nearby airport.")


def search_cities(query: str):
    """search_function for st_searchbox: (label, value) pairs, Europe only."""
    try:
        results = airports.geocode_suggestions(query)
    except Exception:
        return []
    return [(display, (lat, lon, display)) for lat, lon, display in results]


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
    start_selection = st_searchbox(
        search_cities, key="start_searchbox", placeholder="Start location (city, Europe only)"
    )
with col2:
    dest_selection = st_searchbox(
        search_cities, key="dest_searchbox", placeholder="Destination location (city, Europe only)"
    )

radius_km = st.slider("Search radius for nearby airports (km)", 50, 500, 200, step=10)

if st.button("Find nearby airports"):
    if not start_selection or not dest_selection:
        st.error("Pick a start and a destination city from the suggestions first.")
    else:
        with st.spinner("Looking up locations and airline networks..."):
            s_lat, s_lon, s_disp = start_selection
            d_lat, d_lon, d_disp = dest_selection
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

    c5, c6 = st.columns(2)
    with c5:
        max_price = st.number_input("Max price per leg (EUR, 0 = no limit)", min_value=0, value=300, step=10)
    with c6:
        top_n = st.number_input("Number of results to show", min_value=1, value=20, step=1)

    allow_one_stop = st.checkbox(
        "Include 1-stop (self-connect) options -- slower, many more searches",
        value=True,
        help=(
            "Books two separate one-way tickets that connect through a third airport. "
            "Ryanair/Wizz don't interline: no protection if leg 1 is delayed, no baggage "
            "transfer. Only kept if the layover is 2.5-8h, same calendar day, and the "
            "total is no more than the cheapest direct flight that day."
        ),
    )

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
            route_graph = ff.build_route_graph(cached_networks())

            status = st.empty()

            def report(msg: str) -> None:
                status.write(msg)

            outbound_journeys = ff.build_journeys(
                origin_codes, dest_codes, outbound_from, outbound_to, rates,
                route_graph, allow_one_stop, "Outbound", on_step=report,
            )
            inbound_journeys = ff.build_journeys(
                dest_codes, origin_codes, return_from, return_to, rates,
                route_graph, allow_one_stop, "Return", on_step=report,
            )
            status.empty()

            outbound_journeys.sort(key=lambda j: j.price_eur)
            inbound_journeys.sort(key=lambda j: j.price_eur)

            if not outbound_journeys and not inbound_journeys:
                st.warning(
                    "No flights found. Try widening the date windows, raising the price cap, "
                    "or picking more airports."
                )

            def leg_detail(l: ff.Leg) -> str:
                extra = f" ({l.price_original:.0f} {l.currency})" if l.currency != "EUR" else ""
                return f"{l.airline} {l.origin}→{l.destination} {l.departure:%Y-%m-%d %H:%M} {l.price_eur:.0f} EUR{extra}"

            def journey_row(j: ff.Journey) -> dict:
                return {
                    "Route": j.route_str,
                    "Departure": j.departure.strftime("%Y-%m-%d %H:%M"),
                    "Notes": j.note,
                    "Detail": " | ".join(leg_detail(l) for l in j.legs),
                    "Price (EUR)": round(j.price_eur),
                }

            def show_results(title: str, journeys: list[ff.Journey], file_name: str) -> None:
                st.subheader(title)
                if not journeys:
                    st.write("No flights found for this direction.")
                    return
                top = journeys[:top_n]
                st.dataframe(pd.DataFrame(journey_row(j) for j in top), use_container_width=True, hide_index=True)
                full_df = pd.DataFrame(journey_row(j) for j in journeys)
                st.download_button(
                    f"Download all {len(journeys)} results as CSV",
                    full_df.to_csv(index=False),
                    file_name=file_name,
                    mime="text/csv",
                    key=file_name,
                )

            col_out, col_in = st.columns(2)
            with col_out:
                show_results(f"Top {min(top_n, len(outbound_journeys))} cheapest outbound flights", outbound_journeys, "outbound_flights.csv")
            with col_in:
                show_results(f"Top {min(top_n, len(inbound_journeys))} cheapest return flights", inbound_journeys, "inbound_flights.csv")
