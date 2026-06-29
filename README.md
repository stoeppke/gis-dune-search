# dune-search

Detect inland (eolian) dunes in **Brandenburg, Germany** by combining a 1 m
airborne-laser-scan DEM with surface masks derived from geological maps or
OpenStreetMap landcover.

Most Brandenburg dunes are low, forested, and not individually delineated on
maps. The approach here is simple and physically motivated: dunes are *steep
patches of sand*. So the pipeline intersects a **slope mask** computed from the
DGM1 elevation model with a **sand-surface mask**, vectorises the overlap into
candidate polygons, and ranks them.

## How it works

```
WGS84 point ──► AOI (EPSG:25833) ──► fetch DGM1 (WCS) ──► slope (Horn 3×3)
                                  └─► fetch sand mask ────────┐
                                                              ▼
                                     (slope ≥ threshold) ∩ sand ──► candidate polygons
                                                              ▼
                                              rank by area / slope, dedupe, render
```

Two sand-surface masks are available:

- **`geology`** (default) — sand-dominant lithology polygons from the LBGR/LGB
  Brandenburg GK25 (1:25 000) geological map, fetched via WFS. This captures
  dunes whose surface is forested but whose substrate is mapped as eolian or
  meltwater sand. Lithology is classified from the German `kennung`/`beschreibung`
  text fields (see `geology.py`).
- **`osm`** — `natural=sand|dune|bare_rock` polygons from OpenStreetMap via
  Overpass. This captures *visibly bare* sand at the surface, filtering out junk
  (sandpits, animal enclosures, sports pitches, sandboxes) and tiny features.

Two run modes:

- **Single-tile** — one square AOI around a point. Fetches DEM + geology, runs
  detection, and renders a hillshade/slope quicklook PNG.
- **Wide-AOI tiled search** (`--radius > 0`) — splits a circular area into
  ~15 km tiles, fetches and processes them concurrently, dissolves candidates
  across tile borders, and prints a spatially-diverse top-N with Google Maps
  satellite links.

## Data sources

All sources are public and require no API key:

| Source | Service | Used for |
| --- | --- | --- |
| Brandenburg DGM1 (1 m LiDAR DEM) | WCS 2.0.1 (`isk.geobasis-bb.de`) | elevation → slope |
| LBGR/LGB GK25 geological map | WFS 2.0.0 (`inspire.brandenburg.de`) | sand-substrate mask |
| OpenStreetMap landcover | Overpass API | bare-sand mask |

Native CRS throughout is **EPSG:25833** (UTM zone 33N).

## Install

The project uses [`uv`](https://docs.astral.sh/uv/). Python ≥ 3.11.

```bash
uv sync
```

Key dependencies: `rasterio`, `geopandas`, `shapely`, `pyproj`, `numpy`,
`scipy`, `matplotlib`, `requests`, `lxml`, `tqdm`.

## Usage

Run the end-to-end demo around the built-in reference dune (near Beelitz /
Glauer Berge):

```bash
uv run dune-search --demo
```

Single AOI around a custom point with a 3 km half-width:

```bash
uv run dune-search --lat 52.3149 --lon 13.1063 --half 3000 --out out/
```

Wide tiled search within a 40 km radius using the OSM bare-sand mask:

```bash
uv run dune-search --lat 52.3149 --lon 13.1063 --radius 40000 --mask osm --out out/
```

### Useful options

| Flag | Default | Meaning |
| --- | --- | --- |
| `--lat`, `--lon` | reference dune | AOI centre (WGS84) |
| `--half` | 2000 | half-width of square AOI in metres (single-tile mode) |
| `--radius` | 0 | if > 0, run the wide tiled search within this radius (m) |
| `--tile-size` | 15000 | tile edge length (m) for the tiled search |
| `--mask` | `geology` | wide-mode surface mask: `geology` or `osm` |
| `--scale-factor` | 0.2 | WCS downsample; 0.2 → 5 m grid, 1.0 → native 1 m (wide mode defaults to 0.1 / 10 m) |
| `--slope-threshold` | 8.0 | slope (°) above which a pixel is "steep" |
| `--min-area` | 500 | minimum candidate polygon area (m²) |
| `--strict-dune-mask` | off | restrict the geology mask to polygons explicitly mapped as *Düne* |
| `--osm-min-area` | 5000 | OSM polygon area threshold (m²) |
| `--top` | 5 | number of spatially-diverse top candidates to print |
| `--min-spacing` | 2000 | minimum spacing (m) between reported top candidates |
| `--workers` | 4 | concurrent tile/polygon workers (wide mode) |
| `--out` | `out/` | output directory |

## Output

Written to the `--out` directory (gitignored). Depending on mode:

- `dem.tif`, `slope.tif`, `hillshade.tif` — rasters
- `geology_all.gpkg`, `geology_sand.gpkg` — parsed geology layers
- `dune_candidates.gpkg` / `wide_candidates.gpkg` / `osm_candidates.gpkg` — candidates
- `wide_top.gpkg` / `osm_top.gpkg` — selected top-N
- `quicklook.png` — hillshade + slope QC panel (single-tile mode)

Each candidate carries `area_m2`, `mean_slope_deg`, `max_slope_deg` (and for OSM,
`p90_slope_deg`, `relief_m`, `score`).

## Module map

| Module | Responsibility |
| --- | --- |
| `cli.py` | argument parsing and the single-tile / wide / OSM run orchestration |
| `config.py` | service endpoints, defaults, reference-dune coordinates |
| `aoi.py` | WGS84 ↔ EPSG:25833 transforms, square AOI construction |
| `dem.py` | DGM1 WCS GetCoverage fetch |
| `geology.py` | GK25 WFS fetch, GML parsing, lithology classification |
| `slope.py` | Horn 3×3 slope and hillshade |
| `detect.py` | (slope ≥ threshold) ∩ sand → candidate polygons + slope stats |
| `wide.py` | tiled wide-AOI search, concurrency, cross-tile dissolve, diversity selection |
| `osm.py` | Overpass bare-sand polygon fetch and junk filtering |
| `osm_search.py` | per-polygon DEM + slope enrichment and scoring |
| `viz.py` | matplotlib quicklook |

## Notes & caveats

- This is a candidate-*generation* tool, not a validated classifier — expect
  false positives (road embankments, pit walls, kettle-hole rims) and review
  results against the satellite links / quicklook.
- Both web services return idiosyncratic responses (CRS stamping, unencoded
  WCS parentheses, BBOX axis order, HTML error pages with 200 status); see the
  inline comments in `dem.py` and `geology.py`.
- Region-specific: thresholds, the GK25 nomenclature, and the WCS/WFS endpoints
  are tuned for Brandenburg.
