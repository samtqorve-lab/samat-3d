#!/usr/bin/env python3
"""Cut/fill volume between a survey DSM and a reference surface.

The reference is either an earlier flight's dsm.tif (co-registered volume, e.g.
month-over-month extraction) or a flat design elevation in meters (e.g. a known
bench/floor level from the mine plan) - useful for a single flight with no prior
survey to compare against.

Usage:
  volume.py --dsm work/project/odm_dem/dsm.tif --out work/out/volume.json \
      (--reference path/to/previous_dsm.tif | --reference-elevation 1234.5) \
      [--diff-out work/out/diff.tif] [--aoi pit_boundary.geojson] [--threshold 0.05]

Sign convention: diff = current - reference. Negative means the surface is now
lower (material removed -> cut/excavation); positive means higher (fill).

Memory: the raster is processed in horizontal strips (--block-rows), so even a
multi-hundred-megapixel 5 cm DSM needs only a few hundred MB of RAM.

--aoi    GeoJSON polygon(s) in the DSM's CRS; only pixels inside are counted. Without it
         the whole DSM (including undisturbed ground around the pit) is counted.
--threshold  ignore |diff| below this many meters (DSM noise); default 0 = count everything.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.warp import reproject, Resampling
from rasterio.windows import Window, transform as window_transform

DEFAULT_BLOCK_ROWS = 512


def load_aoi_geometries(path):
    """Polygon geometries from a GeoJSON file (FeatureCollection, Feature or bare geometry)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    kind = data.get("type")
    if kind == "FeatureCollection":
        geoms = [f["geometry"] for f in data.get("features", []) if f.get("geometry")]
    elif kind == "Feature":
        geoms = [data["geometry"]]
    else:
        geoms = [data]
    geoms = [g for g in geoms if g.get("type") in ("Polygon", "MultiPolygon")]
    if not geoms:
        sys.exit("error: --aoi contains no Polygon/MultiPolygon geometry")
    return geoms


def read_block(src, window):
    arr = src.read(1, window=window, masked=True).astype("float32").filled(np.nan)
    return arr


def reference_block(ref_src, dst_crs, dst_transform, shape):
    dst = np.full(shape, np.nan, dtype="float32")
    reproject(
        source=rasterio.band(ref_src, 1), destination=dst,
        src_transform=ref_src.transform, src_crs=ref_src.crs,
        dst_transform=dst_transform, dst_crs=dst_crs,
        resampling=Resampling.bilinear, src_nodata=ref_src.nodata, dst_nodata=np.nan,
    )
    return dst


def compute(dsm_path, reference=None, reference_elevation=None, diff_out=None,
            aoi=None, threshold=0.0, block_rows=DEFAULT_BLOCK_ROWS):
    geoms = load_aoi_geometries(aoi) if aoi else None
    cut = fill = 0.0
    valid_pixels = 0
    ref_src = rasterio.open(reference) if reference else None
    dst = None
    try:
        with rasterio.open(dsm_path) as src:
            pixel_area = abs(src.transform.a * src.transform.e)
            profile, crs = src.profile, src.crs
            if diff_out:
                out_profile = profile.copy()
                out_profile.update(dtype="float32", count=1, nodata=np.nan, compress="deflate",
                                   tiled=True, blockxsize=256, blockysize=256, bigtiff="IF_SAFER")
                dst = rasterio.open(diff_out, "w", **out_profile)
            for row in range(0, src.height, block_rows):
                win = Window(0, row, src.width, min(block_rows, src.height - row))
                cur = read_block(src, win)
                wtransform = window_transform(win, src.transform)
                if ref_src is not None:
                    ref = reference_block(ref_src, crs, wtransform, cur.shape)
                else:
                    ref = np.float32(reference_elevation)
                diff = cur - ref
                if geoms is not None:
                    outside = geometry_mask(geoms, out_shape=cur.shape, transform=wtransform, invert=False)
                    diff = np.where(outside, np.nan, diff)
                if threshold > 0:
                    with np.errstate(invalid="ignore"):
                        small = np.abs(diff) < threshold
                    diff = np.where(small, np.float32(0), diff).astype("float32")
                valid = ~np.isnan(diff)
                n = int(valid.sum())
                valid_pixels += n
                if n:
                    d = diff[valid]
                    cut += float(-d[d < 0].sum(dtype="float64"))
                    fill += float(d[d > 0].sum(dtype="float64"))
                if dst is not None:
                    dst.write(diff.astype("float32"), 1, window=win)
    finally:
        if ref_src is not None:
            ref_src.close()
        if dst is not None:
            dst.close()

    if valid_pixels == 0:
        sys.exit("error: no valid overlapping area between the DSM and the reference (or AOI)")
    cut *= pixel_area
    fill *= pixel_area
    return {
        "reference": reference or f"flat elevation {reference_elevation} m",
        "pixel_area_m2": round(pixel_area, 6),
        "valid_pixels": valid_pixels,
        "area_m2": round(valid_pixels * pixel_area, 2),
        "aoi": str(aoi) if aoi else None,
        "threshold_m": threshold,
        "cut_m3": round(cut, 2),
        "fill_m3": round(fill, 2),
        "net_change_m3": round(fill - cut, 2),
        "crs": str(crs),
        "unit": "cubic meters",
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsm", required=True)
    ap.add_argument("--reference", default=None, help="a previous flight's dsm.tif")
    ap.add_argument("--reference-elevation", type=float, default=None,
                    help="flat design elevation in meters instead of a reference DSM")
    ap.add_argument("--out", required=True)
    ap.add_argument("--diff-out", default=None, help="optional GeoTIFF of (current - reference)")
    ap.add_argument("--aoi", default=None, help="GeoJSON polygon(s) in the DSM CRS limiting the volume area")
    ap.add_argument("--threshold", type=float, default=0.0, help="ignore |diff| below this many meters")
    ap.add_argument("--block-rows", type=int, default=DEFAULT_BLOCK_ROWS)
    a = ap.parse_args()

    if bool(a.reference) == bool(a.reference_elevation is not None):
        sys.exit("error: give exactly one of --reference or --reference-elevation")
    if a.threshold < 0:
        sys.exit("error: --threshold must be >= 0")

    result = compute(a.dsm, a.reference, a.reference_elevation, a.diff_out,
                     a.aoi, a.threshold, a.block_rows)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
