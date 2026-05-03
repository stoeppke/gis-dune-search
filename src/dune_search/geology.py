"""Fetch sand / dune polygons from the LBGR Brandenburg WFS.

The WFS does not honour `CQL_FILTER` reliably for this layer, so we fetch every
GK feature inside the AOI bounding box and filter client-side on the
`kennung` and `beschreibung` text fields.

Brandenburg's GK nomenclature encodes geomorphology in the `kennung` suffix:
  - ``,,d``       → Düne (eolian dune sand)
  - ``,,gf``      → glazifluviatile Sande (meltwater sands; sandy substrate but
                   not dunes)
  - ``,,ut``      → Talsand (broad valley/Urstromtal sand sheets)
  - ``,,p-f``     → periglazial-fluviatile Sande
  - ``,,l-f``     → See-/Altwassersande
  - ``,,b``       → Stillwasser-/Beckenablagerungen (often Feinsand)
The description text additionally contains words like "Duenen", "Flugsand",
"Windablagerungen", "Sand, fein-..." we can match on.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import geopandas as gpd
import requests
from lxml import etree
from shapely.geometry import MultiPolygon, Polygon

from .aoi import AOI
from .config import GK_TYPENAME, GK_WFS_URL, NATIVE_CRS

GML = "{http://www.opengis.net/gml/3.2}"
APP = "{http://www.deegree.org/app}"
WFS = "{http://www.opengis.net/wfs/2.0}"

# Indicators that a polygon represents an actual dune deposit.
DUNE_TOKENS = ("duenen", "duene", "flugsand", "windablagerung", "aeolisch")

# A polygon is sand-dominant when the lithology section (text after the *last*
# colon in the description) starts with one of these tokens. This is far more
# robust than substring-matching on "Sand" because moraine descriptions like
# "Schluff, sandig bis stark sandig, schwach kiesig, mit Steinen" are *not*
# sand-dominant and would otherwise leak into the result.
SAND_LITHO_PRIMARY = (
    "sand", "feinsand", "mittelsand", "grobsand",
    "fein- und mittelsand", "fein- bis mittelsand", "fein- und mittelkoerniger sand",
    "duenensand", "flugsand",
)


def _kennung_is_dune(kennung: str) -> bool:
    k = kennung.lower().strip()
    return bool(re.search(r"(^|[/, ])q[a-z0-9-]*,*,d(\b|$|[ /(])", k))


def _primary_lithology(beschreibung: str) -> str:
    """Return the lithology of the *surface* unit, lowercased & trimmed.

    Single-layer entries follow the pattern "Genetic unit (Subtype): Lithology".
    Compound entries chain layers with " - ueber " ("over"), with the surface
    layer first. We therefore split on the first " - ueber " (if any) before
    extracting the part after the first colon.
    """
    surface = beschreibung.split(" - ueber ")[0]
    if ":" not in surface:
        return surface.lower().strip()
    return surface.split(":", 1)[1].lower().strip()


def _is_sand_dominant(litho: str) -> bool:
    # Strip leading separators and check the first token group.
    stripped = litho.lstrip(" ,;")
    return any(stripped.startswith(t) for t in SAND_LITHO_PRIMARY)


def classify(kennung: str, beschreibung: str) -> str:
    """One of "dune", "sand", "other"."""
    text = beschreibung.lower()
    if _kennung_is_dune(kennung) or any(t in text for t in DUNE_TOKENS):
        return "dune"
    if _is_sand_dominant(_primary_lithology(beschreibung)):
        return "sand"
    return "other"


# Backwards compatibility — older code imported `_classify`.
_classify = classify


def fetch_geology(aoi: AOI, out_path: Path, typename: str = GK_TYPENAME,
                  timeout: int = 180) -> Path:
    """Download every feature whose bbox intersects the AOI as raw XML."""
    if aoi.crs != NATIVE_CRS:
        raise ValueError(f"Geology WFS expects {NATIVE_CRS}")

    # Note: this server expects the BBOX in (minx,miny,maxx,maxy) east/north
    # order with the short EPSG:25833 form. The urn-form rejects requests.
    bbox = f"{aoi.minx},{aoi.miny},{aoi.maxx},{aoi.maxy},EPSG:25833"
    params = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": typename,
        "BBOX": bbox,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(
        GK_WFS_URL,
        params=params,
        headers={"User-Agent": "dune-search/0.1"},
        timeout=timeout,
    ) as r:
        r.raise_for_status()
        out_path.write_bytes(r.content)
    return out_path


def _poslist_to_polygon(poslist: str) -> Polygon | None:
    nums = poslist.split()
    if len(nums) < 6:
        return None
    coords = [(float(nums[i]), float(nums[i + 1])) for i in range(0, len(nums), 2)]
    return Polygon(coords)


def _parse_polygons(elem: etree._Element) -> Iterable[Polygon]:
    # The WFS returns gml:MultiSurface > gml:surfaceMember > gml:Polygon.
    for surf in elem.iter(f"{GML}Polygon"):
        ext_ring = surf.find(f"{GML}exterior/{GML}LinearRing/{GML}posList")
        if ext_ring is None or not ext_ring.text:
            continue
        ext = _poslist_to_polygon(ext_ring.text)
        if ext is None:
            continue
        holes = []
        for inner in surf.findall(f"{GML}interior/{GML}LinearRing/{GML}posList"):
            if inner.text:
                h = _poslist_to_polygon(inner.text)
                if h is not None:
                    holes.append(list(h.exterior.coords))
        yield Polygon(list(ext.exterior.coords), holes)


def parse_features(xml_path: Path, typename: str = GK_TYPENAME) -> gpd.GeoDataFrame:
    tag = typename.split(":", 1)[-1]
    tree = etree.parse(str(xml_path))
    root = tree.getroot()

    rows = []
    for member in root.iter(f"{WFS}member"):
        feat = member.find(f"{APP}{tag}")
        if feat is None:
            continue
        kennung = feat.findtext(f"{APP}kennung", default="").strip()
        beschreibung = feat.findtext(f"{APP}beschreibung", default="").strip()
        alter = feat.findtext(f"{APP}alter", default="").strip()
        polys = list(_parse_polygons(feat))
        if not polys:
            continue
        geom = polys[0] if len(polys) == 1 else MultiPolygon(polys)
        rows.append(
            {
                "kennung": kennung,
                "beschreibung": beschreibung,
                "alter": alter,
                "klasse": classify(kennung, beschreibung),
                "geometry": geom,
            }
        )

    if not rows:
        return gpd.GeoDataFrame(
            {"kennung": [], "beschreibung": [], "alter": [], "klasse": [], "geometry": []},
            geometry="geometry", crs=NATIVE_CRS,
        )
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=NATIVE_CRS)


def load_dunes(xml_path: Path, typename: str = GK_TYPENAME) -> gpd.GeoDataFrame:
    """Return only the polygons mapped as dune deposits."""
    gdf = parse_features(xml_path, typename=typename)
    if gdf.empty:
        return gdf
    return gdf[gdf["klasse"] == "dune"].copy()


def load_sand(xml_path: Path, typename: str = GK_TYPENAME) -> gpd.GeoDataFrame:
    """Return polygons whose surface substrate is sand-dominant (incl. dunes)."""
    gdf = parse_features(xml_path, typename=typename)
    if gdf.empty:
        return gdf
    return gdf[gdf["klasse"].isin(("dune", "sand"))].copy()
