"""Tests for scripts/glb_rtc.py - run with: python -m pytest tests"""
import json
import struct
import sys
from pathlib import Path

import numpy as np
import pytest
import trimesh

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import glb_rtc  # noqa: E402


def make_glb(tmp_path, name="m.glb"):
    mesh = trimesh.Trimesh(
        vertices=np.array([[0, 0, 0], [10, 0, 1], [10, 10, 2], [0, 10, 3]], dtype=float),
        faces=np.array([[0, 1, 2], [0, 2, 3]]), process=False)
    p = tmp_path / name
    mesh.export(str(p), file_type="glb")
    return p, mesh


def split(data):
    magic, version, length = struct.unpack_from("<III", data, 0)
    jl, jt = struct.unpack_from("<II", data, 12)
    return magic, version, length, jl, jt, data[20:20 + jl], data[20 + jl:]


def test_no_rtc_by_default(tmp_path):
    p, _ = make_glb(tmp_path)
    assert glb_rtc.read_rtc(p) is None


def test_write_then_read_roundtrip(tmp_path):
    p, _ = make_glb(tmp_path)
    glb_rtc.write_rtc(p, [650123.5, 3892100.25, 1743.2])
    assert glb_rtc.read_rtc(p) == [650123.5, 3892100.25, 1743.2]
    assert glb_rtc.read_rtc(p.read_bytes()) == [650123.5, 3892100.25, 1743.2]


def test_two_value_center_gets_zero_z(tmp_path):
    p, _ = make_glb(tmp_path)
    glb_rtc.write_rtc(p, [1.0, 2.0])
    assert glb_rtc.read_rtc(p) == [1.0, 2.0, 0.0]


def test_container_stays_valid_and_binary_chunk_is_untouched(tmp_path):
    p, _ = make_glb(tmp_path)
    before = p.read_bytes()
    glb_rtc.write_rtc(p, [650123.5, 3892100.25, 0.0])
    after = p.read_bytes()
    magic, version, length, jl, jt, _json, rest = split(after)
    assert magic == 0x46546C67 and version == 2
    assert length == len(after)
    assert jl % 4 == 0                      # JSON chunk padded to 4 bytes
    assert jt == 0x4E4F534A
    assert rest == split(before)[6]         # BIN chunk byte-for-byte identical
    doc = json.loads(_json.decode())
    assert "CESIUM_RTC" in doc["extensionsUsed"]
    assert "CESIUM_RTC" not in doc.get("extensionsRequired", [])


def test_mesh_still_loads_in_trimesh_with_same_geometry(tmp_path):
    p, mesh = make_glb(tmp_path)
    glb_rtc.write_rtc(p, [650123.5, 3892100.25, 1743.2])
    again = trimesh.load(str(p), force="mesh")
    assert np.allclose(np.sort(again.vertices, axis=0), np.sort(mesh.vertices, axis=0))
    assert len(again.faces) == 2


def test_writing_twice_replaces_not_duplicates(tmp_path):
    p, _ = make_glb(tmp_path)
    glb_rtc.write_rtc(p, [1, 2, 3])
    glb_rtc.write_rtc(p, [4, 5, 6])
    assert glb_rtc.read_rtc(p) == [4.0, 5.0, 6.0]
    doc = json.loads(split(p.read_bytes())[5].decode())
    assert doc["extensionsUsed"].count("CESIUM_RTC") == 1


def test_try_copy_rtc_copies_to_lods_and_never_raises(tmp_path):
    src, _ = make_glb(tmp_path, "model.glb")
    glb_rtc.write_rtc(src, [650000.0, 3890000.0, 0.0])
    lod1, _ = make_glb(tmp_path, "model_lod1.glb")
    lod2, _ = make_glb(tmp_path, "model_lod2.glb")
    done = glb_rtc.try_copy_rtc(src, [lod1, lod2, tmp_path / "missing.glb"])
    assert len(done) == 2
    assert glb_rtc.read_rtc(lod1) == [650000.0, 3890000.0, 0.0]
    assert glb_rtc.read_rtc(lod2) == [650000.0, 3890000.0, 0.0]
    # source without RTC → nothing to copy, no error
    plain, _ = make_glb(tmp_path, "plain.glb")
    other, _ = make_glb(tmp_path, "other.glb")
    assert glb_rtc.try_copy_rtc(plain, [other]) == []
    assert glb_rtc.read_rtc(other) is None
    # unreadable source → no error
    assert glb_rtc.try_copy_rtc(tmp_path / "nope.glb", [other]) == []


def test_bad_input_is_rejected_cleanly(tmp_path):
    bad = tmp_path / "bad.glb"
    bad.write_bytes(b"not a glb at all, definitely")
    with pytest.raises(ValueError):
        glb_rtc.read_rtc(bad)
    with pytest.raises(ValueError):
        glb_rtc.write_rtc_bytes(b"x" * 30, [1, 2])
    p, _ = make_glb(tmp_path)
    with pytest.raises(ValueError):
        glb_rtc.write_rtc_bytes(p.read_bytes(), [float("nan"), 1])
    with pytest.raises(ValueError):
        glb_rtc.write_rtc_bytes(p.read_bytes(), [1])
    assert glb_rtc.try_write_rtc([bad], [1, 2, 3]) == []   # swallowed, file left alone
    assert bad.read_bytes() == b"not a glb at all, definitely"


def test_odm_style_file_with_existing_extension_is_read(tmp_path):
    p, _ = make_glb(tmp_path)
    data = p.read_bytes()
    doc, rest = glb_rtc._parse(data)
    doc["extensions"] = {"CESIUM_RTC": {"center": [650000, 3890000, 0]}}
    doc["extensionsUsed"] = ["KHR_materials_unlit", "CESIUM_RTC"]
    payload = json.dumps(doc).encode()
    payload += b" " * ((4 - len(payload) % 4) % 4)
    body = struct.pack("<II", len(payload), 0x4E4F534A) + payload + rest
    odm = struct.pack("<III", 0x46546C67, 2, 12 + len(body)) + body
    assert glb_rtc.read_rtc(odm) == [650000.0, 3890000.0, 0.0]
