"""Controlled exercise of Canadian stop selection (needs network: Nominatim + OSRM).

Natural runs never pick an Ontario stop because US prices in the file are
lower, so this harness removes every non-Ontario station from the candidate
pool and routes across the border with a LOW starting fuel. It checks that
Canadian rows can be purchased from, that legs stay within range, and shows
the price-units note. It is a test of the code path, not a claim about what
the employer specified. Run from the project root:
    PYTHONPATH=. python scripts/exercise_canadian_stops.py
"""
import os,sys,json
os.environ['DJANGO_SETTINGS_MODULE']='fuelroute.settings'
import django; django.setup()
from router import services, views
orig=services.load_stations
def only_ca(*a,**k):
    return [s for s in orig(*a,**k) if s.state.strip() in ('ON',)]
services.load_stations=only_ca
for s,f,g in [("Detroit, MI","Buffalo, NY",3),("Port Huron, MI","Buffalo, NY",3),("Detroit, MI","Buffalo, NY",50)]:
    try:
        d=views.compute_route(s,f,float(g),None)
        print(s,'->',f,'start gal',g,'| miles',d['total_distance_miles'],'driven',d['total_driven_miles'],'max leg',d['max_leg_miles'],'cash',d['cash_paid_en_route'],'total',d['total_money_spent'])
        for p in d['fuel_purchases']: print('  ',p['name'],p['city'].strip(),p['state'].strip(),p['coord_source'],'price',p['price_per_gallon'],'leg',p['leg_miles'],'buy',p.get('gallons_purchased',p.get('gallons')),'cost',p['cost'])
        print('  starting_fuel price_source',d['starting_fuel']['price_source'])
        print('  note:',d['price_units_note'][:400])
        print('  range_check',d['range_check'])
    except Exception as e: print(s,f,g,'ERR',type(e).__name__,str(e)[:200])
