#!/usr/bin/env python3
"""Chunked AES-256-GCM file encryption (format S3D1), compatible with WebCrypto.

File layout:
  magic       4 bytes   b"S3D1"
  prefix      8 bytes   random nonce prefix
  chunk_size  4 bytes   big-endian, plaintext bytes per chunk
  chunks...   each chunk = AES-GCM output (ciphertext || 16-byte tag)

Nonce for chunk i = prefix || uint32_be(i)
AAD   for chunk i = uint32_be(i) || final_flag (1 byte, 1 = last chunk)

The final flag makes truncation detectable. Key is 32 bytes, passed as base64
in the S3D_KEY_B64 environment variable.
"""
import argparse
import base64
import os
import struct
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"S3D1"
CHUNK = 4 * 1024 * 1024
TAG = 16


def load_key():
    raw = os.environ.get("S3D_KEY_B64", "")
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception:
        key = b""
    if len(key) != 32:
        sys.exit("S3D_KEY_B64 must be a base64-encoded 32-byte key")
    return key


def encrypt_file(src, dst, key, chunk=CHUNK):
    aes = AESGCM(key)
    prefix = os.urandom(8)
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        fo.write(MAGIC + prefix + struct.pack(">I", chunk))
        i = 0
        cur = fi.read(chunk)
        while True:
            nxt = fi.read(chunk) if len(cur) == chunk else b""
            final = 0 if nxt else 1
            nonce = prefix + struct.pack(">I", i)
            aad = struct.pack(">IB", i, final)
            fo.write(aes.encrypt(nonce, cur, aad))
            if final:
                break
            cur, i = nxt, i + 1


def decrypt_file(src, dst, key):
    aes = AESGCM(key)
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        head = fi.read(16)
        if len(head) != 16 or head[:4] != MAGIC:
            raise ValueError("bad header")
        prefix = head[4:12]
        chunk = struct.unpack(">I", head[12:16])[0]
        if not 0 < chunk <= 64 * 1024 * 1024:
            raise ValueError("bad chunk size")
        size = chunk + TAG
        i = 0
        cur = fi.read(size)
        while True:
            if len(cur) < TAG:
                raise ValueError("truncated file")
            nxt = fi.read(size) if len(cur) == size else b""
            final = 0 if nxt else 1
            nonce = prefix + struct.pack(">I", i)
            aad = struct.pack(">IB", i, final)
            fo.write(aes.decrypt(nonce, cur, aad))
            if final:
                break
            cur, i = nxt, i + 1


BUNDLE_MAGIC = b"S3DB"


def unpack_bundle(data, out_dir):
    """Unpack a decrypted photo bundle: S3DB | count u32 | (name_len u16, name, data_len u32, data)*"""
    off = 4
    (count,) = struct.unpack_from(">I", data, off)
    off += 4
    written = 0
    for _ in range(count):
        (name_len,) = struct.unpack_from(">H", data, off)
        off += 2
        name = data[off:off + name_len].decode("utf-8")
        off += name_len
        (data_len,) = struct.unpack_from(">I", data, off)
        off += 4
        blob = data[off:off + data_len]
        off += data_len
        if len(blob) != data_len:
            raise ValueError("truncated bundle")
        safe = Path(name).name
        if not safe or safe.startswith(".") or Path(safe).suffix.lower() not in (".jpg", ".jpeg"):
            raise ValueError("unexpected file name in bundle")
        (out_dir / safe).write_bytes(blob)
        written += 1
    return written


def decrypt_dir(src_dir, dst_dir, ext, key):
    """Decrypt every *.enc. Each file is either a single photo or a photo bundle (S3DB)."""
    out = Path(dst_dir)
    out.mkdir(parents=True, exist_ok=True)
    files = sorted(Path(src_dir).glob("*.enc"))
    if not files:
        sys.exit("no .enc files found")
    tmp = out / ".decrypt.tmp"
    total = 0
    for f in files:
        decrypt_file(f, tmp, key)
        with open(tmp, "rb") as fh:
            magic = fh.read(4)
        if magic == BUNDLE_MAGIC:
            total += unpack_bundle(tmp.read_bytes(), out)
            tmp.unlink()
        else:
            tmp.rename(out / (f.stem + ext))
            total += 1
    print(f"decrypted {len(files)} files -> {total} photos")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("keygen", help="print a new random base64 key")

    e = sub.add_parser("encrypt", help="encrypt one file")
    e.add_argument("src")
    e.add_argument("dst")

    d = sub.add_parser("decrypt", help="decrypt one file")
    d.add_argument("src")
    d.add_argument("dst")

    dd = sub.add_parser("decrypt-dir", help="decrypt every *.enc in a folder")
    dd.add_argument("src_dir")
    dd.add_argument("dst_dir")
    dd.add_argument("--ext", default=".jpg")

    ed = sub.add_parser("encrypt-dir", help="encrypt every jpg in a folder to NNNN.enc (for tests)")
    ed.add_argument("src_dir")
    ed.add_argument("dst_dir")

    a = p.parse_args()

    if a.cmd == "keygen":
        print(base64.b64encode(os.urandom(32)).decode())
        return

    key = load_key()

    if a.cmd == "encrypt":
        encrypt_file(a.src, a.dst, key)
    elif a.cmd == "decrypt":
        decrypt_file(a.src, a.dst, key)
    elif a.cmd == "decrypt-dir":
        decrypt_dir(a.src_dir, a.dst_dir, a.ext, key)
    elif a.cmd == "encrypt-dir":
        out = Path(a.dst_dir)
        out.mkdir(parents=True, exist_ok=True)
        src = sorted(p for p in Path(a.src_dir).iterdir() if p.suffix.lower() in (".jpg", ".jpeg"))
        for n, f in enumerate(src, 1):
            encrypt_file(f, out / f"{n:04d}.enc", key)
        print(f"encrypted {len(src)} files")


if __name__ == "__main__":
    main()
