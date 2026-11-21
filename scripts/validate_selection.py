"""Independent validation of stop SELECTION: exhaustive subset enumeration.

For each generated instance, every subset of the candidate stations is
enumerated explicitly (2^n masks). For each subset, the minimum cost is
computed by an independent memoised recursion over (position, fuel) that
considers, at every chosen stop, passing through without buying, buying
exactly enough to reach each later point (including the destination,
tank permitting) and filling the tank. The cheapest subset cost must
equal the planner's cost over the full candidate set.

This shares no code with router.services: it is an adversarial
cross-check of both which stops are chosen and how much is bought.

Run: python scripts/validate_selection.py
"""
import random
import sys
from functools import lru_cache
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fuelroute.settings")
import django
django.setup()

from router.services import plan_purchases, Station, RouteInfeasibleError

MPG = 10.0
INF = float("inf")


def subset_min_cost(rows, dest, cap):
    """rows: [(mile, price)]. Min cost over every subset and strategy."""
    chosen = [(m, p) for (m, p) in rows]

    @lru_cache(maxsize=None)
    def solve(k, fuel_key):
        # k: index into chosen (-1 = origin); fuel_key: fuel x 1e6 (int)
        fuel = fuel_key / 1e6
        mile = chosen[k][0] if k >= 0 else 0.0
        price = chosen[k][1] if k >= 0 else None
        best = INF
        finish_need = (dest - mile) / MPG - fuel
        if finish_need <= 1e-9:
            best = 0.0
        elif price is not None and finish_need <= cap - fuel + 1e-9:
            best = finish_need * price
        for t in range(k + 1, len(chosen)):
            dist = chosen[t][0] - mile
            if dist / MPG > cap + 1e-9:
                break
            burn = dist / MPG
            sub = solve(t, round((fuel - burn) * 1e6) if fuel >= burn - 1e-9 else -1) \
                if fuel >= burn - 1e-9 else INF
            if sub < INF:
                best = min(best, sub)
            if price is not None:
                need = burn - fuel
                if need > 1e-9:
                    sub = solve(t, 0)
                    if sub < INF:
                        best = min(best, need * price + sub)
                fill = cap - fuel
                if fill > 1e-9:
                    sub = solve(t, round((cap - burn) * 1e6))
                    if sub < INF:
                        best = min(best, fill * price + sub)
        return best

    start = solve(-1, round(cap * 1e6))
    return None if start == INF else start


def exhaustive_min_cost(rows, dest, cap):
    """Explicit 2^n subset enumeration around the independent recursion."""
    best = None
    n = len(rows)
    for r in range(0, n + 1):
        for combo in combinations(range(n), r):
            sub = [rows[k] for k in combo]
            cost = subset_min_cost(sub, dest, cap)
            if cost is not None and (best is None or cost < best):
                best = cost
    return best


def planner_cost(rows, dest, cap):
    stations = [Station(id=str(k), name=f"S{k}", address="", city="X",
                        state="KS", price=p, lat=0.0, lon=0.0, route_mile=m)
                for k, (m, p) in enumerate(rows)]
    try:
        return sum(p.cost for p in plan_purchases(
            stations, float(dest), cap * MPG, MPG))
    except RouteInfeasibleError:
        return None


def gen_instance(rng):
    n = rng.randint(2, 9)
    dest = rng.randint(300, 1200)
    miles = sorted(rng.sample(range(15, dest - 10), min(n, dest - 25)))
    rows = [(float(m), round(rng.uniform(2.4, 4.2), 2)) for m in miles]
    cap = rng.choice([30.0, 40.0, 48.0, 50.0])
    return rows, float(dest), cap


def run_seed(seed, instances):
    rng = random.Random(seed)
    checked, feasible, mismatches = 0, 0, []
    for _ in range(instances):
        rows, dest, cap = gen_instance(rng)
        checked += 1
        expected = exhaustive_min_cost(rows, dest, cap)
        got = planner_cost(rows, dest, cap)
        if expected is None:
            if got is not None:
                mismatches.append((rows, dest, cap, expected, got,
                                   "planner solved infeasible"))
            continue
        feasible += 1
        if got is None:
            mismatches.append((rows, dest, cap, expected, got,
                               "planner failed feasible"))
        elif abs(got - expected) > 0.01:
            mismatches.append((rows, dest, cap, round(expected, 2),
                               round(got, 2), "cost mismatch"))
    return checked, feasible, mismatches


def main():
    total_bad = 0
    for seed, instances in ((20261119, 120), (777, 120)):
        checked, feasible, mismatches = run_seed(seed, instances)
        total_bad += len(mismatches)
        print(f"seed {seed}: {checked} instances "
              f"({feasible} feasible per enumeration), "
              f"mismatches: {len(mismatches)}")
        for m in mismatches[:5]:
            print("  ", m)
    return 0 if total_bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
