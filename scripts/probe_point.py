"""Debug: which parcel(s) does the statewide/county layer return at a lat/lon, and how big are they?
python scripts/probe_point.py <lat> <lon> [state]"""
import json
import sys

sys.path.insert(0, ".")
from shapely.geometry import shape  # noqa: E402

from pipeline.geo import state_plane_epsg, to_crs, units_to_ft  # noqa: E402
from pipeline.parcel import STATEWIDE, arcgis_query_envelope  # noqa: E402

lat, lon = float(sys.argv[1]), float(sys.argv[2])
state = sys.argv[3] if len(sys.argv) > 3 else "VA"
epsg = state_plane_epsg(lat, lon)
k = units_to_ft(epsg)
for url in STATEWIDE[state]:
    feats = arcgis_query_envelope(url, lat, lon, half_m=60)
    for f in feats:
        g = to_crs(shape(f["geometry"]), 4326, epsg)
        print(json.dumps({"calc_acres": round(g.area * k * k / 43560, 3), "centroid": list(shape(f["geometry"]).centroid.coords)[0],
                          "props": f.get("properties")}, default=str))
