#!/usr/bin/env python3
"""Optional external validation (no sample data is stored in this repo).

Downloads OPIS's publicly posted Truckstop Spread Report sample at run time
and compares its LATITUDE/LONGITUDE for ids that also appear in our station
data with our coordinates, grouped by coord_source. The sample is OPIS
marketing material; this script only reads it and keeps nothing.
A match between ids/names shows a relationship to that feed, not how the
supplied CSV was produced. Needs the `pdftotext` binary (poppler).

Usage: python scripts/validate_against_opis_sample.py
"""
import json
import math
import re
import subprocess
import sys
import tempfile
import urllib.request
from collections import defaultdict
from pathlib import Path

URL = ("https://info.opisnet.com/hubfs/Product%20Campaigns/Truckstop%20Spread"
       "%20Report/OPIS%20Truckstop%20Spread%20Report%20Sample.pdf")
DATA = Path(__file__).resolve().parent.parent / "router/data/stations_geocoded.json"


def hav(a, b, c, d):
    p = math.radians
    x = math.sin(p(c - a) / 2) ** 2 + math.cos(p(a)) * math.cos(p(c)) * math.sin(p(d - b) / 2) ** 2
    return 2 * 3958.8 * math.asin(math.sqrt(x))


def main():
    with tempfile.NamedTemporaryFile(suffix=".pdf") as fh:
        fh.write(urllib.request.urlopen(URL, timeout=60).read())
        fh.flush()
        text = subprocess.run(["pdftotext", "-layout", fh.name, "-"],
                              capture_output=True, text=True, check=True).stdout
    truth = {}
    for line in text.splitlines():
        m = re.match(r"^(\d{5})\s+(.*?)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s", line)
        if m:
            truth[m[1].lstrip("0")] = (float(m[3]), float(m[4]))
    ours = {}
    for r in json.load(open(DATA)):
        ours.setdefault(str(r["id"]), r)
    by_tier = defaultdict(list)
    for i, (la, lo) in truth.items():
        r = ours.get(i)
        if r:
            by_tier[r["coord_source"]].append(hav(la, lo, r["lat"], r["lon"]))
    print(f"sample ids: {len(truth)}, present in our data: {sum(map(len, by_tier.values()))}")
    for tier, errs in sorted(by_tier.items()):
        print(f"{tier:34s} n={len(errs)} max_err_mi={max(errs):.2f} "
              f"errs={[round(e, 2) for e in errs]}")


if __name__ == "__main__":
    sys.exit(main())
