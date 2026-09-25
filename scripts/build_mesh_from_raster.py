#!/usr/bin/env python3
"""Build lightweight textured glTF (.glb) meshes - a full-detail model.glb plus
two decimated LOD levels (model_lod1.glb, model_lod2.glb; see lod.py) - for the
WHOLE merged mine site, from the merged DSM + orthophoto that merge_flights.py
produces.

This is a heightmap mesh - a regular grid draped over the merged DSM, textured with the merged
orthophoto - NOT a re-run of dense photogrammetry across all flights (that would mean fusing each
flight's raw point cloud with ICP alignment and re-meshing, which is out of scope here). It is
good enough to fly over and inspect the whole pit, benches and haul roads at once. Per-flight
photogrammetric detail (loose rock texture, small features, full point-cloud density) stays in
each flight's own model.glb from build-3d.yml - that remains the higher-detail view for
close-up work; this merged mesh is the "whole mine at once" overview.

Usage:
  build_mesh_from_raster.py --dsm work/merge_out/dsm.tif [--ortho work/merge_out/ortho.tif] \
      --out work/merge_out/model.glb [--max-vertices-per-side 400] [--texture-size 2048] \
      [--assets-json work/merge_out/assets.json]

Requires the GDAL command-line tools (already installed by merge-3d.yml) plus the Python
packages numpy, pillow and trimesh. Reads elevation via `gdal_translate -of XYZ` (plain text) -
deliberately avoids the Python GDAL bindings, which are painful to install reliably via pip;
gdal-bin's CLI tools are already on the runner.

KNOWN LIMITATIONS (documented, not silently glossed over):
  - Mesh resolution is capped by --max-vertices-per-side (default 400x400 = up to 320k
    triangles) - coarser than a native ODM photogrammetric mesh of one flight.
  - The texture is the same simple mosaic merge_flights.py produces (no seamline blending),
    so a seam may be visible where two flights overlap.
  - Vertical axis convention matches ODM's own georeferenced .glb (Z = elevation) rather than
    glTF's own Y-up convention, exactly like the per-flight model.glb - model3dViewer.js in the
    app already detects and corrects this the same way it does for a single flight's model.
  - model_lod1.glb/model_lod2.glb are geometry-only quadric decimations of the same mesh;
    texture UV correspondence after heavy decimation is not guaranteed to look clean up close,
    so they are meant for distant/overview rendering, not close-up inspection (use model.glb
    for that).
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lod import write_with_lods  # noqa: E402


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"error running {' '.join(cmd)}:\n{r.stderr.strip()}")
    return r.stdout


def gdalinfo(path):
    return json.loads(run(["gdalinfo", "-json", str(path)]))


def bounds_of(info):
    corners = info["cornerCoordinates"]
    xs = [c[0] for c in corners.values()]
    ys = [c[1] for c in corners.values()]
    return {"minX": min(xs), "maxX": max(xs), "minY": min(ys), "maxY": max(ys)}


def read_grid(dsm_path, max_side, tmp_dir):
    info = gdalinfo(dsm_path)
    w, h = info["size"]
    scale = min(1.0, max_side / max(w, h))
    out_w = max(2, round(w * scale))
    out_h = max(2, round(h * scale))
    small = tmp_dir / "dsm_small.tif"
    run(["gdal_translate", "-outsize", str(out_w), str(out_h), "-r", "average",
         str(dsm_path), str(small)])
    small_info = gdalinfo(small)
    nodata = None
    bands = small_info.get("bands", [])
    if bands and "noDataValue" in bands[0]:
        nodata = bands[0]["noDataValue"]
    xyz = tmp_dir / "dsm_small.xyz"
    run(["gdal_translate", "-of", "XYZ", str(small), str(xyz)])
    xs, ys, zs = [], [], []
    with open(xyz, encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if len(parts) != 3:
                continue
            x, y, z = map(float, parts)
            xs.append(x)
            ys.append(y)
            zs.append(z)
    n = len(xs)
    if n != out_w * out_h:
        sys.exit(f"error: unexpected XYZ point count {n}, expected {out_w * out_h} ({out_w}x{out_h})")
    grid_x = np.array(xs, dtype=np.float64).reshape(out_h, out_w)
    grid_y = np.array(ys, dtype=np.float64).reshape(out_h, out_w)
    grid_z = np.array(zs, dtype=np.float64).reshape(out_h, out_w)
    valid = np.ones((out_h, out_w), dtype=bool)
    if nodata is not None:
        valid &= ~np.isclose(grid_z, nodata)
    return grid_x, grid_y, grid_z, valid, bounds_of(small_info)


def build_texture(ortho_path, max_size, tmp_dir):
    info = gdalinfo(ortho_path)
    w, h = info["size"]
    scale = min(1.0, max_size / max(w, h))
    out_w = max(2, round(w * scale))
    out_h = max(2, round(h * scale))
    png = tmp_dir / "ortho_tex.png"
    band_count = len(info.get("bands", [])) or 3
    band_args = ["-b", "1", "-b", "2", "-b", "3"] if band_count >= 3 else []
    run(["gdal_translate", "-outsize", str(out_w), str(out_h), "-of", "PNG",
         *band_args, str(ortho_path), str(png)])
    return Image.open(png).convert("RGB"), bounds_of(info)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsm", required=True)
    ap.add_argument("--ortho", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-vertices-per-side", type=int, default=400)
    ap.add_argument("--texture-size", type=int, default=2048)
    ap.add_argument("--assets-json", default=None,
                     help="if given, append produced file names (model.glb + LODs) to this JSON array file")
    a = ap.parse_args()

    import trimesh  # imported late so --help works even without it installed

    out_path = Path(a.out)
    tmp = out_path.parent / "_mesh_tmp"
    tmp.mkdir(parents=True, exist_ok=True)

    gx, gy, gz, valid, _dsm_bounds = read_grid(Path(a.dsm), a.max_vertices_per_side, tmp)
    h, w = gz.shape

    valid_z = np.where(valid, gz, np.nan)
    origin = np.array([
        float(np.nanmin(gx)) / 2 + float(np.nanmax(gx)) / 2,
        float(np.nanmin(gy)) / 2 + float(np.nanmax(gy)) / 2,
        float(np.nanmin(valid_z)),
    ])

    texture = None
    uv = None
    if a.ortho and Path(a.ortho).exists():
        texture, ortho_bounds = build_texture(Path(a.ortho), a.texture_size, tmp)
        u = (gx - ortho_bounds["minX"]) / max(1e-6, (ortho_bounds["maxX"] - ortho_bounds["minX"]))
        v = 1.0 - (gy - ortho_bounds["minY"]) / max(1e-6, (ortho_bounds["maxY"] - ortho_bounds["minY"]))
        uv = np.stack([u.ravel(), v.ravel()], axis=1)
    else:
        print("note: no orthophoto given - mesh will be untextured", file=sys.stderr)

    vertices = np.stack([
        (gx - origin[0]).ravel(),
        (gy - origin[1]).ravel(),
        (gz - origin[2]).ravel(),
    ], axis=1)

    faces = []
    for r in range(h - 1):
        for c in range(w - 1):
            if not (valid[r, c] and valid[r, c + 1] and valid[r + 1, c] and valid[r + 1, c + 1]):
                continue
            i00 = r * w + c
            i01 = r * w + c + 1
            i10 = (r + 1) * w + c
            i11 = (r + 1) * w + c + 1
            faces.append([i00, i10, i11])
            faces.append([i00, i11, i01])
    if not faces:
        sys.exit("error: merged DSM produced no valid mesh triangles (all nodata?)")
    faces = np.array(faces, dtype=np.int64)

    if texture is not None:
        material = trimesh.visual.texture.SimpleMaterial(image=texture)
        visual = trimesh.visual.TextureVisuals(uv=uv, image=texture, material=material)
        mesh = trimesh.Trimesh(vertices=vertices, faces=faces, visual=visual, process=False)
    else:
        mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)

    written = write_with_lods(mesh, out_path)
    print(f"mesh: {len(vertices)} vertices, {len(faces)} triangles -> {', '.join(written)}"
          f"{' (textured)' if texture is not None else ' (untextured)'}")

    if a.assets_json:
        p = Path(a.assets_json)
        existing = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
        p.write_text(json.dumps(existing + written), encoding="utf-8")

    for p in tmp.glob("*"):
        p.unlink()
    tmp.rmdir()


if __name__ == "__main__":
    main()
