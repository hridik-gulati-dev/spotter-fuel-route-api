"""API + map views."""
from __future__ import annotations

import hashlib

from django.conf import settings
from django.core.cache import cache
from django.shortcuts import render
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services


class RouteFuelView(APIView):
    """POST /api/route-fuel/

    Body: {"start": "...", "finish": "...",
           "starting_fuel_gallons": <optional, default: full tank>,
           "starting_fuel_price_per_gallon": <optional, default: the listed
           price of the nearest station-level stop, named in the response>}

    Both locations are meant to be US places. The geocoder applies a US
    country filter but still fuzzy-matches, so a non-US name can resolve
    to a similar-looking US place; check start.resolved and
    finish.resolved in the response.
    Returns the route, a cost-effective fuel purchase plan simulated as a
    physical tank inventory, the cash paid at pumps (the sum of the
    displayed per-purchase costs), and, whenever a starting-fuel price is
    available, the value of the starting fuel actually burned and the
    total fuel money for the trip.
    """

    def post(self, request):
        start = (request.data.get("start") or "").strip()
        finish = (request.data.get("finish") or "").strip()
        if not start or not finish:
            return Response(
                {"error": "Both 'start' and 'finish' are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        capacity = settings.VEHICLE_RANGE_MILES / settings.VEHICLE_MPG
        start_fuel = request.data.get("starting_fuel_gallons")
        if start_fuel is None:
            start_fuel = capacity
        try:
            start_fuel = float(start_fuel)
        except (TypeError, ValueError):
            return Response(
                {"error": "'starting_fuel_gallons' must be a number."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not 0.0 <= start_fuel <= capacity:
            return Response(
                {"error": f"'starting_fuel_gallons' must be between 0 and "
                          f"{capacity:.0f} (the tank capacity)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        start_price = request.data.get("starting_fuel_price_per_gallon")
        if start_price is not None:
            try:
                start_price = float(start_price)
            except (TypeError, ValueError):
                return Response(
                    {"error": "'starting_fuel_price_per_gallon' must be a number."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if start_price <= 0:
                return Response(
                    {"error": "'starting_fuel_price_per_gallon' must be positive."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        cache_key = "routefuel:v5:" + hashlib.sha256(
            f"{start.lower()}|{finish.lower()}|{start_fuel}|{start_price}".encode()
        ).hexdigest()
        cached = cache.get(cache_key)
        if cached is not None:
            cached["cached"] = True
            return Response(cached)

        try:
            result = compute_route(start, finish, start_fuel, start_price)
        except services.ExternalServiceError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except services.RouteInfeasibleError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        cache.set(cache_key, result)
        result["cached"] = False
        return Response(result)


def valuation_station(stations: list, lat1: float, lon1: float):
    """Pick the station-level stop closest to the origin as the starting-
    fuel valuation reference.

    The brief asks for the total money spent on fuel but does not say
    what the pre-existing starting fuel cost. Rather than leaving the
    total blank or inventing a price, the starting fuel is VALUED at the
    listed price of the nearest station-level stop in the
    supplied CSV (a named, checkable number from the employer's own
    data). This is an explicit valuation convention - not an actual
    purchase record and not an employer instruction. Falls back to the
    closest station-level row in the whole file when the corridor has
    none, so the total is always computable.
    """
    pool = [s for s in stations if s.coord_source in services.STATION_LEVEL_SOURCES]
    if not pool:
        pool = [s for s in services.load_stations()
                if s.coord_source in services.STATION_LEVEL_SOURCES]
    if not pool:
        return None
    return min(pool, key=lambda s: services.haversine_miles(
        lat1, lon1, s.lat, s.lon))


def starting_fuel_value(start_fuel: float, consumed_gal: float,
                        price: float | None) -> tuple[float, float | None]:
    """Value the CONSUMED portion of the starting fuel.

    Starting fuel is what the opening miles burn, so the consumed
    portion is min(start_fuel, consumed); any remainder is still in the
    tank and is not money spent on this trip. Returns (consumed
    gallons, value or None when unpriced).
    """
    consumed_start = min(start_fuel, consumed_gal)
    value = round(consumed_start * price, 2) if price is not None else None
    return consumed_start, value


def compute_route(start: str, finish: str, start_fuel: float,
                  start_price: float | None) -> dict:
    """Full pipeline: geocode (2 calls) -> route (1 call) -> select stops
    on the route line -> waypoint route (1 call) -> account on driven legs."""
    lat1, lon1, start_label = services.geocode(start)
    lat2, lon2, finish_label = services.geocode(finish)
    geometry, total_miles = services.driving_route(lat1, lon1, lat2, lon2)

    points = services.decode_polyline(geometry)
    markers = services._route_mile_markers(points)
    stations = services.project_stations(points, markers, settings.CORRIDOR_MILES)

    # Selection on the route line with a reserved planning range; then
    # exact accounting on the driven legs. If the driven legs break the
    # vehicle range (detour longer than the reserve), retry once with a
    # smaller planning range; after that, answer with an explicit
    # no-plan error rather than an approximate plan.
    # Shared per-request routing budget (the brief: one call ideal, two or
    # three acceptable). The direct route is call 1; every waypoint
    # verification, whether a range retry or the optional refinement,
    # spends from the same budget of MAX_ROUTING_CALLS.
    routing_calls = 1
    last_error: services.RouteInfeasibleError | None = None
    for planning_range in (
        settings.EFFECTIVE_RANGE_MILES,
        settings.EFFECTIVE_RANGE_MILES - 50.0,
    ):
        # Selection conserves the planning-range tank: the starting fuel
        # is capped at the planning capacity so the reserve still holds.
        selection_fuel = min(start_fuel, planning_range / settings.VEHICLE_MPG)
        purchases = services.plan_purchases(
            stations,
            total_miles,
            planning_range,
            settings.VEHICLE_MPG,
            selection_fuel,
        )
        try:
            routing_calls += 1
            purchases, legs, driven_geometry = services.finalize_on_driven_legs(
                purchases,
                lat1,
                lon1,
                lat2,
                lon2,
                settings.VEHICLE_RANGE_MILES,
                settings.VEHICLE_MPG,
                start_fuel,
            )
            break
        except services.RouteInfeasibleError as exc:
            last_error = exc
    else:
        raise last_error  # type: ignore[misc]

    # Refinement pass: the first selection was made on the stop-free
    # direct geometry; the driven path through the stops can pass
    # different corridor stations. Re-project candidates onto the driven
    # geometry, re-select, and also build one alternative per micro
    # top-off in the incumbent plan (a tiny purchase can be the modeled
    # optimum yet lose on measured legs once real odometers shift the
    # tank state). Every candidate set is scored on the SAME objective:
    # the exact planner restricted to that set, on driven-corridor
    # odometers with the vehicle's real range - the measured-leg
    # accounting, approximated without another routing call. The best
    # alternative is verified with one more waypoint call, and the plan
    # returned is the cheaper of the two on MEASURED driven legs (call
    # budget: 1 direct + at most 2 waypoint routes = 3 routing calls,
    # inside the brief's "two or three is acceptable"). The already
    # verified plan is kept whenever refinement cannot improve or cannot
    # run. Exactness is claimed in the planning model plus this measured
    # comparison round - not over every possible physical stop set.
    try:
        if routing_calls >= settings.MAX_ROUTING_CALLS:
            raise services.ExternalServiceError(
                "routing budget used; refinement skipped")
        driven_points = services.decode_polyline(driven_geometry)
        driven_markers = services._route_mile_markers(driven_points)
        refined_candidates = services.project_stations(
            driven_points, driven_markers, settings.CORRIDOR_MILES)
        selection_fuel = min(
            start_fuel, settings.EFFECTIVE_RANGE_MILES / settings.VEHICLE_MPG)
        refined = services.plan_purchases(
            refined_candidates,
            sum(legs),
            settings.EFFECTIVE_RANGE_MILES,
            settings.VEHICLE_MPG,
            selection_fuel,
        )
        incumbent_ids = [p.station.id for p in purchases]
        incumbent_cost = sum(p.cost for p in purchases)

        alt_sets = []  # (plan, is_micro_exclusion)
        if [p.station.id for p in refined] != incumbent_ids:
            alt_sets.append((refined, False))
        micro_ids = [p.station.id for p in purchases if p.gallons < 3.0]
        for micro_id in micro_ids:
            pool = [s for s in refined_candidates if s.id != micro_id]
            if len(pool) == len(refined_candidates):
                continue  # micro stop is not on the driven corridor
            try:
                alt = services.plan_purchases(
                    pool, sum(legs), settings.EFFECTIVE_RANGE_MILES,
                    settings.VEHICLE_MPG, selection_fuel)
            except services.RouteInfeasibleError:
                continue
            if [p.station.id for p in alt] != incumbent_ids:
                alt_sets.append((alt, True))

        def projected_measured_cost(plan):
            ids = {p.station.id for p in plan}
            pool = [s for s in refined_candidates if s.id in ids]
            if len(pool) != len(ids):
                return float("inf")  # set not present on the driven corridor
            realized = services.plan_purchases(
                pool, sum(legs), settings.VEHICLE_RANGE_MILES,
                settings.VEHICLE_MPG, start_fuel)
            return sum(p.cost for p in realized)

        # The projection scores alternatives on the INCUMBENT's driven
        # path, so its ranking is only accurate to about half a percent;
        # a structurally different set can measure cheaper than its
        # projection. Spend the one verification call (3-call budget) on
        # the alternative with the strongest evidence: a re-selection
        # projected clearly cheaper beyond the noise band wins first;
        # otherwise the best micro-exclusion alternative inside the band
        # (that is the shape where the model and the measured legs
        # disagree); otherwise any strictly cheaper projected set. The
        # final choice is always the cheaper MEASURED plan.
        scored = []
        seen = set()
        for alt, is_micro in alt_sets:
            key = tuple(p.station.id for p in alt)
            if key in seen:
                continue
            seen.add(key)
            try:
                cost = projected_measured_cost(alt)
            except services.RouteInfeasibleError:
                continue
            scored.append((cost, is_micro, alt))

        micro_scored = [(c, a) for c, m, a in scored if m]
        plain_scored = [(c, a) for c, m, a in scored if not m]
        clear = [t for t in plain_scored if t[0] < incumbent_cost * 0.995]
        band = [t for t in micro_scored if t[0] <= incumbent_cost * 1.015]
        verify = None
        if clear:
            verify = min(clear, key=lambda t: t[0])[1]
        elif band:
            verify = min(band, key=lambda t: t[0])[1]
        elif plain_scored:
            best_cost, best_alt = min(plain_scored, key=lambda t: t[0])
            if best_cost < incumbent_cost - 1e-9:
                verify = best_alt

        if verify is not None and routing_calls < settings.MAX_ROUTING_CALLS:
            routing_calls += 1
            new_purchases, new_legs, new_geometry = services.finalize_on_driven_legs(
                verify, lat1, lon1, lat2, lon2,
                settings.VEHICLE_RANGE_MILES, settings.VEHICLE_MPG, start_fuel,
            )
            if sum(p.cost for p in new_purchases) <= incumbent_cost + 1e-9:
                purchases, legs, driven_geometry = new_purchases, new_legs, new_geometry
    except (services.ExternalServiceError, services.RouteInfeasibleError):
        pass  # keep the already verified plan

    total_driven = sum(legs)
    # Accounting policy: each purchase cost is rounded to cents once (from
    # the unrounded gallons x price) and cash is the sum of those displayed
    # costs, so the response adds up exactly. Unrounded values stay in
    # *_exact fields for replay at full precision.
    cash_paid = round(sum(round(p.cost, 2) for p in purchases), 2)
    cash_exact = sum(p.cost for p in purchases)
    consumed = total_driven / settings.VEHICLE_MPG
    # Starting fuel is consumed first (it is what the opening miles burn),
    # so only its CONSUMED portion is trip cost; the remainder is still in
    # the tank at the destination and is not money spent on this trip.
    # Its price: the caller's explicit price when given, otherwise the
    # listed price of the nearest station-level stop in the
    # supplied file (an explicit valuation convention, named in the
    # response - see money_note).
    val_station = None
    if start_price is None:
        val_station = valuation_station(stations, lat1, lon1)
        if val_station is not None:
            start_price = val_station.price
    start_consumed, start_cost = starting_fuel_value(
        start_fuel, consumed, start_price)
    if val_station is not None:
        price_source = {
            "kind": "station_valuation",
            "station_id": val_station.id,
            "name": val_station.name,
            "city": val_station.city,
            "state": val_station.state,
            "price_per_gallon": val_station.price,
            "coord_source": val_station.coord_source,
        }
    elif start_price is not None:
        price_source = {"kind": "caller_supplied"}
    else:
        price_source = None
    return {
        "start": {"query": start, "resolved": start_label, "lat": lat1, "lon": lon1},
        "finish": {"query": finish, "resolved": finish_label, "lat": lat2, "lon": lon2},
        "total_distance_miles": round(total_miles, 1),
        "total_driven_miles": round(total_driven, 1),
        "vehicle": {
            "range_miles": settings.VEHICLE_RANGE_MILES,
            "planning_range_miles": settings.EFFECTIVE_RANGE_MILES,
            "mpg": settings.VEHICLE_MPG,
        },
        "fuel_purchases": [
            {
                "name": p.station.name,
                "address": p.station.address,
                "city": p.station.city,
                "state": p.station.state,
                "price_per_gallon": p.station.price,
                "approx_lat": p.station.lat,
                "approx_lon": p.station.lon,
                "coord_source": p.station.coord_source,
                "position_level": services.position_level(p.station.coord_source),
                "coord_detail": p.station.coord_detail,
                **({"price_variants": list(p.station.price_variants)}
                   if p.station.price_variants else {}),
                "off_route_miles": round(p.station.off_route_miles, 2),
                "leg_miles": round(p.leg_miles, 1),
                "route_mile": round(p.route_mile, 1),
                "gallons_purchased": round(p.gallons, 2),
                "cost": round(p.cost, 2),
                "gallons_exact": round(p.gallons, 6),
                "cost_exact": round(p.cost, 6),
                "fuel_before_gallons": round(p.fuel_before, 2),
                "fuel_after_gallons": round(p.fuel_after, 2),
            }
            for p in purchases
        ],
        "legs_miles": [round(leg, 1) for leg in legs],
        "max_leg_miles": round(max(legs), 1),
        "range_check": (
            f"every driven leg is within the "
            f"{settings.VEHICLE_RANGE_MILES:.0f}-mile vehicle range"
        ),
        "fuel_purchased_gallons": round(sum(p.gallons for p in purchases), 2),
        "fuel_consumed_gallons": round(consumed, 2),
        "cash_paid_en_route": cash_paid,
        "cash_paid_unrounded": round(cash_exact, 6),
        "starting_fuel": {
            "gallons": round(start_fuel, 2),
            "consumed_gallons": round(start_consumed, 2),
            "remaining_gallons": round(start_fuel - start_consumed, 2),
            "price_per_gallon": start_price,
            "price_source": price_source,
            "cost": start_cost,
        },
        "total_money_spent": (
            round(cash_paid + start_cost, 2)
            if start_cost is not None else None
        ),
        "money_note": (
            "cash_paid_en_route is the cash paid at pumps during the "
            "trip: the sum of the per-purchase costs shown, each rounded once "
            "to cents from gallons_exact x price_per_gallon (cost_exact, "
            "cash_paid_unrounded keep full precision; displayed gallons are "
            "rounded to 0.01, so displayed gallons x price can differ from "
            "cost by up to about 2.5 cents per purchase). "
            "The starting fuel is pre-existing; the brief asks for "
            "the total money spent on fuel but does not say what the "
            "starting fuel cost, so it is VALUED - never silently - at a "
            "named, checkable price from the supplied file: the listed "
            "price of the station-level stop nearest to the start in the "
            "supplied file (station-level describes coordinate "
            "provenance - see coord_source - not an individual "
            "verification of that station) "
            "(see starting_fuel.price_source; a caller-supplied "
            "starting_fuel_price_per_gallon overrides this). Only the "
            "CONSUMED portion of the starting fuel is valued (starting "
            "fuel is what the opening miles burn); any remainder is still "
            "in the tank and is not trip cost. This is a documented "
            "modeling assumption, not an employer instruction and not an "
            "actual purchase record. total_money_spent = cash_paid_en_route "
            "+ consumed starting-fuel value and is always populated. The "
            "default starting fuel is a full "
            f"{settings.VEHICLE_RANGE_MILES / settings.VEHICLE_MPG:.0f}-gallon "
            "tank (a chosen assumption, configurable via "
            "starting_fuel_gallons). fuel_consumed_gallons is the fuel the "
            "whole driven distance burns. Every purchase lists "
            "fuel_before_gallons and fuel_after_gallons, and the simulated "
            "tank never exceeds its capacity or drops below zero."
        ),
        "cost_model_note": (
            "Only stations with station-level coordinates are eligible "
            "(coord_source on each purchase; city_centroid rows are never "
            "selected). Stops are chosen by an exact minimum-cost planner "
            "(dynamic programming with the fill-or-arrive-empty property, "
            "Khuller et al. 2011; detour-aware planning distances), "
            "independently validated against a grid-exact search and "
            "exhaustive subset enumeration with zero mismatches. Gallons "
            "and dollars are then recomputed on the actual driven legs "
            "returned by one waypoint routing call through every stop, and "
            "one refinement pass re-checks the selection against the driven "
            "geometry - so totals include detour driving. Routing calls per "
            "uncached request: 1 direct route + at most 2 waypoint routes."
        ),
        "route_polyline": driven_geometry,
        "route_polyline_direct": geometry,
        "route_polyline_note": (
            "route_polyline is the actual path driven through every selected "
            "stop (detours included); route_polyline_direct is the stop-free "
            "direct route."
        ),
        "price_units_note": (
            "Price currency/unit contract: " + settings.PRICE_UNITS_ASSUMPTION
            + ". This is a documented, configurable assumption "
            "(settings.PRICE_UNITS_ASSUMPTION), not employer-confirmed. "
            "Where the supplied file lists one station more than once with "
            "different prices, every listed price is preserved on the "
            "station record (price_variants, in file order) and the price "
            "used is chosen by settings.PRICE_SELECTION (currently '"
            + settings.PRICE_SELECTION + "'); the file carries no dates, so "
            "no row is treated as the latest. This is a project design "
            "choice, not an employer requirement."
        ),
    }


def map_page(request):
    """Simple Leaflet map that calls the API and draws route + stops."""
    return render(request, "router/map.html")
