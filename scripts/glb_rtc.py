#!/usr/bin/env python3
"""Read / write the CESIUM_RTC extension of a binary glTF (.glb) without re-encoding the mesh.

Why: ODM writes vertex coordinates relative to a UTM origin and stores that origin in
extensions.CESIUM_RTC.center = [easting, northing, 0] of its own *_geo.glb. The web app
(samat-admin, lib/model3dGeo.js) reads exactly that field to place the model on the map
(UTM coordinates, license boundary overlay, comparison of two flights).

trimesh (used for the LOD levels and for the merged-site heightmap mesh) drops this
extension when it re-exports, so those files would silently lose their position. These
helpers copy / write the extension by rewriting only the GLB JSON chunk; the binary chunk
(geometry, textures, Draco data) is passed through byte-for-byte.

Never raises in the `try_*` variants - a missing position must never fail a
publish step; the app simply shows "no georeference" for that file.
"""
import json
import struct
import sys
from pathlib import Path

MAGIC = 0x46546C67      # "glTF"
CHUNK_JSON = 0x4E4F534A  # "JSON"
EXT = "CESIUM_RTC"


def _parse(data):
    if len(data) < 20:
        raise ValueError("file too short for a GLB")
    magic, version, length = struct.unpack_from("<III", data, 0)
    if magic != MAGIC:
        raise ValueError("not a binary glTF (bad magic)")
    if version != 2:
        raise ValueError(f"unsupported glTF version {version}")
    if length > len(data):
        raise ValueError("truncated GLB")
    chunk_len, chunk_type = struct.unpack_from("<II", data, 12)
    if chunk_type != CHUNK_JSON:
        raise ValueError("first GLB chunk is not JSON")
    json_end = 20 + chunk_len
    if json_end > len(data):
        raise ValueError("truncated GLB JSON chunk")
    doc = json.loads(data[20:json_end].decode("utf-8"))
    return doc, data[json_end:length]


def read_rtc(path_or_bytes):
    """Return [x, y, z] floats from extensions.CESIUM_RTC.center, or None."""
    data = path_or_bytes if isinstance(path_or_bytes, (bytes, bytearray)) else Path(path_or_bytes).read_bytes()
    doc, _ = _parse(bytes(data))
    center = ((doc.get("extensions") or {}).get(EXT) or {}).get("center")
    if not isinstance(center, list) or len(center) < 2:
        return None
    try:
        out = [float(v) for v in center[:3]]
    except (TypeError, ValueError):
        return None
    if len(out) == 2:
        out.append(0.0)
    return out if all(v == v and abs(v) != float("inf") for v in out) else None


def write_rtc_bytes(data, center):
    """Return new GLB bytes with CESIUM_RTC.center = center ([x, y] or [x, y, z])."""
    c = [float(v) for v in center]
    if len(c) == 2:
        c.append(0.0)
    if len(c) != 3 or any(v != v or abs(v) == float("inf") for v in c):
        raise ValueError("center must be 2 or 3 finite numbers")
    doc, rest = _parse(bytes(data))
    doc.setdefault("extensions", {})[EXT] = {"center": c}
    used = doc.setdefault("extensionsUsed", [])
    if EXT not in used:
        used.append(EXT)
    # note: CESIUM_RTC is deliberately NOT added to extensionsRequired - viewers that do not
    # know it (three.js GLTFLoader) would refuse to load the model. The vertices stay valid
    # relative coordinates either way.
    payload = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    payload += b" " * ((4 - len(payload) % 4) % 4)  # JSON chunk is padded with spaces to 4 bytes
    body = struct.pack("<II", len(payload), CHUNK_JSON) + payload + rest
    return struct.pack("<III", MAGIC, 2, 12 + len(body)) + body


def write_rtc(path, center):
    """Rewrite the .glb at `path` in place with the given RTC center."""
    p = Path(path)
    p.write_bytes(write_rtc_bytes(p.read_bytes(), center))


def try_copy_rtc(src_path, dst_paths):
    """Copy the RTC center of src to every dst; return the list of files updated. Never raises."""
    try:
        center = read_rtc(src_path)
    except Exception as exc:  # noqa: BLE001 - best effort by design
        print(f"warning: cannot read RTC from {src_path} ({exc})", file=sys.stderr)
        return []
    if center is None:
        return []
    done = []
    for dst in dst_paths:
        try:
            write_rtc(dst, center)
            done.append(str(dst))
        except Exception as exc:  # noqa: BLE001
            print(f"warning: cannot write RTC to {dst} ({exc})", file=sys.stderr)
    return done


def try_write_rtc(paths, center):
    """Write `center` into each glb in `paths`; return files updated. Never raises."""
    done = []
    for p in paths:
        try:
            write_rtc(p, center)
            done.append(str(p))
        except Exception as exc:  # noqa: BLE001
            print(f"warning: cannot write RTC to {p} ({exc})", file=sys.stderr)
    return done


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("read")
    r.add_argument("glb")
    w = sub.add_parser("write")
    w.add_argument("glb")
    w.add_argument("x", type=float)
    w.add_argument("y", type=float)
    w.add_argument("z", type=float, nargs="?", default=0.0)
    a = ap.parse_args()
    if a.cmd == "read":
        print(json.dumps(read_rtc(a.glb)))
    else:
        write_rtc(a.glb, [a.x, a.y, a.z])
