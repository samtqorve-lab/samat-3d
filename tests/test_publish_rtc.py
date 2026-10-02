"""publish.py / mesh builder integration of the RTC helper (no GDAL / ODM needed).

Run with: pip install numpy trimesh fast-simplification pillow pytest && python -m pytest tests
"""
import sys
from pathlib import Path

import numpy as np
import pytest
import trimesh

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import glb_rtc  # noqa: E402
import publish  # noqa: E402
from lod import write_with_lods  # noqa: E402


def grid_mesh(n=40):
    xs, ys = np.meshgrid(np.arange(n, dtype=float), np.arange(n, dtype=float))
    zs = np.sin(xs / 5) * 3
    verts = np.stack([xs.ravel(), ys.ravel(), zs.ravel()], axis=1)
    faces = []
    for r in range(n - 1):
        for c in range(n - 1):
            a = r * n + c
            faces += [[a, a + n, a + n + 1], [a, a + n + 1, a + 1]]
    return trimesh.Trimesh(vertices=verts, faces=np.array(faces), process=False)


def test_make_lods_keeps_utm_origin_of_the_full_model(tmp_path):
    full = tmp_path / "in" / "odm_textured_model_geo.glb"
    full.parent.mkdir()
    grid_mesh().export(str(full), file_type="glb")
    glb_rtc.write_rtc(full, [650123.5, 3892100.25, 0.0])   # what ODM's obj2glb does
    out = tmp_path / "out"
    out.mkdir()
    names = publish.make_lods(full, out)
    assert names, "expected at least one LOD to be written"
    for name in names:
        assert glb_rtc.read_rtc(out / name) == [650123.5, 3892100.25, 0.0], name


def test_make_lods_without_rtc_is_unchanged_behaviour(tmp_path):
    full = tmp_path / "model.glb"
    grid_mesh().export(str(full), file_type="glb")
    out = tmp_path / "out"
    out.mkdir()
    names = publish.make_lods(full, out)
    assert names
    for name in names:
        assert glb_rtc.read_rtc(out / name) is None


def test_merged_mesh_files_all_get_the_origin(tmp_path):
    """Mirrors build_mesh_from_raster.main(): write_with_lods then try_write_rtc(origin)."""
    out_path = tmp_path / "model.glb"
    origin = np.array([650500.25, 3891000.75, 1712.3])
    written = write_with_lods(grid_mesh(), out_path)
    assert len(written) >= 2
    done = glb_rtc.try_write_rtc([out_path.parent / n for n in written], [float(origin[0]), float(origin[1]), float(origin[2])])
    assert len(done) == len(written)
    for n in written:
        assert glb_rtc.read_rtc(out_path.parent / n) == [650500.25, 3891000.75, 1712.3]
    # geometry still loads
    assert len(trimesh.load(str(out_path), force="mesh").faces) == len(grid_mesh().faces)


@pytest.mark.parametrize("line,expected", [
    ("WGS84 UTM 38N", "EPSG:32638"),
    ("WGS84 UTM 39N\n650000 3890000", "EPSG:32639"),
    ("WGS84 UTM 33S", "EPSG:32733"),
    ("WGS84 UTM 5N", "EPSG:32605"),
    ("WGS 84 UTM 38n", "EPSG:32638"),
    ("WGS84 UTM 61N", None),
    ("WGS84 UTM 0N", None),
    ("something else", None),
    ("", None),
])
def test_crs_from_coords_txt(tmp_path, line, expected):
    f = tmp_path / "coords.txt"
    f.write_text(line, encoding="utf-8")
    assert publish.crs_from_coords_txt(f) == expected


def test_crs_from_coords_txt_missing_file():
    assert publish.crs_from_coords_txt(None) is None
    assert publish.crs_from_coords_txt("/no/such/file") is None


def test_summary_prefers_explicit_job_crs_over_detected(tmp_path):
    f = tmp_path / "coords.txt"
    f.write_text("WGS84 UTM 38N", encoding="utf-8")
    assert publish.summarize(None, {"mode": "survey", "crs": "EPSG:32639"}, None, f)["crs"] == "EPSG:32639"
    assert publish.summarize(None, {"mode": "survey"}, None, f)["crs"] == "EPSG:32638"
    assert "crs" not in publish.summarize(None, {"mode": "preview"}, None, None)
    # invalid explicit crs falls back to detection, never to a garbage value
    assert publish.summarize(None, {"crs": "not-a-crs"}, None, f)["crs"] == "EPSG:32638"
