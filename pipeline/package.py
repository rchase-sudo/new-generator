"""Due diligence package (FORMA style, same layout as the 1120 Lot 6 Pocahontas Trail package).

  job (listing, parcel, zoning, program, layout metrics) + concept_plan.png + render.jpg
  -> pass 1: Claude researches cost / rent / fee / permit inputs (web search)
  -> proforma computed here (three scenarios, residual land, break-even rent)
  -> pass 2: Claude writes the narrative around the computed numbers (so text and tables agree)
  -> HTML -> WeasyPrint PDF (letter portrait pages, Exhibit A / B1 landscape)
"""
import datetime as _dt
import html as _html
import json
import os
import re

from . import claude

ASSETS = os.environ.get("FORMA_ASSETS") or os.path.join(os.path.dirname(__file__), "assets")
OCC, MGMT = 0.95, 0.06

e = lambda s: _html.escape(str(s if s is not None else ""), quote=False)  # noqa: E731
f = lambda n: f"${n:,.0f}"  # noqa: E731
fp = lambda n: f"(${-n:,.0f})" if n < 0 else f"${n:,.0f}"  # noqa: E731


def _num(x, default=0.0):
    try:
        if isinstance(x, str):
            x = re.sub(r"[^0-9.\-]", "", x)
        return float(x)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------------------------------------ facts
def facts(job):
    L = job.get("listing") or {}
    P = job.get("parcel") or {}
    if P.get("parts"):
        P = {**P["parts"][0], "acres": sum(_num(p.get("acres")) for p in P["parts"])}
    A = P.get("attrs") or {}
    Z = job.get("zoning") or {}
    S = job.get("summary") or {}
    M = job.get("layout") or {}
    prog = job.get("program") or {}
    acres = _num(L.get("acres")) or _num(P.get("acres")) or _num(S.get("parcel_acres"))
    county = L.get("county") or (A.get("county") or "").title()
    county = county if not county or "county" in county.lower() or "parish" in county.lower() else f"{county} County"
    addr = ", ".join(x for x in [L.get("lot"), L.get("street_address") or P.get("address")] if x)
    return {
        "address": addr, "city": L.get("city") or (A.get("scity") or "").title(), "state": L.get("state") or A.get("state2"),
        "zip": L.get("zip") or A.get("szip5"), "county": county, "acres": round(acres, 2),
        "asking_price": _num(L.get("price")) or None, "listing_zoning": L.get("zoning"), "listing_summary": L.get("summary"),
        "listing_id": L.get("listing_id"), "broker": L.get("broker"), "utilities": L.get("utilities"),
        "frontage_ft": L.get("frontage_ft") or S.get("frontage_ft"), "fronting_roads": S.get("fronting_roads"),
        "apn": P.get("apn"), "owner": P.get("owner"), "assessed_value": A.get("parval"), "assessed_type": A.get("parvaltype"),
        "last_sale": {"date": A.get("saledate"), "price": A.get("saleprice")}, "legal": A.get("legaldesc"),
        "median_hh_income_tract": A.get("median_household_income"), "pop_growth_5yr_pct": A.get("population_growth_past_5_years"),
        "elevation_ft": [A.get("lowest_parcel_elevation"), A.get("highest_parcel_elevation")],
        "jurisdiction": Z.get("jurisdiction"), "district": Z.get("district"), "district_name": Z.get("district_name"),
        "ordinance": Z.get("ordinance"), "zoning_confidence": Z.get("confidence"), "zoning_uses": Z.get("uses"),
        "zoning_notes": Z.get("notes"), "parking_setback_note": Z.get("parking_setback_note"), "buffer_notes": Z.get("buffer_notes"),
        "max_height_ft": Z.get("max_height_ft"), "min_lot_area_sf": Z.get("min_lot_area_sf"), "zoning_sources": (Z.get("sources") or [])[:8],
        "setbacks_ft": M.get("setbacks_ft") or S.get("setbacks_ft"), "pavement_setbacks_ft": M.get("parking_setbacks_ft"),
        "buildable_acres": S.get("buildable_acres"), "frontage_buildable_acres": S.get("frontage_buildable_acres"),
        "wetland_acres": S.get("wetland_acres"), "wetland_types": S.get("wetland_types"), "flood_sfha_acres": S.get("flood_sfha_acres"),
        "flood_zones": S.get("flood_zones"),
        "use": prog.get("use"), "rationale": prog.get("rationale"), "parking_calc": (prog.get("parking") or {}).get("calc"),
        "buildings": [{k: b.get(k) for k in ("name", "label", "use", "footprint_sf", "stories", "drive_thru", "notes")}
                      for b in prog.get("buildings") or []],
        "plan": {k: M.get(k) for k in ("building_sf", "gfa_sf", "stalls", "stalls_required", "ada_stalls", "impervious_sf",
                                       "impervious_pct", "lot_coverage_pct", "development_acres", "remaining_developable_acres",
                                       "far", "pond_sf")},
        "plan_buildings": [{k: b.get(k) for k in ("name", "use", "w_ft", "d_ft", "footprint_sf", "stories", "drive_thru")}
                           for b in M.get("buildings") or []],
    }


# ------------------------------------------------------------------------------------------------ pass 1
INPUTS_SYS = """You are a commercial real estate development analyst preparing inputs for a preliminary development proforma
for a vacant land listing. Research the jurisdiction with web search: current utility connection / tap / impact fees,
regional construction costs, NNN rents and cap rates for the proposed uses, the permitting sequence and agencies,
demographics, and the land price. Use real local agency names (county/city planning, state DOT district, state
environmental agency, utility). Be conservative and realistic. Return ONLY one JSON object, no prose. Write inch sizes as "2-in." (never a bare " inside strings)."""

INPUTS_SCHEMA = """{
 "land": {"price": number, "basis": "Per <source> asking price | Estimated from <comps/assessment> (price on request)",
          "assessment": number|null, "assessment_note": "e.g. 2026 county assessment"},
 "demographics": {"median_hh_income": number|null, "population": number|null, "growth_note": "short", "geography": "county or city", "source": "e.g. ACS 2023 5-yr"},
 "site": [ {"item": "Clearing & Grading", "qty": "±2.0 AC", "unit": "—", "subtotal": number, "note": "short"} ],   // 6-9 lines: grading, paving (qty = pavement SF, unit $/SF), curb/walks, on-site utilities, stormwater, landscaping, signage, DOT/city entrance; scale to THIS plan
 "vertical": [ {"item": "Building A — Retail Shell", "building": "<exact building name>", "qty": "12,000 SF", "unit": "$185 / SF", "subtotal": number, "note": "short"} ],  // 1-2 lines per building (shell, TI if typical); max 6 lines
 "utility_fee": {"amount": number, "qty": "e.g. 2 × 2-in. taps", "unit": "e.g. $52,000", "note": "e.g. Water + sewer connection; FY2027 schedule"},
 "permitting_ls": number, "legal_ls": number, "leasing_ls": number,
 "rents": [ {"building": "<exact building name>", "rent_psf": number, "basis": "short"} ],   // NNN $/SF/yr, one per building
 "cap_rate": number,   // e.g. 0.07
 "cap_note": "short — product type the cap reflects",
 "phase1": ["<building name(s) built first — the anchor / most viable>"],
 "permits": [ {"permit": "...", "agency": "...", "timeline": "2–4 months", "cost": "$3,000–$8,000", "risk": "LOW | MODERATE | HIGH — CRITICAL PATH | MODERATE — COST"} ],  // 7-10 rows ending with Building Permits and Certificate of Occupancy
 "entitlement_months": "e.g. 6–12",
 "traffic": "AADT figure with year and source if found, else what to pull and from where",
 "sources": ["url", ...]
}"""


RESEARCH_SYS = """You are a commercial real estate development analyst researching a vacant land listing for a preliminary
development proforma. Use web search to find, for THIS jurisdiction: utility connection / tap / impact fees (current
schedule), the land price or comparable land sales and the assessment, demographics (median household income, population,
growth), traffic counts on the fronting road, regional construction costs for the proposed building types and site work,
NNN rents and cap rates for the proposed uses, and the permitting sequence with real agency names (city/county planning,
state DOT district, state environmental agency, utility). Report findings as concise notes with figures, dates and
source URLs. Where nothing is found, give a conservative regional benchmark and label it as such."""

FILL_SYS = """You turn research notes into proforma inputs for a FORMA due diligence package. Use the research figures;
where research is missing use conservative regional benchmarks. Scale every cost to the plan quantities given
(impervious SF, building SF, stalls, development acres). Building names must match the program exactly."""


def research_inputs(F):
    notes, src = claude.ask(RESEARCH_SYS, f"Property and approved concept plan:\n{json.dumps(F, default=str)}",
                            max_tokens=6000, web_search=12, timeout=900, label="Package: research")
    user = (f"Property and approved concept plan:\n{json.dumps(F, default=str)}\n\nRESEARCH NOTES:\n{notes}\n\n"
            f"Call submit_inputs with an object in exactly this shape (comments describe the fields):\n{INPUTS_SCHEMA}\n"
            "If the asking price is null, estimate a land price from comparable land sales or the assessment and say so in land.basis.")
    data = claude.ask_tool(FILL_SYS, user, "submit_inputs", "Submit the proforma inputs.", max_tokens=16000, timeout=900,
                           label="Package: cost & rent inputs")
    data["sources"] = list(dict.fromkeys((data.get("sources") or []) + src))[:20]
    data["research_notes"] = notes[:6000]
    return data


# ------------------------------------------------------------------------------------------------ proforma
def proforma(F, I):
    blds = F["buildings"] or [{"name": "Building", "footprint_sf": F["plan"].get("building_sf") or 0, "stories": 1}]
    sf = {b["name"]: _num(b.get("footprint_sf")) * max(1, int(_num(b.get("stories"), 1))) for b in blds}
    names = list(sf)
    phase1 = [n for n in (I.get("phase1") or []) if n in sf] or names[:1]
    if len(phase1) == len(names) and len(names) > 1:
        phase1 = names[:1]
    rent = {}
    for r in I.get("rents") or []:
        if r.get("building") in sf:
            rent[r["building"]] = (_num(r.get("rent_psf")), r.get("basis") or "")
    for n in names:
        rent.setdefault(n, (24.0, "FORMA estimate"))
    cap = _num(I.get("cap_rate"), 0.07) or 0.07
    cap = cap / 100 if cap > 1 else cap
    land_price = _num((I.get("land") or {}).get("price")) or _num(F.get("asking_price")) or 0
    clip = lambda t, n=90: (str(t or "")[:n - 1].rsplit(" ", 1)[0] + "…") if len(str(t or "")) > n else str(t or "")  # noqa: E731
    site = [(r.get("item", ""), clip(r.get("qty", ""), 28), clip(r.get("unit", "—"), 24), round(_num(r.get("subtotal"))), clip(r.get("note", "")))
            for r in (I.get("site") or [])][:9]
    vert = [(r.get("item", ""), clip(r.get("qty", ""), 28), clip(r.get("unit", ""), 24), round(_num(r.get("subtotal"))), clip(r.get("note", "")),
             r.get("building") if r.get("building") in sf else names[0]) for r in (I.get("vertical") or [])][:6]
    uf = I.get("utility_fee") or {}

    def build(bset, include_land=True):
        v = [r for r in vert if r[5] in bset]
        hard = sum(r[3] for r in site) + sum(r[3] for r in v)
        frac = sum(sf[n] for n in bset) / (sum(sf.values()) or 1)
        soft = [("Architecture + Engineering (A&amp;E)", "8% of hard", "—", round(hard * 0.08), "Civil, architectural, MEP, environmental"),
                ("Permitting, Plan Review &amp; Escrow", "LS", "—", round(_num(I.get("permitting_ls"), 50000)), "Site plan, DOT, environmental, building"),
                ("Utility Connection / Impact Fees", e(clip(uf.get("qty") or "LS", 28)), e(clip(uf.get("unit") or "—", 24)), round(_num(uf.get("amount"))), e(clip(uf.get("note") or ""))),
                ("Construction Management / GC Fee", "5% of hard", "—", round(hard * 0.05), "Owner's rep or GC overhead + fee"),
                ("Legal, Accounting, Entity Costs", "LS", "—", round(_num(I.get("legal_ls"), 35000) * (0.85 if frac < 1 else 1)), "LLC, leases, title work")]
        pre = hard + sum(r[3] for r in soft)
        soft += [("Construction Loan Interest (Carry)", "12 mo @ 8.0%", "—", round(pre * 0.55 * 0.08), "~55% average outstanding balance"),
                 ("Leasing Commissions &amp; Marketing", "LS", "—", round(_num(I.get("leasing_ls"), 50000) * (0.6 if frac < 1 else 1)), "Tenant leasing"),
                 ("Contingency (10%)", "—", "10%", round(hard * 0.10), "Applied to hard costs only")]
        land = [("Land Acquisition", f"{F['acres']} AC", f"${land_price / (F['acres'] or 1):,.0f} / AC", round(land_price), e((I.get("land") or {}).get("basis") or "Asking price")),
                ("Closing Costs, Due Diligence, Title", "—", "2.5%", round(land_price * 0.025), "Survey, title, legal, Phase I ESA")] if include_land else []
        st = sum(r[3] for r in soft)
        lt = sum(r[3] for r in land)
        return dict(site=site, vert=[r[:5] for r in v], soft=soft, land=land, hard=hard, soft_total=st, land_total=lt, tdc=hard + st + lt)

    def noi(bset):
        rows, tot = [], 0
        for n in bset:
            g = sf[n] * rent[n][0] * OCC
            m = g * MGMT
            rows.append((n, round(sf[n]), rent[n][0], round(g), round(m), rent[n][1]))
            tot += g - m
        return rows, round(tot)

    A, B, C = build(phase1), build(names), build(names, False)
    nA, nB = noi(phase1), noi(names)
    out = {}
    for k, m, n in (("A", A, nA[1]), ("B", B, nB[1]), ("C", C, nB[1])):
        val = n / cap
        out[k] = dict(tdc=m["tdc"], noi=n, yld=n / m["tdc"] if m["tdc"] else 0, value=round(val), profit=round(val - m["tdc"]))
    tsf = sum(sf.values()) or 1
    blend = sum(sf[n] * rent[n][0] for n in names) / tsf
    be = lambda tdc: tdc * cap / ((1 - MGMT) * OCC) / tsf  # noqa: E731
    return dict(A=A, B=B, C=C, out=out, noiA=nA, noiB=nB, cap=cap, land_price=land_price, sf=sf, names=names, phase1=phase1,
                rent=rent, blend=blend, be_ask=be(B["tdc"]), be_zero=be(C["tdc"]), resid=out["C"]["value"] - C["tdc"], tsf=tsf)


# ------------------------------------------------------------------------------------------------ pass 2
TEXT_SYS = """You write FORMA due diligence packages for vacant commercial land listings. FORMA is a one-person preliminary
design practice; refer to the work as "FORMA" or "the FORMA plan", never "we". Style: plain, specific, factual, short
declarative sentences, numbers with units, no hype, no marketing adjectives, no em-dash chains. Every figure you state must
come from the facts, research inputs or computed proforma given to you; never invent figures. Where something is
unconfirmed, say what must be confirmed and with whom. Keep each field within its length limit so the fixed-size pages
do not overflow. Return ONLY one JSON object."""

TEXT_SCHEMA = """{
 "cover_use": "UPPERCASE use line, ≤ 40 chars, e.g. RETAIL / QSR / MEDICAL OFFICE DEVELOPMENT",
 "cover_meta": ["County · ±4.35 Acres (Single Parcel) · AG Agricultural District", "program line: buildings + SF · FORMA Preliminary Site Plan on File"],
 "hbu_title": "≤ 70 chars, e.g. Retail Strip + QSR Pad + Medical Office — Three Buildings, 21,800 SF",
 "overview": "≤ 120 words",
 "highlights": ["6 bullets, ≤ 32 words each"],
 "exec_summary": "≤ 140 words; ends by pointing to Sections 05–07 for whether the project pencils",
 "zoning_h3": "≤ 45 chars", "zoning_text": "≤ 120 words, bulk standards with ordinance section numbers where known",
 "site_h3": "≤ 45 chars", "site_text": "≤ 115 words, road context, frontage, constraints, where the pod sits, traffic data",
 "uses": [["Use category", "Permit status", "Fit for this site", "Notes ≤ 12 words"]],   // 7-8 rows, proposed uses first
 "env_boxes": [["big ≤ 9 chars", "CAPTION ≤ 34 CHARS", "≤ 32 words"]],   // exactly 3: environmental, setback/access, utilities
 "program_notes": {"<building name>": "≤ 10 words"},
 "parking_note": "≤ 10 words", "access_note": "≤ 10 words",
 "plan_text": ["≤ 110 words describing Exhibit A layout", "≤ 55 words on phasing / approximate limits"],
 "permits_intro": "≤ 95 words",
 "permit_boxes": [["big", "CAPTION", "≤ 32 words"]],   // exactly 3
 "scenario_a": "≤ 16 words describing the Phase 1 only program",
 "noi_note": "≤ 55 words: rent basis, what to confirm",
 "returns_note": "≤ 50 words: cap rate rationale, which scenarios carry land",
 "returns_boxes": [["big", "CAPTION", "≤ 28 words"]],   // exactly 3: base yield vs cap, residual land, realistic buyer / path
 "residual_boxes": [["big", "CAPTION", "≤ 28 words"]],  // exactly 3: yield range A→C, all-in cost per SF, levers to a positive outcome
 "disclaimer_agencies": "comma list of agencies to confirm with",
 "disclaimer_sources": "1-2 sentences naming the sources of zoning, listing facts, demographics and fees"
}"""


_S = {"type": "string"}
_L = {"type": "array", "items": {"type": "string"}}
_B = {"type": "array", "items": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 3}, "minItems": 3, "maxItems": 3}
TEXT_PROPS = {
    **{k: _S for k in ("cover_use", "hbu_title", "overview", "exec_summary", "zoning_h3", "zoning_text", "site_h3", "site_text",
                       "parking_note", "access_note", "permits_intro", "scenario_a", "noi_note", "returns_note",
                       "disclaimer_agencies", "disclaimer_sources")},
    "cover_meta": {**_L, "maxItems": 2}, "highlights": {**_L, "maxItems": 6}, "plan_text": {**_L, "maxItems": 2},
    "uses": {"type": "array", "items": {"type": "array", "items": {"type": "string"}, "minItems": 4, "maxItems": 4}, "maxItems": 8},
    "env_boxes": _B, "permit_boxes": _B, "returns_boxes": _B, "residual_boxes": _B,
    "program_notes": {"type": "object", "additionalProperties": {"type": "string"}},
}


def write_text(F, I, PF):
    o = PF["out"]
    nums = {"scenarios": {k: {kk: (round(v, 4) if isinstance(v, float) else v) for kk, v in o[k].items()} for k in o},
            "cap_rate": PF["cap"], "land_price": PF["land_price"], "residual_land_value": PF["resid"],
            "blended_rent_psf": round(PF["blend"], 2), "break_even_rent_land_at_price": round(PF["be_ask"], 2),
            "break_even_rent_land_zero": round(PF["be_zero"], 2), "hard_cost_B": PF["B"]["hard"],
            "all_in_cost_per_sf": round(PF["B"]["tdc"] / PF["tsf"]), "phase1_buildings": PF["phase1"],
            "rents": {n: r[0] for n, r in PF["rent"].items()}, "utility_fee": I.get("utility_fee")}
    I = {k: v for k, v in I.items() if k != "research_notes"}
    user = (f"FACTS:\n{json.dumps(F, default=str)}\n\nRESEARCH INPUTS:\n{json.dumps(I, default=str)}\n\n"
            f"COMPUTED PROFORMA (authoritative numbers):\n{json.dumps(nums, default=str)}\n\n"
            f"Write the package text. Return JSON in exactly this shape:\n{TEXT_SCHEMA}")
    return claude.ask_tool(TEXT_SYS, user.replace("Return JSON in exactly this shape", "Call submit_text with an object in exactly this shape"),
                           "submit_text", "Submit the package text. Each field once, within its word limit.", max_tokens=16000,
                           timeout=900, properties=TEXT_PROPS, label="Package: writing")


# ------------------------------------------------------------------------------------------------ html
def _css():
    fd = os.path.join(ASSETS, "fonts")
    return f"""
@page {{ size: letter; margin: 0 }}
@page land {{ size: 11in 8.5in; margin: 0 }}
@font-face {{ font-family: H; src: url(file://{fd}/texgyreheros-regular.otf) }}
@font-face {{ font-family: H; font-weight: bold; src: url(file://{fd}/texgyreheros-bold.otf) }}
@font-face {{ font-family: S; src: url(file://{fd}/LiberationSerif-Regular.ttf) }}
""" + CSS


CSS = '''
* { box-sizing: border-box }
body { margin: 0; font-family: H; color: #1a1a1a; font-size: 7.6pt; line-height: 1.5 }
section { page-break-after: always; position: relative; overflow: hidden }
.pg { width: 8.5in; height: 11in; padding: 0.42in 0.6in 0.5in }
.hdr { display: flex; justify-content: space-between; align-items: baseline; border-bottom: 1.2pt solid #1a1a1a; padding-bottom: 4pt; font-size: 6.2pt; color: #555; letter-spacing: .04em }
.logo { color: #a8843f; font-weight: bold; letter-spacing: .14em; font-size: 6.6pt } .logo b { color: #1a1a1a }
.bar { background: #0e0e0e; color: #fff; font-weight: bold; font-size: 11pt; letter-spacing: .3em; padding: 8pt 12pt; margin: 12pt 0 10pt }
.bar .n { color: #a8843f }
.lbl { color: #a8843f; font-weight: bold; font-size: 8.8pt; letter-spacing: .22em; margin: 11pt 0 5pt }
h2 { font-size: 12.5pt; margin: 2pt 0 5pt } h3 { font-size: 10.5pt; margin: 2pt 0 4pt; line-height: 1.25 }
p { margin: 0 0 5pt; font-size: 7.6pt } ul { margin: 0; padding-left: 10pt } li { margin-bottom: 2.5pt }
.two { display: flex; gap: 18pt } .two > div { flex: 1 }
.stats { display: flex; flex-wrap: wrap; border-top: .75pt solid #ddd; border-left: .75pt solid #ddd; margin: 8pt 0 4pt }
.st { width: 25%; border-right: .75pt solid #ddd; border-bottom: .75pt solid #ddd; padding: 9pt 4pt 8pt; text-align: center }
.st .v { font-weight: bold; font-size: 13pt } .st .l { font-size: 5.8pt; letter-spacing: .12em; color: #666; margin-top: 3pt; line-height: 1.5 }
table { width: 100%; border-collapse: collapse; font-size: 6.9pt; margin-bottom: 4pt }
th { background: #0e0e0e; color: #fff; text-align: left; font-size: 6.2pt; letter-spacing: .14em; padding: 5pt 6pt }
td { padding: 5pt 6pt; border-bottom: .5pt solid #e2ded5; vertical-align: top; line-height: 1.4 }
tbody tr:nth-child(odd) td { background: #f3f1ec }
.r { text-align: right } th.r { text-align: right }
tr.grp td { background: #e9e6df !important; font-weight: bold; padding: 4pt 6pt }
tr.tot td { background: #ebe5d8 !important; font-weight: bold; border-top: .75pt solid #b7ad98 }
table.cost td:nth-child(4) { white-space: nowrap }
.boxes { display: flex; gap: 10pt; margin-top: 10pt }
.box { flex: 1; border: 1.4pt solid #1a1a1a; padding: 9pt 10pt 8pt }
.box .big { color: #a8843f; font-weight: bold; font-size: 14pt; margin-bottom: 5pt }
.box .cap { font-weight: bold; font-size: 6.6pt; letter-spacing: .18em; margin-bottom: 3pt } .box p { font-size: 6.9pt; line-height: 1.5; margin: 0 }
.scen { display: flex; gap: 8pt; margin-bottom: 4pt } .scen > div { flex: 1; border: .75pt solid #ccc; padding: 7pt 9pt }
.scen .sl { color: #a8843f; font-weight: bold; font-size: 6.2pt; letter-spacing: .15em } .scen b { font-size: 9pt; display: block; margin: 3pt 0 } .scen p { font-size: 6.4pt; color: #555; margin: 0 }
.note { font-size: 6.2pt; color: #666; margin-top: 4pt }
.disc { font-size: 5.8pt; color: #555; border-top: .75pt solid #ddd; padding-top: 6pt; margin-top: 10pt; line-height: 1.55 }
.foot { position: absolute; left: .6in; right: .6in; bottom: .3in; text-align: center; font-size: 5.8pt; letter-spacing: .18em; color: #8a857a; border-top: .5pt solid #e2ded5; padding-top: 5pt }
.cover { width: 8.5in; height: 11in; background: #111; color: #fff; text-align: center; padding-top: 2.8in }
.mark { width: 1.1in; margin: 0 auto } .rule { height: 1pt; background: #a8843f } .F { font-family: S; font-size: 58pt; line-height: 1.15; color: #f2efe8 }
.forma { font-size: 12pt; letter-spacing: .25em; margin-top: 12pt; white-space: nowrap; margin-left: -.4in; margin-right: -.4in } .pd { font-size: 5.8pt; color: #8f8a7e; margin-top: 8pt; white-space: nowrap; margin-left: -1in; margin-right: -1in }
.short { width: .9in; height: .5pt; background: #555; margin: .4in auto .3in }
.pda { color: #b8924f; font-size: 9pt; letter-spacing: .25em } h1 { font-size: 21pt; margin: 14pt 0 6pt; letter-spacing: .02em }
.sub { font-size: 12pt; letter-spacing: .3em; color: #d6d2c8 } .addr { font-size: 13.5pt; margin-top: 30pt } .meta { font-size: 7.5pt; color: #c9c5bb; margin-top: 10pt; line-height: 1.9 }
.prep { font-size: 7pt; letter-spacing: .3em; color: #888; margin-top: 36pt }
.cfoot { position: absolute; bottom: .5in; left: 0; right: 0; font-size: 5.8pt; letter-spacing: .2em; color: #8a857a }
.land { page: land; width: 11in; height: 8.5in }
.img img { width: 11in; height: 8.5in; object-fit: contain; display: block }
.ex { padding: .45in .35in } .ex img { width: 100%; height: 6.75in; object-fit: cover; display: block }
.ex .cap { background: #0e0e0e; color: #fff; display: flex; justify-content: space-between; gap: 20pt; padding: 8pt 12pt; font-size: 7pt; letter-spacing: .1em }
.ex .cap .g { color: #a8843f; font-weight: bold; letter-spacing: .3em } .ex .cap .r { color: #ccc; font-size: 6.4pt; text-align: right; max-width: 4.3in; letter-spacing: .06em }
'''


def _table(head, rows, cls=""):
    h = "".join(f'<th class="{c}">{t}</th>' for t, c in head)
    out = []
    for r in rows:
        if isinstance(r, str):
            out.append(f'<tr class="grp"><td colspan="{len(head)}">{r}</td></tr>')
            continue
        tc = r[-1] if isinstance(r[-1], dict) else {}
        cells = r[:-1] if isinstance(r[-1], dict) else r
        out.append(f'<tr class="{tc.get("cls", "")}">' + "".join(f'<td class="{head[i][1]}">{c}</td>' for i, c in enumerate(cells)) + "</tr>")
    return f'<table class="{cls}"><thead><tr>{h}</tr></thead><tbody>{"".join(out)}</tbody></table>'


def _boxes(items):
    items = (list(items or []) + [["—", "", ""]] * 3)[:3]
    return '<div class="boxes">' + "".join(
        f'<div class="box"><div class="big">{e(a)}</div><div class="cap">{e(str(b).upper())}</div><p>{e(c)}</p></div>'
        for a, b, c, *_ in items) + "</div>"


def _stats(items):
    return '<div class="stats">' + "".join(f'<div class="st"><div class="v">{a}</div><div class="l">{b}</div></div>' for a, b in items) + "</div>"


def _tc(t):
    """Title case that keeps acronyms (QSR, MOB, ADA) as written."""
    return " ".join(w if (w.isupper() and len(w) <= 4) else w.capitalize() for w in str(t or "").split())


def _money_short(n):
    if not n:
        return "—"
    return f"${n / 1e6:.2f}M" if n >= 1e6 else f"${n / 1e3:,.0f}K"


def build_html(F, I, PF, T, plan_img, render_img, today=None):
    today = today or _dt.date.today()
    month = today.strftime("%B %Y").upper()
    street = F["address"] or "Site"
    house = (re.match(r"\s*(\d+)", street) or re.match(r".*?(\d+)", street) or [None, None])[1] or "0000"
    slug = "-".join(x for x in [re.sub(r"^\d+\s*", "", street.split(",")[-1]).strip().split(" ")[0], F["city"], F["state"]] if x).upper()
    code = f"JOB {house} · {slug}"
    loc = ", ".join(x for x in [street, F["city"], F["state"]] if x)
    foot_txt = f"FORMA · {code} · PREPARED {month} · CONFIDENTIAL · PRELIMINARY ANALYSIS — NOT FOR FINANCING OR CONSTRUCTION"
    A_, B_, C_, O = PF["A"], PF["B"], PF["C"], PF["out"]
    tsf = PF["tsf"]
    plan = F["plan"]
    acres = F["acres"]
    price = PF["land_price"]
    priced = bool(F.get("asking_price"))
    dem = I.get("demographics") or {}
    inc = dem.get("median_hh_income") or F.get("median_hh_income_tract")
    nb = len(PF["names"])

    def page(num, title, body):
        return (f'<section class="pg"><div class="hdr"><span class="logo"><b>F</b>ORMA</span><span>{e(loc)} · Due Diligence Package</span></div>'
                f'<div class="bar"><span class="n">{num}</span> {title}</div>{body}<div class="foot">{e(foot_txt)}</div></section>')

    meta = T.get("cover_meta") or []
    cover = f'''<section class="cover">
<div class="mark"><div class="rule"></div><div class="F">F</div><div class="rule"></div>
<div class="forma">F O R M A</div><div class="pd">P R E L I M I N A R Y &nbsp; D E S I G N</div></div>
<div class="short"></div>
<div class="pda">P R E L I M I N A R Y &nbsp;&nbsp; D E V E L O P M E N T &nbsp;&nbsp; A N A L Y S I S</div>
<h1>DUE DILIGENCE PACKAGE</h1>
<div class="sub">{e(T.get("cover_use") or (F.get("use") or "COMMERCIAL").upper() + " DEVELOPMENT")}</div>
<div class="addr">{e(street)} · {e(", ".join(x for x in [F["city"], " ".join(x for x in [F["state"], F["zip"]] if x)] if x))}</div>
<div class="meta">{"<br>".join(e(m) for m in meta[:2])}</div>
<div class="prep">PREPARED {month} · CONFIDENTIAL</div>
<div class="cfoot">F O R M A &nbsp; · &nbsp; {e(code)} &nbsp; · &nbsp; PRELIMINARY ANALYSIS — NOT FOR FINANCING OR CONSTRUCTION</div>
</section>'''

    stats1 = [(f"{acres:g} AC", "GROSS SITE AREA"),
              (f"±{_num(F.get('buildable_acres')):.1f} AC", "BUILDABLE AREA (FORMA)"),
              (_money_short(price), f"{'ASKING PRICE' if priced else 'EST. LAND VALUE'} (${price / (acres or 1) / 1000:,.0f}K/AC)" if price else "PRICE ON REQUEST"),
              (e(F.get("district") or F.get("listing_zoning") or "—"), f"ZONING — {e((F.get('district_name') or 'DISTRICT').upper())}"),
              (f"{tsf:,.0f} SF", f"{nb} BUILDING{'S' if nb != 1 else ''} (FORMA PLAN)"),
              (f"{plan.get('stalls') or '—'}", "PARKING SPACES (FORMA PLAN)"),
              (f"±{_num(F.get('frontage_ft')):,.0f} FT" if F.get("frontage_ft") else "—", f"FRONTAGE{' ON ' + e(F['fronting_roads'][0].upper()) if F.get('fronting_roads') else ''}"),
              (_money_short(inc) if inc else "—", f"MEDIAN HH INCOME{' — ' + e(str(dem.get('geography') or '').upper()) if dem.get('geography') else ''}")]
    p1 = page("01", "OPPORTUNITY SUMMARY — OVERVIEW", f'''
<div class="lbl">HIGHEST &amp; BEST USE</div>
<h2>{e(T.get("hbu_title") or F.get("use"))}</h2>
<p>{e(T.get("overview"))}</p>
{_stats(stats1)}
<div class="lbl">INVESTMENT HIGHLIGHTS</div>
<ul>{"".join(f"<li>{e(h)}</li>" for h in (T.get("highlights") or [])[:6])}</ul>
<div class="lbl">EXECUTIVE SUMMARY</div>
<p>{e(T.get("exec_summary"))}</p>''')

    uses = [tuple(e(c) for c in (list(r) + ["", "", "", ""])[:4]) for r in (T.get("uses") or [])[:8]]
    p2 = page("02", "LOCATION &amp; ZONING — REGULATORY", f'''
<div class="two">
<div><div class="lbl">CURRENT ZONING</div><h3>{e(T.get("zoning_h3"))}</h3><p>{e(T.get("zoning_text"))}</p></div>
<div><div class="lbl">SITE POSITION</div><h3>{e(T.get("site_h3"))}</h3><p>{e(T.get("site_text"))}</p></div></div>
<div class="lbl">PERMITTED USES BY ZONING ({e(F.get("district") or "")}) — {e((F.get("jurisdiction") or "").upper())}</div>
{_table([("USE CATEGORY", ""), ("PERMIT STATUS", ""), ("FIT FOR THIS SITE", ""), ("NOTES", "")], uses)}
<div class="lbl">ENVIRONMENTAL &amp; ACCESS STATUS</div>
{_boxes(T.get("env_boxes"))}''')

    pn = T.get("program_notes") or {}
    pb = {b["name"]: b for b in F["plan_buildings"]}
    prog_rows = []
    for b in F["buildings"]:
        n = b["name"]
        dims = pb.get(n) or {}
        dim = f" — {dims['w_ft']:.0f}' × {dims['d_ft']:.0f}'" if dims.get("w_ft") and dims.get("d_ft") else ""
        st = int(_num(b.get("stories"), 1)) or 1
        prog_rows.append((f"{e(n)} — {e(_tc(b.get('label') or b.get('use') or ''))}{dim}{f' ({st} stories)' if st > 1 else ''}",
                          f"{_num(b.get('footprint_sf')):,.0f} SF fp", f"{PF['sf'][n]:,.0f} SF", e(pn.get(n) or b.get("use") or ""),
                          "PHASE 1" if n in PF["phase1"] else "PHASE 2"))
    pave = max(0, _num(plan.get("impervious_sf")) - _num(plan.get("building_sf")))
    dev_ac = _num(plan.get("development_acres"))
    rem = _num(plan.get("remaining_developable_acres"))
    prog_rows += [("Parking, Drive Aisles &amp; Walks", f"±{pave:,.0f} SF", f"{plan.get('stalls') or '—'} spaces", e(T.get("parking_note") or ""), "PHASE 1"),
                  ("Site Access", "—", "1 access", e(T.get("access_note") or ""), "PHASE 1")]
    if rem > 0.05:
        prog_rows.append(("Remaining Developable Area", f"~{rem:.1f} AC", "—", "Future pad or expansion", "FUTURE"))
    if _num(F.get("wetland_acres")) > 0.05:
        prog_rows.append(("Mapped Wetlands (Preserved)", f"~{_num(F['wetland_acres']):.1f} AC", "—", "Left undisturbed; delineation pending", "—"))
    prog_rows.append((f"Total (Phase 1{'–2' if nb > 1 else ''})", f"~{dev_ac:.1f} AC disturbed", f"{tsf:,.0f} SF",
                      f"FAR {_num(plan.get('far')):.3f}; impervious ±{_num(plan.get('impervious_sf')):,.0f} SF ≈ {_num(plan.get('impervious_pct')):.0f}% of site", "—", {"cls": "tot"}))
    pt = (T.get("plan_text") or []) + ["", ""]
    p3 = page("03", "DEVELOPMENT PROGRAM — PHASED BUILD-OUT", f'''
{_stats([(f"{acres:g} AC", f"GROSS SITE — {acres * 43560:,.0f} SF LAND AREA"),
         (f"~{_num(F.get('wetland_acres')):.1f} AC" if _num(F.get("wetland_acres")) > 0.05 else f"~{_num(F.get('buildable_acres')):.1f} AC",
          "MAPPED WETLANDS" if _num(F.get("wetland_acres")) > 0.05 else "BUILDABLE AFTER SETBACKS"),
         (f"~{dev_ac:.1f} AC", "DEVELOPMENT POD (LIMIT OF DISTURBANCE)"), (f"{tsf:,.0f} SF", "TOTAL BUILT AREA, ALL PHASES")])}
<div class="lbl">PHASED DEVELOPMENT PROGRAM — {e((F.get("use") or "").upper())}</div>
{_table([("COMPONENT", ""), ("AREA", "r"), ("UNITS / SF", "r"), ("NOTES", ""), ("PHASE", "")], prog_rows)}
<div class="lbl">PRELIMINARY SITE PLAN</div>
<p>{e(pt[0])}</p><p>{e(pt[1])}</p>''')

    exA = f'<section class="land img"><img src="{plan_img}"></section>'
    exB = f'''<section class="land ex"><img src="{render_img}">
<div class="cap"><div><span class="g">EXHIBIT B1</span> &nbsp; AERIAL VIEW{(" FROM " + e(F["fronting_roads"][0].upper())) if F.get("fronting_roads") else ""} — {e((F.get("use") or "").upper())}</div><div class="r">Artist's conceptual rendering — illustrative only. Stormwater features, landscaping and surroundings are approximate; see Exhibit A for the governing layout.</div></div></section>'''

    permits = [tuple(e(r.get(k, "")) for k in ("permit", "agency", "timeline", "cost", "risk")) for r in (I.get("permits") or [])[:10]]
    p4 = page("04", f"PERMITTING ROADMAP — {e(I.get('entitlement_months') or '6–12')} MONTH ENTITLEMENT", f'''
<p>{e(T.get("permits_intro"))}</p>
<div class="lbl">REQUIRED APPROVALS &amp; PERMITS — ESTIMATED SEQUENCE (PHASE 1 THROUGH CERTIFICATE OF OCCUPANCY)</div>
{_table([("PERMIT / APPROVAL", ""), ("AGENCY", ""), ("EST. TIMELINE", ""), ("EST. COST", ""), ("RISK", "")], permits)}
{_boxes(T.get("permit_boxes"))}''')

    H = [("COST LINE ITEM", ""), ("QUANTITY", "r"), ("UNIT COST", "r"), ("SUBTOTAL", "r"), ("NOTES", "")]
    cr = lambda items: [(e(r[0]) if "&amp;" not in r[0] else r[0], e(r[1]) if "&" not in str(r[1]) else r[1], e(r[2]) if "&" not in str(r[2]) else r[2], f(r[3]), e(r[4]) if "&" not in str(r[4]) else r[4]) for r in items]  # noqa: E731
    all_rows = (["Land"] + cr(B_["land"]) if B_["land"] else []) + ["Site &amp; Infrastructure"] + cr(B_["site"]) + \
               ["Vertical Construction"] + cr(B_["vert"]) + ["Soft Costs"] + cr(B_["soft"])
    # page 05 holds the scenario cards + as many rows as fit (by estimated wrapped height); the rest goes to 05 cont.
    def _h(r):
        if isinstance(r, str):
            return 15.0
        import math as _m
        txt = lambda v: re.sub(r"<[^>]+>|&\w+;", "x", str(v))  # noqa: E731
        lines = max(_m.ceil(len(txt(r[0])) / 30), _m.ceil(len(txt(r[1])) / 14), _m.ceil(len(txt(r[2])) / 12),
                    _m.ceil(len(txt(r[4])) / 48), 1)
        return 10.0 + lines * 9.7
    budget, used, cut = 495.0, 0.0, 0
    for r in all_rows:
        if used + _h(r) > budget:
            break
        used += _h(r)
        cut += 1
    if cut and isinstance(all_rows[cut - 1], str):   # never leave a group header orphaned at the bottom
        cut -= 1
    rows1, rows2 = all_rows[:cut], all_rows[cut:]
    rows2 += [(f"Total Project Cost — {'Phase 1 + 2' if nb > 1 else 'All Phases'}", "", "", f"~{f(B_['tdc'])}", f"Rounded: ~${B_['tdc'] / 1e6:.2f}M", {"cls": "tot"}),
              ("Total Cost / SF (Building)", "", "", f"~${B_['tdc'] / tsf:,.0f} / SF", f"Full {acres:g} AC land basis carried by {tsf:,.0f} SF", {"cls": "tot"})]
    a_names = " + ".join(PF["phase1"])
    p5 = page("05", "PROFORMA — BASE CASE (ALL PHASES STABILIZED)", f'''
<div class="scen"><div><div class="sl">A — CONSERVATIVE</div><b>Phase 1 Only</b><p>{e(T.get("scenario_a") or a_names)}</p></div>
<div><div class="sl">B — BASE</div><b>Base Case</b><p>Full FORMA plan ({tsf:,.0f} SF); tenants on NNN leases, 95% occupancy</p></div>
<div><div class="sl">C — RESIDUAL</div><b>Land at $0</b><p>Base Case program with land and closing costs removed — tests what the land is worth</p></div></div>
<div class="lbl">TOTAL DEVELOPMENT COST — FULL PLAN ({tsf:,.0f} SF, {nb} BUILDING{"S" if nb != 1 else ""})</div>
{_table(H, rows1, "cost")}''')

    NH = [("COMPONENT", ""), ("SF", "r"), ("RENT NNN", "r"), ("OCC.", "r"), ("ANNUAL REVENUE", "r"), ("NOTES", "")]
    nrows = []
    for i, (n, s, r, g, m, basis) in enumerate(PF["noiB"][0]):
        nrows.append((f"{e(n)} — {e(_tc(next((b.get('label') or b.get('use') for b in F['buildings'] if b['name'] == n), '')))}",
                      f"{s:,}", f"${r:.0f} / SF", "95%", f(g), e(basis)))
        nrows.append(("Less: Management / Reserves (6%)", "", "", "", f"({f(m)})", "NNN lease; owner-side opex limited" if i == 0 else "", {}))
        if n == PF["phase1"][-1] and len(PF["phase1"]) < nb:
            nrows.append(("Phase 1 NOI", "", "", "", f(O["A"]["noi"]), f"Development yield: {O['A']['yld']:.1%} (on Scenario A TDC)", {"cls": "tot"}))
    nrows.append(("Combined NOI (All Phases)", "", "", "", f(O["B"]["noi"]), f"Development yield: {O['B']['yld']:.1%}", {"cls": "tot"}))
    p6 = page("05", "PROFORMA — CONTINUED", f'''
{_table(H, rows2, "cost")}
<div class="lbl">STABILIZED NOI — PHASE 1 &amp; ALL PHASES (YEAR 2 PROJECTION)</div>
{_table(NH, nrows)}
<p class="note">{e(T.get("noi_note"))} Scenario A carries the full site and entrance cost (TDC ≈ ${O["A"]["tdc"] / 1e6:.2f}M).</p>''')

    capp = f"{PF['cap'] * 100:.2f}".rstrip("0").rstrip(".") + "%"
    p7 = page("06", "RETURNS BY SCENARIO — THREE OUTCOMES", f'''
<div class="lbl">SCENARIO COMPARISON — CONSERVATIVE → RESIDUAL LAND TEST</div>
{_table([("SCENARIO", ""), ("TDC", "r"), ("STABILIZED NOI", "r"), ("DEV. YIELD", "r"), ("VALUE @ CAP RATE", "r"), ("PROFIT / (LOSS)", "r"), ("STATUS", "")], [
        (f"A — Conservative ({e(a_names)})", f(O["A"]["tdc"]), f(O["A"]["noi"]), f"{O['A']['yld']:.1%}", f"{f(O['A']['value'])} @ {capp} cap", fp(O["A"]["profit"]), "PHASE 1 ONLY"),
        ("B — Base Case (Full plan)", f(O["B"]["tdc"]), f(O["B"]["noi"]), f"{O['B']['yld']:.1%}", f"{f(O['B']['value'])} @ {capp} cap", fp(O["B"]["profit"]), "FULL FORMA PLAN"),
        ("C — Residual (Land at $0)", f(O["C"]["tdc"]), f(O["C"]["noi"]), f"{O['C']['yld']:.1%}", f"{f(O['C']['value'])} @ {capp} cap", fp(O["C"]["profit"]), "LAND VALUE TEST")])}
<p class="note">Figures above are FORMA preliminary estimates for illustrative purposes; see Section 07 for the residual land walk and disclaimer. {e(T.get("returns_note"))}</p>
{_boxes(T.get("returns_boxes"))}''')

    land_basis = e((I.get("land") or {}).get("basis") or ("Asking price" if priced else "FORMA estimate"))
    p8 = page("07", "RESIDUAL LAND — SCENARIO DETAIL", f'''
<div class="lbl">RESIDUAL LAND VALUE WALK</div>
{_table([("ITEM", ""), ("AMOUNT", "r"), ("BASIS", "")], [
        (f"Stabilized NOI ({tsf:,.0f} SF)", f(O["B"]["noi"]), "; ".join(f"${r[0]:.0f}/SF {e(n)}" for n, r in PF["rent"].items()) + ", NNN, 95% occ."),
        ("Market Cap Rate", capp, e(I.get("cap_note") or "")),
        ("Stabilized Value", f(O["C"]["value"]), f"{f(O['B']['noi'])} / {PF['cap']:.3f}"),
        ("Less: Development Cost Excluding Land", f"({f(C_['tdc'])})", f"Hard {f(C_['hard'])} + soft {f(C_['soft_total'])}"),
        ("Residual Land Value", fp(PF["resid"]), "Value the program supports for the land", {"cls": "tot"}),
        ("Asking Price" if priced else "Estimated Land Value", f(price), land_basis),
        ("Gap to " + ("Asking Price" if priced else "Land Value"), fp(PF["resid"] - price), "Residual value less land price", {"cls": "tot"})])}
<div class="lbl">BREAK-EVEN RENT — WHAT WOULD MAKE THE PROGRAM WORK</div>
{_table([("ITEM", ""), ("AMOUNT", "r"), ("BASIS", "")], [
        ("Market Blended Rent (NNN)", f"${PF['blend']:.0f} / SF", "SF-weighted average of the building rents"),
        ("Break-Even Rent — Land at " + ("Asking Price" if priced else "Est. Value"), f"${PF['be_ask']:.0f} / SF", f"{f(B_['tdc'] * PF['cap'])} NOI to support ${B_['tdc'] / 1e6:.2f}M TDC @ {capp}"),
        ("Break-Even Rent — Land at $0", f"${PF['be_zero']:.0f} / SF", f"{f(C_['tdc'] * PF['cap'])} NOI to support ${C_['tdc'] / 1e6:.2f}M @ {capp}"),
        ("Rent Gap (Land at " + ("Asking" if priced else "Est. Value") + ")", f"{'~' if PF['be_ask'] > PF['blend'] else ''}${PF['be_ask'] - PF['blend']:.0f} / SF",
         f"~{abs(PF['be_ask'] / PF['blend'] - 1):.0%} {'above' if PF['be_ask'] > PF['blend'] else 'below'} market" if PF["blend"] else "", {"cls": "tot"})])}
{_boxes(T.get("residual_boxes"))}
<div class="disc"><b>FORMA — Preliminary Development Analysis Disclaimer.</b> This document is prepared by FORMA as a preliminary feasibility and conceptual planning supplement intended to accompany a site plan for listing purposes. All cost estimates, revenue projections, cap rates, and development yields are based on publicly available market data, regional construction benchmarks, and industry comparables as of {e(f"Q{(today.month - 1) // 3 + 1} {today.year}")}. They are not guarantees of actual development costs or investment returns. This analysis does not constitute licensed engineering, architectural, legal, financial, or appraisal services. All figures must be verified by qualified licensed professionals prior to any investment decision, land acquisition, or construction commitment. Zoning, environmental, permitting requirements, utility capacity and fees, and traffic data are subject to change and must be confirmed directly with {e(T.get("disclaimer_agencies") or F.get("jurisdiction") or "the local jurisdiction")}. Wetland and flood limits shown are from public mapping and FORMA plan linework and are not a jurisdictional determination. The rendering (Exhibit B1) is a conceptual illustration only; Exhibit A governs layout and parking. {e(T.get("disclaimer_sources"))} FORMA makes no representation regarding the suitability of this site for any specific use.<br>
<b>FORMA · {e(code)} · PREPARED {month} · CONFIDENTIAL<br>PRELIMINARY ANALYSIS — NOT FOR FINANCING OR CONSTRUCTION</b></div>''')

    body = cover + p1 + p2 + p3 + exA + (exB if render_img else "") + p4 + p5 + p6 + p7 + p8
    return f'<html><head><meta charset="utf-8"><style>{_css()}</style></head><body>{body}</body></html>'


def package_name(F):
    return f"{', '.join(x for x in [F['address'], F['city']] if x)} - Package.pdf".replace("/", "-")


def build(job, plan_png, render_jpg, out_pdf, inputs=None, text=None):
    """Returns (F, inputs, text, proforma). Pass cached inputs/text to rebuild without new Claude calls."""
    import weasyprint
    F = facts(job)
    I = inputs or research_inputs(F)
    PF = proforma(F, I)
    T = text or write_text(F, I, PF)
    html = build_html(F, I, PF, T, "file://" + os.path.abspath(plan_png), "file://" + os.path.abspath(render_jpg) if render_jpg else None)
    with open(out_pdf.replace(".pdf", ".html"), "w") as fh:
        fh.write(html)
    weasyprint.HTML(string=html, base_url=os.path.dirname(os.path.abspath(out_pdf))).write_pdf(out_pdf)
    return F, I, T, PF
