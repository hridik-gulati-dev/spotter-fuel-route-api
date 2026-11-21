"""Offline profiling of the exact planner: time, peak memory, DP states.

Runs synthetic worst-case corridors (adversarial random prices, dense
stations, long routes) far larger than any real corridor, under an
optional address-space cap (run with `ulimit -v`), so a memory blowup
aborts the profiler instead of the box. Also profiles one REAL corridor
(New York, NY -> Miami, FL) through the live geocoding/routing pipeline
(2 geocoding + 1 routing call, no loop) to report real candidate counts.

Run: python scripts/profile_planner.py [--live]
"""
import random
import resource
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fuelroute.settings")
import django
django.setup()

from router import services
from router.services import Station, plan_purchases

MPG = 10.0


def synth_corridor(rng, n, total_miles, detour_max=5.0):
    miles = sorted(rng.uniform(5, total_miles - 5) for _ in range(n))
    return [
        Station(id=f"s{k}", name=f"S{k}", address="", city="X", state="KS",
                price=round(rng.uniform(2.4, 4.2), 2),
                lat=0.0, lon=0.0, route_mile=m,
                off_route_miles=round(rng.uniform(0, detour_max), 2))
        for k, m in enumerate(miles)
    ]


def profile(label, stations, total_miles, range_miles=450.0, start_fuel=None):
    tracemalloc.start()
    t0 = time.perf_counter()
    try:
        purchases = plan_purchases(stations, total_miles, range_miles, MPG, start_fuel)
        outcome = f"{len(purchases)} purchases, ${sum(p.cost for p in purchases):.2f}"
    except services.RouteInfeasibleError as exc:
        outcome = f"infeasible ({exc})"
    except services.PlanningBudgetError:
        outcome = "STATE BUDGET EXCEEDED (explicit error, no plan returned)"
    dt = time.perf_counter() - t0
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    stats = services.LAST_PLAN_STATS
    print(f"{label}: {dt:.2f}s, peak {peak / 1e6:.1f} MB, "
          f"states {stats.get('states_created', '?'):,}, "
          f"max frontier {stats.get('max_frontier', '?')}, {outcome}")


def main():
    rng = random.Random(99)
    # Spacing ~2 mi apart across 1,600 mi is far denser than the real
    # station-level coverage; 800 candidates is beyond any real corridor.
    for n, miles in ((100, 800), (200, 800), (400, 1600), (800, 3200)):
        profile(f"synthetic n={n} route={miles}mi",
                synth_corridor(rng, n, miles), miles)
    # Partial starting fuel (12 gal), the case the old reduction got wrong.
    for n, miles in ((100, 800), (200, 1600), (400, 1600)):
        profile(f"synthetic partial start 12 gal n={n} route={miles}mi",
                synth_corridor(rng, n, miles, detour_max=12.0), miles,
                start_fuel=12.0)

    if "--live" in sys.argv:
        lat1, lon1, _ = services.geocode("New York, NY")
        lat2, lon2, _ = services.geocode("Miami, FL")
        geometry, total = services.driving_route(lat1, lon1, lat2, lon2)
        points = services.decode_polyline(geometry)
        markers = services._route_mile_markers(points)
        candidates = services.project_stations(
            points, markers, 10.0)
        profile(f"LIVE NYC-MIA ({total:.0f} mi, {len(candidates)} candidates)",
                candidates, total)


if __name__ == "__main__":
    main()
