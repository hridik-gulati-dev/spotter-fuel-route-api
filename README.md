# Fuel Route API

Django REST API for planning a driving route between US locations, choosing fuel stops and reporting fuel cost. The vehicle has a 500-mile range and uses 10 miles per gallon.

## Run locally

Use Python 3.12 or newer for the newer Django pin in `constraints.txt`. Python 3.10/3.11 use its Django 5.2 LTS pin instead.

From the project directory on macOS or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -c constraints.txt
python manage.py check
python manage.py runserver
```

The server starts at `http://127.0.0.1:8000/`. Open `http://127.0.0.1:8000/map/` to enter the locations and see the route and fuel stops.

No API keys are needed. An internet connection is required for geocoding, routing and map tiles. Keep both the supplied CSV and `router/data/stations_geocoded.json` in their existing paths. There are no project models to migrate.

For an unpinned install, use `python -m pip install -r requirements.txt` instead. The constraints file records reproducible dependency versions; it does not track future Django releases automatically.

## Call the API

Send a POST request to `http://127.0.0.1:8000/api/route-fuel/` with the `Content-Type: application/json` header:

```json
{
  "start": "New York, NY",
  "finish": "Miami, FL"
}
```

Or run:

```bash
curl -X POST http://127.0.0.1:8000/api/route-fuel/ \
  -H 'Content-Type: application/json' \
  -d '{"start":"New York, NY","finish":"Miami, FL"}'
```

The response includes resolved locations, direct and driven distances, route polylines, fuel purchases, fuel consumed, pump payments, starting-fuel valuation and range checks. Check the resolved locations, since geocoding can match similar names.

Optional inputs:

- `starting_fuel_gallons`: initial fuel from 0 to 50 gallons, default 50.
- `starting_fuel_price_per_gallon`: price used to value consumed starting fuel. If omitted, a nearest selectable station supplies a reference price.

`cash_paid_en_route` is the sum of fuel purchases during the trip. `total_money_spent` also includes the value of starting fuel consumed. A short trip can therefore have zero pump payments while still consuming fuel with a reported value.

## Tests

```bash
python manage.py test
```

The 48-test suite mocks external services. It checks API behavior, fuel inventory and cost accounting, stop selection, routing-call limits, failure cases and refinement. It does not establish live service availability or validate every station coordinate.

Optional independent planner checks:

```bash
python scripts/validate_optimizer.py
python scripts/validate_selection.py
python scripts/validate_partial_fuel_detours.py
```

## Implementation and limits

- Nominatim resolves the endpoints. OSRM supplies driving routes. The routing budget is at most three routing calls per uncached request, separate from geocoding calls.
- Candidate stops are selected near the route, then a waypoint route through the selected stops checks actual driven legs and fuel use. Planning uses a reserve below the 500-mile vehicle range. A bounded refinement pass can compare another stop set.
- Stop and purchase optimization is within the candidate/planning model, not a guarantee of the cheapest physical route across every possible station.
- Only station-level coordinate tiers are selectable by default. City centroids are excluded. Exit-level junction positions are also excluded unless `SPOTTER_ALLOW_EXIT_LEVEL=1` is explicitly set. Coordinates have differing evidence quality and are not all individually verified.
- The supplied fuel-price CSV lacks coordinates and has incomplete geographic coverage. Some routes return HTTP 422 because the selectable data and planning limits do not yield a verified plan. That does not mean the real trip is impossible.
- Missing or invalid inputs return HTTP 400. Use clear US place names and inspect the resolved endpoints. The US geocoding filter does not reject every foreign place name.
- Repeated station IDs use the mean listed price by default. `SPOTTER_PRICE_SELECTION` can select another supported policy. The supplied prices have no dates, and their currency/unit convention is not confirmed. Prices are used as provided, without cross-border conversion.
- Successful results are cached in memory for 24 hours per process. Restarting clears the cache. Public routing/geocoding services have availability and rate limits. The geocoding rate limiter is per process; a multi-worker deployment needs a shared limiter.
- This is a local assessment server, not a production deployment. The development secret key, debug setting and allowed hosts must be changed before deployment.

## Project layout

- `router/services.py`: geocoding, routing and fuel planner.
- `router/views.py`: API, verification, refinement and accounting.
- `router/templates/router/map.html`: map page.
- `router/tests.py`: offline tests.
- `router/data/stations_geocoded.json`: prepared station locations.
- `fuelroute/`: Django settings and URLs.
- `scripts/`: validation and profiling utilities.

## Data attribution

- Fuel prices: CSV provided with the assessment.
- City coordinates: GeoNames, CC BY 4.0.
- Station and road/exit records: OpenStreetMap contributors, ODbL.
- Map display: Leaflet and OpenStreetMap.

The supplied assessment data and derived station file are included for assessment review. No separate redistribution license for the supplied CSV is asserted.
