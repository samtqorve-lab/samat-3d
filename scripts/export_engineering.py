#!/usr/bin/env python3
"""Generate a CAD-friendly engineering export from a survey DSM: contour lines
as a DXF, via the GDAL command-line tools (consistent with the rest of this repo,
which avoids the Python GDAL bindings).

Usage:
  export_engineering.py --dsm dsm.tif --out-dir work/out --interval 1.0 [--smooth-cell 0.5]

Writes:
  contours.dxf   one polyline per elevation step, attribute ELEV in meters

Requires gdal_contour and ogr2ogr (the gdal-bin apt package).

Smoothing: contours traced on the raw 5 cm DSM follow every pebble and photogrammetry
speckle, giving a huge, jagged DXF that CAD programs choke on. Before tracing, the DSM
is averaged down to --smooth-cell meters per pixel (default 0.5 m; 0 = use the raw DSM).
That removes sub-half-meter noise - irrelevant for 1 m contours - and cuts the file
size by orders of magnitude. If the DSM is already coarser than --smooth-cell it is used as is.

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
import tempfile
from pathlib import Path

DEFAULT_SMOOTH_CELL = 0.5
_NODATA = -9999.0


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"error running {' '.join(cmd)}:\n{r.stderr.strip()}")
    return r.stdout


def smoothed_dsm(dsm, dest, cell):
    """Write an area-averaged copy of the DSM at `cell` meters/pixel to `dest`.

    Returns True if a smoothed copy was written, False if the DSM is already at least as
    coarse as `cell` (or smoothing is off) and the original should be used.
    The copy is small, so it is read in one go; GDAL streams the averaging from the source.
    """
    if not cell or cell <= 0:
        return False
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import Affine

    with rasterio.open(dsm) as src:
        native = abs(src.transform.a)
        factor = cell / native if native > 0 else 1.0
        if factor <= 1.0:
            return False
        out_w = max(2, int(round(src.width / factor)))
        out_h = max(2, int(round(src.height / factor)))
        data = src.read(1, out_shape=(out_h, out_w), resampling=Resampling.average,
                        masked=True).astype("float32")
        transform = src.transform * Affine.scale(src.width / out_w, src.height / out_h)
        profile = {
            "driver": "GTiff", "height": out_h, "width": out_w, "count": 1, "dtype": "float32",
            "crs": src.crs, "transform": transform, "nodata": _NODATA, "compress": "deflate",
        }
    with rasterio.open(dest, "w", **profile) as dst:
        dst.write(data.filled(_NODATA).astype("float32"), 1)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsm", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--interval", type=float, default=1.0, help="contour interval in meters")
    ap.add_argument("--smooth-cell", type=float, default=DEFAULT_SMOOTH_CELL,
                    help="average the DSM down to this many meters/pixel before tracing (0 = raw DSM)")
    a = ap.parse_args()
    if a.interval <= 0:
        sys.exit("error: --interval must be > 0")

    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    gpkg = out / "_contours.gpkg"
    dxf = out / "contours.dxf"

    with tempfile.TemporaryDirectory() as tmp:
        source = Path(a.dsm)
        smooth_path = Path(tmp) / "dsm_smooth.tif"
        used = smoothed_dsm(a.dsm, smooth_path, a.smooth_cell)
        if used:
            source = smooth_path
            print(f"contours traced on DSM averaged to {a.smooth_cell} m/pixel")
        run(["gdal_contour", "-a", "ELEV", "-i", str(a.interval), str(source), str(gpkg)])
    run(["ogr2ogr", "-f", "DXF", str(dxf), str(gpkg)])
    gpkg.unlink(missing_ok=True)
    print(f"contours: {a.interval} m interval -> {dxf}")


if __name__ == "__main__":
    main()
