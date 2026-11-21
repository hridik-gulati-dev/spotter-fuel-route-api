"""Minimal Django settings for the Spotter fuel-route API.

No database-backed models are used; SQLite is kept only so the default
Django check/test machinery has a configured database.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "dev-only-not-for-production"
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "rest_framework",
    "router",
]

MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "fuelroute.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    }
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# In-memory cache: repeated identical route requests are served instantly.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "TIMEOUT": 60 * 60 * 24,
    }
}

# External free services (no API keys required)
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_MIN_INTERVAL_SECONDS = 1.1  # public Nominatim policy: max 1 request/second
NOMINATIM_USER_AGENT = "spotter-fuel-route-assessment/1.0 (candidate project)"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
OSRM_WAYPOINT_URL = "https://router.project-osrm.org/route/v1/driving"

# Vehicle assumptions from the assignment
VEHICLE_RANGE_MILES = 500.0
VEHICLE_MPG = 10.0
# Planning uses a reduced effective range: the reserve absorbs city-level
# station-coordinate error and small detours to reach a stop.
EFFECTIVE_RANGE_MILES = 450.0  # planning range; 50 mi reserve for detours

# Price currency/unit contract. The supplied CSV states neither a currency
# nor a unit for Retail Price, and the assignment text does not specify one.
# All prices are therefore used EXACTLY AS GIVEN, in one shared unit space,
# with no conversion between US and Canadian rows. The identification of the
# file as an OPIS truckstop export (OPIS Truckstop ID column; OPIS describes
# this report family as per-gallon across the US and Canada) supports - but
# does not prove - per-gallon pricing in a single currency. Change this
# setting only with source confirmation; it is a disclosure, not a fact.
PRICE_UNITS_ASSUMPTION = (
    "prices used as provided in the supplied CSV; single shared currency "
    "per gallon, no cross-border conversion applied (source does not state "
    "currency or unit)"
)

# Fuel-stop search parameters
CORRIDOR_MILES = 10.0       # how far off the route a station may be

# Price chosen when the supplied file lists one station id on several rows
# with different prices (the file has no dates). One of: mean (default),
# median, min, max, first, last. A design choice of this project, not an
# employer requirement; every raw price stays on the station record.
PRICE_SELECTION = os.environ.get("SPOTTER_PRICE_SELECTION", "mean")

# OFF by default. An exit-level position (OSM junction named by the CSV
# address) is a candidate-discovery aid, not a fuel station; measured error
# reaches 12.6 mi, so it is not emitted as a purchase stop unless this is
# switched on explicitly. Not an employer requirement.
ALLOW_EXIT_LEVEL_STOPS = os.environ.get("SPOTTER_ALLOW_EXIT_LEVEL", "0") == "1"

# Per-request ceiling on calls to the routing API (direct + waypoint
# verifications, including the range retry and optional refinement).
MAX_ROUTING_CALLS = 3
