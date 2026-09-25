#!/usr/bin/env python3
"""Mosaic the DSM (and orthophoto, if present) of several already-processed flights
into one seamless, georeferenced coverage — for large mine sites that a single
drone flight can't cover.

Usage:
  merge_flights.py --flights work/flights --out work/merge_out \
      [--crs EPSG:32638] [--resampling cubic]

Expects, for two or more flights:
  work/flights/<job_id>/dsm.tif      required (flight must have run in "survey" mode)
  work/flights/<job_id>/ortho.tif    optional

(the merge-3d workflow downloads and decrypts these before calling this script)

Writes into --out:
  dsm.tif       merged elevation model, reprojected to one common CRS
  ortho.tif     merged orthophoto (only produced if every flight has one)
  assets.json   list of the produced names, e.g. ["dsm.tif", "ortho.tif"]
  summary.json  small non-sensitive summary: source job ids, CRS, size, bounds,
                and any consistency warnings (see below)

Requires GDAL command-line tools (gdalbuildvrt, gdalwarp, gdal_translate,
gdalsrsinfo, gdalinfo) — the merge-3d workflow installs the `gdal-bin` package.
Kept to plain GDAL rather than a Python GIS library to keep the runner light.

CONSISTENCY CHECKS (best-effort, never fail the merge — they only warn):
  - Overlap check: after reprojecting every flight to the common CRS, if none of
    the flights' bounding boxes overlap at all, that's very likely a mistake (wrong
    job_ids, or flights of unrelated sites) rather than a legitimately gapped site,
    so a warning is printed and recorded in summary.json.
  - Vertical consistency check: for any two flights whose footprints DO overlap, a
    coarse elevation sample is compared in the overlap area. GDAL's reprojection
    only corrects the HORIZONTAL coordinate system — it does not know about or fix
    a vertical datum mismatch (e.g. one flight georeferenced with ellipsoidal GPS
    heights, another tied to a local benchmark/orthometric elevation). A large
    median offset between two overlapping flights is a strong sign of exactly that,
    and would silently corrupt any cut/fill volume computed on the merged result —
    so it is surfaced as a warning rather than left for someone to discover later
    in a wrong volume number.

KNOWN LIMITATION: this is a straightforward raster mosaic (last-pixel-wins on
overlaps), not a proper orthomosaic — there is no seamline feathering or
colour/exposure balancing between flights, so a seam may be visible where two
flights overlap in the orthophoto. Good enough for terrain/volume use; a nicer
blend is future work.
"""
import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

OVERLAP_VERTICAL_WARN_M = 0.5   # median elevation offset in an overlap area worth flagging
SAMPLE_GRID = 40                # coarse sample grid size (per side) for the vertical check


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"error running {' '.join(cmd)}:\n{r.stderr.strip()}")
    return r.stdout


def srs_of(path):
    out = run(["gdalsrsinfo", "-o", "epsg", str(path)]).strip()
    return out or None


def find_flights(root):
    flights = []
    root = Path(root)
    if not root.exists():
        sys.exit(f"error: {root} does not exist")
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        dsm = d / "dsm.tif"
        if not dsm.exists():
            print(f"warning: skipping flight {d.name} — no dsm.tif "
                  f"(it must have been run in \"survey\" mode)", file=sys.stderr)
            continue
        ortho = d / "ortho.tif"
        flights.append({"id": d.name, "dsm": dsm, "ortho": ortho if ortho.exists() else None})
    return flights


def reproject_or_copy(src, dst, src_crs, target_crs, resampling):
    if src_crs == target_crs:
        dst.write_bytes(src.read_bytes())
        return
    run(["gdalwarp", "-t_srs", target_crs, "-r", resampling, "-multi",
         "-co", "COMPRESS=DEFLATE", "-overwrite", str(src), str(dst)])


def mosaic(sources, dst, tmp_dir, resampling):
    vrt = tmp_dir / f"{dst.stem}.vrt"
    run(["gdalbuildvrt", "-r", resampling, str(vrt), *[str(s) for s in sources]])
    run(["gdal_translate", "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES", str(vrt), str(dst)])


def bounds_of(path):
    info = json.loads(run(["gdalinfo", "-json", str(path)]))
    xs = [c[0] for c in info["cornerCoordinates"].values()]
    ys = [c[1] for c in info["cornerCoordinates"].values()]
    return {"minX": min(xs), "maxX": max(xs), "minY": min(ys), "maxY": max(ys)}


def bbox_overlap(a, b):
    ox0, oy0 = max(a["minX"], b["minX"]), max(a["minY"], b["minY"])
    ox1, oy1 = min(a["maxX"], b["maxX"]), min(a["maxY"], b["maxY"])
    if ox1 <= ox0 or oy1 <= oy0:
        return None
    return ox0, oy0, ox1, oy1


def sample_elevations(path, minx, miny, maxx, maxy, tmp_dir, size=SAMPLE_GRID):
    """Coarse elevation sample of `path` over a window, as a flat list of z values
    (same grid size/order for any two calls with the same window, so two flights'
    samples over the same overlap window line up cell-for-cell)."""
    xyz = tmp_dir / f"_sample_{Path(path).stem}_{abs(hash((minx, miny, maxx, maxy)))}.xyz"
    run(["gdal_translate", "-projwin", str(minx), str(maxy), str(maxx), str(miny),
         "-outsize", str(size), str(size), "-of", "XYZ", str(path), str(xyz)])
    zs = []
    for line in xyz.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 3:
            zs.append(float(parts[2]))
    xyz.unlink(missing_ok=True)
    return zs


def check_consistency(bounds_by_id, path_by_id, tmp_dir, warnings):
    """Best-effort sanity checks across reprojected flights. Never raises — appends
    human-readable entries to `warnings` (also printed to stderr) instead."""
    ids = list(bounds_by_id)
    any_overlap = False
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a_id, b_id = ids[i], ids[j]
            window = bbox_overlap(bounds_by_id[a_id], bounds_by_id[b_id])
            if window is None:
                continue
            any_overlap = True
            try:
                za = sample_elevations(path_by_id[a_id], *window, tmp_dir)
                zb = sample_elevations(path_by_id[b_id], *window, tmp_dir)
            except SystemExit:
                continue  # sampling is best-effort; a failure here should not fail the merge
            n = min(len(za), len(zb))
            diffs = sorted(za[k] - zb[k] for k in range(n)
                            if not (math.isnan(za[k]) or math.isnan(zb[k])))
            if len(diffs) < 10:
                continue
            median = diffs[len(diffs) // 2]
            if abs(median) > OVERLAP_VERTICAL_WARN_M:
                msg = (f"warning: flights {a_id} and {b_id} overlap but differ by a median of "
                       f"{median:.2f} m in elevation there — possible vertical datum mismatch "
                       f"or georeferencing error between these two flights (this would silently "
                       f"skew any volume computed on the merged result)")
                print(msg, file=sys.stderr)
                warnings.append({"type": "vertical_mismatch", "flights": [a_id, b_id],
                                  "median_offset_m": round(median, 3)})

    if not any_overlap:
        msg = ("warning: none of the given flights spatially overlap after reprojection — "
               "double-check that these job_ids really are the same site")
        print(msg, file=sys.stderr)
        warnings.append({"type": "no_overlap", "message": msg})


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--flights", default="work/flights")
    ap.add_argument("--out", default="work/merge_out")
    ap.add_argument("--crs", default=None,
                     help="target EPSG code, e.g. EPSG:32638 (default: first flight's own CRS)")
    ap.add_argument("--resampling", default="cubic")
    a = ap.parse_args()

    flights = find_flights(a.flights)
    if len(flights) < 2:
        sys.exit(f"error: need at least 2 flights with a dsm.tif to merge (found {len(flights)})")

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "_tmp"
    tmp.mkdir(exist_ok=True)

    crs_by_id = {}
    for f in flights:
        crs = srs_of(f["dsm"])
        if not crs:
            sys.exit(f"error: flight {f['id']} has no coordinate reference system — "
                      "flights must be georeferenced (GPS EXIF, geo.txt or gcp_list.txt) to be merged")
        crs_by_id[f["id"]] = crs

    target_crs = a.crs or crs_by_id[flights[0]["id"]]

    have_ortho = all(f["ortho"] for f in flights)
    if not have_ortho:
        missing = [f["id"] for f in flights if not f["ortho"]]
        print(f"note: {len(missing)} flight(s) have no ortho.tif ({', '.join(missing)}) — "
              f"merged ortho.tif will be skipped", file=sys.stderr)

    dsm_reproj, ortho_reproj = [], []
    dsm_path_by_id = {}
    for f in flights:
        crs = crs_by_id[f["id"]]
        d_out = tmp / f"{f['id']}_dsm.tif"
        reproject_or_copy(f["dsm"], d_out, crs, target_crs, a.resampling)
        dsm_reproj.append(d_out)
        dsm_path_by_id[f["id"]] = d_out
        if have_ortho:
            o_out = tmp / f"{f['id']}_ortho.tif"
            reproject_or_copy(f["ortho"], o_out, crs, target_crs, "cubic")
            ortho_reproj.append(o_out)

    warnings = []
    try:
        bounds_by_id = {fid: bounds_of(p) for fid, p in dsm_path_by_id.items()}
        check_consistency(bounds_by_id, dsm_path_by_id, tmp, warnings)
    except SystemExit as exc:
        print(f"warning: consistency checks skipped ({exc})", file=sys.stderr)

    mosaic(dsm_reproj, out / "dsm.tif", tmp, a.resampling)
    produced = ["dsm.tif"]
    if have_ortho:
        mosaic(ortho_reproj, out / "ortho.tif", tmp, "cubic")
        produced.append("ortho.tif")

    info = json.loads(run(["gdalinfo", "-json", str(out / "dsm.tif")]))
    summary = {
        "flights": [f["id"] for f in flights],
        "flightCount": len(flights),
        "crs": target_crs,
        "size": info.get("size"),
        "cornerCoordinates": info.get("cornerCoordinates"),
    }
    if warnings:
        summary["warnings"] = warnings
    (out / "assets.json").write_text(json.dumps(produced), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    print("merged:", ", ".join(produced), "| crs:", target_crs, "| flights:", len(flights),
          f"| warnings: {len(warnings)}" if warnings else "")

    for p in tmp.glob("*"):
        p.unlink()
    tmp.rmdir()


if __name__ == "__main__":
    main()
