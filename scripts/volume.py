#!/usr/bin/env python3
"""Cut/fill volume between a survey DSM and a reference surface.

The reference is either an earlier flight's dsm.tif (co-registered volume, e.g.
month-over-month extraction) or a flat design elevation in meters (e.g. a known
bench/floor level from the mine plan) - useful for a single flight with no prior
survey to compare against.

Usage:
  volume.py --dsm work/project/odm_dem/dsm.tif --out work/out/volume.json \
      (--reference path/to/previous_dsm.tif | --reference-elevation 1234.5) \
      [--diff-out work/out/diff.tif]

Sign convention: diff = current - reference. Negative means the surface is now
lower (material removed -> cut/excavation); positive means higher (fill).
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling


def read_current(path):
    with rasterio.open(path) as src:
        arr = src.read(1).astype("float64")
        if src.nodata is not None:
            arr[arr == src.nodata] = np.nan
        return arr, src.profile, src.transform, src.crs


def read_reference_aligned(ref_path, profile, transform, crs):
    """Reproject/resample the reference DSM onto the current DSM's exact grid."""
    dst = np.full((profile["height"], profile["width"]), np.nan, dtype="float64")
    with rasterio.open(ref_path) as ref:
        reproject(
            source=rasterio.band(ref, 1),
            destination=dst,
            src_transform=ref.transform, src_crs=ref.crs,
            dst_transform=transform, dst_crs=crs,
            resampling=Resampling.bilinear,
            src_nodata=ref.nodata, dst_nodata=np.nan,
        )
    return dst


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsm", required=True)
    ap.add_argument("--reference", default=None, help="a previous flight's dsm.tif")
    ap.add_argument("--reference-elevation", type=float, default=None,
                     help="flat design elevation in meters instead of a reference DSM")
    ap.add_argument("--out", required=True)
    ap.add_argument("--diff-out", default=None, help="optional GeoTIFF of (current - reference)")
    a = ap.parse_args()

    if bool(a.reference) == bool(a.reference_elevation is not None):
        sys.exit("error: give exactly one of --reference or --reference-elevation")

    cur, profile, transform, crs = read_current(a.dsm)
    pixel_area = abs(transform.a * transform.e)

    if a.reference:
        ref = read_reference_aligned(a.reference, profile, transform, crs)
    else:
        ref = np.full_like(cur, a.reference_elevation)

    diff = cur - ref
    valid = ~np.isnan(diff)
    if not valid.any():
        sys.exit("error: no valid overlapping area between the DSM and the reference")
    d = diff[valid]

    cut = -d[d < 0].sum() * pixel_area
    fill = d[d > 0].sum() * pixel_area

    result = {
        "reference": a.reference or f"flat elevation {a.reference_elevation} m",
        "pixel_area_m2": round(pixel_area, 6),
        "valid_pixels": int(valid.sum()),
        "cut_m3": round(float(cut), 2),
        "fill_m3": round(float(fill), 2),
        "net_change_m3": round(float(fill - cut), 2),
        "crs": str(crs),
        "unit": "cubic meters",
    }
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))

    if a.diff_out:
        full = np.where(valid, diff, np.nan)
        out_profile = profile.copy()
        out_profile.update(dtype="float32", count=1, nodata=np.nan, compress="deflate")
        with rasterio.open(a.diff_out, "w", **out_profile) as dst:
            dst.write(full.astype("float32"), 1)


if __name__ == "__main__":
    main()
