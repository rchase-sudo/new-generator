"""Geocoding, state-plane selection and coordinate transforms."""
from functools import lru_cache

from pyproj import CRS, Transformer
from pyproj.aoi import AreaOfInterest
from pyproj.database import query_crs_info
from shapely.geometry import mapping, shape
from shapely.ops import transform as shp_transform

from .http import get


def geocode(listing: dict):
    """Return (lat, lon, label) for the listing. Tries Census (numbered addresses) then Nominatim."""
    street, city, state = listing.get("street_address"), listing.get("city"), listing.get("state")
    zipc = listing.get("zip") or ""
    if street and any(ch.isdigit() for ch in street.split()[0]):
        try:
            r = get("https://geocoding.geo.census.gov/geocoder/locations/onelineaddress",
                    params={"address": f"{street}, {city}, {state} {zipc}", "benchmark": "Public_AR_Current", "format": "json"})
            m = r.json()["result"]["addressMatches"]
            if m:
                c = m[0]["coordinates"]
                return c["y"], c["x"], m[0]["matchedAddress"]
        except Exception:  # noqa: BLE001
            pass
    for q in [f"{street or listing.get('road_name')}, {city}, {state} {zipc}",
              f"{listing.get('road_name') or street}, {listing.get('county') or ''} County, {state}",
              f"{city}, {state} {zipc}"]:
        try:
            r = get("https://nominatim.openstreetmap.org/search", params={"q": q, "format": "json", "limit": 1, "countrycodes": "us"})
            js = r.json()
            if js:
                return float(js[0]["lat"]), float(js[0]["lon"]), js[0].get("display_name", q)
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError("Could not geocode the listing location")


@lru_cache(maxsize=256)
def state_plane_epsg(lat: float, lon: float) -> int:
    """NAD83 state plane (US survey feet / ft) zone covering the point; falls back to UTM in metres."""
    aoi = AreaOfInterest(west_lon_degree=lon, south_lat_degree=lat, east_lon_degree=lon, north_lat_degree=lat)
    best = None
    for info in query_crs_info(auth_name="EPSG", area_of_interest=aoi, contains=True):
        n = info.name
        if not n.startswith("NAD83 / ") or "(ftUS)" not in n and "(ft)" not in n:
            continue
        if "UTM" in n or "Conus" in n or "Albers" in n or "deprecated" in n.lower() or info.deprecated:
            continue
        a = info.area_of_use
        size = (a.east - a.west) * (a.north - a.south)
        if best is None or size < best[0]:
            best = (size, int(info.code))
    if best:
        return best[1]
    zone = int((lon + 180) / 6) + 1
    return 26900 + zone  # NAD83 / UTM zone N (metres)


def units_to_ft(epsg: int) -> float:
    u = CRS.from_epsg(epsg).axis_info[0].unit_conversion_factor  # metres per unit
    return u / 0.3048


@lru_cache(maxsize=64)
def _tf(src: int, dst: int):
    return Transformer.from_crs(src, dst, always_xy=True)


def to_crs(geom, src: int, dst: int):
    t = _tf(src, dst)
    return shp_transform(lambda x, y, z=None: t.transform(x, y), geom)


def geojson_to(geojson: dict, dst: int, src: int = 4326):
    return to_crs(shape(geojson), src, dst)


def to_geojson(geom, src: int):
    return mapping(to_crs(geom, src, 4326))
