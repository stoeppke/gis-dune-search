"""Endpoints, default parameters, and the reference dune location."""
from __future__ import annotations

from dataclasses import dataclass

# Brandenburg DGM1 (1 m airborne-laser-scan DEM) via WCS 2.0.1.
# Single Float32 elevation band; native CRS EPSG:25833 (UTM 33N).
DGM_WCS_URL = "https://isk.geobasis-bb.de/ows/dgm_wcs"
DGM_COVERAGE_ID = "bb_dgm"

# LBGR/LGB Brandenburg geological maps WFS 2.0.0 (deegree).
# Feature types: app:gk25 (1:25k), app:gk100, app:gk300.
GK_WFS_URL = "https://inspire.brandenburg.de/services/gk_wfs"
GK_TYPENAME = "app:gk25"

NATIVE_CRS = "EPSG:25833"

# Reference: known dune location near Beelitz/Glauer Berge area.
# 52°18'53.6"N 13°06'22.8"E
REF_LAT = 52 + 18 / 60 + 53.6 / 3600
REF_LON = 13 + 6 / 60 + 22.8 / 3600


@dataclass(frozen=True)
class Defaults:
    # Half-width of the square AOI around the reference point, in metres.
    aoi_half_m: int = 2000
    # WCS scale factor — 1.0 keeps the native 1 m grid; 0.2 yields a 5 m grid
    # which is plenty for catching dune morphology and ~25× faster to process.
    scale_factor: float = 0.2
    # Slope threshold (degrees) above which a pixel is considered "steep".
    slope_threshold_deg: float = 8.0
    # Minimum candidate dune area (m²) to keep after vectorisation.
    min_area_m2: float = 500.0
