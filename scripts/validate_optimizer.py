"""Independent validation of the exact fuel planner against a
brute-force baseline on small generated fixed-route instances.

Baseline: shortest path (Dijkstra) over states (node, fuel_on_arrival in
0.1-gal units). Distances are generated in whole miles and consumption is
0.1 gal/mile, so every reachable fuel level is grid-aligned and the
discretised search is exact for these instances - it shares no logic with
the planner under test.

Run: python scripts/validate_optimizer.py
"""
import heapq
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fuelroute.settings")
import django
django.setup()

from router.services import plan_purchases, Station

MPG = 10.0
STEP = 0.1  # gallons per mile


def exact_min_cost(nodes, dest_mile, capacity_gal):
    """nodes: [(mile, price)] sorted; origin at mile 0. The vehicle starts
    with a FULL tank that is pre-existing and free, and the origin sells
    nothing. Returns minimum cash cost to arrive at dest with >= 0 fuel,
    or None."""
    all_nodes = [(0.0, None)] + [(m, p) for (m, p) in nodes if m > 0] + [(dest_mile, None)]
    # state: (node_index, fuel_units) with fuel measured in 0.1-gal units
    cap_units = int(round(capacity_gal / STEP))
    start = (0, cap_units)
    dist = {start: 0.0}
    pq = [(0.0, start)]
    while pq:
        cost, (i, fuel) = heapq.heappop(pq)
        if i == len(all_nodes) - 1:
            return cost  # destination popped: globally minimal
        if cost > dist.get((i, fuel), float("inf")):
            continue
        mile_i, price_i = all_nodes[i]
        # drive to each later node
        for j in range(i + 1, len(all_nodes)):
            mile_j, _ = all_nodes[j]
            need = int(round((mile_j - mile_i) / (MPG * STEP)))
            if need > cap_units:
                break
            variants = []
            if need > fuel:
                # must buy at i to make the leg (not possible at destination)
                if price_i is None:
                    continue
                variants.append((cost + (need - fuel) * STEP * price_i, 0))
            else:
                variants.append((cost, fuel - need))
            # also consider brimming the tank at i before the leg
            if price_i is not None:
                variants.append((cost + (cap_units - fuel) * STEP * price_i,
                                 cap_units - need))
            for new_cost, new_fuel in variants:
                state = (j, new_fuel)
                if new_cost < dist.get(state, float("inf")):
                    dist[state] = new_cost
                    heapq.heappush(pq, (new_cost, state))
    dest_idx = len(all_nodes) - 1
    best = min((c for (i, f), c in dist.items() if i == dest_idx), default=None)
    return best


def planner_cost(stations_rows, dest_mile, capacity_gal):
    stations = [Station(id=str(k), name=f"S{k}", address="", city="X", state="KS",
                        price=p, lat=0.0, lon=0.0, route_mile=m)
                for k, (m, p) in enumerate(stations_rows)]
    purchases = plan_purchases(
        stations, dest_mile, capacity_gal * MPG, MPG,
    )
    return sum(p.cost for p in purchases)


def gen_instance(rng):
    n = rng.randint(2, 8)
    dest = rng.randint(300, 1400)
    mile = 0.0
    rows = [(0.0, round(rng.uniform(2.5, 4.0), 2))]
    while mile < dest - 50 and len(rows) < n:
        mile += rng.randint(30, 220)
        if mile >= dest:
            break
        price = round(rng.uniform(2.5, 4.0), 2)
        if rng.random() < 0.25 and len(rows) > 0:
            price = rows[-1][1]  # force price ties
        rows.append((float(mile), price))
    capacity = rng.choice([30.0, 40.0, 48.0, 50.0])
    return rows, float(dest), capacity


def main():
    rng = random.Random(20261119)
    checked, feasible, mismatches = 0, 0, []
    for _ in range(3000):
        rows, dest, cap = gen_instance(rng)
        checked += 1
        exact = exact_min_cost(rows, dest, cap)
        try:
            planner = planner_cost(rows, dest, cap)
        except Exception:
            planner = None
        if exact is None:
            if planner is not None:
                mismatches.append((rows, dest, cap, exact, planner, "planner solved infeasible"))
            continue
        feasible += 1
        if planner is None:
            mismatches.append((rows, dest, cap, exact, planner, "planner failed feasible"))
        elif abs(planner - exact) > 0.01:
            mismatches.append((rows, dest, cap, round(exact, 2), round(planner, 2), "cost mismatch"))
    print(f"instances checked: {checked} (feasible per baseline: {feasible})")
    print(f"mismatches: {len(mismatches)}")
    for m in mismatches[:5]:
        print("  ", m)
    return 0 if not mismatches else 1


if __name__ == "__main__":
    raise SystemExit(main())
