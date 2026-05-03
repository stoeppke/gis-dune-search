"""Build a square AOI in EPSG:25833 around a WGS84 point."""
from __future__ import annotations

from dataclasses import dataclass

from pyproj import Transformer

from .config import NATIVE_CRS

_to_25833 = Transformer.from_crs("EPSG:4326", NATIVE_CRS, always_xy=True)
_to_4326 = Transformer.from_crs(NATIVE_CRS, "EPSG:4326", always_xy=True)


@dataclass(frozen=True)
class AOI:
    minx: float
    miny: float
    maxx: float
    maxy: float
    crs: str = NATIVE_CRS

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.minx, self.miny, self.maxx, self.maxy)

    @property
    def width_m(self) -> float:
        return self.maxx - self.minx

    @property
    def height_m(self) -> float:
        return self.maxy - self.miny


def aoi_from_lonlat(lon: float, lat: float, half_m: float) -> AOI:
    cx, cy = _to_25833.transform(lon, lat)
    return AOI(cx - half_m, cy - half_m, cx + half_m, cy + half_m)


def lonlat_from_25833(x: float, y: float) -> tuple[float, float]:
    lon, lat = _to_4326.transform(x, y)
    return lon, lat


def to_25833(lon: float, lat: float) -> tuple[float, float]:
    return _to_25833.transform(lon, lat)
