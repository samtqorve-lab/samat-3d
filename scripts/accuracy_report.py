#!/usr/bin/env python3
"""Render a one-page PDF summary of a survey job: georeferencing accuracy (from
ODM's stats.json), cut/fill volume (if given), and a shaded-relief preview of
the DSM. Meant for an engineer or manager who wants the numbers without opening
the 3D viewer.

Usage:
  accuracy_report.py --dsm dsm.tif --stats odm_report/stats.json \
      --out report.pdf [--volume volume.json] [--job job.json]
"""
import argparse
import json
from pathlib import Path

import numpy as np
import rasterio
from matplotlib import pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages


def hillshade(z, dx, dy, azdeg=315, altdeg=45):
    az, alt = np.radians(azdeg), np.radians(altdeg)
    gy, gx = np.gradient(z, dy, dx)
    slope = np.pi / 2 - np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    shaded = np.sin(alt) * np.sin(slope) + np.cos(alt) * np.cos(slope) * np.cos(az - aspect)
    return np.clip(shaded, 0, 1)


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
    a = ap.parse_args()

    with rasterio.open(a.dsm) as src:
        z = src.read(1).astype("float64")
        if src.nodata is not None:
            z[z == src.nodata] = np.nan
        dx, dy = src.transform.a, -src.transform.e
        crs = str(src.crs)

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
        lines = [f"CRS: {crs}", f"Mode: {job.get('mode', '-')}", f"Georef: {job.get('georef', '-')}", ""]

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
