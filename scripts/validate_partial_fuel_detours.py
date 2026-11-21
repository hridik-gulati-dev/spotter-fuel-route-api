"""Independent validation of the exact planner on the FULL input domain:
partial starting fuel AND off-route detours (the earlier validators only
covered a full tank and zero detours).

Baseline: Dijkstra over states (node, fuel in 0.1-gallon units) where the
only actions are "buy one 0.1 gal unit at the current station" and "drive
to a later node on the fuel in the tank". It buys unit by unit, so it does
not rely on the fill-or-arrive-empty theorem the planner uses. Distances,
detours and starting fuel are whole miles / whole units at 10 mpg, so every
fuel level is grid-aligned and the search is exact for these instances.
Detour model matches the planner: travel i -> j burns
(mile_j - mile_i) + off_i + off_j miles, origin and destination have off 0.

Run: python scripts/validate_partial_fuel_detours.py
"""
import heapq
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fuelroute.settings")
import django
django.setup()

from router.services import Station, plan_purchases

MPG = 10.0


def baseline(nodes, dest, cap_units, start_units):
    """nodes: [(mile, price, off)] stations; returns min cash or None."""
    allv = [(0, None, 0)] + [(m, p, o) for (m, p, o) in nodes] + [(dest, None, 0)]
    last = len(allv) - 1
    dist = {(0, start_units): 0.0}
    pq = [(0.0, 0, start_units)]
    while pq:
        cost, i, f = heapq.heappop(pq)
        if cost > dist.get((i, f), float("inf")):
            continue
        if i == last:
            return cost
        m_i, p_i, o_i = allv[i]
        if p_i is not None and f < cap_units:
            nc = cost + 0.1 * p_i
            if nc < dist.get((i, f + 1), float("inf")) - 1e-12:
                dist[(i, f + 1)] = nc
                heapq.heappush(pq, (nc, i, f + 1))
        for j in range(i + 1, last + 1):
            m_j, _p, o_j = allv[j]
            need = (m_j - m_i) + o_i + o_j  # 1 unit per mile
            if need <= f:
                nf = f - need
                if cost < dist.get((j, nf), float("inf")) - 1e-12:
                    dist[(j, nf)] = cost
                    heapq.heappush(pq, (cost, j, nf))
    return None


def planner(nodes, dest, cap_units, start_units):
    sts = [Station(id=str(k), name=f"S{k}", address="", city="X", state="KS",
                   price=p, lat=0.0, lon=0.0, route_mile=float(m),
                   off_route_miles=float(o))
           for k, (m, p, o) in enumerate(nodes)]
    ps = plan_purchases(sts, float(dest), cap_units * 0.1 * MPG, MPG, start_units * 0.1)
    return sum(p.cost for p in ps)


def gen(rng):
    dest = rng.randint(150, 900)
    cap_units = rng.choice([200, 300, 400, 500])  # 20..50 gal = 200..500 miles at 10 mpg
    nodes, mile = [], 0
    for _ in range(rng.randint(1, 9)):
        mile += rng.randint(10, 160)
        if mile >= dest:
            break
        price = round(rng.uniform(2.5, 4.0), 2)
        if nodes and rng.random() < 0.25:
            price = nodes[-1][1]
        nodes.append((mile, price, rng.randint(0, 12)))
    start = rng.randint(0, cap_units)
    return nodes, dest, cap_units, start


def main(seed=20261120, count=4000):
    rng = random.Random(seed)
    feasible = mism = 0
    shown = []
    for _ in range(count):
        nodes, dest, cap, start = gen(rng)
        want = baseline(nodes, dest, cap, start)
        try:
            got = planner(nodes, dest, cap, start)
        except Exception:
            got = None
        if want is None:
            if got is not None:
                mism += 1; shown.append(("planner solved an infeasible case", nodes, dest, cap, start, got))
            continue
        feasible += 1
        if got is None or abs(got - want) > 1e-6:
            mism += 1
            shown.append(("mismatch", nodes, dest, cap, start, want, got))
    print(f"seed {seed}: {count} instances ({feasible} feasible per baseline), mismatches: {mism}")
    for s in shown[:3]:
        print("  ", s)
    return 1 if mism else 0


if __name__ == "__main__":
    raise SystemExit(max(main(20261120), main(99, 3000)))
