"""Unit tests. External services are mocked: the suite runs fully offline."""
from unittest.mock import patch

from django.test import TestCase

from . import services, views
from .services import Station


def st(id_, price, mile, off=0.0):
    return Station(id=id_, name=f"S{id_}", address="", city="X", state="KS",
                   price=price, lat=0.0, lon=0.0, route_mile=mile,
                   off_route_miles=off)


class PolylineTests(TestCase):
    def test_decode_known_polyline(self):
        # Reference pair from the polyline spec: "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
        # = (38.5, -120.2), (40.7, -120.95), (43.252, -126.453)
        pts = services.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
        self.assertEqual(len(pts), 3)
        self.assertAlmostEqual(pts[0][0], 38.5, places=5)
        self.assertAlmostEqual(pts[0][1], -120.2, places=5)
        self.assertAlmostEqual(pts[2][0], 43.252, places=5)
        self.assertAlmostEqual(pts[2][1], -126.453, places=5)


class InventoryPlannerTests(TestCase):
    """The exact planner simulates a real tank: the vehicle starts full,
    only pump purchases are charged, and the tank never overfills or runs
    dry."""
    RANGE = 480.0  # planning range -> 48 gal tank
    MPG = 10.0
    CAP = RANGE / MPG

    def plan(self, stations, total_miles):
        return services.plan_purchases(stations, total_miles, self.RANGE,
                                       self.MPG)

    def assert_inventory_sound(self, purchases, total_miles, start_fuel=None):
        fuel = self.CAP if start_fuel is None else start_fuel
        prev_mile = 0.0
        for p in purchases:
            burned = (p.route_mile - prev_mile) / self.MPG
            self.assertAlmostEqual(p.fuel_before, fuel - burned, places=4)
            self.assertGreaterEqual(p.fuel_before, -1e-9)
            self.assertAlmostEqual(p.fuel_after, p.fuel_before + p.gallons, places=4)
            self.assertLessEqual(p.fuel_after, self.CAP + 1e-9)
            fuel, prev_mile = p.fuel_after, p.route_mile
        # tank must reach the destination
        self.assertGreaterEqual(fuel * self.MPG, total_miles - prev_mile - 1e-6)

    def test_short_trip_needs_no_purchase(self):
        """240-mile trip fits the free starting tank: nothing is bought,
        nothing is charged."""
        purchases = self.plan([st("a", 3.10, 8.0)], 240.0)
        self.assertEqual(purchases, [])

    def test_underbuy_to_reach_cheaper_station(self):
        """Trip 900: free tank reaches the $2.50 station at mile 450;
        buy only the 42 gal the rest of the trip needs there."""
        stations = [st("exp", 3.60, 200.0), st("cheap", 2.50, 450.0)]
        purchases = self.plan(stations, 900.0)
        self.assertEqual([p.station.id for p in purchases], ["cheap"])
        self.assertAlmostEqual(purchases[0].gallons, 42.0, places=4)
        self.assertAlmostEqual(purchases[0].cost, 42 * 2.50, places=2)
        self.assert_inventory_sound(purchases, 900.0)

    def test_fill_only_shortfall_when_nothing_cheaper(self):
        """Trip 700, one $3.00 station at 300: arrive on 18 free gal,
        buy the 22 gal shortfall, no more."""
        purchases = self.plan([st("a", 3.00, 300.0)], 700.0)
        self.assertEqual(len(purchases), 1)
        self.assertAlmostEqual(purchases[0].fuel_before, 18.0, places=4)
        self.assertAlmostEqual(purchases[0].gallons, 22.0, places=4)
        self.assertAlmostEqual(purchases[0].fuel_after, 40.0, places=4)
        self.assertAlmostEqual(purchases[0].cost, 22 * 3.00, places=2)
        self.assert_inventory_sound(purchases, 700.0)

    def test_two_stop_inventory_trace(self):
        """Trip 950: fill up at $2.90@300 (30 gal), top up 17 gal at
        $3.10@600. Consumption 95 gal = 48 free + 47 bought."""
        stations = [st("a", 2.90, 300.0), st("b", 3.10, 600.0)]
        purchases = self.plan(stations, 950.0)
        self.assertAlmostEqual(purchases[0].gallons, 30.0, places=4)
        self.assertAlmostEqual(purchases[1].gallons, 17.0, places=4)
        self.assertAlmostEqual(sum(p.gallons for p in purchases), 47.0, places=3)
        self.assertAlmostEqual(sum(p.cost for p in purchases),
                               30 * 2.90 + 17 * 3.10, places=2)
        self.assert_inventory_sound(purchases, 950.0)

    def test_skips_expensive_station_for_reachable_cheaper(self):
        stations = [st("mid-expensive", 3.60, 200.0), st("far-cheap", 2.90, 440.0)]
        purchases = self.plan(stations, 800.0)
        bought_at = [p.station.id for p in purchases]
        self.assertNotIn("mid-expensive", bought_at)
        self.assertIn("far-cheap", bought_at)
        self.assert_inventory_sound(purchases, 800.0)

    def test_detour_cost_counts_against_cheap_price(self):
        """Detour-aware planning: a $2.90 station 25 miles off the route
        (50 extra driven miles = 5 gal burned) loses to the $3.00 station
        sitting on the route. A price-only comparison would pick wrong."""
        on_route = st("on", 3.00, 300.0, off=0.0)
        off_route = st("off", 2.90, 300.0, off=25.0)
        purchases = self.plan([on_route, off_route], 700.0)
        self.assertEqual([p.station.id for p in purchases], ["on"])
        self.assertAlmostEqual(purchases[0].gallons, 22.0, places=4)

    def test_infeasible_without_station(self):
        with self.assertRaises(services.RouteInfeasibleError):
            self.plan([], 1200.0)

    def test_infeasible_gap_too_wide(self):
        """Stations exist but 600 miles apart with a 480-mile tank."""
        stations = [st("a", 3.00, 100.0), st("b", 3.00, 700.0)]
        with self.assertRaises(services.RouteInfeasibleError):
            self.plan(stations, 900.0)

    def test_configurable_starting_fuel(self):
        """Trip 700 with only 20 gal (200 mi) at the start: the free fuel
        cannot reach the $3.00 station at 300, so the plan must use the
        $4.00 station at 150 first."""
        stations = [st("near-pricey", 4.00, 150.0), st("far-cheap", 3.00, 300.0)]
        purchases = services.plan_purchases(stations, 700.0, self.RANGE,
                                            self.MPG, 20.0)
        self.assertEqual(purchases[0].station.id, "near-pricey")
        self.assert_inventory_sound(purchases, 700.0, start_fuel=20.0)

    def test_empty_starting_tank_needs_station_at_mile_zero(self):
        """With 0 starting fuel the vehicle cannot move: no reachable
        station means an explicit no-plan, not free fuel."""
        with self.assertRaises(services.RouteInfeasibleError):
            services.plan_purchases([st("a", 3.00, 100.0)], 500.0,
                                    self.RANGE, self.MPG, 0.0)


class ExactnessTests(TestCase):
    """The planner is checked against an independent brute force written
    for this test: enumerate every subset of stops and, per subset, every
    fill-or-arrive-empty strategy by direct recursion (no shared code)."""
    MPG = 10.0

    def brute_force(self, rows, dest, cap_gal, start_fuel):
        """rows: [(mile, price)]. Returns min cash cost or None."""
        n = len(rows)
        best = [None]

        def rec(i, fuel, cost):
            # i: next candidate index; at mile m_i with `fuel` gallons
            mile = rows[i][0] if i >= 0 else 0.0
            price = rows[i][1] if i >= 0 else None
            # try to finish from here
            finish_need = (dest - mile) / self.MPG - fuel
            if finish_need <= 1e-9:
                if best[0] is None or cost < best[0]:
                    best[0] = cost
            elif price is not None and finish_need <= cap_gal - fuel + 1e-9:
                # buy exactly the shortfall here (tank permitting) and finish
                cand = cost + finish_need * price
                if best[0] is None or cand < best[0]:
                    best[0] = cand
            for j in range(i + 1, n):
                need = (rows[j][0] - mile) / self.MPG - fuel
                if (rows[j][0] - mile) / self.MPG > cap_gal + 1e-9:
                    break
                if need <= 1e-9:
                    rec(j, fuel - (rows[j][0] - mile) / self.MPG, cost)
                if price is not None:
                    if need > 1e-9:
                        rec(j, 0.0, cost + need * price)
                    fill = cap_gal - fuel
                    if fill > 1e-9:
                        rec(j, cap_gal - (rows[j][0] - mile) / self.MPG,
                            cost + fill * price)

        rec(-1, start_fuel, 0.0)
        return best[0]

    def test_matches_brute_force_on_random_instances(self):
        import random
        rng = random.Random(424242)
        checked = 0
        for _ in range(120):
            n = rng.randint(1, 6)
            dest = rng.randint(200, 900)
            miles = sorted(rng.sample(range(20, dest - 10), min(n, dest - 30)))
            rows = [(float(m), round(rng.uniform(2.4, 4.2), 2)) for m in miles]
            cap = rng.choice([30.0, 40.0, 48.0])
            start = cap
            stations = [st(f"s{k}", p, m) for k, (m, p) in enumerate(rows)]
            expected = self.brute_force(rows, float(dest), cap, start)
            try:
                got = sum(p.cost for p in services.plan_purchases(
                    stations, float(dest), cap * self.MPG, self.MPG))
            except services.RouteInfeasibleError:
                got = None
            if expected is None:
                self.assertIsNone(got, f"solved infeasible {rows} {dest} {cap}")
            else:
                self.assertIsNotNone(got, f"failed feasible {rows} {dest} {cap}")
                self.assertAlmostEqual(got, expected, places=6,
                                       msg=f"{rows} {dest} {cap}")
            checked += 1
        self.assertEqual(checked, 120)


class StationLevelFilterTests(TestCase):
    @patch("router.services.load_stations")
    def test_centroid_rows_are_never_candidates(self, mock_load):
        """city_centroid stations sit on the route but must be invisible."""
        points = [(0.0, i / 100.0) for i in range(101)]  # ~69 mi straight line
        markers = services._route_mile_markers(points)
        grounded = Station(id="g", name="G", address="", city="X", state="KS",
                           price=3.0, lat=0.0, lon=0.5,
                           coord_source="osm_exit_brand")
        approx = Station(id="b", name="B", address="", city="X", state="KS",
                         price=2.0, lat=0.0, lon=0.5,
                         coord_source="city_centroid")
        mock_load.return_value = [grounded, approx]
        found = services.project_stations(points, markers, 5.0)
        self.assertEqual([s.id for s in found], ["g"])

    def test_no_candidates_but_start_tank_covers_trip_is_zero_purchases(self):
        """No selectable stop on the corridor must not block a trip the
        starting fuel covers (regression: SF to LA returned 422)."""
        self.assertEqual(services.plan_purchases([], 380.0, 450.0, 10.0), [])
        self.assertEqual(services.plan_purchases([], 380.0, 450.0, 10.0, 38.0), [])
        with self.assertRaises(services.RouteInfeasibleError):
            services.plan_purchases([], 380.0, 450.0, 10.0, 30.0)

    def test_no_grounded_station_is_an_explicit_no_plan(self):
        with self.assertRaises(services.RouteInfeasibleError):
            services.plan_purchases([], 1200.0, 450.0, 10.0)


class DrivenLegAccountingTests(TestCase):
    @staticmethod
    def _station(id_, price):
        return Station(id=id_, name=f"S{id_}", address="", city="X", state="KS",
                       price=price, lat=0.0, lon=0.0,
                       coord_source="osm_exit_brand")

    @patch("router.services.waypoint_route")
    def test_gallons_recomputed_on_driven_legs(self, mock_wp):
        """Legs 100 + 250 + 250 = 600 driven miles. The free 50-gal
        starting tank covers 500; only the 10-gal shortfall is bought,
        at the cheapest stop the tank reaches (b, skipping pricier a)."""
        mock_wp.return_value = ([100.0, 250.0, 250.0], "poly")
        selected = [
            services.Purchase(self._station("a", 3.00), 0.0, 0.0, 0.0),
            services.Purchase(self._station("b", 2.90), 0.0, 0.0, 0.0),
        ]
        final, legs, _geom = services.finalize_on_driven_legs(
            selected, 0.0, 0.0, 0.0, 0.0, 500.0, 10.0)
        self.assertEqual(len(legs), 3)
        self.assertEqual([p.station.id for p in final], ["b"])
        self.assertAlmostEqual(final[0].gallons, 10.0, places=3)
        self.assertAlmostEqual(final[0].fuel_before, 15.0, places=3)
        self.assertAlmostEqual(final[0].fuel_after, 25.0, places=3)
        self.assertAlmostEqual(final[0].cost, 29.0, places=2)
        self.assertAlmostEqual(final[0].leg_miles, 350.0, places=3)

    @patch("router.services.waypoint_route")
    def test_starting_tank_is_never_charged(self, mock_wp):
        """Regression: a trip within the 500-mile starting tank costs $0
        at pumps; fuel burned before the first stop comes out of the
        pre-existing tank, not out of a purchase."""
        mock_wp.return_value = ([250.0, 250.0], "poly")
        selected = [services.Purchase(self._station("a", 3.00), 0.0, 0.0, 0.0)]
        final, legs, _ = services.finalize_on_driven_legs(
            selected, 0.0, 0.0, 0.0, 0.0, 500.0, 10.0)
        self.assertEqual(final, [])
        # one tenth of a mile beyond the tank: buy exactly the 0.01-gal shortfall
        mock_wp.return_value = ([250.0, 250.1], "poly")
        final, _, _ = services.finalize_on_driven_legs(
            selected, 0.0, 0.0, 0.0, 0.0, 500.0, 10.0)
        self.assertEqual(len(final), 1)
        self.assertAlmostEqual(final[0].gallons, 0.01, places=3)
        self.assertAlmostEqual(final[0].fuel_before, 25.0, places=3)

    @patch("router.services.waypoint_route")
    def test_over_range_leg_is_rejected(self, mock_wp):
        mock_wp.return_value = ([600.0, 10.0], "poly")
        selected = [services.Purchase(self._station("a", 3.00), 0.0, 0.0, 0.0)]
        with self.assertRaises(services.RouteInfeasibleError):
            services.finalize_on_driven_legs(
                selected, 0.0, 0.0, 0.0, 0.0, 500.0, 10.0)

    @patch("router.services.waypoint_route")
    def test_skipped_stop_keeps_correct_legs(self, mock_wp):
        """If the tank reaches stop c, stop b is skipped and c's purchase
        reports the combined leg from the start."""
        mock_wp.return_value = ([100.0, 100.0, 100.0, 250.0], "poly")
        selected = [
            services.Purchase(self._station("a", 3.00), 0.0, 0.0, 0.0),
            services.Purchase(self._station("b", 3.20), 0.0, 0.0, 0.0),
            services.Purchase(self._station("c", 2.80), 0.0, 0.0, 0.0),
        ]
        final, _legs, _geom = services.finalize_on_driven_legs(
            selected, 0.0, 0.0, 0.0, 0.0, 500.0, 10.0)
        bought = {p.station.id: p for p in final}
        self.assertNotIn("b", bought)
        self.assertAlmostEqual(bought["c"].leg_miles, 300.0, places=3)


class RefinementTests(TestCase):
    """The refinement pass re-checks the stop set against the driven
    geometry: one extra waypoint call, only when the set changed, and the
    verified plan is kept whenever refinement cannot improve."""

    @staticmethod
    def rst(id_, price, mile):
        """Station-level candidate (selection and valuation ignore
        city_centroid rows)."""
        s = st(id_, price, mile)
        s.coord_source = "osm_exit_brand"
        return s

    def _run(self, candidates_first, candidates_second, wp_side_effect):
        with patch("router.views.services.geocode",
                   side_effect=[(0.0, 0.0, "A"), (1.0, 1.0, "B")]), \
             patch("router.views.services.driving_route",
                   return_value=("poly", 700.0)), \
             patch("router.views.services.decode_polyline",
                   return_value=[(0.0, 0.0), (0.1, 0.1)]), \
             patch("router.views.services.project_stations",
                   side_effect=[candidates_first, candidates_second]), \
             patch("router.views.services.waypoint_route",
                   side_effect=wp_side_effect) as mock_wp:
            result = views.compute_route("a", "b", 50.0, None)
        return result, mock_wp

    def test_retry_and_refinement_share_the_three_call_routing_budget(self):
        """Direct route (1) + failed first verification (2) + retry (3)
        uses the whole budget, so the optional refinement must be skipped
        even though a cheaper set (y) exists on the driven geometry."""
        x = self.rst("x", 3.00, 300.0)
        y = self.rst("y", 2.50, 350.0)
        with patch("router.views.services.driving_route") as mock_direct:
            mock_direct.return_value = ("poly", 700.0)
            with patch("router.views.services.geocode",
                       side_effect=[(0.0, 0.0, "A"), (1.0, 1.0, "B")]), \
                 patch("router.views.services.decode_polyline",
                       return_value=[(0.0, 0.0), (0.1, 0.1)]), \
                 patch("router.views.services.project_stations",
                       side_effect=[[x], [x, y]]), \
                 patch("router.views.services.waypoint_route",
                       side_effect=[([300.0, 600.0], "g1"),
                                    ([300.0, 400.0], "g2"),
                                    ([350.0, 350.0], "g3")]) as mock_wp:
                result = views.compute_route("a", "b", 50.0, None)
        self.assertEqual(mock_wp.call_count, 2)
        self.assertEqual(mock_direct.call_count + mock_wp.call_count, 3)
        self.assertEqual([p["name"] for p in result["fuel_purchases"]], ["Sx"])
        self.assertEqual(result["route_polyline"], "g2")

    def test_refinement_runs_when_budget_remains(self):
        """Without a retry only 2 calls are used, so refinement may spend
        the third (existing adopted-set behaviour), never a fourth."""
        x = self.rst("x", 3.00, 300.0)
        y = self.rst("y", 2.50, 350.0)
        _result, mock_wp = self._run(
            [x], [x, y],
            [([300.0, 400.0], "g1"), ([350.0, 350.0], "g2"),
             ([350.0, 350.0], "g3")])
        self.assertLessEqual(mock_wp.call_count + 1, 3)

    def test_better_set_on_driven_geometry_is_adopted(self):
        """Direct-geometry candidates only have pricey X; the driven path
        passes cheaper Y, so the plan is re-verified through Y."""
        x = self.rst("x", 3.00, 300.0)
        y = self.rst("y", 2.50, 350.0)
        result, mock_wp = self._run(
            [x], [x, y],
            [([300.0, 400.0], "geom1"), ([350.0, 350.0], "geom2")])
        self.assertEqual(mock_wp.call_count, 2)
        self.assertEqual([p["name"] for p in result["fuel_purchases"]], ["Sy"])
        self.assertAlmostEqual(result["cash_paid_en_route"], 50.0, places=2)
        self.assertEqual(result["route_polyline"], "geom2")

    def test_same_set_means_no_second_waypoint_call(self):
        x = self.rst("x", 3.00, 300.0)
        result, mock_wp = self._run(
            [x], [x], [([300.0, 400.0], "geom1")])
        self.assertEqual(mock_wp.call_count, 1)
        self.assertEqual(result["route_polyline"], "geom1")
        # total_money_spent is always populated: cash $60 + consumed
        # starting fuel (50 gal of the 70 burned) valued at Sx's $3.00.
        self.assertAlmostEqual(result["total_money_spent"], 210.0, places=2)
        self.assertEqual(result["starting_fuel"]["price_source"]["kind"],
                         "station_valuation")
        self.assertEqual(result["starting_fuel"]["price_source"]["name"], "Sx")
        self.assertAlmostEqual(result["starting_fuel"]["cost"], 150.0, places=2)

    def test_micro_exclusion_is_measured_and_adopted_when_cheaper(self):
        """CHI-DAL regression: a tiny top-off can be the modeled optimum
        yet lose on measured legs. The incumbent buys 19 gal at A plus a
        1.2 gal micro top-off at B; excluding B measures cheaper once B's
        detour disappears, so the comparison round must adopt the
        micro-free set."""
        a = self.rst("a", 3.00, 300.0)
        b = self.rst("b", 2.99, 680.0)
        a2 = self.rst("a", 3.00, 305.0)
        b2 = self.rst("b", 2.99, 690.0)
        with patch("router.views.services.geocode",
                   side_effect=[(0.0, 0.0, "A"), (1.0, 1.0, "B")]), \
             patch("router.views.services.driving_route",
                   return_value=("poly", 690.0)), \
             patch("router.views.services.decode_polyline",
                   return_value=[(0.0, 0.0), (0.1, 0.1)]), \
             patch("router.views.services.project_stations",
                   side_effect=[[a, b], [a2, b2]]), \
             patch("router.views.services.waypoint_route",
                   side_effect=[([305.0, 385.0, 12.0], "geomMicro"),
                                ([305.0, 380.0], "geomClean")]) as mock_wp:
            result = views.compute_route("a", "b", 50.0, None)
        # incumbent verification + one comparison verification only
        self.assertEqual(mock_wp.call_count, 2)
        self.assertEqual([p["name"] for p in result["fuel_purchases"]], ["Sa"])
        # measured on the micro-free legs: (68.5 - 50) gal at $3.00
        self.assertAlmostEqual(result["cash_paid_en_route"], 55.5, places=2)
        self.assertEqual(result["route_polyline"], "geomClean")

    def test_accounting_adds_up_at_displayed_and_exact_precision(self):
        """Displayed purchase costs sum to cash, cash plus starting fuel
        equals the total, and the exact fields replay at full precision."""
        a = self.rst("a", 2.999, 250.0)
        b = self.rst("b", 3.0743, 520.0)
        with patch("router.views.services.geocode",
                   side_effect=[(0.0, 0.0, "A"), (1.0, 1.0, "B")]), \
             patch("router.views.services.driving_route",
                   return_value=("poly", 800.0)), \
             patch("router.views.services.decode_polyline",
                   return_value=[(0.0, 0.0), (0.1, 0.1)]), \
             patch("router.views.services.project_stations",
                   return_value=[a, b]), \
             patch("router.views.services.waypoint_route",
                   return_value=([250.0, 270.0, 280.0], "geom")):
            r = views.compute_route("a", "b", 30.0, None)
        ps = r["fuel_purchases"]
        self.assertGreaterEqual(len(ps), 1)
        self.assertAlmostEqual(sum(p["cost"] for p in ps), r["cash_paid_en_route"], places=9)
        self.assertAlmostEqual(r["cash_paid_en_route"] + r["starting_fuel"]["cost"],
                               r["total_money_spent"], places=9)
        for p in ps:
            self.assertAlmostEqual(p["gallons_exact"] * p["price_per_gallon"], p["cost_exact"], places=5)
            self.assertLess(abs(p["gallons_purchased"] * p["price_per_gallon"] - p["cost"]), 0.03)
        self.assertAlmostEqual(sum(p["cost_exact"] for p in ps), r["cash_paid_unrounded"], places=5)

    def test_waypoint_call_ceiling_with_competing_alternatives(self):
        """Call budget regression: even when a clearly cheaper re-selection
        AND a micro-exclusion both qualify, the refinement spends exactly
        one verification call - never more than two waypoint calls in
        total (1 direct + 2 waypoint = 3 routing calls per request)."""
        a = self.rst("a", 3.00, 300.0)
        b = self.rst("b", 2.99, 680.0)
        a2 = self.rst("a", 3.00, 305.0)
        b2 = self.rst("b", 2.99, 690.0)
        c2 = self.rst("c", 2.20, 310.0)  # cheaper, only on the driven corridor
        with patch("router.views.services.geocode",
                   side_effect=[(0.0, 0.0, "A"), (1.0, 1.0, "B")]), \
             patch("router.views.services.driving_route",
                   return_value=("poly", 690.0)), \
             patch("router.views.services.decode_polyline",
                   return_value=[(0.0, 0.0), (0.1, 0.1)]), \
             patch("router.views.services.project_stations",
                   side_effect=[[a, b], [a2, b2, c2]]), \
             patch("router.views.services.waypoint_route",
                   side_effect=[([305.0, 385.0, 12.0], "geomMicro"),
                                ([310.0, 375.0], "geomC")]) as mock_wp:
            result = views.compute_route("a", "b", 50.0, None)
        self.assertLessEqual(mock_wp.call_count, 2)
        # the beyond-noise cheaper set wins the single verification slot
        self.assertEqual([p["name"] for p in result["fuel_purchases"]], ["Sc"])
        # measured on C's legs: (68.5 - 50) gal at $2.20
        self.assertAlmostEqual(result["cash_paid_en_route"], 40.7, places=2)
        self.assertEqual(result["route_polyline"], "geomC")

    def test_refinement_failure_keeps_verified_plan(self):
        x = self.rst("x", 3.00, 300.0)
        with patch("router.views.services.geocode",
                   side_effect=[(0.0, 0.0, "A"), (1.0, 1.0, "B")]), \
             patch("router.views.services.driving_route",
                   return_value=("poly", 700.0)), \
             patch("router.views.services.decode_polyline",
                   return_value=[(0.0, 0.0), (0.1, 0.1)]), \
             patch("router.views.services.project_stations",
                   side_effect=[[x], services.ExternalServiceError("boom")]), \
             patch("router.views.services.waypoint_route",
                   return_value=([300.0, 400.0], "geom1")) as mock_wp:
            result = views.compute_route("a", "b", 50.0, None)
        self.assertEqual(mock_wp.call_count, 1)
        self.assertEqual([p["name"] for p in result["fuel_purchases"]], ["Sx"])
        self.assertAlmostEqual(result["cash_paid_en_route"], 60.0, places=2)


class ValuationTests(TestCase):
    """The starting fuel is valued at the nearest VERIFIED station-level
    stop's listed price - a named, checkable number from the supplied
    file, never an invented one."""

    @staticmethod
    def _st(id_, price, lat, lon, source="osm_exit_brand"):
        return Station(id=id_, name=f"S{id_}", address="", city="X",
                       state="KS", price=price, lat=lat, lon=lon,
                       coord_source=source)

    def test_nearest_station_level_wins_over_cheaper_far(self):
        near = self._st("near", 2.99, 0.0, 0.1)
        far_cheap = self._st("far", 2.00, 5.0, 5.0)
        picked = views.valuation_station([near, far_cheap], 0.0, 0.0)
        self.assertEqual(picked.id, "near")

    def test_centroid_rows_are_never_valuation_references(self):
        centroid_close_cheap = self._st("c", 1.00, 0.0, 0.01,
                                        source="city_centroid")
        grounded = self._st("g", 3.50, 0.0, 0.2)
        picked = views.valuation_station([centroid_close_cheap, grounded],
                                         0.0, 0.0)
        self.assertEqual(picked.id, "g")

    @patch("router.services.load_stations")
    def test_corridor_miss_falls_back_to_whole_file(self, mock_load):
        grounded = self._st("g", 3.10, 1.0, 1.0)
        mock_load.return_value = [grounded]
        picked = views.valuation_station([], 0.0, 0.0)
        self.assertEqual(picked.id, "g")


class ApiTests(TestCase):
    SHAPE = {
        "start": {"query": "a", "resolved": "a", "lat": 1, "lon": 1},
        "finish": {"query": "b", "resolved": "b", "lat": 2, "lon": 2},
        "total_distance_miles": 300.0,
        "vehicle": {"range_miles": 500.0, "planning_range_miles": 480.0, "mpg": 10.0},
        "fuel_purchases": [{"name": "S", "address": "",
                            "city": "X", "state": "KS", "price_per_gallon": 3.1,
                            "approx_lat": 1, "approx_lon": 1, "route_mile": 5.0,
                            "gallons_purchased": 30.0, "cost": 93.0,
                            "fuel_before_gallons": 20.0,
                            "fuel_after_gallons": 50.0}],
        "fuel_purchased_gallons": 30.0,
        "fuel_consumed_gallons": 80.0,
        "cash_paid_en_route": 93.0,
        "starting_fuel": {"gallons": 50.0, "price_per_gallon": None, "cost": None},
        "total_money_spent": None,
        "money_note": "note",
        "cost_model_note": "note",
        "route_polyline": "_p~iF~ps|U",
    }

    def test_starting_fuel_valued_only_when_consumed(self):
        """A 240-mile trip consumes 24 of the 50 starting gallons: priced
        at $3.00 the starting fuel contributes $72, not $150 - the
        remaining 26 gallons are still in the tank and are not trip
        cost. Unpriced, the value is None and no price is invented."""
        consumed, value = views.starting_fuel_value(50.0, 24.0, 3.00)
        self.assertAlmostEqual(consumed, 24.0)
        self.assertAlmostEqual(value, 72.0)
        consumed, value = views.starting_fuel_value(50.0, 128.31, 3.00)
        self.assertAlmostEqual(consumed, 50.0)
        self.assertAlmostEqual(value, 150.0)
        consumed, value = views.starting_fuel_value(50.0, 24.0, None)
        self.assertIsNone(value)

    def test_missing_fields_400(self):
        resp = self.client.post("/api/route-fuel/", {"start": ""}, "application/json")
        self.assertEqual(resp.status_code, 400)

    def test_starting_fuel_over_capacity_400(self):
        resp = self.client.post(
            "/api/route-fuel/",
            {"start": "a", "finish": "b", "starting_fuel_gallons": 51.0},
            "application/json")
        self.assertEqual(resp.status_code, 400)

    def test_starting_fuel_price_must_be_positive(self):
        resp = self.client.post(
            "/api/route-fuel/",
            {"start": "a", "finish": "b", "starting_fuel_price_per_gallon": 0},
            "application/json")
        self.assertEqual(resp.status_code, 400)

    @patch("router.views.compute_route")
    def test_success_response_shape(self, mock_compute):
        mock_compute.return_value = dict(self.SHAPE)
        resp = self.client.post("/api/route-fuel/", {"start": "a", "finish": "b"},
                                "application/json")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        for key in ("total_distance_miles", "fuel_purchases", "cash_paid_en_route",
                    "total_money_spent", "route_polyline", "vehicle"):
            self.assertIn(key, body)
        self.assertFalse(body["cached"])

    @patch("router.views.compute_route")
    def test_repeat_request_served_from_cache(self, mock_compute):
        mock_compute.return_value = dict(self.SHAPE)
        self.client.post("/api/route-fuel/", {"start": "x", "finish": "y"}, "application/json")
        resp = self.client.post("/api/route-fuel/", {"start": "x", "finish": "y"}, "application/json")
        self.assertTrue(resp.json()["cached"])
        self.assertEqual(mock_compute.call_count, 1)


class PriceSelectionAndSourceAccountingTests(TestCase):
    """Repeated source ids, price policy and source-id completeness."""

    def test_select_price_policies(self):
        raw = [3.0, 3.4, 3.2, 3.2]
        for pol, want in [("mean", 3.2), ("median", 3.2), ("min", 3.0),
                          ("max", 3.4), ("first", 3.0), ("last", 3.2)]:
            with self.settings(PRICE_SELECTION=pol):
                self.assertAlmostEqual(services.select_price(raw), want)

    def test_mean_is_order_independent(self):
        with self.settings(PRICE_SELECTION="mean"):
            self.assertAlmostEqual(services.select_price([3.0, 3.6]),
                                   services.select_price([3.6, 3.0]))

    def test_loader_one_station_per_id_with_raw_prices(self):
        import csv
        from collections import defaultdict
        from django.conf import settings
        src = defaultdict(list)
        with open(settings.BASE_DIR / "fuel-prices-for-be-assessment.csv") as fh:
            for r in csv.DictReader(fh):
                src[r["OPIS Truckstop ID"].strip()].append(float(r["Retail Price"]))
        services._STATIONS = None
        try:
            with self.settings(PRICE_SELECTION="mean"):
                stations = services.load_stations()
            self.assertEqual({s.id for s in stations}, set(src))
            self.assertEqual(len(stations), len(src))
            for s in stations:
                e = src[s.id]
                self.assertAlmostEqual(s.price, sum(e) / len(e), places=9)
                if len(set(e)) > 1:
                    self.assertEqual(list(s.price_variants), e)
                else:
                    self.assertFalse(s.price_variants)
        finally:
            services._STATIONS = None

    def test_unlocated_never_selectable(self):
        self.assertNotIn("unlocated", services.STATION_LEVEL_SOURCES)
        self.assertNotIn("city_centroid", services.STATION_LEVEL_SOURCES)


class PricePolicyEndToEndTests(TestCase):
    """The chosen price policy must drive optimisation, purchase records,
    cash totals and the disclosure note, not only DB ingestion."""

    ROWS = [{"id": "x1", "name": "X", "address": "", "city": "C", "state": "KS",
             "price": 3.0, "lat": 0.05, "lon": 0.05,
             "coord_source": "osm_exit_brand", "price_variants": [3.0, 3.6]}]

    def _run(self, policy):
        services._STATIONS = None
        try:
            with self.settings(PRICE_SELECTION=policy), \
                 patch("router.services.json.load", return_value=[dict(r) for r in self.ROWS]):
                cands = services.load_stations()
                for c in cands:
                    c.route_mile = 300.0
                with patch("router.views.services.geocode",
                           side_effect=[(0.0, 0.0, "A"), (1.0, 1.0, "B")]), \
                     patch("router.views.services.driving_route",
                           return_value=("poly", 700.0)), \
                     patch("router.views.services.decode_polyline",
                           return_value=[(0.0, 0.0), (0.1, 0.1)]), \
                     patch("router.views.services.project_stations",
                           return_value=cands), \
                     patch("router.views.services.waypoint_route",
                           return_value=([300.0, 400.0], "g")):
                    return views.compute_route("a", "b", 50.0, None)
        finally:
            services._STATIONS = None

    def test_policy_changes_price_cash_and_note(self):
        mean = self._run("mean")
        first = self._run("first")
        pm, pf = mean["fuel_purchases"][0], first["fuel_purchases"][0]
        self.assertAlmostEqual(pm["price_per_gallon"], 3.3, places=6)
        self.assertAlmostEqual(pf["price_per_gallon"], 3.0, places=6)
        # 700 mi at 10 mpg = 70 gal; 50 in tank, so 20 gal bought here.
        self.assertAlmostEqual(mean["cash_paid_en_route"], 20 * 3.3, places=2)
        self.assertAlmostEqual(first["cash_paid_en_route"], 60.0, places=2)
        self.assertEqual(pm["price_variants"], [3.0, 3.6])
        self.assertIn("settings.PRICE_SELECTION (currently 'mean')",
                      mean["price_units_note"])
        self.assertIn("(currently 'first')", first["price_units_note"])
        self.assertIn("not employer-confirmed", mean["price_units_note"])
        self.assertIn("not an employer requirement", mean["price_units_note"])


class ExitLevelTierTests(TestCase):
    def test_exit_level_labelled_and_switchable(self):
        self.assertEqual(services.position_level("osm_exit_junction"), "exit")
        self.assertEqual(services.position_level("osm_exit_brand"), "station")
        self.assertNotIn("city_centroid", services.EXIT_LEVEL_SOURCES)
        # default OFF: exit-level rows are discovery candidates only
        self.assertFalse(services.EXIT_LEVEL_SOURCES & services.STATION_LEVEL_SOURCES)

    def test_exit_level_rows_in_data_carry_evidence(self):
        import json
        from django.conf import settings
        rows = json.load(open(settings.BASE_DIR / "router/data/stations_geocoded.json"))
        ex = [r for r in rows if r["coord_source"] == "osm_exit_junction"]
        self.assertTrue(ex)
        for r in ex:
            self.assertIn("EXIT-LEVEL", r["coord_detail"])
            self.assertIn("EXIT", r["address"].upper())


class PartialFuelDetourExactnessTests(TestCase):
    """Regression for a transition reduction that dropped valid cheaper
    states when the start tank was partial and stations had detours (the
    earlier validators only covered a full tank and zero detours).
    scripts/validate_partial_fuel_detours.py is the randomised version."""

    def test_partial_start_fuel_with_detours_finds_the_cheaper_plan(self):
        # Found by the independent baseline: 17.7 gal at the start, a 40 gal
        # tank, stops at mile 22 ($3.19, 12 mi detour) and mile 156
        # ($3.97, 3 mi detour), destination at 235. The best plan costs
        # 25.408; the old reduction returned 26.158.
        stations = [st("a", 3.19, 22.0, 12.0), st("b", 3.97, 156.0, 3.0)]
        plan = services.plan_purchases(stations, 235.0, 400.0, 10.0, 17.7)
        self.assertAlmostEqual(sum(p.cost for p in plan), 25.408, places=3)

    def test_second_case_two_detoured_stops(self):
        stations = [st("a", 3.08, 96.0, 8.0), st("b", 3.45, 181.0, 3.0)]
        plan = services.plan_purchases(stations, 311.0, 400.0, 10.0, 26.7)
        self.assertAlmostEqual(sum(p.cost for p in plan), 17.25, places=3)


class AuditReproTests(TestCase):
    def test_independent_audit_case_partial_fuel_with_detours(self):
        # From the independent audit: 21.5 gal start, 30 gal tank, six detoured
        # stops. The true optimum is 34.995 (fill 10.15 gal at the first
        # stop, then 1.85 gal at the fifth); the old reduction paid 35.67.
        rows = [(15, 2.87, 1.5), (28, 4.18, 5.9), (72, 3.59, 8.0),
                (125, 2.84, 4.8), (148, 3.17, 0.5), (220, 3.22, 0.5)]
        stations = [st(str(i), p, float(m), o) for i, (m, p, o) in enumerate(rows)]
        plan = services.plan_purchases(stations, 331.0, 300.0, 10.0, 21.5)
        self.assertAlmostEqual(sum(p.cost for p in plan), 34.995, places=3)


class SharedStateTests(TestCase):
    def test_project_stations_returns_copies_and_leaves_shared_rows_alone(self):
        shared = [Station(id="1", name="S", address="", city="X", state="KS",
                          price=3.0, lat=0.0, lon=0.5, coord_source="chain_locator")]
        pts = [(0.0, 0.0), (0.0, 0.5), (0.0, 1.0)]
        mk = [0.0, 34.5, 69.0]
        with patch("router.services.load_stations", return_value=shared):
            got = services.project_stations(pts, mk, 50.0)
        self.assertEqual(len(got), 1)
        self.assertIsNot(got[0], shared[0])
        self.assertGreater(got[0].route_mile, 0.0)
        self.assertEqual(shared[0].route_mile, 0.0)
        self.assertEqual(shared[0].off_route_miles, 0.0)


class NominatimPolicyTests(TestCase):
    def test_requests_are_spaced_and_repeats_are_cached(self):
        from django.core.cache import cache
        cache.clear()

        class Resp:
            status_code = 200
            def json(self):
                return [{"lat": "1.0", "lon": "2.0", "display_name": "X"}]

        services._nominatim_last_start = 0.0
        with patch("router.services.requests.get", return_value=Resp()) as g, \
             patch("router.services.time.sleep") as slept:
            services.geocode("Place One")
            services.geocode("Place Two")
            services.geocode("place   one")  # same query, cached
        self.assertEqual(g.call_count, 2)
        self.assertEqual(slept.call_count, 1)
        self.assertGreater(slept.call_args[0][0], 0.5)
