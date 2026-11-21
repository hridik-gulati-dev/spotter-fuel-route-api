"""Core services: geocoding, routing and fuel-stop optimisation.

External services (both free, no API key, no account):
- Nominatim (OpenStreetMap) for geocoding place names to coordinates.
- OSRM public demo server for driving routes.

Per uncached request the normal path makes 4 external calls: 2
geocoding (start, finish) + 2 calls to the map/routing API (direct
route, then one waypoint route through every selected stop). The
assessment budgets calls to the map/routing API ("one call ... is
ideal, two or three is acceptable"); 2 routing calls sit inside that
budget. The rare range retry adds at most one more routing call
(bounded: never more than 3). Results are
cached in Django's cache keyed by the normalised (start, finish) pair,
so repeat requests are served instantly without any external call.

Planning happens in two phases:
1. SELECTION: only stations whose coordinates were matched at station
   level (coord_source != city_centroid) are eligible; a cost-effective
   set of stops is chosen on the route line with a planning range below
   the vehicle's real 500-mile range, leaving reserve for detours.
2. VERIFICATION + ACCOUNTING: one waypoint route call through ALL
   selected stops yields the actual driven legs (detour roads
   included). Gallons and dollars are recomputed on those driven legs
   and every leg is range-checked. If a leg exceeds the vehicle range
   the selection is retried once with a smaller planning range; if it
   still fails, the API answers 422 with an explicit no-plan error
   rather than a plan built on approximations.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from dataclasses import dataclass, field, replace

import requests
from django.conf import settings
from django.core.cache import cache

EARTH_RADIUS_MILES = 3958.7613


class ExternalServiceError(Exception):
    """Raised when geocoding or routing fails."""


class RouteInfeasibleError(Exception):
    """Raised when no sequence of fuel stops can cover the route."""


# ---------------------------------------------------------------- geo helpers

def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles between two lat/lon points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def decode_polyline(polyline: str, precision: int = 5) -> list[tuple[float, float]]:
    """Decode a Google/OSRM encoded polyline into [(lat, lon), ...]."""
    points: list[tuple[float, float]] = []
    index, lat, lon = 0, 0, 0
    factor = 10 ** precision
    while index < len(polyline):
        for coord in ("lat", "lon"):
            result, shift = 0, 0
            while True:
                b = ord(polyline[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if coord == "lat":
                lat += delta
            else:
                lon += delta
        points.append((lat / factor, lon / factor))
    return points


# ---------------------------------------------------------------- externals

_NOMINATIM_LOCK = threading.Lock()
_nominatim_last_start = 0.0


def _nominatim_throttle() -> None:
    """Nominatim usage policy: at most one request per second. This spaces
    the requests of ONE process; several worker processes would each need
    their own limit or a shared one (documented limitation)."""
    global _nominatim_last_start
    with _NOMINATIM_LOCK:
        wait = settings.NOMINATIM_MIN_INTERVAL_SECONDS - (
            time.monotonic() - _nominatim_last_start)
        if wait > 0:
            time.sleep(wait)
        _nominatim_last_start = time.monotonic()


def geocode(place: str) -> tuple[float, float, str]:
    """Geocode a US place name with Nominatim. Returns (lat, lon, label).

    Follows the public Nominatim policy: an identifying User-Agent, at most
    one request per second per process, and results cached (the shared
    cache, a day) so repeats do not hit the service again."""
    cache_key = "geocode:v1:" + hashlib.sha256(
        " ".join(place.lower().split()).encode()).hexdigest()
    hit_cached = cache.get(cache_key)
    if hit_cached is not None:
        return hit_cached
    _nominatim_throttle()
    resp = requests.get(
        settings.NOMINATIM_URL,
        params={
            "q": place,
            "format": "jsonv2",
            "limit": 1,
            "countrycodes": "us",  # assignment: both locations are within the USA
        },
        headers={"User-Agent": settings.NOMINATIM_USER_AGENT},
        timeout=15,
    )
    if resp.status_code != 200:
        raise ExternalServiceError(f"Geocoding failed (HTTP {resp.status_code})")
    results = resp.json()
    if not results:
        raise ExternalServiceError(f"Could not geocode location: {place!r}")
    hit = results[0]
    value = (float(hit["lat"]), float(hit["lon"]), hit.get("display_name", place))
    cache.set(cache_key, value)
    return value


def driving_route(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[str, float]:
    """One OSRM call. Returns (encoded polyline, distance in miles)."""
    url = settings.OSRM_URL.format(lon1=lon1, lat1=lat1, lon2=lon2, lat2=lat2)
    resp = requests.get(
        url,
        params={"overview": "full", "geometries": "polyline", "steps": "false"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise ExternalServiceError(f"Routing failed (HTTP {resp.status_code})")
    payload = resp.json()
    if payload.get("code") != "Ok" or not payload.get("routes"):
        raise ExternalServiceError("No driving route found between the locations")
    route = payload["routes"][0]
    return route["geometry"], route["distance"] / 1609.344  # metres -> miles


def waypoint_route(coords: list[tuple[float, float]]) -> tuple[list[float], str]:
    """One OSRM call through every waypoint in order.

    coords are (lat, lon) in visit order. Returns (leg distances in
    miles, encoded polyline of the full driven geometry). The legs are
    real road legs, including the access roads to each stop, and the
    geometry is the actual path driven through every stop.
    """
    path = ";".join(f"{lon},{lat}" for lat, lon in coords)
    resp = requests.get(
        f"{settings.OSRM_WAYPOINT_URL}/{path}",
        params={"overview": "full", "geometries": "polyline", "steps": "false"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise ExternalServiceError(f"Waypoint routing failed (HTTP {resp.status_code})")
    payload = resp.json()
    if payload.get("code") != "Ok" or not payload.get("routes"):
        raise ExternalServiceError("No driving route found through the selected stops")
    route = payload["routes"][0]
    legs = [leg["distance"] / 1609.344 for leg in route["legs"]]
    return legs, route["geometry"]


# ---------------------------------------------------------------- stations

@dataclass
class Station:
    id: str
    name: str
    address: str
    city: str
    state: str
    price: float
    lat: float
    lon: float
    coord_source: str = "city_centroid"  # how lat/lon was established (see README)
    coord_detail: str = ""  # extra identity evidence, e.g. "unique_brand_city"
    price_variants: tuple = ()  # all raw prices the file lists for this id, file order
    route_mile: float = 0.0  # filled in when projected onto a route
    off_route_miles: float = 0.0  # distance from route polyline to the station


@dataclass
class Purchase:
    station: Station
    route_mile: float
    gallons: float
    cost: float
    fuel_before: float = 0.0  # tank contents on arrival at this stop
    fuel_after: float = 0.0  # tank contents after this purchase
    leg_miles: float = 0.0  # driven miles from the previous point to this stop


# coord_source values whose coordinates were established at station level
# (see README). Only these may be selected as fuel stops; city_centroid
# rows are city-level approximations and are never eligible.
STATION_LEVEL_SOURCES = {
    "osm_exit_brand",
    "osm_brand_near_city",
    "census_address",
    "osm_name_match",
    "osm_exit_unique_fuel",
    "chain_locator",
    "chain_locator_rebrand",
    "chain_locator_cityexit",
    "chain_locator_corroborated",
    "osm_exit_name_poi",
    "osm_brand_near_city_exitconfirmed",
}

# Exit-level positions: the CSV address names an interchange (exit number
# plus highway); the coordinate is the OpenStreetMap junction carrying that
# exit ref on that highway. It is NOT the station's own position. Measured
# against 295 stations with official-locator coordinates: median 0.24 mi,
# p90 1.13 mi, p95 2.4 mi, max 12.6 mi. Eligible as a stop only while
# settings.ALLOW_EXIT_LEVEL_STOPS is true, and always labelled
# position_level="exit" in the response.
EXIT_LEVEL_SOURCES = {"osm_exit_junction"}
if getattr(settings, "ALLOW_EXIT_LEVEL_STOPS", True):
    STATION_LEVEL_SOURCES = STATION_LEVEL_SOURCES | EXIT_LEVEL_SOURCES


def position_level(coord_source: str) -> str:
    return "exit" if coord_source in EXIT_LEVEL_SOURCES else "station"


_STATIONS: list[Station] | None = None
_LOAD_LOCK = threading.Lock()


def select_price(raw: list[float]) -> float:
    """Pick one price from the raw prices listed for a station id.

    Policies (settings.PRICE_SELECTION): mean (default, order-independent,
    uses every listed price equally), median, min, max, first, last.
    first/last depend on file order, which carries no meaning here.
    """
    policy = getattr(settings, "PRICE_SELECTION", "mean")
    vals = list(raw)
    if policy == "min":
        return min(vals)
    if policy == "max":
        return max(vals)
    if policy == "first":
        return vals[0]
    if policy == "last":
        return vals[-1]
    if policy == "median":
        v = sorted(vals)
        n = len(v)
        return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2
    return sum(vals) / len(vals)


def load_stations() -> list[Station]:
    """Load the price file once and keep it in memory. The returned
    Station objects are shared and must be treated as read-only;
    project_stations returns per-request copies."""
    global _STATIONS
    if _STATIONS is None:
        with _LOAD_LOCK:
            if _STATIONS is None:
                _STATIONS = _build_stations()
    return _STATIONS


def _build_stations() -> list[Station]:
    data_path = settings.BASE_DIR / "router" / "data" / "stations_geocoded.json"
    with open(data_path) as fh:
        rows = json.load(fh)
    # One Station per source id. The file lists some ids on several
    # rows (same address/city/state/rack, different price and
    # sometimes a different spelling of the name); all raw prices are
    # kept in file order and ONE price is chosen by an explicit,
    # order-independent policy (settings.PRICE_SELECTION). The file has
    # no dates, so no row is "latest".
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["id"], []).append(row)
    stations: list[Station] = []
    for rid, grp in groups.items():
        row = dict(grp[0])
        raw = list(row.get("price_variants") or [])
        if not raw:
            raw = [g["price"] for g in grp]
        row["price"] = select_price(raw)
        row["price_variants"] = (
            tuple(raw) if len(set(raw)) > 1 else ())
        stations.append(Station(**row))
    return stations


# ---------------------------------------------------------------- optimiser

def _route_mile_markers(points: list[tuple[float, float]]) -> list[float]:
    """Cumulative distance in miles at every route point."""
    markers = [0.0]
    for (la1, lo1), (la2, lo2) in zip(points, points[1:]):
        markers.append(markers[-1] + haversine_miles(la1, lo1, la2, lo2))
    return markers


def project_stations(
    points: list[tuple[float, float]],
    markers: list[float],
    corridor_miles: float,
) -> list[Station]:
    """Attach stations within `corridor_miles` of the route, tagged with
    their approximate mile along the route.

    Pre-filters by a bounding box around the route before doing distance
    math, which keeps the per-request work small (typically a few hundred
    candidate stations instead of all 7.5k).
    """
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    pad = corridor_miles / 69.0  # ~miles per degree latitude
    min_lat, max_lat = min(lats) - pad, max(lats) + pad
    min_lon, max_lon = min(lons) - pad, max(lons) + pad

    # Sample every Nth route point for the projection scan; with full
    # OSRM geometry points are dense (often <100 m apart), so sampling
    # keeps this O(candidates * sampled_points) without losing accuracy
    # at the corridor scale.
    step = max(1, len(points) // 1500)
    sampled = list(range(0, len(points), step))
    if sampled[-1] != len(points) - 1:
        sampled.append(len(points) - 1)

    found: list[Station] = []
    for st in load_stations():
        if st.coord_source not in STATION_LEVEL_SOURCES:
            continue  # only station-level locations may be selected
        if not (min_lat <= st.lat <= max_lat and min_lon <= st.lon <= max_lon):
            continue
        best_mile, best_dist = None, float("inf")
        for i in sampled:
            d = haversine_miles(st.lat, st.lon, points[i][0], points[i][1])
            if d < best_dist:
                best_dist, best_mile = d, markers[i]
        if best_dist <= corridor_miles and best_mile is not None:
            # a per-request copy: the loaded Station objects are shared
            # between concurrent requests and must not be mutated
            found.append(replace(st, route_mile=best_mile,
                                 off_route_miles=best_dist))
    found.sort(key=lambda s: s.route_mile)
    return found


class PlanningBudgetError(ExternalServiceError):
    """Raised when the exact planner exceeds its explicit state budget.

    This is never silently swallowed or worked around with a heuristic:
    the API surfaces it as an explicit error instead of returning a
    plan whose optimality is unverified.
    """


# Hard safety cap on DP states created in one planning call. Normal
# corridors stay a small fraction of this (dominance pruning keeps the
# per-node frontier tiny); the cap exists so a pathological input fails
# loudly instead of exhausting memory. See scripts/profile_planner.py.
PLANNER_STATE_BUDGET = 500_000

# Populated after every _exact_plan call for profiling and tests.
LAST_PLAN_STATS: dict = {}


def _exact_plan(
    nodes: list[tuple[float, float, Station]],
    total_miles: float,
    capacity_gal: float,
    mpg: float,
    start_fuel_gal: float | None = None,
) -> list[Purchase]:
    """Exact minimum-cost fuel purchases over fixed stations on a line.

    Model: stations are fixed purchase points on a line; the vehicle
    starts at mile 0 carrying `start_fuel_gal` gallons (default: a full
    tank) that are pre-existing and never charged; the origin and
    destination sell nothing; the tank holds `capacity_gal` gallons.
    Travel between two nodes burns the detour-aware distance
    d(i, j) = (mile_j - mile_i) + off_i + off_j, where off_i is the
    station's one-way off-route distance (0 for origin/destination), so
    reaching a stop pays its access road and returning to the route pays
    its egress.

    Algorithm: exact dynamic programming over states (node, arrival
    fuel). By the fill-or-arrive-empty property (Khuller, Malekian &
    Mestre, "To Fill or Not to Fill: The Gas Station Problem", 2011),
    a purchase either leaves exactly enough to arrive empty at a later
    point or fills the tank. With a partial starting tank and detour
    distances, passing a station without buying must also be kept for
    every surviving state (an independent audit found an earlier version
    that dropped it), so each state considers three moves: pass through,
    buy to arrive empty, and fill. Three measures keep this
    exact while bounded in memory (no per-state plan copies, which is
    what made an earlier draft blow up):

    1. States are merged on (node, fuel) keeping the cheapest cost.
    2. Per node, dominated states are pruned before expansion: state A
       dominates state B when A has at least as much fuel at no higher
       cost, because from A every continuation available to B stays
       feasible and never costs more (buy what B would buy, but less).
       Pruning removes only states that can never lead to a strictly
       better plan, so the result is unchanged.
    3. Reconstruction uses parent pointers (one per state), not stored
       plans, so memory is O(states), not O(states x plan length).

    Correctness is checked independently of this code by
    scripts/validate_optimizer.py (grid-exact Dijkstra baseline) and
    scripts/validate_selection.py (exhaustive subset enumeration with an
    independent strategy recursion); both report zero mismatches.

    Raises RouteInfeasibleError when the stations cannot cover the
    route, and PlanningBudgetError if the state budget is exceeded
    (surfaced explicitly, never replaced by an approximate answer).
    """
    if start_fuel_gal is None:
        start_fuel_gal = capacity_gal

    stations_only = [s for (_m, _p, s) in nodes]
    pos = [0.0] + [m for (m, _p, _s) in nodes] + [total_miles]
    price = [None] + [p for (_m, p, _s) in nodes] + [None]
    off = [0.0] + [s.off_route_miles for s in stations_only] + [0.0]
    n = len(pos) - 1  # index of the destination node
    capacity_miles = capacity_gal * mpg

    # states[i]: {fuel_on_arrival: [cost, parent_key, buy_gallons]} where
    # parent_key is (node_index, fuel_on_arrival) of the previous state -
    # slots are final once their node is expanded (transitions only go
    # forward), so (node, fuel) is a stable identity and no per-state
    # plan copies are ever stored.
    states: list[dict[float, list]] = [dict() for _ in pos]
    states[0][round(start_fuel_gal, 6)] = [0.0, None, 0.0]
    created = 1
    max_frontier = 1

    def prune(slot: dict) -> None:
        """Drop dominated states: sorted by fuel descending, a state stays
        only when strictly cheaper than every state with more fuel."""
        best_cost = float("inf")
        for fuel in sorted(slot, reverse=True):
            if slot[fuel][0] < best_cost - 1e-12:
                best_cost = slot[fuel][0]
            else:
                del slot[fuel]

    for i in range(n + 1):
        slot = states[i]
        if not slot:
            continue
        prune(slot)
        if len(slot) > max_frontier:
            max_frontier = len(slot)
        price_i = price[i]
        off_i = off[i]
        pos_i = pos[i]

        # Transitions. After pruning, the frontier is sorted so that MORE
        # fuel costs MORE (a state with more fuel and no higher cost would
        # have dominated the others). Therefore no pass-through state
        # dominates another, and EVERY state with enough fuel is expanded
        # for pass-through. Two reductions stay exact because they depend
        # only on the landing state, which does not depend on the source:
        # - fill at i, then drive to j: lands at (capacity - burn) whatever
        #   the source was; cost = cost - fuel * price_i + capacity *
        #   price_i, so the source minimising (cost - fuel * price_i) over
        #   ALL states gives the cheapest such arrival;
        # - buy exactly enough to arrive empty at j: lands at 0; the
        #   source must hold LESS than burn (otherwise nothing is bought),
        #   and the cheapest is the minimiser of (cost - fuel * price_i)
        #   over the states with fuel < burn.
        items = list(slot.items())
        if price_i is not None:
            fill_fuel, fill_state = min(
                items, key=lambda kv: kv[1][0] - kv[0] * price_i)
            fill_cost = fill_state[0]

        for j in range(i + 1, n + 1):
            gap = pos[j] - pos_i
            if gap + off_i > capacity_miles + 1e-9:
                break  # pos grows with j; no later j is reachable
            dist = gap + off_i + off[j]
            if dist > capacity_miles + 1e-9:
                continue
            burn = dist / mpg
            tgt = states[j]
            for f_i, st_i in items:
                if f_i >= burn - 1e-9:
                    # reach j on the fuel already in the tank, no purchase
                    key = round(max(f_i - burn, 0.0), 6)
                    cur = tgt.get(key)
                    if cur is None or st_i[0] < cur[0] - 1e-12:
                        tgt[key] = [st_i[0], (i, f_i), 0.0]
                        created += 1
            if price_i is not None:
                low = [kv for kv in items if kv[0] < burn - 1e-9]
                if low:
                    e_fuel, e_state = min(
                        low, key=lambda kv: kv[1][0] - kv[0] * price_i)
                    need = burn - e_fuel
                    new_cost = e_state[0] + need * price_i
                    cur = tgt.get(0.0)
                    if cur is None or new_cost < cur[0] - 1e-12:
                        tgt[0.0] = [new_cost, (i, e_fuel), need]
                        created += 1
                # fill the tank at i, then drive to j
                buy = capacity_gal - fill_fuel
                if buy > 1e-9:
                    key = round(capacity_gal - burn, 6)
                    new_cost = fill_cost + buy * price_i
                    cur = tgt.get(key)
                    if cur is None or new_cost < cur[0] - 1e-12:
                        tgt[key] = [new_cost, (i, fill_fuel), buy]
                        created += 1
            if len(tgt) > 64:
                prune(tgt)  # keep unexpanded slots small and bounded
            if created > PLANNER_STATE_BUDGET:
                LAST_PLAN_STATS.update(candidates=len(nodes), states_created=created,
                                       max_frontier=max_frontier, budget_exceeded=True)
                raise PlanningBudgetError(
                    f"Exact planner exceeded its {PLANNER_STATE_BUDGET}-state "
                    f"safety budget on {len(nodes)} candidates; refusing "
                    "to return an unverified plan"
                )

    if not states[n]:
        raise RouteInfeasibleError(
            "No fuel plan found: the stations with a usable coordinate along "
            "this route cannot cover it with the fuel available. This is a "
            "limit of station coverage and the planning range, not a finding "
            "that the trip is physically impossible"
        )
    prune(states[n])
    best_fuel = min(states[n], key=lambda f: states[n][f][0])

    # Walk parent keys back, collecting purchases (a buy at node i is
    # recorded on the transition out of state (i, fuel)).
    chain: list[tuple[int, float, float, float]] = []  # (node, buy, before, after)
    key: tuple[int, float] | None = (n, best_fuel)
    while key is not None and key[0] > 0:
        node_i, fuel_i = key
        _cost, parent, buy = states[node_i][fuel_i]
        if buy > 1e-9 and parent is not None:
            p_node, p_fuel = parent
            chain.append((p_node, buy, p_fuel, p_fuel + buy))
        key = parent
    chain.reverse()
    LAST_PLAN_STATS.clear()
    LAST_PLAN_STATS.update(
        candidates=len(nodes), states_created=created,
        max_frontier=max_frontier)
    return [
        Purchase(
            stations_only[node - 1],
            pos[node],
            buy,
            buy * price[node],
            fuel_before=before,
            fuel_after=after,
        )
        for node, buy, before, after in chain
    ]


def plan_purchases(
    stations: list[Station],
    total_miles: float,
    effective_range_miles: float,
    mpg: float,
    start_fuel_gal: float | None = None,
) -> list[Purchase]:
    """Select the minimum-cost set of fuel stops on the route line.

    Model: the route is a line, every corridor station is a purchase point
    with a fixed price and a known off-route distance (detour-aware
    planning distances), and the tank holds `effective_range_miles / mpg`
    gallons (a planning range below the vehicle's real range, leaving
    reserve for geometry approximations). The vehicle starts with
    `start_fuel_gal` gallons (default: a full planning tank); starting
    fuel is pre-existing and only fuel bought at stops is charged. The
    exact planner (see _exact_plan) returns the provably cheapest plan in
    this model. Raises RouteInfeasibleError when the station-level stops
    along the route cannot cover it; the caller reports that rather than
    falling back to approximate locations.
    """
    nodes = [(s.route_mile, s.price, s) for s in stations if s.route_mile > 0]
    capacity_gal = effective_range_miles / mpg
    if not nodes:
        # No selectable stop on the corridor. The trip is still plannable
        # when the starting fuel alone covers it (no purchase needed);
        # otherwise there is no grounded plan.
        start_gal = capacity_gal if start_fuel_gal is None else start_fuel_gal
        if start_gal * mpg + 1e-9 >= total_miles:
            return []
        raise RouteInfeasibleError(
            "No fuel plan found: no station with a usable coordinate lies "
            "along this route, and the starting fuel does not cover it. This "
            "is a limit of station coverage, not a finding that the trip is "
            "physically impossible"
        )
    return _exact_plan(nodes, total_miles, capacity_gal, mpg, start_fuel_gal)


def finalize_on_driven_legs(
    purchases: list[Purchase],
    start_lat: float,
    start_lon: float,
    finish_lat: float,
    finish_lon: float,
    vehicle_range_miles: float,
    mpg: float,
    start_fuel_gal: float | None = None,
) -> tuple[list[Purchase], list[float], str]:
    """Recompute gallons and dollars on actual driven legs.

    One waypoint routing call through the start, every selected stop and
    the destination returns real road legs (detour access roads
    included). Purchases are then re-optimised with the exact planner
    over the same stops on driven-leg odometers and the vehicle's real
    range (off-route distances are already inside the driven legs, so
    they are zero here), so the reported gallons and cost reflect the
    actual driving distance. The vehicle starts with `start_fuel_gal`
    gallons (default: a full tank); starting fuel is pre-existing and
    only pump purchases are charged. Raises RouteInfeasibleError if any
    driven leg exceeds the vehicle range.
    """
    waypoints = [(start_lat, start_lon)] + [
        (p.station.lat, p.station.lon) for p in purchases
    ] + [(finish_lat, finish_lon)]
    legs, geometry = waypoint_route(waypoints)  # len(legs) = len(purchases) + 1

    capacity_gal = vehicle_range_miles / mpg
    worst = max(legs)
    if worst > vehicle_range_miles:
        raise RouteInfeasibleError(
            f"Driven leg of {worst:.0f} mi exceeds the "
            f"{vehicle_range_miles:.0f}-mile vehicle range"
        )

    # Real odometer at every waypoint: odo[0] = start, odo[i] = stop i,
    # odo[-1] = destination.
    odo = [0.0]
    for leg in legs:
        odo.append(odo[-1] + leg)

    # Copies with zero off-route distance: detours are inside the driven
    # legs now, and the shared station records must keep their real
    # off-route values for planning.
    nodes: list[tuple[float, float, Station]] = [
        (odo[i + 1], purchase.station.price,
         replace(purchase.station, off_route_miles=0.0))
        for i, purchase in enumerate(purchases)
    ]
    final = _exact_plan(nodes, odo[-1], capacity_gal, mpg, start_fuel_gal)

    # The planner may skip a selected stop when the tank already covers the
    # gap, so legs are reconstructed from real odometer differences, never
    # by zipping purchases against the leg list.
    prev_odo = 0.0
    for purchase in final:
        purchase.leg_miles = purchase.route_mile - prev_odo
        prev_odo = purchase.route_mile
    return final, legs, geometry
