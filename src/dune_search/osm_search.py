"""Detect dunes by intersecting OSM bare-sand polygons with DEM-derived slope.

Workflow:
  1. Fetch every ``natural=sand|dune|bare_rock`` polygon within ``radius_m`` of
     the centre, dropping junk tags (sandpits, animal enclosures, sports
     pitches, …) and very small polygons.
  2. For each polygon, fetch a small DEM tile covering its bbox plus a buffer
     (so we can also see the steep flanks at the edges), compute slope, and
     report the mean / max / 90th-percentile slope inside the polygon.
  3. Rank by ``score = sqrt(area) * p90_slope``. ``sqrt(area)`` rewards larger
     features without letting a single huge mostly-flat surface dominate, and
     p90 of slope captures dune flanks rather than averages.
  4. Pick a spatially-diverse top-N (≥ ``min_spacing`` apart).
"""
from __future__ import annotations

import concurrent.futures as cf
import math
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio import features
from scipy.ndimage import gaussian_filter
from tqdm import tqdm

from .aoi import AOI, to_25833
from .config import NATIVE_CRS
from .dem import fetch_dem
from .osm import fetch_osm_sand


def _slope_stats(dem_path: Path, polygon, smooth_sigma: float = 1.0) -> dict:
    with rasterio.open(dem_path) as src:
        z = src.read(1, masked=True).filled(np.nan).astype("float32")
        transform = src.transform
        dx = abs(src.transform.a); dy = abs(src.transform.e)
        out_shape = src.shape

    if smooth_sigma:
        flat = np.where(np.isnan(z), 0.0, z)
        flat = gaussian_filter(flat, smooth_sigma)
        z = np.where(np.isnan(z), np.nan, flat)

    p = np.pad(z, 1, mode="edge")
    a = p[:-2, :-2]; b = p[:-2, 1:-1]; c = p[:-2, 2:]
    d = p[1:-1, :-2]; f = p[1:-1, 2:]
    g = p[2:, :-2]; h = p[2:, 1:-1]; i = p[2:, 2:]
    dzdx = ((c + 2 * f + i) - (a + 2 * d + g)) / (8 * dx)
    dzdy = ((g + 2 * h + i) - (a + 2 * b + c)) / (8 * dy)
    slope = np.degrees(np.arctan(np.hypot(dzdx, dzdy))).astype("float32")
    slope[np.isnan(z)] = np.nan

    mask = features.rasterize(
        [(polygon, 1)], out_shape=out_shape, transform=transform,
        fill=0, dtype="uint8",
    ).astype(bool)
    inside = slope[mask & ~np.isnan(slope)]
    relief = np.nan
    z_in = z[mask & ~np.isnan(z)]
    if z_in.size:
        relief = float(np.nanmax(z_in) - np.nanmin(z_in))
    if not inside.size:
        return {"mean_slope_deg": np.nan, "p90_slope_deg": np.nan,
                "max_slope_deg": np.nan, "relief_m": relief}
    return {
        "mean_slope_deg": float(inside.mean()),
        "p90_slope_deg": float(np.percentile(inside, 90)),
        "max_slope_deg": float(inside.max()),
        "relief_m": relief,
    }


def _enrich_polygon(row, work_dir: Path, scale_factor: float, buffer_m: float) -> dict:
    geom = row.geometry
    minx, miny, maxx, maxy = geom.bounds
    aoi = AOI(minx - buffer_m, miny - buffer_m, maxx + buffer_m, maxy + buffer_m)
    dem_path = work_dir / f"osm_{row.osm_id}.tif"
    if not dem_path.exists():
        try:
            fetch_dem(aoi, dem_path, scale_factor=scale_factor, timeout=120)
        except Exception as e:
            return {"osm_id": row.osm_id, "error": str(e)[:80]}
    try:
        stats = _slope_stats(dem_path, geom, smooth_sigma=1.0)
    except Exception as e:
        return {"osm_id": row.osm_id, "error": str(e)[:80]}
    stats["osm_id"] = row.osm_id
    return stats


def run_osm_search(
    centre_lon: float, centre_lat: float, radius_m: int, work_dir: Path,
    *,
    min_polygon_area_m2: float = 5_000.0,
    scale_factor: float = 0.1,        # 10 m grid
    polygon_buffer_m: float = 100.0,  # context around the polygon
    workers: int = 6,
) -> gpd.GeoDataFrame:
    work_dir.mkdir(parents=True, exist_ok=True)
    cache = work_dir / "osm_sand.json"
    print("  → fetching OSM bare-sand polygons via Overpass …")
    polys = fetch_osm_sand(
        centre_lon, centre_lat, radius_m,
        cache_path=cache, min_area_m2=min_polygon_area_m2,
    )
    print(f"  → {len(polys)} OSM sand polygons after filter "
          f"(area ≥ {min_polygon_area_m2/1e4:.1f} ha)")
    if polys.empty:
        return polys

    dem_dir = work_dir / "dem"
    dem_dir.mkdir(parents=True, exist_ok=True)

    enriched = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(_enrich_polygon, row, dem_dir, scale_factor, polygon_buffer_m): row
            for row in polys.itertuples()
        }
        for fut in tqdm(cf.as_completed(futs), total=len(futs), desc="OSM polys"):
            enriched.append(fut.result())

    stats = gpd.GeoDataFrame(enriched).set_index("osm_id")
    polys = polys.set_index("osm_id").join(stats).reset_index()
    polys = polys.dropna(subset=["mean_slope_deg"]).copy()

    polys["score"] = np.sqrt(polys["area_m2"]) * polys["p90_slope_deg"].fillna(0)
    return polys.sort_values("score", ascending=False).reset_index(drop=True)


def select_diverse(cands: gpd.GeoDataFrame, min_spacing_m: float, top: int = 5,
                   sort_col: str = "score") -> gpd.GeoDataFrame:
    if cands.empty:
        return cands
    pool = cands.sort_values(sort_col, ascending=False).reset_index(drop=True)
    kept_idx, kept_cents = [], []
    for i, row in pool.iterrows():
        c = row.geometry.centroid
        if all(c.distance(k) >= min_spacing_m for k in kept_cents):
            kept_idx.append(i); kept_cents.append(c)
        if len(kept_idx) >= top:
            break
    return pool.iloc[kept_idx].reset_index(drop=True)
