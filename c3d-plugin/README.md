# Drafted for Civil 3D

`DRAFTEDIMPORT` builds a Drafted design inside the open drawing as native Civil 3D objects, using that drawing's
(your firm template's) styles, parts lists and layers:

- EG and FG TIN surfaces (with the FG outer boundary)
- Storm and sanitary gravity pipe networks on your parts lists (sizes matched to the design, inverts and rims kept)
- Detention ponds as Civil 3D 2027 pond objects (contours every foot at 3:1), with the outfall pipe connected
- Grading feature lines (pavement edges, building pads at FFE) in a site
- The drive alignment with EG and design (layout) profiles, vertical curves included
- COGO points (building corners at FFE)
- Everything else (property, setbacks, buildings, pavement, striping, walks, water main and services, force main,
  electric, gas, erosion control) as linework on your layers

`DRAFTEDSTANDARDS` writes the open template's layers, styles and parts lists to a JSON file you can upload to Drafted
(Civil 3D template), so Drafted's DXF output uses your layer names too.

## Install

Unzip `Drafted.bundle.zip` into `%APPDATA%\Autodesk\ApplicationPlugins\` (so the folder is
`ApplicationPlugins\Drafted.bundle`) and restart Civil 3D. The commands load the first time you type them.
Or `NETLOAD` `Drafted.bundle\Contents\DraftedC3D.dll` for the current session.

## Use

1. New drawing from your firm's .dwt, coordinate system set to the zone in the package (manifest.json `crs.epsg`).
2. `DRAFTEDIMPORT`, pick `drafted_c3d.zip` from the Drafted plan set downloads.
3. Check the styles, parts lists and layer matches (your template's are preselected; choices are remembered) and Import.

Built by CI (`.github/workflows/c3d-plugin.yml`) against Autodesk's reference NuGet packages.
