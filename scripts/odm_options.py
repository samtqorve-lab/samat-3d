#!/usr/bin/env python3
"""Build the OpenDroneMap command-line options for a job.

The app puts a small job.json in the encrypted upload:

  {
    "version": 1,
    "mode": "preview" | "survey",           # survey = accurate DSM for volume work
    "georef": "exif" | "geo" | "gcp",       # camera GPS | PPK/RTK positions file | ground control points
    "gpsAccuracy": 0.05,                    # meters, used with georef=geo
    "demResolution": 5,                     # cm/pixel of the DSM (survey)
    "quality": "standard" | "high",         # survey only
    "ortho": false,                         # also build the orthophoto (survey)
    "crs": "EPSG:32638",                    # optional output CRS
    "referenceElevation": 1234.5            # optional: design floor (m) for volume.py cut/fill (survey only)
  }

Usage:
  odm_options.py --job work/job.json --images work/project/images [--override "..."]

Prints one line of options to stdout. Problems are reported on stderr with exit code 2, so the
workflow fails in the first minute instead of after hours of processing.
"""
import argparse
import json
import re
import sys
from pathlib import Path

# The project folder is mounted at /datasets inside the ODM container (see build-3d.yml).
CONTAINER_IMAGES = "/datasets/project/images"

PREVIEW = [
    "--pc-quality", "low", "--feature-quality", "low",
    "--mesh-size", "100000", "--mesh-octree-depth", "10",
    "--skip-orthophoto", "--skip-report", "--gltf", "--max-concurrency", "4",
]

MIN_GCP = 3
MIN_GCP_RECOMMENDED = 5


def fail(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(2)


def note(msg):
    print(msg, file=sys.stderr)


def load_job(path):
    if not path or not Path(path).exists():
        return {}
    try:
        job = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"job.json is not valid JSON ({exc})")
    if not isinstance(job, dict):
        fail("job.json must be a JSON object")
    return job


def number(job, key, default, lo, hi):
    v = job.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo <= v <= hi):
        fail(f"job.json: {key} must be a number between {lo} and {hi}")
    return v


def data_lines(path):
    """Non-empty, non-comment lines after the CRS header."""
    lines = [ln.strip() for ln in Path(path).read_text(encoding="utf-8").splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    if not lines:
        fail(f"{Path(path).name} is empty")
    header, rows = lines[0], lines[1:]
    if not (header.upper().startswith("EPSG:") or header.startswith("+proj")):
        fail(f"{Path(path).name}: first line must be the coordinate system, e.g. EPSG:32638 (got: {header[:40]!r})")
    return header, rows


def check_names(rows, name_col, images_dir, label):
    have = {p.name for p in Path(images_dir).iterdir() if p.suffix.lower() in (".jpg", ".jpeg")}
    referenced = set()
    for r in rows:
        parts = r.split()
        if len(parts) > name_col:
            referenced.add(parts[name_col])
    matched = referenced & have
    if not matched:
        fail(f"{label}: none of the image names match the uploaded photos "
             f"(file lists {len(referenced)} names, e.g. {sorted(referenced)[:2]})")
    missing = sorted(referenced - have)
    if missing:
        note(f"warning: {label} mentions {len(missing)} image(s) that were not uploaded, e.g. {missing[:3]}")
    return len(matched)


def build(job, images_dir, override=""):
    if override and override.strip():
        return override.split()

    mode = job.get("mode", "preview")
    georef = job.get("georef", "exif")
    if mode not in ("preview", "survey"):
        fail(f"job.json: unknown mode {mode!r}")
    if georef not in ("exif", "geo", "gcp"):
        fail(f"job.json: unknown georef {georef!r}")
    if mode == "preview":
        return list(PREVIEW)

    quality = job.get("quality", "standard")
    if quality not in ("standard", "high"):
        fail(f"job.json: unknown quality {quality!r}")
    dem_res = number(job, "demResolution", 5, 1, 50)
    fmt = lambda v: str(int(v)) if float(v).is_integer() else str(v)  # noqa: E731
    level = "high" if quality == "high" else "medium"

    opts = [
        "--dsm", "--dem-resolution", fmt(dem_res),
        "--pc-quality", level, "--feature-quality", level,
        "--mesh-size", "200000", "--mesh-octree-depth", "11",
        "--gltf", "--max-concurrency", "4",
    ]
    if job.get("ortho"):
        opts += ["--orthophoto-resolution", fmt(dem_res)]
    else:
        opts += ["--skip-orthophoto"]

    if georef == "geo":
        f = Path(images_dir) / "geo.txt"
        if not f.exists():
            fail("georef=geo but geo.txt was not uploaded")
        _, rows = data_lines(f)
        n = check_names(rows, 0, images_dir, "geo.txt")
        acc = number(job, "gpsAccuracy", 0.05, 0.001, 20)
        note(f"geo.txt: {n} camera positions, gps accuracy {acc} m")
        opts += ["--geo", f"{CONTAINER_IMAGES}/geo.txt", "--gps-accuracy", fmt(acc)]
    elif georef == "gcp":
        f = Path(images_dir) / "gcp_list.txt"
        if not f.exists():
            fail("georef=gcp but gcp_list.txt was not uploaded")
        _, rows = data_lines(f)
        # x y z im_x im_y image_name [gcp_name]
        n = check_names(rows, 5, images_dir, "gcp_list.txt")
        points = {r.split()[6] if len(r.split()) > 6 else f"{r.split()[0]},{r.split()[1]}" for r in rows if len(r.split()) >= 6}
        note(f"gcp_list.txt: {len(rows)} observations of {len(points)} point(s) on {n} photo(s)")
        if len(points) < MIN_GCP:
            fail(f"at least {MIN_GCP} distinct ground control points are required (found {len(points)})")
        if len(points) < MIN_GCP_RECOMMENDED:
            note(f"warning: only {len(points)} ground control points; {MIN_GCP_RECOMMENDED}+ is recommended")
        opts += ["--gcp", f"{CONTAINER_IMAGES}/gcp_list.txt"]

    crs = job.get("crs")
    if crs:
        if not isinstance(crs, str) or not re.fullmatch(r"EPSG:\d{4,6}", crs):
            fail("job.json: crs must look like EPSG:32638")
        opts += ["--crs", crs]
    return opts


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--job", default="work/job.json")
    ap.add_argument("--images", default="work/project/images")
    ap.add_argument("--override", default="")
    a = ap.parse_args()
    job = load_job(a.job)
    print(" ".join(build(job, a.images, a.override)))


if __name__ == "__main__":
    main()
