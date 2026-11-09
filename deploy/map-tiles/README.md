# Map tiles — the Form Builder Location field

The Location field shows a map when this folder holds this install's own map.
Empty (the default) = no map: the person enters latitude / longitude or uses
"Use my location". Nothing is ever fetched from a third-party tile server.

The gateway serves two paths from here, read-only, to anyone (the portal is
anonymous; a basemap holds no tenant data):

| File | Served at | Needed for |
|---|---|---|
| `basemap.pmtiles` | `/api/v1/map/basemap.pmtiles` | the map |
| `fonts/<font>/<range>.pbf` | `/api/v1/map/fonts/…` | place and street names (optional) |

## Getting the files (once, on a machine with internet access)

1. The map — a Protomaps basemap (OpenStreetMap data), cut to your area with the
   `pmtiles` tool (https://github.com/protomaps/go-pmtiles):

       pmtiles extract https://build.protomaps.com/<YYYYMMDD>.pmtiles basemap.pmtiles \
         --bbox=<west>,<south>,<east>,<north> --maxzoom=15

   A country is typically 0.5–5 GB; the whole planet is ~120 GB.
2. Names (optional) — the `fonts/` folder of
   https://github.com/protomaps/basemaps-assets (Noto Sans Regular, Medium, Italic).

Copy both here, so the folder holds `basemap.pmtiles` and `fonts/`. The gateway
reads the folder live — no restart. The map opens on the extract's area and
cannot be panned outside it.

Attribution: the map shows "© OpenStreetMap", as the data licence (ODbL) requires.
