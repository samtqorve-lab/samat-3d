#!/usr/bin/env python3
"""Collect the ODM outputs of a job, encrypt them and prepare the release upload.

Usage:
  publish.py --project work/project --out work/out [--job work/job.json]

Reads the key from S3D_KEY_B64 (same as s3d.py). Writes into --out:
  model.glb.enc        textured 3D model (viewer)
  model_lod1.glb.enc   decimated ~35 percent overview mesh (best-effort, may be absent)
  model_lod2.glb.enc   decimated ~10 percent overview mesh (best-effort, may be absent)
  dsm.tif.enc          georeferenced elevation model (survey mode, used for volume)
  stats.json.enc       ODM report data (accuracy numbers, GCP errors)
  ortho.tif.enc        orthophoto (only if it was built and is small enough)
  pointcloud.laz.enc   ODM's georeferenced point cloud (survey mode, if produced)
  contours.dxf.enc     CAD contour lines (survey mode, if work/project/analysis/contours.dxf exists)
  report.pdf.enc       accuracy/volume PDF (survey mode, if work/project/analysis/report.pdf exists)
  assets.json          list of the produced names, e.g. ["model.glb", "dsm.tif"]
  summary.json         small non-sensitive summary for the app (numbers + CRS code + volume)

Exit code 1 if the outputs required for the job's mode are missing. Optional/soft
assets (LODs, point cloud, contours, report) are skipped with a warning rather than
failing the job if they are missing or oversized - only model.glb/dsm.tif are
treated as must-have for their respective modes.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from s3d import encrypt_file, load_key  # noqa: E402

MAX_ASSET = 1_900_000_000      # GitHub release assets must stay under 2 GB
MAX_ORTHO = 250_000_000        # the app downloads/decrypts in memory; keep the orthophoto modest
SOFT_ASSETS = {"pointcloud.laz", "contours.dxf", "report.pdf", "model_lod1.glb", "model_lod2.glb"}


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


def summarize(stats_path, job, volume_path=None):
    summary = {"mode": job.get("mode", "preview"), "georef": job.get("georef", "exif")}
    crs = job.get("crs")
    # crs is a plain coordinate-system code (e.g. "EPSG:32638"), not sensitive; kept as-is
    # (not numeric) so the merge-3d workflow and the app can tell flights apart/compatible.
    if isinstance(crs, str) and re.fullmatch(r"EPSG:\d{4,6}", crs):
        summary["crs"] = crs
    if stats_path:
        try:
            stats = json.loads(Path(stats_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stats = {}
        for key in ("gcp_errors", "gps_errors"):
            val = numeric_leaves(stats.get(key))
            if val:
                summary[key] = val
        proc = stats.get("processing_statistics")
        if isinstance(proc, dict):
            t = numeric_leaves({"total_time": proc.get("total_time")})
            if t:
                summary.update(t)
    if volume_path and Path(volume_path).exists():
        try:
            vol = json.loads(Path(volume_path).read_text(encoding="utf-8"))
            keep = {k: vol[k] for k in ("cut_m3", "fill_m3", "net_change_m3") if k in vol}
            if keep:
                summary["volume"] = keep
        except (OSError, ValueError):
            pass
    return summary


def make_lods(glb_path, out_dir):
    """Best-effort: write model_lod1.glb / model_lod2.glb (decimated) into out_dir,
    next to the full-detail glb. Returns the list of names actually written.
    Never raises - a LOD failure should never fail the whole publish step."""
    try:
        import trimesh
        from lod import LOD_RATIOS
        mesh = trimesh.load(str(glb_path), force="mesh")
    except Exception as exc:
        print(f"warning: skipping LOD generation ({exc})", file=sys.stderr)
        return []
    written = []
    face_count = len(mesh.faces)
    for name, ratio in LOD_RATIOS.items():
        target = max(100, int(face_count * ratio))
        if target >= face_count:
            continue
        try:
            simplified = mesh.simplify_quadric_decimation(face_count=target)
            simplified.export(str(Path(out_dir) / name), file_type="glb")
            written.append(name)
        except Exception as exc:
            print(f"warning: LOD {name} skipped ({exc})", file=sys.stderr)
    return written


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
    pointcloud = find_first(project, ["odm_georeferencing/odm_georeferenced_model.laz",
                                       "**/odm_georeferenced_model.laz"])
    analysis = project / "analysis"
    contours = find_first(analysis, ["contours.dxf"]) if analysis.exists() else None
    report = find_first(analysis, ["report.pdf"]) if analysis.exists() else None
    volume_json = analysis / "volume.json"

    if survey and dsm is None:
        print("error: survey job finished without odm_dem/dsm.tif", file=sys.stderr)
        sys.exit(1)
    if glb is None and not survey:
        print("error: ODM produced no .glb", file=sys.stderr)
        sys.exit(1)
    if glb is None:
        print("warning: no .glb model was produced (the DSM is still available)", file=sys.stderr)

    lod_names = make_lods(glb, out) if glb is not None else []

    candidates = [("model.glb", glb), ("dsm.tif", dsm if survey else None),
                  ("stats.json", stats if survey else None), ("ortho.tif", ortho if survey else None)]
    for name in lod_names:
        candidates.append((name, out / name))
    if survey:
        candidates.append(("pointcloud.laz", pointcloud))
        candidates.append(("contours.dxf", contours))
        candidates.append(("report.pdf", report))

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
            if name in SOFT_ASSETS:
                print(f"skip {name}: {size / 1e9:.2f} GB is over the GitHub asset limit", file=sys.stderr)
                continue
            print(f"error: {name} is {size / 1e9:.2f} GB, over the GitHub asset limit", file=sys.stderr)
            sys.exit(1)
        encrypt_file(src, out / f"{name}.enc", key)
        produced.append(name)
        print(f"encrypted {name}: {size / 1e6:.1f} MB")

    (out / "assets.json").write_text(json.dumps(produced), encoding="utf-8")
    (out / "summary.json").write_text(
        json.dumps(summarize(stats, job, volume_json if volume_json.exists() else None)), encoding="utf-8")
    print("assets:", ", ".join(produced))

    # tidy up: only *.enc, assets.json and summary.json should remain in --out
    for f in out.iterdir():
        if f.suffix != ".enc" and f.name not in ("assets.json", "summary.json"):
            f.unlink()


if __name__ == "__main__":
    main()
