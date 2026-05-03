"""Quick-look PNG for visual QC of a dune-detection run."""
from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import rasterio


def quicklook(
    hillshade_path: Path,
    slope_path: Path,
    sand_polys: gpd.GeoDataFrame,
    candidates: gpd.GeoDataFrame,
    out_path: Path,
    ref_point_25833: tuple[float, float] | None = None,
) -> Path:
    with rasterio.open(hillshade_path) as src:
        hs = src.read(1)
        extent = (src.bounds.left, src.bounds.right, src.bounds.bottom, src.bounds.top)
        bounds = src.bounds
    with rasterio.open(slope_path) as src:
        slope = src.read(1)

    # Clip vector overlays to the raster bounds so the panel doesn't zoom out.
    from shapely.geometry import box
    aoi_poly = box(bounds.left, bounds.bottom, bounds.right, bounds.top)
    if not sand_polys.empty:
        sand_polys = sand_polys.copy()
        sand_polys["geometry"] = sand_polys.geometry.intersection(aoi_poly)
        sand_polys = sand_polys[~sand_polys.is_empty]
    if not candidates.empty:
        candidates = candidates.copy()
        candidates["geometry"] = candidates.geometry.intersection(aoi_poly)
        candidates = candidates[~candidates.is_empty]

    fig, axes = plt.subplots(1, 2, figsize=(14, 7), constrained_layout=True)

    axes[0].imshow(hs, cmap="gray", extent=extent, origin="upper", vmin=0, vmax=255)
    axes[0].set_xlim(bounds.left, bounds.right)
    axes[0].set_ylim(bounds.bottom, bounds.top)
    axes[0].set_title("Hillshade + sand polygons + dune candidates")
    if not sand_polys.empty:
        sand_polys.boundary.plot(ax=axes[0], color="tan", linewidth=0.6)
    if not candidates.empty:
        candidates.plot(ax=axes[0], facecolor="red", edgecolor="darkred",
                        alpha=0.45, linewidth=0.4)
    if ref_point_25833 is not None:
        axes[0].scatter(*ref_point_25833, c="cyan", s=80, marker="x",
                        linewidths=2, label="reference")
        axes[0].legend(loc="lower left")

    im = axes[1].imshow(slope, cmap="magma", extent=extent, origin="upper",
                       vmin=0, vmax=np.nanpercentile(slope, 99))
    axes[1].set_title("Slope (degrees)")
    fig.colorbar(im, ax=axes[1], label="slope (°)")

    for ax in axes:
        ax.set_xlabel("X (EPSG:25833)")
        ax.set_ylabel("Y (EPSG:25833)")
        ax.ticklabel_format(useOffset=False, style="plain")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    return out_path
