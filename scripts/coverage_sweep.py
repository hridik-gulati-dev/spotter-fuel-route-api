"""Corridor coverage sweep (diagnostic, needs network: Nominatim + OSRM).

Method: for each long route, take the OSRM route geometry, find every CSV
station row within 10 mi (straight-line, to a sampled polyline) of it, and
report the largest stretch of route between consecutive such rows, counting
the start and the finish as ends. Run twice: (a) only rows the planner may
select (station-level coordinate tiers) and (b) every row that has any
coordinate, including city-centroid and exit-junction positions.
Limits: (b) uses approximate positions, so its distances are an indication
of where the file has rows, NOT precise road gaps. Distances are straight
line to a sampled polyline (about 1/400 of its points), mile marks follow
the route. Gaps ignore starting fuel, detours and price. A small gap does
not mean the planner finds a plan, and a large gap with equal (a) and (b)
values means the file itself has no row in that stretch.
Run from the project root: PYTHONPATH=. python scripts/coverage_sweep.py
(about 100 s, 12 routes, 24 geocoding calls and 12 routing calls, paced).
"""
import os,sys,time
os.environ['DJANGO_SETTINGS_MODULE']='fuelroute.settings'
sys.path.insert(0,'.')

import django; django.setup()
from router import services as S
pairs=[("San Francisco, CA","Portland, OR"),("Los Angeles, CA","Seattle, WA"),("Seattle, WA","Salt Lake City, UT"),("Boston, MA","Atlanta, GA"),("Houston, TX","Phoenix, AZ"),("Miami, FL","Chicago, IL"),("Denver, CO","Los Angeles, CA"),("Dallas, TX","Minneapolis, MN"),("Atlanta, GA","Dallas, TX"),("Salt Lake City, UT","San Francisco, CA"),("Las Vegas, NV","Boise, ID"),("Phoenix, AZ","Denver, CO")]
st=S.load_stations()
def gaps(pts,mk,pred):
    sam=list(range(0,len(pts),max(1,len(pts)//400)))
    ms=[]
    la=[p[0] for p in pts];lo=[p[1] for p in pts];pad=0.2
    for s in st:
        if not pred(s): continue
        if not(min(la)-pad<=s.lat<=max(la)+pad and min(lo)-pad<=s.lon<=max(lo)+pad): continue
        bd,bm=1e9,0
        for i in sam:
            d=S.haversine_miles(s.lat,s.lon,pts[i][0],pts[i][1])
            if d<bd: bd,bm=d,mk[i]
        if bd<=10: ms.append(bm)
    ms=sorted(ms); L=mk[-1]
    pts_=[0]+ms+[L]
    return max(b-a for a,b in zip(pts_,pts_[1:])),len(ms)
for a,b in pairs:
    try:
        A=S.geocode(a);B=S.geocode(b); time.sleep(1.1)
        poly,d=S.driving_route(A[0],A[1],B[0],B[1]); pts=S.decode_polyline(poly); mk=S._route_mile_markers(pts)
        g1=gaps(pts,mk,lambda s:s.lat and s.coord_source in S.STATION_LEVEL_SOURCES)
        g2=gaps(pts,mk,lambda s:s.lat)
        print(f"{a} -> {b}: {d:.0f} mi | selectable: maxgap {g1[0]:.0f} n={g1[1]} | any-row(incl centroid/exit): maxgap {g2[0]:.0f} n={g2[1]}")
    except Exception as e: print(a,b,'ERR',e)
