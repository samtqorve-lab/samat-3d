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
        out = Path(a.dst_dir)
        out.mkdir(parents=True, exist_ok=True)
        files = sorted(Path(a.src_dir).glob("*.enc"))
        if not files:
            sys.exit("no .enc files found")
        for f in files:
            decrypt_file(f, out / (f.stem + a.ext), key)
        print(f"decrypted {len(files)} files")
    elif a.cmd == "encrypt-dir":
        out = Path(a.dst_dir)
        out.mkdir(parents=True, exist_ok=True)
        src = sorted(p for p in Path(a.src_dir).iterdir() if p.suffix.lower() in (".jpg", ".jpeg"))
        for n, f in enumerate(src, 1):
            encrypt_file(f, out / f"{n:04d}.enc", key)
        print(f"encrypted {len(src)} files")


if __name__ == "__main__":
    main()
