#!/usr/bin/env python3
"""Collect the ODM outputs of a job, encrypt them and prepare the release upload.

Usage:
  publish.py --project work/project --out work/out [--job work/job.json]

Reads the key from S3D_KEY_B64 (same as s3d.py). Writes into --out:
  model.glb.enc   textured 3D model (viewer)
  dsm.tif.enc     georeferenced elevation model (survey mode, used for volume)
  stats.json.enc  ODM report data (accuracy numbers, GCP errors)
  ortho.tif.enc   orthophoto (only if it was built and is small enough)
  assets.json     list of the produced names, e.g. ["model.glb", "dsm.tif"]
  summary.json    small non-sensitive summary for the app (numbers only)

Exit code 1 if the outputs required for the job's mode are missing.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from s3d import encrypt_file, load_key  # noqa: E402

MAX_ASSET = 1_900_000_000      # GitHub release assets must stay under 2 GB
MAX_ORTHO = 250_000_000        # the app downloads/decrypts in memory; keep the orthophoto modest


def find_first(root, patterns):
    for pat in patterns:
        hits = sorted(Path(root).glob(pat))
        if hits:
            return hits[0]
    return None


def numeric_leaves(obj, depth=0):
    """Keep only small numeric structures (no strings) so the summary is safe to store."""
    if depth > 3:
        return None
    if isinstance(obj, bool):
        return None
    if isinstance(obj, (int, float)):
        return round(float(obj), 4)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and len(k) <= 40:
                r = numeric_leaves(v, depth + 1)
                if r is not None and r != {}:
                    out[k] = r
        return out
    return None


def summarize(stats_path, job):
    summary = {"mode": job.get("mode", "preview"), "georef": job.get("georef", "exif")}
    if not stats_path:
        return summary
    try:
        stats = json.loads(Path(stats_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return summary
    for key in ("gcp_errors", "gps_errors"):
        val = numeric_leaves(stats.get(key))
        if val:
            summary[key] = val
    proc = stats.get("processing_statistics")
    if isinstance(proc, dict):
        t = numeric_leaves({"total_time": proc.get("total_time")})
        if t:
            summary.update(t)
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", default="work/project")
    ap.add_argument("--out", default="work/out")
    ap.add_argument("--job", default="work/job.json")
    a = ap.parse_args()

    job = {}
    if a.job and Path(a.job).exists():
        try:
            job = json.loads(Path(a.job).read_text(encoding="utf-8"))
        except ValueError:
            job = {}
    survey = job.get("mode") == "survey"

    project = Path(a.project)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    glb = find_first(project, ["**/*_geo.glb", "**/*.glb"])
    dsm = find_first(project, ["odm_dem/dsm.tif", "**/odm_dem/dsm.tif"])
    stats = find_first(project, ["odm_report/stats.json", "**/odm_report/stats.json"])
    ortho = find_first(project, ["odm_orthophoto/odm_orthophoto.tif", "**/odm_orthophoto/odm_orthophoto.tif"])

    candidates = [("model.glb", glb), ("dsm.tif", dsm if survey else None),
                  ("stats.json", stats if survey else None), ("ortho.tif", ortho if survey else None)]

    if survey and dsm is None:
        print("error: survey job finished without odm_dem/dsm.tif", file=sys.stderr)
        sys.exit(1)
    if glb is None and not survey:
        print("error: ODM produced no .glb", file=sys.stderr)
        sys.exit(1)
    if glb is None:
        print("warning: no .glb model was produced (the DSM is still available)", file=sys.stderr)

    key = load_key()
    produced = []
    for name, src in candidates:
        if src is None:
            continue
        size = src.stat().st_size
        if name == "ortho.tif" and size > MAX_ORTHO:
            print(f"skip {name}: {size / 1e6:.0f} MB is over the {MAX_ORTHO / 1e6:.0f} MB limit", file=sys.stderr)
            continue
        if size > MAX_ASSET:
            print(f"error: {name} is {size / 1e9:.2f} GB, over the GitHub asset limit", file=sys.stderr)
            sys.exit(1)
        encrypt_file(src, out / f"{name}.enc", key)
        produced.append(name)
        print(f"encrypted {name}: {size / 1e6:.1f} MB")

    (out / "assets.json").write_text(json.dumps(produced), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summarize(stats, job)), encoding="utf-8")
    print("assets:", ", ".join(produced))


if __name__ == "__main__":
    main()
