"""Combine slope mask + sand polygons into candidate dune polygons."""
from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio import features
from shapely.geometry import shape

from .config import NATIVE_CRS


def detect_dunes(
    slope_path: Path,
    sand_polys: gpd.GeoDataFrame,
    out_path: Path,
    slope_threshold_deg: float = 8.0,
    min_area_m2: float = 500.0,
) -> gpd.GeoDataFrame:
    """Vectorise (slope > threshold) ∩ sand_polys and write a GeoPackage.

    "sand_polys" should be the dune-classified GK25 polygons; passing the
    broader sand layer instead also works and yields more candidates.
    """
    with rasterio.open(slope_path) as src:
        slope = src.read(1)
        transform = src.transform
        crs = src.crs
        nodata = src.nodata

    if str(crs).split(":")[-1] != "25833":
        raise ValueError(f"slope raster CRS {crs} != EPSG:25833")

    valid = ~np.isnan(slope) if nodata is None or np.isnan(nodata) else slope != nodata
    steep_mask = (slope >= slope_threshold_deg) & valid
    if not steep_mask.any():
        empty = gpd.GeoDataFrame(
            {"area_m2": [], "mean_slope_deg": [], "max_slope_deg": []},
            geometry=[],
            crs=NATIVE_CRS,
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        empty.to_file(out_path, driver="GPKG")
        return empty

    # Vectorise the boolean steep-mask → polygons.
    shapes = features.shapes(
        steep_mask.astype("uint8"), mask=steep_mask, transform=transform
    )
    polys = [shape(g) for g, v in shapes if v == 1]
    steep = gpd.GeoDataFrame(geometry=polys, crs=NATIVE_CRS)

    if sand_polys.empty:
        candidates = steep.copy()
    else:
        sand_union = sand_polys.unary_union
        candidates = steep[steep.intersects(sand_union)].copy()
        candidates["geometry"] = candidates.intersection(sand_union)
        candidates = candidates[~candidates.is_empty]

    candidates = candidates.explode(index_parts=False, ignore_index=True)
    candidates["area_m2"] = candidates.geometry.area
    candidates = candidates[candidates["area_m2"] >= min_area_m2].reset_index(drop=True)

    # Per-polygon slope stats (sample the raster).
    if not candidates.empty:
        with rasterio.open(slope_path) as src:
            mean_slopes, max_slopes = [], []
            for geom in candidates.geometry:
                mask = features.rasterize(
                    [(geom, 1)],
                    out_shape=src.shape,
                    transform=src.transform,
                    fill=0,
                    dtype="uint8",
                ).astype(bool)
                vals = slope[mask & valid]
                if vals.size:
                    mean_slopes.append(float(vals.mean()))
                    max_slopes.append(float(vals.max()))
                else:
                    mean_slopes.append(np.nan)
                    max_slopes.append(np.nan)
        candidates["mean_slope_deg"] = mean_slopes
        candidates["max_slope_deg"] = max_slopes

    out_path.parent.mkdir(parents=True, exist_ok=True)
    candidates.to_file(out_path, driver="GPKG")
    return candidates
