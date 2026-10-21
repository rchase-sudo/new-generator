"""Geocoding, state-plane selection and coordinate transforms."""
from functools import lru_cache

from pyproj import CRS, Transformer
from pyproj.aoi import AreaOfInterest
from pyproj.database import query_crs_info
from shapely.geometry import mapping, shape
from shapely.ops import transform as shp_transform

from .http import get


def google_geocode(q, zipc=None, city=None):
    """Google Geocoding API (same engine as Google Maps). Returns (lat, lon, label, location_type) or None.
    When the listing gives a ZIP / city, the answer must be in it: on highways Google will otherwise interpolate the
    same house number in another town (2839 US 70 Hwy, Durham 27703 -> Hillsborough 27278)."""
    import os
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key:
        return None
    params = {"address": q, "key": key, "region": "us"}
    if zipc:
        params["components"] = f"postal_code:{str(zipc)[:5]}|country:US"
    try:
        js = get("https://maps.googleapis.com/maps/api/geocode/json", params=params).json()
        from .usage import GEOCODE_USD, add
        add("Geocode address", "Google", GEOCODE_USD)
    except Exception:  # noqa: BLE001
        return None
    res = js.get("results") or []
    if js.get("status") != "OK" or not res:
        return None
    r = res[0]
    comp = {t: c for c in r.get("address_components", []) for t in c.get("types", [])}
    gzip = (comp.get("postal_code") or {}).get("short_name", "")[:5]
    towns = {(comp.get(t) or {}).get("long_name", "").lower() for t in ("locality", "postal_town", "sublocality", "administrative_area_level_3")}
    if zipc and gzip and gzip != str(zipc)[:5]:
        return None
    if not zipc and city and towns - {""} and city.lower() not in towns:
        return None
    if r.get("partial_match") and r["geometry"].get("location_type") == "APPROXIMATE":
        return None
    loc = r["geometry"]["location"]
    return loc["lat"], loc["lng"], f'{r.get("formatted_address", q)} [google {r["geometry"].get("location_type")}]', r["geometry"].get("location_type")


def geocode(listing: dict):
    """Return (lat, lon, label) for the listing.
    Google first (what Google Maps shows for the address), then Census (numbered addresses), then Nominatim.
    This point is authoritative: parcels are looked up AT it, never re-anchored by a parcel vendor's address search."""
    street, city, state = listing.get("street_address"), listing.get("city"), listing.get("state")
    zipc = listing.get("zip") or ""
    if street or listing.get("road_name"):
        q = ", ".join(x for x in [street or listing.get("road_name"), city, f"{state or ''} {zipc}".strip()] if x)
        g = google_geocode(q, zipc=zipc or None, city=city)
        # an approximate (city / ZIP centroid) answer is worse than Census for a numbered address
        if g and (g[3] in ("ROOFTOP", "RANGE_INTERPOLATED") or not (street and street.split()[0][:1].isdigit())):
            return g[0], g[1], g[2]
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
    road = street or listing.get("road_name")
    qs = []
    if road:
        qs.append(", ".join(x for x in [road, city, f"{state or ''} {zipc}".strip()] if x))
        if listing.get("county"):
            qs.append(f"{road}, {listing['county']} County, {state}")
    qs.append(", ".join(x for x in [city, f"{state or ''} {zipc}".strip()] if x))
    if zipc:
        qs.append(f"{zipc}, USA")
    for q in qs:
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
