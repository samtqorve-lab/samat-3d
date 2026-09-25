#!/usr/bin/env python3
"""Generate a CAD-friendly engineering export from a survey DSM: contour lines
as a DXF, via the GDAL command-line tools (consistent with the rest of this repo,
which avoids the Python GDAL bindings).

Usage:
  export_engineering.py --dsm dsm.tif --out-dir work/out --interval 1.0

Writes:
  contours.dxf   one polyline per elevation step, attribute ELEV in meters

Requires gdal_contour and ogr2ogr (the gdal-bin apt package).

Note on the other "engineering exports": dsm.tif is already a GeoTIFF (no
conversion needed), and the point cloud ODM produces
(odm_georeferencing/odm_georeferenced_model.laz) is published as-is by
publish.py under the name pointcloud.laz - a standard LASzip-compressed LAS
file that QGIS, CloudCompare, Global Mapper and recent Civil 3D all read
natively, so no separate conversion step lives here for that one.
"""
import argparse
import subprocess
import sys
from pathlib import Path


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"error running {' '.join(cmd)}:\n{r.stderr.strip()}")
    return r.stdout


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsm", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--interval", type=float, default=1.0, help="contour interval in meters")
    a = ap.parse_args()

    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    gpkg = out / "_contours.gpkg"
    dxf = out / "contours.dxf"

    run(["gdal_contour", "-a", "ELEV", "-i", str(a.interval), str(a.dsm), str(gpkg)])
    run(["ogr2ogr", "-f", "DXF", str(dxf), str(gpkg)])
    gpkg.unlink(missing_ok=True)
    print(f"contours: {a.interval} m interval -> {dxf}")


if __name__ == "__main__":
    main()
