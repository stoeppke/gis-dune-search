"""Command-line entry point.

Examples
--------
Run the end-to-end demo around the reference dune::

    uv run dune-search --demo

Run with a custom centre and AOI radius::

    uv run dune-search --lat 52.3149 --lon 13.1063 --half 3000 --out out/
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .aoi import aoi_from_lonlat, to_25833
from .config import Defaults, REF_LAT, REF_LON
from .dem import fetch_dem
from .detect import detect_dunes
from .geology import load_dunes, load_sand, fetch_geology, parse_features
from .slope import compute_slope, hillshade
from .viz import quicklook
from .wide import run_wide_search, select_diverse


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dune-search", description=__doc__)
    p.add_argument("--lat", type=float, default=REF_LAT)
    p.add_argument("--lon", type=float, default=REF_LON)
    p.add_argument("--half", type=int, default=Defaults.aoi_half_m,
                   help="half-width of the square AOI in metres (single-tile mode)")
    p.add_argument("--radius", type=int, default=0,
                   help="if > 0, run the wide-AOI tiled search within this radius (m)")
    p.add_argument("--tile-size", type=int, default=15_000,
                   help="tile edge length (m) for the tiled search")
    p.add_argument("--top", type=int, default=5,
                   help="number of spatially-diverse top candidates to print")
    p.add_argument("--min-spacing", type=int, default=2_000,
                   help="minimum spacing (m) between reported top candidates")
    p.add_argument("--scale-factor", type=float, default=Defaults.scale_factor,
                   help="WCS SCALEFACTOR; 0.2 → 5 m grid (fast), 1.0 → native 1 m. "
                        "For wide searches the default is 0.1 (10 m grid).")
    p.add_argument("--slope-threshold", type=float, default=Defaults.slope_threshold_deg)
    p.add_argument("--min-area", type=float, default=Defaults.min_area_m2)
    p.add_argument("--strict-dune-mask", action="store_true",
                   help="restrict the mask to polygons explicitly mapped as Düne; the "
                        "default uses any sand-dominant surface (Düne, Talsand, "
                        "Schmelzwassersand) — many real dunes sit on top of Sander "
                        "and are not separately delineated at 1:25k.")
    p.add_argument("--workers", type=int, default=4,
                   help="concurrent tile workers (wide-AOI mode)")
    p.add_argument("--out", type=Path, default=Path("out"))
    p.add_argument("--demo", action="store_true",
                   help="alias for the defaults — runs around the reference dune")
    args = p.parse_args(argv)

    if args.radius > 0:
        return _run_wide(args)

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    aoi = aoi_from_lonlat(args.lon, args.lat, args.half)
    print(f"AOI (EPSG:25833): {aoi.bbox}, {aoi.width_m:.0f}×{aoi.height_m:.0f} m")

    dem_path = out / "dem.tif"
    print(f"[1/4] Fetching DGM1 → {dem_path}")
    fetch_dem(aoi, dem_path, scale_factor=args.scale_factor)

    geo_xml = out / "gk25.gml"
    print(f"[2/4] Fetching GK25 geology → {geo_xml}")
    fetch_geology(aoi, geo_xml)
    all_units = parse_features(geo_xml)
    sand = load_dunes(geo_xml) if args.strict_dune_mask else load_sand(geo_xml)
    mask_label = "Düne" if args.strict_dune_mask else "sand-bearing"
    all_units.to_file(out / "geology_all.gpkg", driver="GPKG")
    sand.to_file(out / "geology_sand.gpkg", driver="GPKG")
    print(f"      {len(all_units)} features total, {len(sand)} {mask_label}")

    slope_path = out / "slope.tif"
    print(f"[3/4] Computing slope → {slope_path}")
    compute_slope(dem_path, slope_path, smooth_sigma=1.0)
    hillshade(dem_path, out / "hillshade.tif")

    candidates_path = out / "dune_candidates.gpkg"
    print(f"[4/4] Detecting dunes (slope ≥ {args.slope_threshold}°) → {candidates_path}")
    candidates = detect_dunes(
        slope_path,
        sand,
        candidates_path,
        slope_threshold_deg=args.slope_threshold,
        min_area_m2=args.min_area,
    )
    print(f"      {len(candidates)} candidate polygons "
          f"(total area {candidates.area_m2.sum() if len(candidates) else 0:.0f} m²)")

    from .aoi import to_25833
    ref_x, ref_y = to_25833(args.lon, args.lat)

    if not candidates.empty:
        from shapely.geometry import Point
        ref = Point(ref_x, ref_y)
        hits = candidates[candidates.geometry.intersects(ref.buffer(50))]
        print(f"      reference point hits {len(hits)} candidate polygons "
              f"(buffer 50 m)")
        top = candidates.sort_values("area_m2", ascending=False).head(5)
        print("      largest candidates (area_m2 / mean / max slope °):")
        for _, r in top.iterrows():
            print(f"        - {r.area_m2:8.0f}  {r.mean_slope_deg:5.1f}  {r.max_slope_deg:5.1f}")

    print(f"[5/5] Rendering quicklook PNG → {out / 'quicklook.png'}")
    quicklook(
        out / "hillshade.tif",
        slope_path,
        sand,
        candidates,
        out / "quicklook.png",
        ref_point_25833=(ref_x, ref_y),
    )
    return 0


def _run_wide(args) -> int:
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    cx, cy = to_25833(args.lon, args.lat)
    print(f"Wide-AOI search: centre ({args.lat:.4f}, {args.lon:.4f}) "
          f"= ({cx:.0f}, {cy:.0f}) EPSG:25833, radius {args.radius} m")
    sf = args.scale_factor if args.scale_factor < 0.2 else 0.1

    cand = run_wide_search(
        args.lon, args.lat, args.radius, work_dir=out / "tiles",
        tile_m=args.tile_size, scale_factor=sf,
        slope_threshold_deg=args.slope_threshold,
        min_area_m2=args.min_area,
        workers=args.workers,
    )
    cand_path = out / "wide_candidates.gpkg"
    if not cand.empty:
        cand.to_file(cand_path, driver="GPKG")
    print(f"\n{len(cand)} merged candidate polygons → {cand_path}")
    if cand.empty:
        return 0

    top = select_diverse(
        cand, min_spacing_m=args.min_spacing, top=args.top,
        centre_25833=(cx, cy), max_radius_m=args.radius,
    )
    top.to_file(out / "wide_top.gpkg", driver="GPKG")

    from pyproj import Transformer
    t = Transformer.from_crs(25833, 4326, always_xy=True)
    print(f"\nTop {len(top)} (≥ {args.min_spacing} m apart):")
    print(f"{'#':>2}  {'area_m²':>9}  {'mean°':>5}  {'max°':>5}  "
          f"{'lat':>9}  {'lon':>9}  link")
    for i, r in enumerate(top.itertuples(), 1):
        c = r.geometry.centroid
        lon, lat = t.transform(c.x, c.y)
        url = (f"https://www.google.com/maps/place/{lat:.6f},{lon:.6f}/"
               f"@{lat:.6f},{lon:.6f},400m/data=!3m1!1e3")
        print(f"{i:>2}  {r.area_m2:>9.0f}  {r.mean_slope_deg:>5.1f}  "
              f"{r.max_slope_deg:>5.1f}  {lat:>9.5f}  {lon:>9.5f}  {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
