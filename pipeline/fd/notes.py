"""Engineer-style numbered notes per sheet. Note 1 on every sheet is the purpose-of-plan statement; the standing notes
(town standards, 811, TBM, erosion control, rims at finish grade, ...) follow the order used on a typical civil plan set."""


def util_short(U):
    w, s = U.get("has_water", U.get("mode") != "onsite"), U.get("has_sewer", U.get("mode") != "onsite")
    return ("Public water" if w else "Well") + " / " + ("public sewer" if s else "septic") + (" (assumed)" if (w or s) else "")


def util_desc(U):
    w, s = U.get("has_water", U.get("mode") != "onsite"), U.get("has_sewer", U.get("mode") != "onsite")
    if w and s:
        return "assumed to be public water and sanitary sewer in the adjacent right-of-way; main location, depth and capacity must be confirmed with the utility."
    if not (w or s):
        return "by private well and septic system."
    if w:
        return "assumed to be public water in the adjacent right-of-way with a private septic system; the main location, depth and capacity must be confirmed with the utility."
    return "assumed to be public sanitary sewer in the adjacent right-of-way with a private well; the main location, depth and capacity must be confirmed with the utility."


def subject(meta, X):
    pl = meta.get("project_lines") or []
    place = ", ".join(pl[1:3]) if len(pl) > 1 else "THE SUBJECT PARCEL"
    return f"{place} CONSISTING OF {X['facts']['parcel_ac']:.2f} ±AC"


def purpose(meta, X, what):
    return f"The purpose of this plan is to {what} for the development of {subject(meta, X)}."


STD_WORK = "All work shall be performed in accordance with the requirements of the local municipality, the county and the state department of transportation, as applicable."
STD_811 = ("Forma does not warrant the location or elevation of the existing utilities or that all are shown. Contractor shall be responsible for contacting 811 "
           "(Dig Safe / One-Call) at least 72 hours prior to construction in accordance with state law.")
STD_TBM = "Contractor shall verify temporary bench mark (TBM) elevations prior to construction."
STD_EC = ("Contractor shall be responsible for providing and maintaining appropriate erosion control measures throughout construction in accordance with "
          "the state stormwater manual and US EPA best management practices.")
STD_RIMS = "All structures to have rims set at finish grade regardless of stated rim data."
STD_PRELIM = ("These plans are preliminary and not sealed. They are for planning and permit-preparation review only and are not for construction. A licensed engineer "
              "of record must review, revise and seal them before submission or use.")
STD_DISCREP = "Contractor shall notify the engineer of any discrepancy between these plans and field conditions before proceeding with the affected work."


def t1(meta, X):
    f = X["facts"]
    return [purpose(meta, X, "present the preliminary civil site plans") ,
            "These plans were prepared from a concept site layout and public data. Existing conditions are compiled from public GIS data and have not been field surveyed.",
            f"Zoning: {meta.get('zone_line') or 'verify with the municipality'}. Dimensional requirements shown on the Site Development Plan are assumed and must be confirmed with the zoning official.",
            "Topography is from USGS 3DEP lidar (NAVD 88). Buildings are from OSM / Microsoft footprints, wetlands from the National Wetlands Inventory and flood zones from FEMA. None of it is a field survey.",
            "Horizontal datum is NAD 83 and vertical datum is NAVD 88 unless otherwise noted. A field survey is required before final design.",
            "Utility service is " + util_desc(X["U"]),
            "Rainfall depths: " + X["rain"]["source"] + ". Soil group: " + X["soils"]["source"] + ".",
            STD_811, STD_DISCREP, STD_PRELIM]


def c1(meta, X):
    sb = X["L"]["setbacks_ft"]
    return [purpose(meta, X, "depict the existing conditions"),
            "Boundary shown is from county parcel GIS. It is not a boundary survey and carries no legal description.",
            "Topography is 3DEP lidar-derived at a 2 ft interval, NAVD 88. Verify with a field topographic survey.",
            "Building footprints, wetlands (NWI) and flood zones (FEMA) are from public datasets and are approximate.",
            "Existing utilities are not shown. Locate with the 811 one-call and a subsurface utility engineering survey before final design.",
            f"Zoning: {meta.get('zone_line') or 'verify'}. Setbacks used: front {sb.get('front')} ft, side {sb.get('side')} ft, rear {sb.get('rear')} ft. Confirm against the ordinance.",
            STD_811]


def c2(meta, X):
    return [purpose(meta, X, "depict the proposed site layout"), STD_WORK, STD_811,
            "All dimensions are to face of curb unless otherwise noted.",
            "Parking stall count, stall size and accessible (ADA) stalls shall be verified against the local code and the ADA Standards prior to construction.",
            "Pavement markings, signage and traffic control devices shall conform to the MUTCD.",
            "Proposed pavement sections, curb, sidewalk and detail sheets are to be provided in the final plan set.",
            "Site grading, drainage, utilities, landscaping and lighting are shown on sheets C4 through C8. Building architecture is by others.",
            STD_DISCREP]


def c3(meta, X):
    ER, ch = X["ER"], X["D"]["pond"].get("kind") == "chambers"
    return [purpose(meta, X, f"depict the minimum erosion control measures and demolition required") + f" Limits of disturbance: {ER['lod_ac']:.2f} AC.",
            STD_WORK,
            "Contractor is responsible for providing and maintaining erosion control measures throughout construction per the most recent edition of the state stormwater manual. Erosion control measures shall be removed and properly disposed of once the site is stable.",
            STD_811,
            "Install perimeter sediment controls and the stabilized construction entrance before any clearing, grubbing or grading.",
            "Silt fence or filter sock shall be inspected weekly and after every ½ inch rainfall, and maintained until the site is stabilized.",
            "Protect storm inlets and outlets with approved inlet protection as they are built.",
            "Stabilize all disturbed areas within 7 days of final grade with 4\" of suitable loam, seed and mulch, sod or pavement.",
            ("Do not discharge construction runoff to the underground chambers. Install the chambers after the site is stabilized and protect the inlets and outlet control manhole." if ch
             else "The detention pond shall serve as a sediment basin during construction and be cleaned out before final stabilization."),
            ("Disturbance is one acre or more: an NPDES Construction General Permit (NOI) and a SWPPP are required." if ER["npdes"] else "Disturbance is under one acre. Confirm local erosion control permit requirements."),
            "Demolition: remove existing structures, slabs and abandoned utilities as marked. Cap utilities at the main per the utility owner. Haul and dispose of debris per local rules.",
            "Limits of disturbance shall be clearly marked in the field. No work, stockpiling or equipment storage is permitted beyond it.",
            STD_DISCREP]


def c4(meta, X):
    return [purpose(meta, X, "depict the proposed grading"), STD_WORK, STD_811, STD_TBM, STD_EC, STD_RIMS,
            "All spot grades are at bottom of curb unless otherwise specified.",
            f"Pavement: maximum {meta['facts_max_slope']}% along the drive; cross slope 1.5% off the crown; accessible stalls and routes 2% maximum in any direction.",
            "Slab finished floor is set 0.5 ft above the highest adjacent pavement. Verify with architectural plans.",
            "Cut and fill slopes shall be 3H:1V maximum. Stabilize with 4\" of loam and seed, or sod.",
            "Earthwork quantities are from a 5 ft grid surface and are not adjusted for shrink, swell, topsoil stripping or pavement sections.",
            "Proposed contours are shown with heavy lines at a 2 ft interval (10 ft index). Existing contours are shown light and dashed.",
            "No grading, fill or disturbance is proposed within any mapped wetland or stream. Existing ground is held at the wetland boundary."] + list(X.get("wet_warnings") or [])


def c5(meta, X, extra=()):
    return [purpose(meta, X, "depict the proposed drainage system and stormwater calculations"), STD_WORK, STD_811, STD_TBM, STD_EC, STD_RIMS] + list(extra) + [
        "Stormwater management is designed for post-development peak flows not exceeding pre-development peak flows for the design storms listed in the calculations. A stormwater report and operation and maintenance plan shall accompany the final submission.",
        "Storm pipe is shown as the type, size and slope noted. Verify pipe materials, cover and structure sizes against the local standard details.",
        "Provide a minimum of 2 ft cover over pipe or the manufacturer's minimum, whichever is greater."]


def c6(meta, X, extra=()):
    return [purpose(meta, X, "depict the proposed utilities"), STD_WORK, STD_811, STD_TBM, STD_EC,
            "Final design and layout of the electric, telephone and gas services shall conform to the service provider standards.",
            STD_RIMS] + list(extra) + [
        "Water and sewer service lines shall be sized by the project's mechanical engineer. Sprinkler service, where required, shall be per NFPA 13.",
        "Existing utility locations are approximate. Verify the size, material, depth and location of all existing utilities at tie-in points by test pit before construction."]


def c7(meta, X, extra=()):
    return [purpose(meta, X, "depict the fire department review elements"),
            "Truck maneuvering shown is based on a 100 ft ladder truck apparatus unless noted.",
            "Fire department access roads shall be provided and maintained in accordance with NFPA 1, Chapter 18 and the IFC.",
            "Access roads shall provide a minimum unobstructed width of 20 ft and a minimum unobstructed vertical clearance of 13 ft 6 in, and shall be designed to support the imposed loads of responding apparatus (75,000 lb) with an all-weather driving surface.",
            "Access roads shall extend to within 50 ft of at least one exterior door that can be opened from the outside, and any portion of the first story of the building shall be within 150 ft of an access road (450 ft where the building has an NFPA 13, 13R or 13D system).",
            "Dead-end access roads in excess of 150 ft shall be provided with an approved turnaround.",
            "Proposed buildings shall be provided with an automatic sprinkler system designed and installed per NFPA 13 and the local fire department regulations.",
            "Underground water mains serving sprinkler or standpipe systems shall be flushed per NFPA 24 and witnessed by the fire department.",
            "Hydrant spacing and fire flow shall be confirmed with the water provider and the fire department. Plans for fire protection systems shall be submitted to the fire department for review and approval prior to construction."] + list(extra)


def c8(meta, X, extra=()):
    return [purpose(meta, X, "depict the proposed landscaping"),
            "All plants may be substituted for others of similar character as shown on this plan, with the approval of the owner's representative.",
            "Trees shall be complimented with suitable ground cover for protection from ground erosion.",
            "Contractor shall verify locations of all utilities and notify the owner's representative of conflicts.",
            "No plant materials shall be installed until all grading and construction has been completed in the immediate area.",
            "All trees shall be balled and burlapped (B & B) unless otherwise noted or approved by the owner's representative.",
            "Any proposed plant substitutions must be approved in writing by the owner's representative and must conform to the local landscape requirements.",
            "All plant materials installed shall meet or exceed the specifications of the \"American Standard for Nursery Stock\" by the American Association of Nurserymen.",
            "All plant material shall be guaranteed for one year following the date of acceptance.",
            "All disturbed areas shall be loamed and seeded and receive 4\" of suitable loam. Lawns with over 2:1 slopes shall be protected as noted on the erosion control notes and details.",
            "If applicable, the contractor shall have all fall transplanting hazard plants dug in the spring and stored for fall planting."] + list(extra)


def c9(meta, X, extra=()):
    return [purpose(meta, X, "depict the proposed lighting"),
            "Proposed lights shall be mounted at a maximum height per the lighting detail.",
            "Proposed light fixtures shall be full cutoff LED area luminaires or approved equal, dark-sky compliant, 70 CRI minimum at 3000 Kelvin or less.",
            "Photometrics are a simplified point-source (cosine cubed) estimate for a typical full-cutoff LED luminaire. Replace with a manufacturer IES calculation for permit submission.",
            "Target: 1.0 fc average and 0.2 fc minimum over parking and drives (IES RP-20). Confirm the local lighting ordinance for spill light, curfew and color temperature."] + list(extra)
