import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import accuracy_report  # noqa: E402
import volume  # noqa: E402


def write_dsm(path, arr, res=0.5, nodata=-9999.0, x0=500000.0, y0=3900000.0):
    arr = np.where(np.isnan(arr), nodata, arr).astype("float32")
    with rasterio.open(path, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1,
                       dtype="float32", crs="EPSG:32638", transform=from_origin(x0, y0, res, res),
                       nodata=nodata) as dst:
        dst.write(arr, 1)


def test_flat_elevation_volume_matches_analytic(tmp_path):
    # 100 x 100 px at 0.5 m = 50 x 50 m; surface 2 m below the reference over a 20x20 m pit
    z = np.full((100, 100), 100.0, dtype="float32")
    z[30:70, 30:70] = 98.0  # 40x40 px = 20x20 m = 400 m2, 2 m deep -> 800 m3 cut
    dsm = tmp_path / "dsm.tif"
    write_dsm(dsm, z)
    r = volume.compute(str(dsm), reference_elevation=100.0, block_rows=16)  # many strips
    assert r["cut_m3"] == pytest.approx(800.0, rel=1e-4)
    assert r["fill_m3"] == pytest.approx(0.0, abs=1e-6)
    assert r["net_change_m3"] == pytest.approx(-800.0, rel=1e-4)


def test_blockwise_equals_single_block(tmp_path):
    rng = np.random.default_rng(0)
    z = (100 + rng.normal(0, 1.0, (130, 77))).astype("float32")
    dsm = tmp_path / "dsm.tif"
    write_dsm(dsm, z)
    a = volume.compute(str(dsm), reference_elevation=100.0, block_rows=7)
    b = volume.compute(str(dsm), reference_elevation=100.0, block_rows=10_000)
    assert a["cut_m3"] == pytest.approx(b["cut_m3"], rel=1e-6)
    assert a["fill_m3"] == pytest.approx(b["fill_m3"], rel=1e-6)
    assert a["valid_pixels"] == b["valid_pixels"]


def test_nodata_is_ignored(tmp_path):
    z = np.full((40, 40), 99.0, dtype="float32")
    z[:20, :] = np.nan
    dsm = tmp_path / "dsm.tif"
    write_dsm(dsm, z)
    r = volume.compute(str(dsm), reference_elevation=100.0, block_rows=8)
    assert r["valid_pixels"] == 20 * 40
    assert r["cut_m3"] == pytest.approx(20 * 40 * 0.25 * 1.0, rel=1e-4)


def test_aoi_limits_area(tmp_path):
    z = np.full((100, 100), 99.0, dtype="float32")  # 1 m cut everywhere (50x50 m)
    dsm = tmp_path / "dsm.tif"
    write_dsm(dsm, z)
    x0, y0 = 500000.0, 3900000.0
    poly = {"type": "Polygon", "coordinates": [[[x0, y0], [x0 + 10, y0], [x0 + 10, y0 - 10],
                                                  [x0, y0 - 10], [x0, y0]]]}  # 10 x 10 m
    aoi = tmp_path / "aoi.geojson"
    aoi.write_text(json.dumps({"type": "FeatureCollection",
                               "features": [{"type": "Feature", "properties": {}, "geometry": poly}]}))
    r = volume.compute(str(dsm), reference_elevation=100.0, aoi=str(aoi), block_rows=13)
    assert r["cut_m3"] == pytest.approx(100.0, rel=0.05)
    assert r["area_m2"] == pytest.approx(100.0, rel=0.05)


def test_threshold_drops_noise(tmp_path):
    z = np.full((50, 50), 99.98, dtype="float32")  # 2 cm below reference = noise
    dsm = tmp_path / "dsm.tif"
    write_dsm(dsm, z)
    noisy = volume.compute(str(dsm), reference_elevation=100.0)
    clean = volume.compute(str(dsm), reference_elevation=100.0, threshold=0.05)
    assert noisy["cut_m3"] > 0
    assert clean["cut_m3"] == 0


def test_reference_dsm_and_diff_output(tmp_path):
    before = np.full((60, 60), 100.0, dtype="float32")
    after = before.copy()
    after[10:30, 10:30] = 97.0  # 20x20 px = 10x10 m, 3 m -> 300 m3
    a, b = tmp_path / "before.tif", tmp_path / "after.tif"
    write_dsm(a, before)
    write_dsm(b, after)
    out = tmp_path / "diff.tif"
    r = volume.compute(str(b), reference=str(a), diff_out=str(out), block_rows=16)
    assert r["cut_m3"] == pytest.approx(300.0, rel=1e-3)
    with rasterio.open(out) as d:
        arr = d.read(1)
    assert arr[20, 20] == pytest.approx(-3.0)


def test_report_preview_is_decimated_and_pdf_written(tmp_path):
    # 6000 x 4000 px DSM: preview must be capped, never full resolution
    rng = np.random.default_rng(1)
    z = (100 + rng.normal(0, 0.1, (4000, 6000))).astype("float32")
    dsm = tmp_path / "big.tif"
    write_dsm(dsm, z, res=0.05)
    prev, dx, dy, crs, full = accuracy_report.read_preview(str(dsm), 1000)
    assert max(prev.shape) <= 1000 and prev.dtype == np.float32
    assert full == (4000, 6000)
    assert dx == pytest.approx(0.05 * 6, rel=0.02)  # pixel size scales with decimation
    pdf = tmp_path / "report.pdf"
    subprocess.run([sys.executable, str(SCRIPTS / "accuracy_report.py"), "--dsm", str(dsm),
                    "--out", str(pdf), "--max-preview-px", "800"], check=True, capture_output=True)
    assert pdf.exists() and pdf.stat().st_size > 1000


def test_preview_never_upscales(tmp_path):
    z = np.full((30, 40), 5.0, dtype="float32")
    dsm = tmp_path / "small.tif"
    write_dsm(dsm, z)
    prev, *_ = accuracy_report.read_preview(str(dsm), 2000)
    assert prev.shape == (30, 40)
