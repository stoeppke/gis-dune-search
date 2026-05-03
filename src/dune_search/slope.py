"""Compute slope (degrees) from a DEM raster."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling


def compute_slope(dem_path: Path, slope_path: Path, smooth_sigma: float | None = None) -> Path:
    """Read DEM, compute slope in degrees with the Horn 3x3 algorithm, write GeoTIFF.

    Setting `smooth_sigma` applies a small Gaussian pre-filter to suppress
    LiDAR noise before the slope is taken. A sigma of 1-2 px is usually enough.
    """
    with rasterio.open(dem_path) as src:
        dem = src.read(1, masked=True).astype("float32")
        profile = src.profile.copy()
        # Pixel size in metres in EPSG:25833 is just abs(transform).
        dx = abs(src.transform.a)
        dy = abs(src.transform.e)

    if smooth_sigma:
        from scipy.ndimage import gaussian_filter

        filled = dem.filled(np.nan)
        smooth = gaussian_filter(np.where(np.isnan(filled), 0.0, filled), smooth_sigma)
        dem = np.ma.array(smooth, mask=dem.mask)

    # Horn (1981) algorithm — same as GDAL's gdaldem slope.
    z = dem.filled(np.nan)
    # Pad by replicating edges so we keep the original shape.
    p = np.pad(z, 1, mode="edge")
    a = p[:-2, :-2]; b = p[:-2, 1:-1]; c = p[:-2, 2:]
    d = p[1:-1, :-2]; f = p[1:-1, 2:]
    g = p[2:, :-2]; h = p[2:, 1:-1]; i = p[2:, 2:]
    dzdx = ((c + 2 * f + i) - (a + 2 * d + g)) / (8 * dx)
    dzdy = ((g + 2 * h + i) - (a + 2 * b + c)) / (8 * dy)
    slope_rad = np.arctan(np.hypot(dzdx, dzdy))
    slope_deg = np.degrees(slope_rad).astype("float32")
    slope_deg[np.isnan(z)] = np.nan

    profile.update(dtype="float32", count=1, nodata=np.float32(np.nan), compress="deflate")
    slope_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(slope_path, "w", **profile) as dst:
        dst.write(slope_deg, 1)
    return slope_path


def hillshade(dem_path: Path, hs_path: Path, azimuth: float = 315.0,
              altitude: float = 45.0) -> Path:
    """Quick hillshade for visual QC."""
    with rasterio.open(dem_path) as src:
        z = src.read(1, masked=True).filled(np.nan).astype("float32")
        profile = src.profile.copy()
        dx = abs(src.transform.a); dy = abs(src.transform.e)

    p = np.pad(z, 1, mode="edge")
    a = p[:-2, :-2]; b = p[:-2, 1:-1]; c = p[:-2, 2:]
    d = p[1:-1, :-2]; f = p[1:-1, 2:]
    g = p[2:, :-2]; h = p[2:, 1:-1]; i = p[2:, 2:]
    dzdx = ((c + 2 * f + i) - (a + 2 * d + g)) / (8 * dx)
    dzdy = ((g + 2 * h + i) - (a + 2 * b + c)) / (8 * dy)
    slope = np.arctan(np.hypot(dzdx, dzdy))
    aspect = np.arctan2(dzdy, -dzdx)
    az = np.deg2rad(360.0 - azimuth + 90.0)
    alt = np.deg2rad(altitude)
    shaded = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    shaded = np.clip(shaded, 0.0, 1.0)
    out = (255 * shaded).astype("uint8")
    out[np.isnan(z)] = 0

    profile.update(dtype="uint8", count=1, nodata=0, compress="deflate")
    with rasterio.open(hs_path, "w", **profile) as dst:
        dst.write(out, 1)
    return hs_path
