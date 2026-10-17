#!/usr/bin/env python3
"""Render a one-page PDF summary of a survey job: georeferencing accuracy (from
ODM's stats.json), cut/fill volume (if given), and a shaded-relief preview of
the DSM. Meant for an engineer or manager who wants the numbers without opening
the 3D viewer.

Memory note: a 5 cm survey DSM can have hundreds of millions of pixels. The
preview is therefore read *decimated* (at most --max-preview-px on the long side)
as float32, never at full resolution, so the report fits easily in the runner's RAM.

Usage:
  accuracy_report.py --dsm dsm.tif --stats odm_report/stats.json \
      --out report.pdf [--volume volume.json] [--job job.json]
"""
import argparse
import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from matplotlib import pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

DEFAULT_MAX_PREVIEW_PX = 2000


def hillshade(z, dx, dy, azdeg=315, altdeg=45):
    az, alt = np.radians(azdeg), np.radians(altdeg)
    gy, gx = np.gradient(z, dy, dx)
    slope = np.pi / 2 - np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    shaded = np.sin(alt) * np.sin(slope) + np.cos(alt) * np.cos(slope) * np.cos(az - aspect)
    return np.clip(shaded, 0, 1)


def preview_shape(height, width, max_px):
    """Output (rows, cols) with the long side capped at max_px (never upscaled)."""
    scale = min(1.0, max_px / float(max(height, width)))
    return max(2, int(round(height * scale))), max(2, int(round(width * scale)))


def read_preview(path, max_px=DEFAULT_MAX_PREVIEW_PX):
    """Read band 1 decimated to at most max_px on the long side.

    Returns (z float32 with NaN for nodata, dx, dy, crs, (full_height, full_width)).
    dx/dy are the pixel sizes of the *decimated* grid, so hillshade stays correct.
    """
    with rasterio.open(path) as src:
        h, w = src.height, src.width
        out_h, out_w = preview_shape(h, w, max_px)
        z = src.read(1, out_shape=(out_h, out_w), resampling=Resampling.average,
                     masked=True).astype("float32").filled(np.nan)
        dx = abs(src.transform.a) * (w / out_w)
        dy = abs(src.transform.e) * (h / out_h)
        return z, dx, dy, str(src.crs), (h, w)


def load_json(path):
    if not path or not Path(path).exists():
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except ValueError:
        return {}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsm", required=True)
    ap.add_argument("--stats", default=None)
    ap.add_argument("--volume", default=None)
    ap.add_argument("--job", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-preview-px", type=int, default=DEFAULT_MAX_PREVIEW_PX,
                    help="longest side of the shaded-relief preview in pixels")
    a = ap.parse_args()

    z, dx, dy, crs, (full_h, full_w) = read_preview(a.dsm, a.max_preview_px)

    stats = load_json(a.stats)
    volume = load_json(a.volume)
    job = load_json(a.job)

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with PdfPages(a.out) as pdf:
        fig, axes = plt.subplots(1, 2, figsize=(11, 6), gridspec_kw={"width_ratios": [3, 2]})
        axes[0].imshow(hillshade(z, dx, dy), cmap="gray")
        axes[0].set_title("DSM (shaded relief)")
        axes[0].axis("off")

        axes[1].axis("off")
        lines = [f"CRS: {crs}", f"Mode: {job.get('mode', '-')}", f"Georef: {job.get('georef', '-')}",
                 f"DSM size: {full_w} x {full_h} px", ""]

        gcp = stats.get("gcp_errors") or {}
        gps = stats.get("gps_errors") or {}
        if gcp:
            lines.append("Ground control point error (m):")
            lines += [f"  {k}: {v}" for k, v in gcp.items()]
            lines.append("")
        if gps:
            lines.append("GPS error (m):")
            lines += [f"  {k}: {v}" for k, v in gps.items()]
            lines.append("")
        if not gcp and not gps:
            lines.append("(no GCP/GPS error data in stats.json)")
            lines.append("")
        if volume:
            lines += ["Volume:",
                      f"  cut:  {volume.get('cut_m3', '-')} m3",
                      f"  fill: {volume.get('fill_m3', '-')} m3",
                      f"  net:  {volume.get('net_change_m3', '-')} m3",
                      f"  reference: {volume.get('reference', '-')}"]
        axes[1].text(0, 1, "\n".join(lines), va="top", family="monospace", fontsize=9)
        fig.suptitle("SAMAT \u2014 Survey Report")
        pdf.savefig(fig)
        plt.close(fig)

    print(f"report -> {a.out}")


if __name__ == "__main__":
    main()
