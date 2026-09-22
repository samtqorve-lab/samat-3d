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
  summary.json  small non-sensitive summary: source job ids, CRS, size, bounds

Requires GDAL command-line tools (gdalbuildvrt, gdalwarp, gdal_translate,
gdalsrsinfo, gdalinfo) — the merge-3d workflow installs the `gdal-bin` package.
Kept to plain GDAL rather than a Python GIS library to keep the runner light.

KNOWN LIMITATION: this is a straightforward raster mosaic (last-pixel-wins on
overlaps), not a proper orthomosaic — there is no seamline feathering or
colour/exposure balancing between flights, so a seam may be visible where two
flights overlap in the orthophoto. Good enough for terrain/volume use; a nicer
blend is future work.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path


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
    for f in flights:
        crs = crs_by_id[f["id"]]
        d_out = tmp / f"{f['id']}_dsm.tif"
        reproject_or_copy(f["dsm"], d_out, crs, target_crs, a.resampling)
        dsm_reproj.append(d_out)
        if have_ortho:
            o_out = tmp / f"{f['id']}_ortho.tif"
            reproject_or_copy(f["ortho"], o_out, crs, target_crs, "cubic")
            ortho_reproj.append(o_out)

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
    (out / "assets.json").write_text(json.dumps(produced), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    print("merged:", ", ".join(produced), "| crs:", target_crs, "| flights:", len(flights))

    for p in tmp.glob("*"):
        p.unlink()
    tmp.rmdir()


if __name__ == "__main__":
    main()
