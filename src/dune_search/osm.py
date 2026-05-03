"""Fetch OSM bare-sand polygons via Overpass.

We use the OSM landcover tags to find places where sand is *visibly* on the
surface (as opposed to the geological-map mask, which marks polygons whose
substrate is sand but whose surface may be forest). The relevant tags are:

  - ``natural=sand``       Open sand patches.
  - ``natural=dune``       Mapped dunes (rare; most dunes in Brandenburg are
                           tagged as ``natural=sand`` instead).
  - ``natural=bare_rock``  Occasionally used for exposed sandy outcrops.

OSM is also full of small sand features that are *not* what we want — animal
enclosures, sandpits, beach-volleyball courts, sandboxes — so we filter those
out by tag and drop polygons below a minimum area.
"""
from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import requests
from shapely.geometry import Polygon

from .config import NATIVE_CRS

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# If a way carries any of these (key, value) pairs we drop it.
JUNK_TAGS: set[tuple[str, str]] = {
    ("attraction", "animal"),
    ("tourism", "attraction"),
    ("tourism", "zoo"),
    ("leisure", "playground"),
    ("leisure", "pitch"),
    ("leisure", "fitness_station"),
    ("leisure", "swimming_area"),
    ("leisure", "horse_riding"),
    ("leisure", "track"),
    ("playground", "yes"),
    ("playground", "sandpit"),
    ("playground", "swing"),
    ("playground", "climbingframe"),
}
# Any value of these keys is considered junk.
JUNK_KEYS: set[str] = {"sport", "amenity", "playground", "man_made"}


def _is_junk(tags: dict) -> bool:
    if any(k in tags for k in JUNK_KEYS):
        return True
    return any((k, v) in JUNK_TAGS for k, v in tags.items())


def fetch_osm_sand(
    centre_lon: float, centre_lat: float, radius_m: int,
    cache_path: Path | None = None, timeout: int = 90,
    min_area_m2: float = 5_000.0,
) -> gpd.GeoDataFrame:
    """Return surface-sand polygons within `radius_m` of (lat, lon), in EPSG:25833."""
    query = f"""
[out:json][timeout:{timeout}];
(
  way["natural"~"^(sand|dune|bare_rock)$"](around:{radius_m},{centre_lat},{centre_lon});
  rel["natural"~"^(sand|dune)$"](around:{radius_m},{centre_lat},{centre_lon});
);
out geom;
"""
    if cache_path and cache_path.exists():
        import json
        data = json.loads(cache_path.read_text())
    else:
        r = requests.post(OVERPASS_URL, data={"data": query},
                          headers={"User-Agent": "dune-search/0.1"},
                          timeout=timeout + 30)
        r.raise_for_status()
        data = r.json()
        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(r.text)

    rows = []
    for elem in data.get("elements", []):
        if elem.get("type") != "way":
            continue  # multi-polygon relations: skip in v1; rare here
        geom = elem.get("geometry") or []
        if len(geom) < 4:
            continue
        coords = [(p["lon"], p["lat"]) for p in geom]
        if coords[0] != coords[-1]:
            continue
        tags = elem.get("tags", {})
        if _is_junk(tags):
            continue
        rows.append({
            "osm_id": elem["id"],
            "natural": tags.get("natural", ""),
            "name": tags.get("name", ""),
            "geometry": Polygon(coords),
        })

    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    if gdf.empty:
        return gdf.to_crs(NATIVE_CRS)
    gdf = gdf.to_crs(NATIVE_CRS)
    gdf["area_m2"] = gdf.geometry.area
    gdf = gdf[gdf["area_m2"] >= min_area_m2].reset_index(drop=True)
    return gdf
