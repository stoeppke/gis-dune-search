"""Fetch a DGM1 elevation raster from the Brandenburg WCS."""
from __future__ import annotations

from pathlib import Path

import rasterio
import requests
from rasterio.crs import CRS

from .aoi import AOI
from .config import DGM_COVERAGE_ID, DGM_WCS_URL

# WCS GetCoverage for the bb_dgm coverage. SCALEFACTOR<1 down-samples on the
# server side, so we only transfer what we actually need.
_WCS_USER_AGENT = "dune-search/0.1 (+https://github.com/stoeppke/gis-dune-search)"


def fetch_dem(aoi: AOI, out_path: Path, scale_factor: float = 1.0,
              timeout: int = 180) -> Path:
    if aoi.crs != "EPSG:25833":
        raise ValueError("DGM WCS expects EPSG:25833 AOI")

    # WCS 2.0.1 expects multiple SUBSET parameters with literal parentheses.
    # Building the query string by hand keeps the parens unencoded — deegree's
    # WAF rejects "%28"/"%29" forms here with HTTP 403.
    query = (
        "SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage"
        f"&COVERAGEID={DGM_COVERAGE_ID}&FORMAT=image/tiff"
        f"&SUBSET=x({aoi.minx},{aoi.maxx})"
        f"&SUBSET=y({aoi.miny},{aoi.maxy})"
        "&SUBSETTINGCRS=http://www.opengis.net/def/crs/EPSG/0/25833"
        "&OUTPUTCRS=http://www.opengis.net/def/crs/EPSG/0/25833"
        f"&SCALEFACTOR={scale_factor}"
    )
    url = f"{DGM_WCS_URL}?{query}"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(
        url,
        headers={"User-Agent": _WCS_USER_AGENT},
        timeout=timeout,
        stream=True,
    ) as r:
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        if "tiff" not in ct.lower():
            # Server returns an HTML error page instead of 4xx in some cases.
            body = r.content[:500].decode("utf-8", errors="replace")
            raise RuntimeError(f"WCS did not return a GeoTIFF (content-type={ct}): {body}")
        with out_path.open("wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 20):
                fh.write(chunk)

    # The deegree WCS returns a GeoTIFF with the affine transform but no
    # GeoKeys, so rasterio.open returns CRS=None. Stamp the native CRS in.
    with rasterio.open(out_path, "r+") as ds:
        if ds.crs is None:
            ds.crs = CRS.from_epsg(25833)
    return out_path
