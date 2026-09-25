#!/usr/bin/env python3
"""Detect elevation change between two flights of the same site (a DEM of Difference).

Usage:
  change_detection.py --before before_dsm.tif --after after_dsm.tif \
      --out work/out/change_summary.json --diff-out work/out/change.tif \
      [--threshold 0.1]

Reprojects --before onto --after's exact grid (bilinear), computes after - before,
and reports cut/fill volumes plus how much area changed by more than --threshold
meters. Typical drone-photogrammetry DSM noise is a few centimeters, so small
diffs below --threshold are treated as no real change rather than counted as
cut/fill.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling


def read_grid(path):
    with rasterio.open(path) as src:
        arr = src.read(1).astype("float64")
        if src.nodata is not None:
            arr[arr == src.nodata] = np.nan
        return arr, src.profile, src.transform, src.crs


def align(src_path, profile, transform, crs):
    dst = np.full((profile["height"], profile["width"]), np.nan, dtype="float64")
    with rasterio.open(src_path) as s:
        reproject(source=rasterio.band(s, 1), destination=dst,
                  src_transform=s.transform, src_crs=s.crs,
                  dst_transform=transform, dst_crs=crs,
                  resampling=Resampling.bilinear,
                  src_nodata=s.nodata, dst_nodata=np.nan)
    return dst


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--before", required=True)
    ap.add_argument("--after", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--diff-out", default=None)
    ap.add_argument("--threshold", type=float, default=0.1,
                     help="meters; |diff| below this counts as no change")
    a = ap.parse_args()

    after, profile, transform, crs = read_grid(a.after)
    before = align(a.before, profile, transform, crs)
    pixel_area = abs(transform.a * transform.e)

    diff = after - before
    valid = ~np.isnan(diff)
    if not valid.any():
        sys.exit("error: no overlapping valid area between the two flights")
    d = diff[valid]

    changed = np.abs(d) >= a.threshold
    cut = -d[(d < 0) & changed].sum() * pixel_area
    fill = d[(d > 0) & changed].sum() * pixel_area

    result = {
        "before": a.before,
        "after": a.after,
        "threshold_m": a.threshold,
        "pixel_area_m2": round(pixel_area, 6),
        "overlap_pixels": int(valid.sum()),
        "changed_area_m2": round(float(changed.sum() * pixel_area), 2),
        "cut_m3": round(float(cut), 2),
        "fill_m3": round(float(fill), 2),
        "net_change_m3": round(float(fill - cut), 2),
        "max_cut_m": round(float(-d.min()), 3),
        "max_fill_m": round(float(d.max()), 3),
        "crs": str(crs),
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
