# CLAUDE.md

Guidance for Claude Code (and other AI agents) working in this repository.

## What this is

`dune-search` detects inland (eolian) dunes in Brandenburg, Germany by
intersecting a slope mask derived from the 1 m DGM1 LiDAR DEM with a
sand-surface mask (geological map or OpenStreetMap landcover), then vectorising
and ranking the overlap. See `README.md` for the user-facing overview.

## Environment & commands

The project uses [`uv`](https://docs.astral.sh/uv/) and requires Python ≥ 3.11.

```bash
uv sync                       # install/refresh the environment
uv run dune-search --demo     # end-to-end run around the reference dune
uv run dune-search --help     # all CLI options
uv run ruff check src         # lint (ruff is the configured linter)
uv run ruff format src        # format
```

There is **no test suite** and **no CI** in this repo yet. Verify changes by
running the CLI (the `--demo` path exercises the full single-tile pipeline) and
inspecting the generated `out/quicklook.png` / GeoPackages.

⚠️ Running the pipeline hits live public web services (Brandenburg WCS/WFS,
Overpass). Network access is required and runs can be slow. There is no offline
fixture set — be mindful before kicking off a wide search.

## Architecture

The pipeline is a sequence of pure-ish functions, one per module, wired together
in `cli.py`. Data flows:

1. `aoi.py` — turn a WGS84 point into a square AOI in **EPSG:25833** (the native
   CRS used everywhere; do not introduce other CRSs without an explicit reason).
2. `dem.py` — fetch DGM1 elevation via WCS GetCoverage.
3. `geology.py` — fetch GK25 features via WFS, parse GML to a `GeoDataFrame`,
   classify each polygon as `dune` / `sand` / `other`.
4. `slope.py` — Horn 3×3 slope (degrees) and hillshade from the DEM.
5. `detect.py` — `(slope ≥ threshold) ∩ sand polygons` → candidate polygons with
   per-polygon slope stats.
6. `wide.py` — tiled wide-AOI orchestration (concurrent fetch/detect per tile,
   cross-tile dissolve via buffer trick, spatially-diverse top-N).
7. `osm.py` + `osm_search.py` — the alternative OSM bare-sand mask path.
8. `viz.py` — matplotlib quicklook.

`config.py` is the single source of truth for service endpoints, default
parameters (`Defaults`), and the reference-dune coordinates.

## Conventions

- **Style**: ruff, line length 100, target py311. `from __future__ import
  annotations` at the top of every module. Match the existing terse,
  comment-rich style — non-obvious GIS/service quirks are explained inline and
  that documentation is load-bearing.
- **CRS**: everything works in EPSG:25833. Convert to WGS84 only at the
  boundaries (input point, output Google Maps links).
- **Slope computation** is duplicated (intentionally) in `slope.py`, `wide.py`,
  and `osm_search.py` — `wide.py`/`osm_search.py` inline it to keep slope in
  memory and avoid writing a GeoTIFF per tile/polygon. If you change the Horn
  algorithm, change all three.
- **Geology classification** lives in `geology.classify()`; the lithology token
  lists and `kennung` regex are tuned to the German GK25 nomenclature. `_classify`
  is kept as a backwards-compat alias — don't remove it casually.
- **Outputs** go to `out/` (and `out_*`, `data/`), all gitignored. Never commit
  generated rasters, GeoPackages, GML, or PNGs.

## Web-service gotchas (already handled — keep them)

These are non-obvious and easy to "fix" into breakage:

- **WCS** (`dem.py`): the query string is built by hand so the `SUBSET=x(...)`
  parentheses stay **unencoded** — deegree's WAF returns HTTP 403 for `%28`/`%29`.
  The returned GeoTIFF has no GeoKeys, so the CRS is stamped in manually.
- **WFS** (`geology.py`): `CQL_FILTER` is unreliable for this layer, so all
  features in the bbox are fetched and filtered client-side. BBOX must use the
  short `EPSG:25833` form in east/north order; the urn form is rejected.
- Both servers sometimes return an **HTML error page with a 200 status** —
  content-type is checked rather than relying on the status code.
- **Overpass** (`osm.py`): junk landcover (sandpits, sports pitches, animal
  enclosures, sandboxes) is filtered by tag; multipolygon relations are skipped.

## Git workflow

- Develop on the designated feature branch; commit with clear messages; push
  with `git push -u origin <branch>`.
- Do **not** open a pull request unless explicitly asked.
- Do not put model identifiers in commits, code, or any pushed artifact.
