"""Wide-AOI tiled search.

Splits a circular AOI of given radius around a centre point into ~15 km tiles,
fetches geology + DEM per tile (concurrently), runs the slope-on-sand detector
and returns a single deduplicated GeoDataFrame of candidates ranked by area.

The 15 km × 10 m tile (1500x1500 px ≈ 9 MB) is the largest the deegree WCS
serves reliably here.
"""
from __future__ import annotations

import concurrent.futures as cf
import math
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio import features
from scipy.ndimage import gaussian_filter
from shapely.geometry import Point, box, shape
from tqdm import tqdm

from .aoi import AOI, to_25833
from .config import GK_TYPENAME, NATIVE_CRS
from .dem import fetch_dem
from .geology import fetch_geology, parse_features


@dataclass(frozen=True)
class Tile:
    ix: int
    iy: int
    aoi: AOI

    @property
    def name(self) -> str:
        return f"tile_{self.ix:02d}_{self.iy:02d}"


def make_tiles(centre_lon: float, centre_lat: float, radius_m: int,
               tile_m: int = 15_000, overlap_m: int = 200) -> list[Tile]:
    cx, cy = to_25833(centre_lon, centre_lat)
    n = math.ceil(radius_m / tile_m)
    tiles: list[Tile] = []
    for ix in range(-n, n + 1):
        for iy in range(-n, n + 1):
            x0 = cx + ix * tile_m - overlap_m
            y0 = cy + iy * tile_m - overlap_m
            x1 = cx + (ix + 1) * tile_m + overlap_m
            y1 = cy + (iy + 1) * tile_m + overlap_m
            # Tile centre distance from AOI centre — keeps tiles whose centre
            # lies inside the requested radius.
            tx = (x0 + x1) / 2; ty = (y0 + y1) / 2
            if math.hypot(tx - cx, ty - cy) > radius_m + tile_m / 2:
                continue
            tiles.append(Tile(ix, iy, AOI(x0, y0, x1, y1)))
    return tiles


def _detect_in_tile(
    tile: Tile,
    work_dir: Path,
    scale_factor: float,
    typename: str,
    slope_threshold_deg: float,
    min_area_m2: float,
    smooth_sigma: float,
) -> gpd.GeoDataFrame | None:
    tdir = work_dir / tile.name
    tdir.mkdir(parents=True, exist_ok=True)

    geo_xml = tdir / "gk.gml"
    if not geo_xml.exists():
        try:
            fetch_geology(tile.aoi, geo_xml, typename=typename)
        except Exception:
            return None
    units = parse_features(geo_xml, typename=typename)
    if units.empty:
        return None
    sand = units[units["klasse"].isin(("dune", "sand"))].copy()
    if sand.empty:
        return None

    dem_path = tdir / "dem.tif"
    if not dem_path.exists():
        try:
            fetch_dem(tile.aoi, dem_path, scale_factor=scale_factor, timeout=180)
        except Exception:
            return None

    # In-memory slope + threshold mask — skipping the slope GeoTIFF write
    # saves a lot of disk IO across hundreds of tiles.
    with rasterio.open(dem_path) as src:
        z = src.read(1, masked=True).filled(np.nan).astype("float32")
        transform = src.transform
        dx = abs(src.transform.a); dy = abs(src.transform.e)
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

    steep = (slope >= slope_threshold_deg) & ~np.isnan(slope)
    if not steep.any():
        return None

    shapes = features.shapes(steep.astype("uint8"), mask=steep, transform=transform)
    polys = [shape(g) for g, v in shapes if v == 1]
    if not polys:
        return None
    steep_gdf = gpd.GeoDataFrame(geometry=polys, crs=NATIVE_CRS)

    sand_union = sand.unary_union
    cand = steep_gdf[steep_gdf.intersects(sand_union)].copy()
    if cand.empty:
        return None
    cand["geometry"] = cand.intersection(sand_union)
    cand = cand[~cand.is_empty]
    cand = cand.explode(index_parts=False, ignore_index=True)
    cand["area_m2"] = cand.geometry.area
    cand = cand[cand["area_m2"] >= min_area_m2].reset_index(drop=True)
    if cand.empty:
        return None

    # Per-polygon slope stats from the in-memory array.
    means, maxs = [], []
    for geom in cand.geometry:
        mask = features.rasterize(
            [(geom, 1)], out_shape=slope.shape, transform=transform,
            fill=0, dtype="uint8",
        ).astype(bool)
        vals = slope[mask & ~np.isnan(slope)]
        if vals.size:
            means.append(float(vals.mean())); maxs.append(float(vals.max()))
        else:
            means.append(np.nan); maxs.append(np.nan)
    cand["mean_slope_deg"] = means
    cand["max_slope_deg"] = maxs

    # Tile bbox sometimes contains polygons in the overlap collar shared with
    # adjacent tiles. Tag the tile centre so we can deduplicate later if any
    # candidate straddles two tiles.
    cand["tile"] = tile.name
    return cand


def run_wide_search(
    centre_lon: float,
    centre_lat: float,
    radius_m: int,
    work_dir: Path,
    *,
    tile_m: int = 15_000,
    scale_factor: float = 0.1,
    typename: str = GK_TYPENAME,
    slope_threshold_deg: float = 8.0,
    min_area_m2: float = 1500.0,
    smooth_sigma: float = 1.5,
    workers: int = 4,
) -> gpd.GeoDataFrame:
    work_dir.mkdir(parents=True, exist_ok=True)
    tiles = make_tiles(centre_lon, centre_lat, radius_m, tile_m=tile_m)
    print(f"  → {len(tiles)} tiles ({tile_m/1000:.0f} km each)")

    cands: list[gpd.GeoDataFrame] = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(
                _detect_in_tile, t, work_dir, scale_factor, typename,
                slope_threshold_deg, min_area_m2, smooth_sigma,
            ): t
            for t in tiles
        }
        for fut in tqdm(cf.as_completed(futures), total=len(futures), desc="tiles"):
            res = fut.result()
            if res is not None and not res.empty:
                cands.append(res)

    if not cands:
        return gpd.GeoDataFrame(columns=["geometry", "area_m2"], crs=NATIVE_CRS,
                                geometry="geometry")
    merged = gpd.GeoDataFrame(pd.concat(cands, ignore_index=True), crs=NATIVE_CRS)

    # Final dissolve: candidates that straddle tile borders show up twice
    # because of the overlap collar. unary_union over the buffered geometry
    # collapses them into one.
    buffered = merged.buffer(1.0)
    dissolved_geom = gpd.GeoSeries(buffered, crs=NATIVE_CRS).union_all()
    if dissolved_geom.geom_type == "Polygon":
        parts = [dissolved_geom]
    else:
        parts = list(dissolved_geom.geoms)
    out = gpd.GeoDataFrame(geometry=[p.buffer(-1.0) for p in parts], crs=NATIVE_CRS)
    out = out[~out.is_empty].reset_index(drop=True)
    out["area_m2"] = out.geometry.area

    # Re-attach slope stats by sampling the most overlapping tile candidate.
    means, maxs = [], []
    for geom in out.geometry:
        hits = merged[merged.intersects(geom.buffer(1))]
        if hits.empty:
            means.append(np.nan); maxs.append(np.nan)
        else:
            means.append(float(hits["mean_slope_deg"].mean()))
            maxs.append(float(hits["max_slope_deg"].max()))
    out["mean_slope_deg"] = means
    out["max_slope_deg"] = maxs
    return out.sort_values("area_m2", ascending=False).reset_index(drop=True)


def select_diverse(cands: gpd.GeoDataFrame, min_spacing_m: float, top: int = 5,
                   centre_25833: tuple[float, float] | None = None,
                   max_radius_m: int | None = None) -> gpd.GeoDataFrame:
    if cands.empty:
        return cands
    pool = cands.copy()
    if centre_25833 is not None and max_radius_m is not None:
        cx, cy = centre_25833
        pool["_d"] = pool.geometry.centroid.apply(lambda p: math.hypot(p.x - cx, p.y - cy))
        pool = pool[pool["_d"] <= max_radius_m].drop(columns="_d")

    pool = pool.sort_values("area_m2", ascending=False).reset_index(drop=True)
    kept_idx: list[int] = []
    kept_centroids: list[Point] = []
    for i, row in pool.iterrows():
        c = row.geometry.centroid
        if all(c.distance(k) >= min_spacing_m for k in kept_centroids):
            kept_idx.append(i); kept_centroids.append(c)
        if len(kept_idx) >= top:
            break
    return pool.iloc[kept_idx].reset_index(drop=True)
