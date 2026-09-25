#!/usr/bin/env python3
"""Shared helper: write a full-detail mesh plus two decimated LOD levels as .glb.

LOD levels keep ~35%% and ~10%% of the original triangle count via quadric edge-
collapse (trimesh's simplify_quadric_decimation, backed by the fast-simplification
package). Geometry-only: for a textured mesh, UV correspondence after heavy
decimation is not guaranteed to look clean close-up, so LOD1/LOD2 are meant for
distant/overview rendering (map thumbnails, "whole mine" fly-overs) while LOD0
(the original model.glb) stays the one used for close-up inspection.

A LOD failure is never fatal here - it prints a warning and moves on, so a viewer
performance nice-to-have never blocks delivery of the full-detail model.
"""
from pathlib import Path

LOD_RATIOS = {"model_lod1.glb": 0.35, "model_lod2.glb": 0.10}


def write_with_lods(mesh, out_path):
    """out_path is the LOD0 (full-detail) path, e.g. .../model.glb.
    Writes out_path plus model_lod1.glb / model_lod2.glb next to it (skipping any
    level that would not actually be smaller than the mesh already is).
    Returns the list of file names written, out_path.name first."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mesh.export(str(out_path), file_type="glb")
    written = [out_path.name]

    face_count = len(mesh.faces)
    for name, ratio in LOD_RATIOS.items():
        target = max(100, int(face_count * ratio))
        if target >= face_count:
            continue
        try:
            simplified = mesh.simplify_quadric_decimation(face_count=target)
            simplified.export(str(out_path.parent / name), file_type="glb")
            written.append(name)
        except Exception as exc:  # pragma: no cover - decimation is best-effort
            print(f"warning: LOD {name} skipped ({exc})")
    return written
