"""Orchestrates the full-design engine: concept layout + site context -> design dict (geometries in the local frame)."""
import math
import time

import numpy as np
from scipy import ndimage as ndi
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from . import drainage, grading, hydro, site
from .base import CELL, Frame, Grid, existing_surface, lines, polys


def soils_hsg(lat, lon):
    """Hydrologic soil group at the site from USDA Soil Data Access (best effort); falls back to B."""
    try:
        import requests
        q = ("SELECT TOP 1 c.hydgrp, mu.muname FROM mapunit mu JOIN component c ON c.mukey = mu.mukey WHERE mu.mukey IN "
             f"(SELECT DISTINCT mukey FROM SDA_Get_Mukey_from_intersection_with_WktWgs84('POINT({lon} {lat})')) AND c.hydgrp IS NOT NULL ORDER BY c.comppct_r DESC")
        r = requests.post("https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest", data={"query": q, "format": "JSON"}, timeout=25)
        row = r.json()["Table"][0]
        g = (row[0] or "B").strip()[0].upper()
        return {"hsg": g if g in "ABCD" else "B", "muname": row[1], "source": "USDA NRCS Soil Data Access"}
    except Exception:  # noqa: BLE001
        return {"hsg": "B", "muname": None, "source": "assumed hydrologic soil group B (soil data not reachable)"}


def build(ctx, lay, job, rules, prog, log=print):
    """ctx: cached site context; lay: layout.pkl dict (state-plane geometry); returns the design dict."""
    t0 = time.time()
    k = ctx["k"]
    P_sp = ctx["parcel_sp"]
    fr = Frame(P_sp, k)
    sp = lay["sp"]
    F = fr.fwd
    P = F(P_sp)
    L = {"parcel": P, "pave": F(sp["pave"]), "walk": F(sp["walk"]) if sp.get("walk") is not None else None,
         "bldgs": [F(b) for b in sp["bldgs"]], "drive": F(sp["drive"]) if sp.get("drive") is not None else None,
         "pond": F(sp["pond"]) if sp.get("pond") is not None else None, "trees": [F(t) for t in (sp.get("trees") or [])],
         "stalls": [F(s) for s in (sp.get("stalls") or [])], "ada": [F(s) for s in (sp.get("ada") or [])],
         "grass": F(sp["grass"]) if sp.get("grass") is not None else None, "pad": F(sp["pad"]) if sp.get("pad") is not None else None,
         "dev": F(sp["dev"])}
    wet_sp = [w["geom"] for w in (ctx["wet"] if isinstance(ctx["wet"], list) else [])]
    L["wet"] = F(unary_union(wet_sp)) if wet_sp else None
    L["flood"] = F(unary_union([f["geom"] for f in (ctx["fl"] if isinstance(ctx["fl"], list) else []) if f.get("sfha") == "T"])) if isinstance(ctx["fl"], list) and any(f.get("sfha") == "T" for f in ctx["fl"]) else None
    rds = ctx["rds"] if isinstance(ctx["rds"], list) else []
    road_all = unary_union([F(r["centerline"]) for r in rds if r["class"] != "service"]) if rds else None
    L["roads"] = [(r, F(r["centerline"]), F(r["pavement"])) for r in rds]
    exb = [dict(e, geom=F(e["geom"])) for e in (lay.get("existing") or [])]
    L["existing"] = exb
    # context for the sheets: neighbouring buildings, on-site existing buildings, setback line, stall stripes
    from ..ec_drawing import classify_edges, envelope
    bl_all = ctx.get("bld") or []
    L["nbld"] = [F(b["geom"]) for b in bl_all if not b.get("onsite")]
    L["ebld"] = [(b.get("id"), F(b["geom"])) for b in bl_all if b.get("onsite")]
    edges = classify_edges(P_sp, [r["centerline"] for r in rds if r["class"] != "service"], k)
    sb = job.get("setbacks") or {"front": 25, "side": 10, "rear": 10}
    L["setback"] = F(envelope(P_sp, edges, sb, k))
    L["setbacks_ft"] = sb
    L["stripes"] = [F(s_) for s_ in (sp.get("stripes") or [])]
    dem = ctx.get("topo")
    if dem is None:
        raise RuntimeError("Elevation data is required for the full design (the 3DEP lookup did not run for this site)")
    b = P.buffer(90).bounds
    g = Grid(b)
    ex_z = existing_surface(dem, fr, g)
    lat_lon = _latlon(ctx, P_sp)
    lat_ll = lat_lon
    out = {"frame": fr, "grid": g, "L": L, "ex_z": ex_z, "lat": lat_lon[0], "lon": lat_lon[1]}

    # ---- grading
    G = grading.run(L, ex_z, g, road_all)
    z0 = grading.surface(g, ex_z, G["z_design"], G["design_mask"])
    ew0 = grading.earthwork(g, ex_z, z0)
    G["lod_mask"] = ew0["dist_mask"]
    log(f"grading base {time.time() - t0:.1f}s")

    # ---- hydrology / pond
    rain = hydro.rainfall(*lat_lon)
    soils = soils_hsg(*lat_lon)
    stype = hydro.storm_type(*lat_lon)
    D = drainage.design(g, ex_z, z0, G, L, rain, soils, stype, P.area)
    pond = D["pond"]
    # pond into the surface: bank at the top elevation, 3:1 inside to the floor, 6 ft berm top outside
    pm = g.mask(pond["poly"])
    rim = g.mask(pond["poly"].buffer(6)) & ~pm
    dist_in = ndi.distance_transform_edt(pm) * g.cell
    zd = G["z_design"].copy()
    zd[pm] = np.maximum(pond["bottom_el"], pond["top_el"] - dist_in[pm] / 3.0)
    zd[rim & ~G["design_mask"]] = pond["top_el"]
    dmask = G["design_mask"] | pm | rim
    z_prop = grading.surface(g, ex_z, zd, dmask)
    ew = grading.earthwork(g, ex_z, z_prop)
    G["lod_mask"] = ew["dist_mask"]
    G["z_prop"], G["z_design2"], G["design_mask2"] = z_prop, zd, dmask
    net = drainage.storm_network(g, z_prop, D, L, rain)
    deep = max([s.get("rim", 0) - s.get("inv_out", 0) for s in net["structs"] if s["kind"] == "CB" and s.get("inv_out") is not None] or [0])
    if deep > 9.0:     # the layout's pond sits too far below the pavement for a shallow gravity system: site it next to the lot instead
        seed = drainage.site_pond(g, ex_z, z0, G, L, D["inlets"])
        if seed is not None:
            D2 = drainage.design(g, ex_z, z0, G, L, rain, soils, stype, P.area, pond_override=seed)
            D2["warnings"].append("Detention pond moved from the concept location to a site beside the pavement so the storm sewer stays shallow")
            D = D2
            pond = D["pond"]
            pm = g.mask(pond["poly"])
            rim = g.mask(pond["poly"].buffer(6)) & ~pm
            dist_in = ndi.distance_transform_edt(pm) * g.cell
            zd = G["z_design"].copy()
            zd[pm] = np.maximum(pond["bottom_el"], pond["top_el"] - dist_in[pm] / 3.0)
            zd[rim & ~G["design_mask"]] = pond["top_el"]
            dmask = G["design_mask"] | pm | rim
            z_prop = grading.surface(g, ex_z, zd, dmask)
            ew = grading.earthwork(g, ex_z, z_prop)
            G["lod_mask"] = ew["dist_mask"]
            G["z_prop"], G["z_design2"], G["design_mask2"] = z_prop, zd, dmask
            net = drainage.storm_network(g, z_prop, D, L, rain)
    log(f"drainage {time.time() - t0:.1f}s")
    U = site.utilities(g, z_prop, ex_z, G, L, road_all, prog, fr)
    FIRE = site.fire(g, G, L, U)
    LS = site.landscape(L, lat_ll[0])
    LT = site.lighting(g, G, L)
    ER = site.erosion(g, G, L, U, D, net, G["spine"])
    log(f"site {time.time() - t0:.1f}s")
    out.update({"U": U, "FIRE": FIRE, "LS": LS, "LT": LT, "ER": ER})
    out.update({"G": G, "D": D, "net": net, "z_prop": z_prop, "earthwork": ew, "soils": soils, "rain": rain, "stype": stype, "road": road_all})
    return out


def _latlon(ctx, P_sp):
    from ..geo import to_crs
    c = to_crs(P_sp.centroid, ctx["epsg"], 4326)
    return c.y, c.x
